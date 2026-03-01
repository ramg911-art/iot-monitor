"""Device listing and history."""
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user_id
from app.database import get_db
from app.models.device import Device
from app.models.sensor_history import SensorHistory

router = APIRouter(prefix="/api/devices", tags=["devices"])


@router.get("")
async def list_devices(
    device_type: Optional[str] = None,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """List all devices, optionally filtered by type."""
    stmt = select(Device).order_by(Device.device_type, Device.name)
    if device_type:
        stmt = stmt.where(Device.device_type == device_type)
    result = await session.execute(stmt)
    devices = result.scalars().all()
    return [device_to_dict(d) for d in devices]


@router.get("/{device_id}/history")
async def get_device_history(
    device_id: int,
    hours: int = Query(24, ge=1, le=168),
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """Get sensor history for last N hours (for charts)."""
    since = datetime.utcnow() - timedelta(hours=hours)
    stmt = (
        select(SensorHistory)
        .where(
            and_(
                SensorHistory.device_id == device_id,
                SensorHistory.timestamp >= since,
            )
        )
        .order_by(SensorHistory.timestamp.asc())
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()
    return [
        {
            "timestamp": r.timestamp.isoformat() if r.timestamp else None,
            "temperature": r.temperature,
            "humidity": r.humidity,
            "state": r.state,
            "power": r.power,
        }
        for r in rows
    ]


def device_to_dict(d: Device) -> dict:
    return {
        "id": d.id,
        "name": d.name,
        "device_type": d.device_type,
        "source": d.source,
        "state": d.state,
        "temperature": d.temperature,
        "humidity": d.humidity,
        "power": d.power,
        "voltage": d.voltage,
        "current": d.current,
        "online": d.online,
        "ip_address": d.ip_address,
        "ewelink_device_id": d.ewelink_device_id,
        "go2rtc_stream_id": d.go2rtc_stream_id,
        "rtsp_url": d.rtsp_url,
        "parent_device_id": d.parent_device_id,
        "extra_data": d.extra_data,
        "last_seen": d.last_seen.isoformat() if d.last_seen else None,
    }
