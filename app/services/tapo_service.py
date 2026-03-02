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
class ChildPollResult:
    """Result of network-only child poll. Used to pass data from concurrent fetch to sequential DB write."""
    child_id: str
    child_alias: str
    child_type: str  # "door" | "temp_sensor"
    state: Optional[str] = None  # open/closed for door
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    battery: Optional[int] = None
    fetch_ok: bool = True


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


def _extract_float(obj: Any, *attrs: str) -> Optional[float]:
    """Try attrs in order; return first valid float."""
    for attr in attrs:
        val = getattr(obj, attr, None)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    return None


def _extract_int(obj: Any, *attrs: str) -> Optional[int]:
    """Try attrs in order; return first valid int."""
    for attr in attrs:
        val = getattr(obj, attr, None)
        if val is not None:
            try:
                return int(float(val))
            except (TypeError, ValueError):
                pass
    return None


def _extract_t310_sensor_values(child: Any) -> tuple[Optional[float], Optional[float], Optional[int]]:
    """
    Extract temperature, humidity, battery from T310 or similar temp/humidity sensor.
    python-kasa may expose: direct attrs, or via modules (TemperatureSensor, BatterySensor).
    T310 attributes: temperature, humidity, battery_level (or battery, battery_percentage).
    """
    temp = _extract_float(child, "temperature")
    humidity = _extract_float(child, "humidity")
    battery = _extract_int(child, "battery", "battery_level", "battery_percentage")
    # Try modules (python-kasa module-based API)
    modules = getattr(child, "modules", None)
    if modules is not None:
        try:
            if isinstance(modules, dict):
                ts = modules.get("TemperatureSensor") or modules.get("TemperatureControl")
                bs = modules.get("BatterySensor")
            else:
                ts = getattr(modules, "TemperatureSensor", None) or getattr(modules, "TemperatureControl", None)
                bs = getattr(modules, "BatterySensor", None)
            if ts is not None:
                if temp is None:
                    temp = _extract_float(ts, "temperature", "current_temperature")
                if humidity is None:
                    humidity = _extract_float(ts, "humidity", "current_humidity")
            if bs is not None and battery is None:
                battery = _extract_int(bs, "battery", "battery_level", "battery_percentage")
        except Exception:
            pass
    return temp, humidity, battery


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


def _extract_door_state(child: Any) -> str:
    """Extract open/closed from T110 door/contact sensor."""
    is_open = getattr(child, "is_open", None)
    if is_open is not None:
        return "open" if is_open else "closed"
    # ContactSensor module: contact_open, is_open
    modules = getattr(child, "modules", None) or {}
    if isinstance(modules, dict):
        cs = modules.get("ContactSensor")
        if cs is not None:
            open_val = getattr(cs, "contact_open", None) or getattr(cs, "is_open", None)
            if open_val is not None:
                return "open" if open_val else "closed"
    return "closed"


async def _fetch_child_data(child: Any) -> ChildPollResult:
    """
    Network-only: fetch child state from kasa device. No DB access.
    Safe to run concurrently via asyncio.gather.
    Extracts: temperature, humidity, battery (T310), state (T110 door).
    """
    child_id = getattr(child, "device_id", None) or getattr(child, "id", None) or str(id(child))
    child_alias = getattr(child, "alias", None) or getattr(child, "child_id", str(child_id))
    child_type = _infer_child_type(child)
    try:
        if hasattr(child, "update") and callable(getattr(child, "update")):
            await asyncio.wait_for(child.update(), timeout=KASA_TIMEOUT)
    except (asyncio.TimeoutError, Exception) as ex:
        logger.debug("Child update failed for %s: %s", child_alias, ex)
        return ChildPollResult(
            child_id=str(child_id),
            child_alias=child_alias,
            child_type=child_type,
            fetch_ok=False,
        )
    state = None
    temp = None
    humidity = None
    battery = None
    if child_type == "door":
        state = _extract_door_state(child)
    else:
        temp, humidity, battery = _extract_t310_sensor_values(child)
    return ChildPollResult(
        child_id=str(child_id),
        child_alias=child_alias,
        child_type=child_type,
        state=state,
        temperature=temp,
        humidity=humidity,
        battery=battery,
        fetch_ok=True,
    )


async def _apply_child_to_db(
    session: AsyncSession,
    parent: Device,
    child_data: ChildPollResult,
    now: datetime,
    broadcast_cb: Optional[Callable[[Device], Any]],
) -> bool:
    """
    DB-only: insert or update child device. Must run sequentially (single session).
    Returns True if state changed (for broadcast).
    """
    result = await session.execute(
        select(Device).where(
            Device.parent_device_id == parent.id,
            Device.tapo_device_id == child_data.child_id,
        )
    )
    dev = result.scalars().first()
    if dev is None:
        dtype = DEVICE_TYPE_DOOR if child_data.child_type == "door" else DEVICE_TYPE_TEMP_SENSOR
        dev = Device(
            name=child_data.child_alias,
            device_type=dtype,
            source=SOURCE_TAPO_H100,
            parent_device_id=parent.id,
            tapo_device_id=child_data.child_id,
        )
        session.add(dev)
        await session.flush()

    changed = False
    dev.online = child_data.fetch_ok
    dev.last_seen = now

    if not child_data.fetch_ok:
        await session.flush()
        if broadcast_cb:
            await broadcast_cb(dev)
        return changed

    if child_data.child_type == "door":
        if dev.state != child_data.state:
            dev.state = child_data.state
            changed = True
            if child_data.state and await should_store_door_state(session, dev.id, child_data.state):
                await insert_history(session, dev.id, state=child_data.state, timestamp=now)
    else:
        # T310 temp/humidity sensor: update all values, track change for broadcast
        prev_temp, prev_hum, prev_bat = dev.temperature, dev.humidity, dev.battery
        dev.temperature = child_data.temperature
        dev.humidity = child_data.humidity
        if child_data.battery is not None:
            dev.battery = child_data.battery
        if (
            prev_temp != child_data.temperature
            or prev_hum != child_data.humidity
            or (child_data.battery is not None and prev_bat != child_data.battery)
        ):
            changed = True
        if child_data.temperature is not None and await should_store_temperature(
            session, dev.id, child_data.temperature, now
        ):
            await insert_history(
                session,
                dev.id,
                temperature=child_data.temperature,
                humidity=child_data.humidity,
                timestamp=now,
            )

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

        # Poll children: network concurrently, DB sequentially (no session sharing)
        if hasattr(kasa_dev, "children") and kasa_dev.children:
            # Phase 1: Concurrent network fetch (no session)
            fetch_tasks = [_fetch_child_data(child) for child in kasa_dev.children]
            child_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
            # Phase 2: Sequential DB writes (single session)
            for i, r in enumerate(child_results):
                if isinstance(r, Exception):
                    logger.warning("Child fetch failed for child %s: %s", i, r)
                    continue
                if isinstance(r, ChildPollResult):
                    child_changed = await _apply_child_to_db(
                        session, device, r, now, broadcast_cb
                    )
                    if child_changed:
                        changed = True

    await session.flush()
    if changed and broadcast_cb:
        await broadcast_cb(device)
    return changed


def _infer_tapo_device_type(kasa_dev: Any) -> str:
    """
    Infer device type from kasa device.
    Standalone Tapo WiFi switches/plugs → tapo_switch or tapo_plug.
    H100 hub → tapo_h100.
    python-kasa: SmartPlug, SmartStrip, SmartSwitch. Order matters: SmartStrip has children.
    """
    dt = type(kasa_dev).__name__.lower()
    model = (getattr(kasa_dev, "model", "") or "").lower()
    # Plugs and strips first (SmartStrip has children, must not become hub)
    if "plug" in dt or "strip" in dt:
        return DEVICE_TYPE_TAPO_PLUG
    # Hub (H100) - has children but is not plug/strip
    if "hub" in dt or "h100" in model or (hasattr(kasa_dev, "children") and kasa_dev.children):
        return DEVICE_TYPE_TAPO_H100
    # Wall switches
    if "switch" in dt:
        return DEVICE_TYPE_TAPO_SWITCH
    # Default: treat unknown switchable devices as tapo_switch
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

    # For hubs, create child device records (use same extraction as poll)
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
                child_dev.state = _extract_door_state(child)
            else:
                temp, hum, bat = _extract_t310_sensor_values(child)
                child_dev.temperature = temp
                child_dev.humidity = hum
                child_dev.battery = bat
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
