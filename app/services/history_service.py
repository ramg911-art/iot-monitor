"""Sensor history storage with change-based rules."""
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import Device
from app.models.sensor_history import SensorHistory

logger = logging.getLogger(__name__)

# History rules
TEMP_THRESHOLD = 0.2  # °C
TEMP_INTERVAL_MINUTES = 10
POWER_THRESHOLD = 1.0  # Watts
POWER_INTERVAL_MINUTES = 5


async def _get_last_history(session: AsyncSession, device_id: int) -> Optional[SensorHistory]:
    """Get most recent history row for device."""
    stmt = select(SensorHistory).where(SensorHistory.device_id == device_id).order_by(SensorHistory.timestamp.desc())
    result = await session.execute(stmt)
    return result.scalars().first()


async def should_store_temperature(
    session: AsyncSession, device_id: int, temperature: float, now: datetime
) -> bool:
    """Store if change >= 0.2°C OR every 10 minutes."""
    last = await _get_last_history(session, device_id)
    if last is None:
        return True
    if last.temperature is None:
        return True
    if abs(temperature - last.temperature) >= TEMP_THRESHOLD:
        return True
    if (now - last.timestamp) >= timedelta(minutes=TEMP_INTERVAL_MINUTES):
        return True
    return False


async def should_store_door_state(
    session: AsyncSession, device_id: int, state: str
) -> bool:
    """Store only when state changes."""
    last = await _get_last_history(session, device_id)
    if last is None:
        return True
    return last.state != state


async def should_store_power(
    session: AsyncSession, device_id: int, power: float, now: datetime
) -> bool:
    """Store if change >= 1W OR every 5 minutes."""
    last = await _get_last_history(session, device_id)
    if last is None:
        return True
    if last.power is None:
        return True
    if abs((power or 0) - (last.power or 0)) >= POWER_THRESHOLD:
        return True
    if (now - last.timestamp) >= timedelta(minutes=POWER_INTERVAL_MINUTES):
        return True
    return False


async def insert_history(
    session: AsyncSession,
    device_id: int,
    *,
    temperature: Optional[float] = None,
    humidity: Optional[float] = None,
    state: Optional[str] = None,
    power: Optional[float] = None,
    voltage: Optional[float] = None,
    current: Optional[float] = None,
    timestamp: Optional[datetime] = None,
) -> Optional[SensorHistory]:
    """Insert a history row. Caller should check should_store_* first."""
    row = SensorHistory(
        device_id=device_id,
        temperature=temperature,
        humidity=humidity,
        state=state,
        power=power,
        voltage=voltage,
        current=current,
        timestamp=timestamp or datetime.utcnow(),
    )
    session.add(row)
    await session.flush()
    return row
