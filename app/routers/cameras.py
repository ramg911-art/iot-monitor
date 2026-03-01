"""Camera management - go2rtc stream list and playback URLs."""
from typing import Optional

from fastapi import APIRouter, Depends
import httpx

from app.config import get_settings
from app.deps import get_current_user_id

router = APIRouter(prefix="/api/cameras", tags=["cameras"])


@router.get("/streams")
async def list_streams(user_id: int = Depends(get_current_user_id)):
    """List go2rtc streams (for camera grid)."""
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{settings.go2rtc_url}/api/streams")
            if resp.status_code != 200:
                return {"streams": []}
            data = resp.json()
    except Exception:
        return {"streams": []}
    streams = []
    for sid, info in data.items():
        streams.append({
            "id": sid,
            "name": info.get("name", sid),
            "url": f"{settings.go2rtc_url}/api/stream.html?src={sid}",
        })
    return {"streams": streams}
