"""Camera management - go2rtc stream list and playback URLs."""
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import httpx

from app.config import get_settings
from app.deps import get_current_user_id
from app.database import async_session_maker
from app.models.device import Device, DEVICE_TYPE_NVR_CAMERA

router = APIRouter(prefix="/api/cameras", tags=["cameras"])


@router.get("/streams")
async def list_streams(
    user_id: int = Depends(get_current_user_id),
):
    """List go2rtc streams + NVR cameras (for camera grid)."""
    settings = get_settings()
    streams = []

    # go2rtc streams
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{settings.go2rtc_url}/api/streams")
            if resp.status_code == 200:
                data = resp.json()
                for sid, info in data.items():
                    streams.append({
                        "id": sid,
                        "name": info.get("name", sid),
                        "url": f"{settings.go2rtc_url}/api/stream.html?src={sid}",
                        "type": "go2rtc",
                        "device_id": None,
                        "parent_name": None,
                        "online": True,
                    })
    except Exception:
        pass

    # NVR cameras from DB
    async with async_session_maker() as session:
        result = await session.execute(
            select(Device).where(Device.device_type == DEVICE_TYPE_NVR_CAMERA).order_by(Device.name)
        )
        nvr_cams = result.scalars().all()
        for cam in nvr_cams:
            stream_id = cam.go2rtc_stream_id or f"nvr-{cam.id}"
            hls_url = f"{settings.go2rtc_url}/api/hls/{stream_id}/index.m3u8"
            html_url = f"{settings.go2rtc_url}/api/stream.html?src={stream_id}"
            parent_name = None
            if cam.parent_device_id:
                p = await session.get(Device, cam.parent_device_id)
                parent_name = p.name if p else None
            streams.append({
                "id": stream_id,
                "name": cam.name,
                "url": html_url,
                "hls_url": hls_url,
                "type": "nvr_camera",
                "device_id": cam.id,
                "parent_name": parent_name,
                "online": cam.online,
            })

    return {"streams": streams, "go2rtc_url": settings.go2rtc_url}
