"""Camera management - go2rtc stream list and playback URLs."""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
import httpx

from app.config import get_settings
from app.deps import get_current_user_id
from app.database import async_session_maker
from app.models.device import Device, DEVICE_TYPE_NVR_CAMERA

router = APIRouter(prefix="/api/cameras", tags=["cameras"])

# Shared client for go2rtc proxy (HLS parallel segment requests)
go2rtc_client = httpx.AsyncClient(
    timeout=httpx.Timeout(20.0),
    limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
)


def _proxy_url(path: str, query: str = "") -> str:
    """Build proxy URL for frontend (same-origin, avoids CORS/unknown host)."""
    q = f"?{query}" if query else ""
    return f"/api/cameras/go2rtc-proxy/{path}{q}"


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
            stream_name = cam.stream_name or cam.go2rtc_stream_id or f"nvr_ch{cam.channel_number or cam.id}"
            hls_url = _proxy_url("api/stream.m3u8", f"src={stream_name}")
            parent_name = None
            if cam.parent_device_id:
                p = await session.get(Device, cam.parent_device_id)
                parent_name = p.name if p else None
            items.append({
                "id": cam.id,
                "name": cam.name,
                "channel_number": cam.channel_number,
                "stream_name": stream_name,
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


@router.get("/go2rtc-proxy/{path:path}")
async def go2rtc_proxy(path: str, request: Request):
    """
    Production-safe streaming proxy for go2rtc HLS.
    """
    from fastapi.responses import StreamingResponse, Response

    settings = get_settings()
    base = settings.go2rtc_url.rstrip("/")
    query_string = request.url.query
    if query_string:
        url = f"{base}/{path}?{query_string}"
    else:
        url = f"{base}/{path}"

    try:
        async def stream_chunks():
            async with go2rtc_client.stream("GET", url, follow_redirects=True) as upstream:
                async for chunk in upstream.aiter_bytes():
                    yield chunk

        return StreamingResponse(
            stream_chunks(),
            status_code=200,
            headers={"Content-Type": "application/octet-stream"},
        )
    except Exception as e:
        logging.warning("go2rtc proxy failed %s: %s", url, e)
        return Response(
            content=f"Proxy error: {str(e)}",
            status_code=502,
        )
