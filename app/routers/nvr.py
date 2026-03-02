"""Dahua NVR management - add NVR, list cameras, enable/disable channels."""
import json
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user_id
from app.database import get_db
from app.models.device import Device, DEVICE_TYPE_NVR, DEVICE_TYPE_NVR_CAMERA, SOURCE_DAHUA
from app.services.encryption_service import encrypt_token
from app.websocket import ws_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/nvr", tags=["nvr"])


def _get_nvr_stream_name(cam) -> str:
    """Get go2rtc stream name (nvr_ch1, nvr_ch2, ...). Never use parent ID."""
    if cam.stream_name:
        return cam.stream_name
    if cam.channel_number is not None:
        return f"nvr_ch{cam.channel_number}"
    name = cam.name or ""
    match = re.search(r"(?:channel|ch)\s*(\d+)", name, re.IGNORECASE)
    if match:
        return f"nvr_ch{int(match.group(1))}"
    if cam.go2rtc_stream_id:
        return cam.go2rtc_stream_id
    return "nvr_ch1"


class AddNvrRequest(BaseModel):
    name: str
    ip_address: str
    username: str
    password: str
    total_channels: int = 4


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
    """Add NVR. Encrypts password, creates NVR device and child camera devices."""
    if data.total_channels < 1 or data.total_channels > 32:
        raise HTTPException(status_code=400, detail="total_channels must be 1-32")

    existing = await session.execute(
        select(Device).where(
            Device.device_type == DEVICE_TYPE_NVR,
            Device.ip_address == data.ip_address,
        )
    )
    if existing.scalars().first():
        raise HTTPException(status_code=400, detail="NVR with this IP already exists")

    password_enc = encrypt_token(data.password)
    nvr = Device(
        name=data.name or f"NVR {data.ip_address}",
        device_type=DEVICE_TYPE_NVR,
        source=SOURCE_DAHUA,
        ip_address=data.ip_address,
        nvr_username=data.username,
        nvr_password_encrypted=password_enc,
        total_channels=data.total_channels,
        online=True,
    )
    session.add(nvr)
    await session.flush()

    for i in range(1, data.total_channels + 1):
        stream_name = f"nvr_ch{i}"
        ch_dev = Device(
            name=f"Channel {i}",
            device_type=DEVICE_TYPE_NVR_CAMERA,
            source=SOURCE_DAHUA,
            parent_device_id=nvr.id,
            channel_number=i,
            stream_name=stream_name,
            go2rtc_stream_id=stream_name,
            state="on",
            online=True,
            extra_data=json.dumps({"channel": i}),
        )
        session.add(ch_dev)
    await session.flush()

    return AddNvrResponse(
        id=nvr.id,
        name=nvr.name,
        message=f"NVR added with {data.total_channels} camera channels.",
    )


@router.get("/{nvr_id}/cameras")
async def get_nvr_cameras(
    nvr_id: int,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """Return all child cameras for an NVR."""
    result = await session.execute(
        select(Device).where(
            Device.device_type == DEVICE_TYPE_NVR_CAMERA,
            Device.parent_device_id == nvr_id,
        ).order_by(Device.channel_number)
    )
    cams = result.scalars().all()
    nvr_result = await session.execute(select(Device).where(Device.id == nvr_id))
    nvr = nvr_result.scalars().first()
    if not nvr or nvr.device_type != DEVICE_TYPE_NVR:
        raise HTTPException(status_code=404, detail="NVR not found")
    return {
        "nvr": {"id": nvr.id, "name": nvr.name, "ip_address": nvr.ip_address},
        "cameras": [
            {
                "id": c.id,
                "name": c.name,
                "channel_number": c.channel_number,
                "stream_name": _get_nvr_stream_name(c),
                "state": c.state,
                "online": c.online,
            }
            for c in cams
        ],
    }


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
