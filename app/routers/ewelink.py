"""eWeLink OAuth, device sync, toggle, webhook, LAN discovery."""
import asyncio
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user_id
from app.database import get_db
from app.models.device import Device
from app.services.ewelink_service import (
    create_oauth_login_url,
    exchange_oauth_code,
    sync_ewelink_devices,
    toggle_ewelink_device,
    parse_webhook_payload,
)
from app.services.ewelink_lan_service import ewelink_lan_service
from app.websocket import ws_manager

router = APIRouter(prefix="/api/ewelink", tags=["ewelink"])


class CallbackQuery(BaseModel):
    code: str
    region: str = "eu"


class ToggleRequest(BaseModel):
    device_id: str
    turn_on: bool


@router.get("/login-url")
async def get_login_url(user_id: int = Depends(get_current_user_id)):
    """Get eWeLink OAuth URL to redirect user."""
    try:
        url = create_oauth_login_url()
        return {"url": url}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/callback")
async def oauth_callback(
    code: str,
    region: str = "eu",
    session: AsyncSession = Depends(get_db),
):
    """OAuth callback - exchange code for tokens. Redirects to frontend with result."""
    user_id = 1
    ok, msg = await exchange_oauth_code(session, code, region, user_id)
    if not ok:
        return RedirectResponse(url=f"/?ewelink=error&msg={quote(msg)}", status_code=302)
    return RedirectResponse(url="/?ewelink=success", status_code=302)


class PreferLanRequest(BaseModel):
    device_id: str
    prefer_lan: bool


@router.post("/scan-lan")
async def scan_lan_devices(
    user_id: int = Depends(get_current_user_id),
):
    """Trigger LAN connection sync for devices with lan_ip."""
    found = await ewelink_lan_service.run_discovery()
    return {
        "count": len(found),
        "devices": [{"deviceid": d["deviceid"], "ip": d["ip"]} for d in found],
    }


@router.post("/prefer-lan")
async def set_prefer_lan(
    data: PreferLanRequest,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """Set prefer_lan for an eWeLink device."""
    result = await session.execute(
        select(Device).where(Device.ewelink_device_id == data.device_id)
    )
    dev = result.scalars().first()
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found")
    dev.prefer_lan = data.prefer_lan
    await session.flush()
    return {"device_id": data.device_id, "prefer_lan": data.prefer_lan}


@router.post("/sync")
async def sync_devices(
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """Sync devices from eWeLink cloud."""
    async def broadcast(event_type: str, device: Device):
        await ws_manager.broadcast(event_type, {
            "id": device.id,
            "name": device.name,
            "device_type": device.device_type,
            "state": device.state,
            "power": device.power,
            "online": device.online,
            "ewelink_device_id": device.ewelink_device_id,
            "lan_ip": getattr(device, "lan_ip", None),
            "lan_online": getattr(device, "lan_online", False),
            "prefer_lan": getattr(device, "prefer_lan", True),
        })
    devices, err = await sync_ewelink_devices(session, user_id, broadcast)
    if err:
        raise HTTPException(status_code=502, detail=err)
    # Trigger LAN discovery so newly synced devices get picked up quickly
    asyncio.create_task(ewelink_lan_service.run_discovery())
    return {"devices": devices, "count": len(devices)}


@router.post("/toggle")
async def toggle_device(
    data: ToggleRequest,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """Toggle eWeLink device. Uses LAN when available and prefer_lan is True."""
    result = await session.execute(select(Device).where(Device.ewelink_device_id == data.device_id))
    dev = result.scalars().first()
    params_value = "on" if data.turn_on else "off"
    params = {"switch": params_value}

    if dev and getattr(dev, "lan_online", False) and getattr(dev, "prefer_lan", True):
        ok, err = await ewelink_lan_service.send_lan_command(data.device_id, params)
        if ok:
            if dev:
                dev.state = params_value
                await session.flush()
            result = await session.execute(select(Device).where(Device.ewelink_device_id == data.device_id))
            dev = result.scalars().first()
            if dev:
                await ws_manager.broadcast("device", {
                    "id": dev.id,
                    "name": dev.name,
                    "device_type": dev.device_type,
                    "state": dev.state,
                    "power": dev.power,
                    "online": dev.online,
                })
            return {"state": params_value}
        # LAN failed, fall through to cloud
    ok, err = await toggle_ewelink_device(session, user_id, data.device_id, data.turn_on)
    if not ok:
        raise HTTPException(status_code=502, detail=err or "Toggle failed")
    result = await session.execute(select(Device).where(Device.ewelink_device_id == data.device_id))
    dev = result.scalars().first()
    if dev:
        await ws_manager.broadcast("device", {
            "id": dev.id,
            "name": dev.name,
            "device_type": dev.device_type,
            "state": dev.state,
            "power": dev.power,
            "online": dev.online,
        })
    return {"state": "on" if data.turn_on else "off"}


@router.post("/webhook")
async def webhook(request: Request, session: AsyncSession = Depends(get_db)):
    """
    eWeLink webhook endpoint.
    Configure a webhook scene in eWeLink that POSTs to this URL when devices change.
    No auth required (eWeLink doesn't support custom headers) - consider IP allowlist.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    parsed = parse_webhook_payload(body)
    if not parsed:
        return {"status": "ignored", "reason": "Could not parse payload"}
    device_id = parsed.get("deviceid")
    if not device_id:
        return {"status": "ignored"}
    result = await session.execute(select(Device).where(Device.ewelink_device_id == device_id))
    dev = result.scalars().first()
    if dev:
        state = parsed.get("state", "off")
        if isinstance(state, str) and state.lower() in ("on", "off"):
            dev.state = state.lower()
        elif isinstance(state, bool):
            dev.state = "on" if state else "off"
        params = parsed.get("params", {})
        if "power" in params:
            try:
                dev.power = float(params["power"])
            except (TypeError, ValueError):
                pass
        await session.commit()
        await ws_manager.broadcast("device", {
            "id": dev.id,
            "name": dev.name,
            "device_type": dev.device_type,
            "state": dev.state,
            "power": dev.power,
            "online": dev.online,
        })
    return {"status": "ok"}
