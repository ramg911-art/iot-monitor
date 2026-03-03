"""Camera management - go2rtc stream list and NVR camera metadata (no WebRTC proxy)."""
import re

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select, func
import httpx

from app.config import get_settings
from app.deps import get_current_user_id
from app.database import async_session_maker
from app.models.device import Device, DEVICE_TYPE_NVR_CAMERA

router = APIRouter(prefix="/api/cameras", tags=["cameras"])


@router.post("/webrtc-signal")
async def webrtc_signal(
    request: Request,
    src: str = Query(..., alias="src"),
    user_id: int = Depends(get_current_user_id),
):
    """
    Same-origin WebRTC signaling (SDP exchange). Forwards to go2rtc to avoid CORS.
    Media still flows directly browser <-> go2rtc; only the offer/answer is relayed.
    """
    body = await request.body()
    settings = get_settings()
    url = f"{settings.go2rtc_url.rstrip('/')}/api/webrtc?src={src}"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, content=body, headers={"Content-Type": "application/sdp"})
    return PlainTextResponse(content=r.text, status_code=r.status_code, media_type="application/sdp")


def _get_nvr_stream_name(cam) -> str:
    """Get go2rtc stream name (nvr_ch1, nvr_ch2, ...). Never use parent ID."""
    if cam.stream_name:
        return cam.stream_name
    if cam.channel_number is not None:
        return f"nvr_ch{cam.channel_number}"
    # Derive from camera name, e.g. "Channel 3" -> 3
    name = cam.name or ""
    match = re.search(r"(?:channel|ch)\s*(\d+)", name, re.IGNORECASE)
    if match:
        return f"nvr_ch{int(match.group(1))}"
    if cam.go2rtc_stream_id:
        return cam.go2rtc_stream_id
    return "nvr_ch1"


@router.get("/streams")
async def list_streams(
    user_id: int = Depends(get_current_user_id),
):
    """List go2rtc streams + NVR cameras (for camera grid)."""
    settings = get_settings()
    go2rtc_browser = (settings.go2rtc_public_url or settings.go2rtc_url).rstrip("/")
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
                        "url": f"{go2rtc_browser}/api/stream.html?src={sid}",
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
            stream_id = _get_nvr_stream_name(cam)
            hls_url = f"{settings.go2rtc_url}/api/hls/{stream_id}/index.m3u8"
            html_url = f"{go2rtc_browser}/api/stream.html?src={stream_id}"
            parent_name = None
            if cam.parent_device_id:
                p = await session.get(Device, cam.parent_device_id)
                parent_name = p.name if p else None
            streams.append({
                "id": stream_id,
                "name": cam.name,
                "stream_name": stream_id,
                "url": html_url,
                "hls_url": hls_url,
                "type": "nvr_camera",
                "device_id": cam.id,
                "parent_name": parent_name,
                "online": cam.online,
            })

    return {"streams": streams, "go2rtc_url": go2rtc_browser}


@router.get("/nvr-cameras")
async def list_nvr_cameras_paginated(
    page: int = Query(1, ge=1),
    per_page: int = Query(16, ge=1, le=64),
    user_id: int = Depends(get_current_user_id),
):
    """Paginated list of NVR cameras (metadata only). Browser connects directly to go2rtc for WebRTC."""
    async with async_session_maker() as session:
        count_result = await session.execute(
            select(func.count(Device.id)).where(Device.device_type == DEVICE_TYPE_NVR_CAMERA)
        )
        total = count_result.scalar() or 0
        offset = (page - 1) * per_page
        result = await session.execute(
            select(Device)
            .where(Device.device_type == DEVICE_TYPE_NVR_CAMERA)
            .order_by(Device.parent_device_id, Device.channel_number)
            .offset(offset)
            .limit(per_page)
        )
        cams = result.scalars().all()
        items = [
            {
                "id": cam.id,
                "name": cam.name,
                "stream_name": _get_nvr_stream_name(cam),
            }
            for cam in cams
        ]
        return {
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page if total > 0 else 1,
        }
