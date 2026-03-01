"""Tapo H100 hub and standalone WiFi switch/plug integration via python-kasa."""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import (
    DEVICE_TYPE_CAMERA,
    DEVICE_TYPE_DOOR,
    DEVICE_TYPE_TAPO_H100,
    DEVICE_TYPE_TAPO_PLUG,
    DEVICE_TYPE_TAPO_SWITCH,
    SOURCE_TAPO_H100,
    SOURCE_TAPO_WIFI,
    Device,
)
from app.models.sensor_history import SensorHistory
from app.services.history_service import (
    insert_history,
    should_store_door_state,
    should_store_power,
    should_store_temperature,
)

logger = logging.getLogger(__name__)


@dataclass
class TapoDeviceInfo:
    device_id: str
    device_type: str  # tapo_h100, tapo_switch, tapo_plug
    ip_address: str
    name: str
    online: bool
    state: Optional[str] = None
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    power: Optional[float] = None
    voltage: Optional[float] = None
    current: Optional[float] = None
    children: list[dict[str, Any]] = field(default_factory=list)


async def _get_device_by_ip(session: AsyncSession, ip: str) -> Optional[Device]:
    result = await session.execute(select(Device).where(Device.ip_address == ip))
    return result.scalars().first()


async def _get_device_by_tapo_id(session: AsyncSession, tapo_id: str) -> Optional[Device]:
    result = await session.execute(select(Device).where(Device.tapo_device_id == tapo_id))
    return result.scalars().first()


async def test_tapo_connectivity(ip_address: str) -> tuple[bool, str]:
    """Test connectivity to a Tapo device by IP. Returns (success, message)."""
    try:
        from kasa import Discover

        dev = await Discover.discover_single(ip_address)
        if dev is None:
            return False, "Device not found at IP"
        await dev.update()
        return True, f"Connected: {dev.model} - {dev.alias}"
    except Exception as e:
        logger.exception("Tapo connectivity test failed for %s", ip_address)
        return False, str(e)


async def poll_tapo_device(
    session: AsyncSession,
    device: Device,
    broadcast_cb: Optional[Callable] = None,
) -> bool:
    """
    Poll a Tapo device (H100 hub or WiFi switch/plug).
    Updates DB only on change, inserts history per rules.
    Returns True if state changed (for broadcast).
    """
    from kasa import Discover

    if not device.ip_address:
        return False

    try:
        kasa_dev = await Discover.discover_single(device.ip_address)
        if kasa_dev is None:
            device.online = False
            await session.flush()
            if broadcast_cb:
                await broadcast_cb("device", device)
            return True
        await kasa_dev.update()
    except Exception as e:
        logger.warning("Tapo poll failed for %s (%s): %s", device.name, device.ip_address, e)
        device.online = False
        await session.flush()
        if broadcast_cb:
            await broadcast_cb("device", device)
        return True

    device.online = True
    device.last_seen = datetime.utcnow()
    changed = False
    now = device.last_seen

    # Determine device type from kasa
    dev_type = type(kasa_dev).__name__.lower()

    if "plug" in dev_type or "switch" in dev_type:
        # SmartPlug / SmartStrip
        is_on = getattr(kasa_dev, "is_on", None) or getattr(kasa_dev, "is_led_on", False)
        new_state = "on" if is_on else "off"
        if device.state != new_state:
            device.state = new_state
            changed = True
        # Power monitoring
        if hasattr(kasa_dev, "emeter_realtime"):
            try:
                emeter = kasa_dev.emeter_realtime
                if emeter:
                    power = getattr(emeter, "power", None) or getattr(emeter, "power_mw", 0) / 1000
                    voltage = getattr(emeter, "voltage", None) or getattr(emeter, "voltage_mv", 0) / 1000
                    current = getattr(emeter, "current", None) or getattr(emeter, "current_ma", 0) / 1000
                    if power is not None:
                        if device.power != power or device.voltage != voltage or device.current != current:
                            changed = True
                        device.power = power
                        device.voltage = voltage
                        device.current = current
                        if await should_store_power(session, device.id, power or 0, now):
                            await insert_history(session, device.id, power=power, voltage=voltage, current=current, timestamp=now)
            except Exception as ex:
                logger.debug("Emeter read failed: %s", ex)
    else:
        # H100 hub or similar - may have children
        if hasattr(kasa_dev, "children") and kasa_dev.children:
            for child in kasa_dev.children:
                await _poll_h100_child(session, device, child, now, broadcast_cb)
        # Hub may have its own sensors
        if hasattr(kasa_dev, "temperature"):
            temp = kasa_dev.temperature
            if temp is not None and device.temperature != temp:
                device.temperature = temp
                changed = True
                if await should_store_temperature(session, device.id, temp, now):
                    await insert_history(session, device.id, temperature=temp, humidity=device.humidity, timestamp=now)
        if hasattr(kasa_dev, "humidity"):
            device.humidity = kasa_dev.humidity

    await session.flush()
    if changed and broadcast_cb:
        await broadcast_cb("device", device)
    return changed


async def _poll_h100_child(
    session: AsyncSession,
    parent: Device,
    child: Any,
    now: datetime,
    broadcast_cb: Optional[Callable],
) -> None:
    """Poll H100 child (sensor, door). Create Device if not exists."""
    child_id = getattr(child, "device_id", None) or getattr(child, "id", None) or str(id(child))
    child_alias = getattr(child, "alias", None) or getattr(child, "child_id", str(child_id))
    child_type = type(child).__name__.lower()

    result = await session.execute(
        select(Device).where(
            Device.parent_device_id == parent.id,
            Device.tapo_device_id == str(child_id),
        )
    )
    dev = result.scalars().first()
    if dev is None:
        dtype = DEVICE_TYPE_DOOR if "door" in child_type or "contact" in child_type else DEVICE_TYPE_TAPO_H100
        dev = Device(
            name=child_alias,
            device_type=dtype,
            source=SOURCE_TAPO_H100,
            parent_device_id=parent.id,
            tapo_device_id=str(child_id),
        )
        session.add(dev)
        await session.flush()

    if "door" in child_type or "contact" in child_type:
        state = "open" if getattr(child, "is_open", False) else "closed"
        if dev.state != state:
            dev.state = state
            if await should_store_door_state(session, dev.id, state):
                await insert_history(session, dev.id, state=state, timestamp=now)
            if broadcast_cb:
                await broadcast_cb("device", dev)
    else:
        temp = getattr(child, "temperature", None)
        humidity = getattr(child, "humidity", None)
        if temp is not None:
            if dev.temperature != temp or dev.humidity != humidity:
                dev.temperature = temp
                dev.humidity = humidity
                if await should_store_temperature(session, dev.id, temp, now):
                    await insert_history(session, dev.id, temperature=temp, humidity=humidity, timestamp=now)
                if broadcast_cb:
                    await broadcast_cb("device", dev)

    await session.flush()


def _infer_tapo_device_type(kasa_dev: Any) -> str:
    """Infer device type from kasa device."""
    dt = type(kasa_dev).__name__.lower()
    model = (getattr(kasa_dev, "model", "") or "").lower()
    if "plug" in dt:
        return DEVICE_TYPE_TAPO_PLUG
    if "switch" in dt and "hub" not in model and "h100" not in model:
        return DEVICE_TYPE_TAPO_SWITCH
    if "hub" in dt or "h100" in model or hasattr(kasa_dev, "children"):
        return DEVICE_TYPE_TAPO_H100
    return DEVICE_TYPE_TAPO_SWITCH


async def add_tapo_wifi_device(
    session: AsyncSession,
    ip_address: str,
    name: Optional[str] = None,
) -> tuple[Optional[Device], str]:
    """
    Manually add Tapo WiFi switch/plug by IP.
    Returns (device, message).
    """
    from kasa import Discover

    existing = await _get_device_by_ip(session, ip_address)
    if existing:
        return None, "Device with this IP already exists"

    try:
        kasa_dev = await Discover.discover_single(ip_address)
        if kasa_dev is None:
            return None, "Device not found at IP"
        await kasa_dev.update()
    except Exception as e:
        return None, f"Connection failed: {e}"

    dtype = _infer_tapo_device_type(kasa_dev)
    source = SOURCE_TAPO_H100 if dtype == DEVICE_TYPE_TAPO_H100 else SOURCE_TAPO_WIFI
    dev = Device(
        name=name or getattr(kasa_dev, "alias", f"Tapo {dtype}"),
        device_type=dtype,
        source=source,
        ip_address=ip_address,
        tapo_device_id=getattr(kasa_dev, "device_id", None) or ip_address,
        online=True,
        state="on" if getattr(kasa_dev, "is_on", False) else "off",
    )
    if hasattr(kasa_dev, "emeter_realtime") and kasa_dev.emeter_realtime:
        try:
            em = kasa_dev.emeter_realtime
            dev.power = getattr(em, "power", None) or (getattr(em, "power_mw", 0) / 1000)
            dev.voltage = getattr(em, "voltage", None) or (getattr(em, "voltage_mv", 0) / 1000)
            dev.current = getattr(em, "current", None) or (getattr(em, "current_ma", 0) / 1000)
        except Exception:
            pass
    session.add(dev)
    await session.flush()
    return dev, "Device added successfully"


async def toggle_tapo_device(session: AsyncSession, device: Device) -> bool:
    """Toggle Tapo switch/plug on/off. Returns new state."""
    from kasa import Discover

    if not device.ip_address:
        raise ValueError("Device has no IP")
    kasa_dev = await Discover.discover_single(device.ip_address)
    if kasa_dev is None:
        raise ConnectionError("Device not found")
    await kasa_dev.update()
    if kasa_dev.is_on:
        await kasa_dev.turn_off()
    else:
        await kasa_dev.turn_on()
    await kasa_dev.update()
    device.state = "on" if kasa_dev.is_on else "off"
    device.online = True
    await session.flush()
    return kasa_dev.is_on
