"""Tapo H100 hub and standalone WiFi switch/plug integration via python-kasa."""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.device import (
    DEVICE_TYPE_DOOR,
    DEVICE_TYPE_TAPO_H100,
    DEVICE_TYPE_TAPO_PLUG,
    DEVICE_TYPE_TAPO_SWITCH,
    DEVICE_TYPE_TEMP_SENSOR,
    SOURCE_TAPO_H100,
    SOURCE_TAPO_WIFI,
    Device,
)
from app.services.history_service import (
    insert_history,
    should_store_door_state,
    should_store_power,
    should_store_temperature,
)

logger = logging.getLogger(__name__)

# Safety timeout for all kasa operations
KASA_TIMEOUT = 5


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
    battery: Optional[int] = None
    children: list[dict[str, Any]] = field(default_factory=list)


async def _get_device_by_ip(session: AsyncSession, ip: str) -> Optional[Device]:
    result = await session.execute(select(Device).where(Device.ip_address == ip))
    return result.scalars().first()


async def _get_device_by_tapo_id(session: AsyncSession, tapo_id: str) -> Optional[Device]:
    result = await session.execute(select(Device).where(Device.tapo_device_id == tapo_id))
    return result.scalars().first()


def _tapo_discover_kwargs() -> dict:
    """Return kwargs for Discover.discover_single (credentials if configured)."""
    s = get_settings()
    kw: dict = {}
    if s.tapo_username and s.tapo_password:
        kw["username"] = s.tapo_username
        kw["password"] = s.tapo_password
    return kw


def _infer_child_type(child: Any) -> str:
    """Detect child type from model: T110 → door, T310 → temp sensor."""
    model = (getattr(child, "model", "") or "").upper()
    child_type = type(child).__name__.lower()
    if "T110" in model or "door" in child_type or "contact" in child_type:
        return "door"
    if "T310" in model or ("temperature" in child_type and "door" not in child_type):
        return "temp_sensor"
    if "door" in child_type or "contact" in child_type:
        return "door"
    return "temp_sensor"  # Default for unknown sensors


async def test_tapo_connectivity(ip_address: str) -> tuple[bool, str]:
    """Test connectivity to a Tapo device by IP. Returns (success, message)."""
    try:
        from kasa import Discover

        dev = await asyncio.wait_for(
            Discover.discover_single(ip_address, **_tapo_discover_kwargs()),
            timeout=KASA_TIMEOUT,
        )
        if dev is None:
            return False, "Device not found at IP"
        try:
            await asyncio.wait_for(dev.update(), timeout=KASA_TIMEOUT)
            return True, f"Connected: {dev.model} - {dev.alias}"
        finally:
            await dev.disconnect()
    except asyncio.TimeoutError:
        logger.warning("Tapo connectivity test timeout for %s", ip_address)
        return False, "Connection timeout"
    except Exception as e:
        logger.exception("Tapo connectivity test failed for %s", ip_address)
        return False, str(e)


async def _poll_h100_child(
    session: AsyncSession,
    parent: Device,
    child: Any,
    now: datetime,
    broadcast_cb: Optional[Callable],
) -> bool:
    """
    Poll H100 child (sensor, door). Create Device if not exists. Update if exists.
    Returns True if state changed (for broadcast).
    """
    child_id = getattr(child, "device_id", None) or getattr(child, "id", None) or str(id(child))
    child_alias = getattr(child, "alias", None) or getattr(child, "child_id", str(child_id))
    child_type = _infer_child_type(child)

    result = await session.execute(
        select(Device).where(
            Device.parent_device_id == parent.id,
            Device.tapo_device_id == str(child_id),
        )
    )
    dev = result.scalars().first()
    if dev is None:
        dtype = DEVICE_TYPE_DOOR if child_type == "door" else DEVICE_TYPE_TEMP_SENSOR
        dev = Device(
            name=child_alias,
            device_type=dtype,
            source=SOURCE_TAPO_H100,
            parent_device_id=parent.id,
            tapo_device_id=str(child_id),
        )
        session.add(dev)
        await session.flush()

    changed = False
    dev.online = True
    dev.last_seen = now

    # Update child - fetch latest data
    try:
        if hasattr(child, "update") and callable(getattr(child, "update")):
            await asyncio.wait_for(child.update(), timeout=KASA_TIMEOUT)
    except (asyncio.TimeoutError, Exception) as ex:
        logger.debug("Child update failed for %s: %s", child_alias, ex)

    if child_type == "door":
        state = "open" if getattr(child, "is_open", False) else "closed"
        if dev.state != state:
            dev.state = state
            changed = True
            if await should_store_door_state(session, dev.id, state):
                await insert_history(session, dev.id, state=state, timestamp=now)
    else:
        temp = getattr(child, "temperature", None)
        humidity = getattr(child, "humidity", None)
        battery = getattr(child, "battery", None)
        if battery is not None:
            try:
                dev.battery = int(battery) if isinstance(battery, (int, float)) else None
            except (ValueError, TypeError):
                pass
        if temp is not None or humidity is not None:
            if dev.temperature != temp or dev.humidity != humidity:
                dev.temperature = temp
                dev.humidity = humidity
                changed = True
                if temp is not None and await should_store_temperature(session, dev.id, temp, now):
                    await insert_history(session, dev.id, temperature=temp, humidity=humidity, timestamp=now)

    await session.flush()
    if changed and broadcast_cb:
        await broadcast_cb(dev)
    return changed


async def poll_tapo_device(
    session: AsyncSession,
    device: Device,
    broadcast_cb: Optional[Callable[[Device], Any]] = None,
) -> bool:
    """
    Poll a Tapo device (H100 hub or WiFi switch/plug).
    Updates DB only on change, inserts history per rules.
    Returns True if state changed (for broadcast).
    """
    from kasa import Discover

    if not device.ip_address:
        return False

    kasa_dev = None
    try:
        kasa_dev = await asyncio.wait_for(
            Discover.discover_single(device.ip_address, **_tapo_discover_kwargs()),
            timeout=KASA_TIMEOUT,
        )
        if kasa_dev is None:
            device.online = False
            await session.flush()
            if broadcast_cb:
                await broadcast_cb(device)
            return True
        await asyncio.wait_for(kasa_dev.update(), timeout=KASA_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("Tapo poll timeout for %s (%s)", device.name, device.ip_address)
        device.online = False
        await session.flush()
        if broadcast_cb:
            await broadcast_cb(device)
        return True
    except Exception as e:
        logger.warning("Tapo poll failed for %s (%s): %s", device.name, device.ip_address, e)
        device.online = False
        await session.flush()
        if broadcast_cb:
            await broadcast_cb(device)
        return True
    finally:
        if kasa_dev is not None:
            try:
                await kasa_dev.disconnect()
            except Exception as ex:
                logger.debug("Tapo disconnect failed: %s", ex)

    device.online = True
    device.last_seen = datetime.utcnow()
    changed = False
    now = device.last_seen

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
        # H100 hub - poll hub and all children
        if hasattr(kasa_dev, "temperature") and kasa_dev.temperature is not None:
            temp = kasa_dev.temperature
            if device.temperature != temp:
                device.temperature = temp
                changed = True
                if await should_store_temperature(session, device.id, temp, now):
                    await insert_history(session, device.id, temperature=temp, humidity=device.humidity, timestamp=now)
        if hasattr(kasa_dev, "humidity"):
            device.humidity = kasa_dev.humidity

        # Poll children with asyncio.gather for parallelism
        if hasattr(kasa_dev, "children") and kasa_dev.children:
            child_tasks = [
                _poll_h100_child(session, device, child, now, broadcast_cb)
                for child in kasa_dev.children
            ]
            results = await asyncio.gather(*child_tasks, return_exceptions=True)
            for i, r in enumerate(results):
                if isinstance(r, Exception):
                    logger.warning("Child poll failed for child %s: %s", i, r)
                elif r:
                    changed = True

    await session.flush()
    if changed and broadcast_cb:
        await broadcast_cb(device)
    return changed


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
    Manually add Tapo WiFi switch/plug or H100 hub by IP.
    For hubs, discovers and creates child device records.
    Returns (device, message).
    """
    from kasa import Discover

    existing = await _get_device_by_ip(session, ip_address)
    if existing:
        return None, "Device with this IP already exists"

    kasa_dev = None
    try:
        kasa_dev = await asyncio.wait_for(
            Discover.discover_single(ip_address, **_tapo_discover_kwargs()),
            timeout=KASA_TIMEOUT,
        )
        if kasa_dev is None:
            return None, "Device not found at IP"
        await asyncio.wait_for(kasa_dev.update(), timeout=KASA_TIMEOUT)
    except asyncio.TimeoutError:
        return None, "Connection timeout"
    except Exception as e:
        return None, f"Connection failed: {e}"
    finally:
        if kasa_dev is not None:
            try:
                await kasa_dev.disconnect()
            except Exception as ex:
                logger.debug("Tapo disconnect failed: %s", ex)

    dtype = _infer_tapo_device_type(kasa_dev)
    source = SOURCE_TAPO_H100 if dtype == DEVICE_TYPE_TAPO_H100 else SOURCE_TAPO_WIFI
    now = datetime.utcnow()
    dev = Device(
        name=name or getattr(kasa_dev, "alias", f"Tapo {dtype}"),
        device_type=dtype,
        source=source,
        ip_address=ip_address,
        tapo_device_id=getattr(kasa_dev, "device_id", None) or ip_address,
        online=True,
        last_seen=now,
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
    if dtype == DEVICE_TYPE_TAPO_H100:
        if hasattr(kasa_dev, "temperature"):
            dev.temperature = kasa_dev.temperature
        if hasattr(kasa_dev, "humidity"):
            dev.humidity = kasa_dev.humidity

    session.add(dev)
    await session.flush()

    # For hubs, create child device records
    if dtype == DEVICE_TYPE_TAPO_H100 and hasattr(kasa_dev, "children") and kasa_dev.children:
        for child in kasa_dev.children:
            child_id = getattr(child, "device_id", None) or getattr(child, "id", None) or str(id(child))
            child_alias = getattr(child, "alias", None) or getattr(child, "child_id", str(child_id))
            child_type = _infer_child_type(child)
            child_dtype = DEVICE_TYPE_DOOR if child_type == "door" else DEVICE_TYPE_TEMP_SENSOR
            child_dev = Device(
                name=child_alias,
                device_type=child_dtype,
                source=SOURCE_TAPO_H100,
                parent_device_id=dev.id,
                tapo_device_id=str(child_id),
                online=True,
                last_seen=now,
            )
            if child_type == "door":
                child_dev.state = "open" if getattr(child, "is_open", False) else "closed"
            else:
                child_dev.temperature = getattr(child, "temperature", None)
                child_dev.humidity = getattr(child, "humidity", None)
                child_dev.battery = getattr(child, "battery", None)
                if isinstance(child_dev.battery, (int, float)):
                    child_dev.battery = int(child_dev.battery)
                else:
                    child_dev.battery = None
            session.add(child_dev)
        await session.flush()

    return dev, "Device added successfully"


async def _control_plug(
    session: AsyncSession,
    device: Device,
    turn_on: bool,
) -> bool:
    """Turn plug on or off. Returns new state."""
    from kasa import Discover

    if not device.ip_address:
        raise ValueError("Device has no IP")
    kasa_dev = await asyncio.wait_for(
        Discover.discover_single(device.ip_address, **_tapo_discover_kwargs()),
        timeout=KASA_TIMEOUT,
    )
    if kasa_dev is None:
        raise ConnectionError("Device not found")
    try:
        await asyncio.wait_for(kasa_dev.update(), timeout=KASA_TIMEOUT)
        if turn_on:
            await asyncio.wait_for(kasa_dev.turn_on(), timeout=KASA_TIMEOUT)
        else:
            await asyncio.wait_for(kasa_dev.turn_off(), timeout=KASA_TIMEOUT)
        await asyncio.wait_for(kasa_dev.update(), timeout=KASA_TIMEOUT)
        new_state = kasa_dev.is_on
        device.state = "on" if new_state else "off"
        device.online = True
        await session.flush()
        return new_state
    finally:
        await kasa_dev.disconnect()


async def turn_on_device(session: AsyncSession, device: Device) -> bool:
    """Turn Tapo switch/plug ON. Returns new state (True = on)."""
    return await _control_plug(session, device, turn_on=True)


async def turn_off_device(session: AsyncSession, device: Device) -> bool:
    """Turn Tapo switch/plug OFF. Returns new state (False = off)."""
    return await _control_plug(session, device, turn_on=False)


async def toggle_tapo_device(session: AsyncSession, device: Device) -> bool:
    """Toggle Tapo switch/plug on/off. Returns new state."""
    from kasa import Discover

    if not device.ip_address:
        raise ValueError("Device has no IP")
    kasa_dev = await asyncio.wait_for(
        Discover.discover_single(device.ip_address, **_tapo_discover_kwargs()),
        timeout=KASA_TIMEOUT,
    )
    if kasa_dev is None:
        raise ConnectionError("Device not found")
    try:
        await asyncio.wait_for(kasa_dev.update(), timeout=KASA_TIMEOUT)
        if kasa_dev.is_on:
            await asyncio.wait_for(kasa_dev.turn_off(), timeout=KASA_TIMEOUT)
        else:
            await asyncio.wait_for(kasa_dev.turn_on(), timeout=KASA_TIMEOUT)
        await asyncio.wait_for(kasa_dev.update(), timeout=KASA_TIMEOUT)
        new_state = kasa_dev.is_on
        device.state = "on" if new_state else "off"
        device.online = True
        await session.flush()
        return new_state
    finally:
        await kasa_dev.disconnect()
