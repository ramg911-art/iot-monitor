"""Camera management - go2rtc stream list and playback URLs."""
import re
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
import httpx

from app.config import get_settings
from app.deps import get_current_user_id
from app.database import async_session_maker
from app.models.device import Device, DEVICE_TYPE_NVR_CAMERA

router = APIRouter(prefix="/api/cameras", tags=["cameras"])

def _proxy_url(path: str, query: str = "") -> str:
    """Build proxy URL for frontend (same-origin, avoids CORS/unknown host)."""
    q = f"?{query}" if query else ""
    return f"/api/cameras/go2rtc-proxy/{path}{q}"


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
            stream_id = _get_nvr_stream_name(cam)
            hls_url = f"{settings.go2rtc_url}/api/hls/{stream_id}/index.m3u8"
            html_url = f"{settings.go2rtc_url}/api/stream.html?src={stream_id}"
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

    return {"streams": streams, "go2rtc_url": settings.go2rtc_url}


@router.get("/nvr-cameras")
async def list_nvr_cameras_paginated(
    page: int = Query(1, ge=1),
    per_page: int = Query(16, ge=1, le=64),
    user_id: int = Depends(get_current_user_id),
):
    """Paginated list of NVR cameras for 4x4 grid (16 per page)."""
    async with async_session_maker() as session:
        settings = get_settings()
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
        items = []
        for cam in cams:
            go2rtc_src = _get_nvr_stream_name(cam)
            hls_url = _proxy_url("api/stream.m3u8", f"src={go2rtc_src}")
            parent_name = None
            if cam.parent_device_id:
                p = await session.get(Device, cam.parent_device_id)
                parent_name = p.name if p else None
            items.append({
                "id": cam.id,
                "name": cam.name,
                "channel_number": cam.channel_number,
                "stream_name": go2rtc_src,
                "hls_url": hls_url,
                "parent_name": parent_name,
                "online": cam.online,
                "state": cam.state,
            })
        return {
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page if total > 0 else 1,
            "go2rtc_url": settings.go2rtc_url,
        }


GO2RTC_URL = "http://127.0.0.1:1984"
_go2rtc_client = httpx.AsyncClient(timeout=None)


@router.api_route("/go2rtc-proxy/{path:path}", methods=["GET", "POST", "OPTIONS"])
async def go2rtc_proxy(path: str, request: Request):
    target_url = f"{GO2RTC_URL}/{path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    body = await request.body()

    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ["host", "content-length", "origin"]
    }

    if request.method in ("POST", "OPTIONS"):
        resp = await _go2rtc_client.request(
            request.method,
            target_url,
            content=body,
            headers=headers,
        )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=dict(resp.headers),
            media_type=resp.headers.get("content-type"),
        )

    async def stream_generator():
        async with _go2rtc_client.stream(
            request.method,
            target_url,
            content=body,
            headers=headers,
        ) as resp:
            async for chunk in resp.aiter_raw():
                yield chunk

    return StreamingResponse(
        stream_generator(),
        status_code=200,
        media_type="application/sdp",
    )
