"""Dahua NVR management - add NVR, list channels, enable/disable cameras."""
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user_id
from app.database import get_db
from app.models.device import Device, DEVICE_TYPE_NVR, DEVICE_TYPE_NVR_CAMERA, SOURCE_DAHUA
from app.websocket import ws_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/nvr", tags=["nvr"])


class AddNvrRequest(BaseModel):
    ip_address: str
    username: str
    password: str


class AddNvrResponse(BaseModel):
    id: int
    name: str
    message: str


@router.post("/add", response_model=AddNvrResponse)
async def add_nvr(
    data: AddNvrRequest,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """Add Dahua NVR by IP. Stores credentials in device extra_data."""
    existing = await session.execute(
        select(Device).where(
            Device.device_type == DEVICE_TYPE_NVR,
            Device.ip_address == data.ip_address,
        )
    )
    if existing.scalars().first():
        raise HTTPException(status_code=400, detail="NVR with this IP already exists")

    extra = json.dumps({"username": data.username, "password": data.password})
    dev = Device(
        name=f"NVR {data.ip_address}",
        device_type=DEVICE_TYPE_NVR,
        source=SOURCE_DAHUA,
        ip_address=data.ip_address,
        extra_data=extra,
        online=True,
    )
    session.add(dev)
    await session.flush()

    # Create placeholder channels (replace with real Dahua discovery)
    for ch in range(1, 5):
        ch_dev = Device(
            name=f"Channel {ch}",
            device_type=DEVICE_TYPE_NVR_CAMERA,
            source=SOURCE_DAHUA,
            parent_device_id=dev.id,
            go2rtc_stream_id=f"nvr-{dev.id}-ch{ch}",
            state="on",
            online=True,
            extra_data=json.dumps({"channel": ch}),
        )
        session.add(ch_dev)
    await session.flush()

    return AddNvrResponse(id=dev.id, name=dev.name, message="NVR added with 4 placeholder channels.")


@router.post("/camera/{device_id}/enable")
async def enable_camera(
    device_id: int,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """Enable NVR camera channel."""
    result = await session.execute(select(Device).where(Device.id == device_id))
    dev = result.scalars().first()
    if not dev or dev.device_type != DEVICE_TYPE_NVR_CAMERA:
        raise HTTPException(status_code=404, detail="NVR camera not found")
    dev.state = "on"
    dev.online = True
    await session.flush()
    await ws_manager.broadcast_device_update(dev)
    return {"state": "on"}


@router.post("/camera/{device_id}/disable")
async def disable_camera(
    device_id: int,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """Disable NVR camera channel."""
    result = await session.execute(select(Device).where(Device.id == device_id))
    dev = result.scalars().first()
    if not dev or dev.device_type != DEVICE_TYPE_NVR_CAMERA:
        raise HTTPException(status_code=404, detail="NVR camera not found")
    dev.state = "off"
    await session.flush()
    await ws_manager.broadcast_device_update(dev)
    return {"state": "off"}
