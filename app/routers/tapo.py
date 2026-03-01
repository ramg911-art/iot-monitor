"""Tapo device management - add switch/hub by IP, test, toggle, on, off."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user_id
from app.database import get_db
from app.models.device import Device
from app.services.tapo_service import (
    add_tapo_wifi_device,
    test_tapo_connectivity,
    toggle_tapo_device,
    turn_on_device,
    turn_off_device,
)
from app.websocket import ws_manager

router = APIRouter(prefix="/api/tapo", tags=["tapo"])


class AddTapoDeviceRequest(BaseModel):
    ip_address: str
    name: Optional[str] = None


class AddTapoDeviceResponse(BaseModel):
    id: int
    name: str
    device_type: str
    message: str


@router.get("/test/{ip_address}")
async def test_connection(
    ip_address: str,
    user_id: int = Depends(get_current_user_id),
):
    """Test Tapo device connectivity before adding."""
    ok, msg = await test_tapo_connectivity(ip_address)
    return {"success": ok, "message": msg}


@router.post("/add", response_model=AddTapoDeviceResponse)
async def add_device(
    data: AddTapoDeviceRequest,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """Manually add Tapo WiFi switch/plug or H100 hub by IP."""
    dev, msg = await add_tapo_wifi_device(session, data.ip_address, data.name)
    if not dev:
        raise HTTPException(status_code=400, detail=msg)
    await ws_manager.broadcast_device_update(dev)
    return AddTapoDeviceResponse(id=dev.id, name=dev.name, device_type=dev.device_type, message=msg)


@router.post("/{device_id}/toggle")
async def toggle_device(
    device_id: int,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """Toggle Tapo switch/plug on/off."""
    result = await session.execute(select(Device).where(Device.id == device_id))
    dev = result.scalars().first()
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found")
    if dev.device_type not in ("tapo_switch", "tapo_plug"):
        raise HTTPException(status_code=400, detail="Not a Tapo switch/plug")
    try:
        new_state = await toggle_tapo_device(session, dev)
        await ws_manager.broadcast_device_update(dev)
        return {"state": "on" if new_state else "off"}
    except (ConnectionError, ValueError) as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/{device_id}/on")
async def turn_on(
    device_id: int,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """Turn Tapo switch/plug ON."""
    result = await session.execute(select(Device).where(Device.id == device_id))
    dev = result.scalars().first()
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found")
    if dev.device_type not in ("tapo_switch", "tapo_plug"):
        raise HTTPException(status_code=400, detail="Not a Tapo switch/plug")
    try:
        new_state = await turn_on_device(session, dev)
        await ws_manager.broadcast_device_update(dev)
        return {"state": "on" if new_state else "off"}
    except (ConnectionError, ValueError) as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/{device_id}/off")
async def turn_off(
    device_id: int,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """Turn Tapo switch/plug OFF."""
    result = await session.execute(select(Device).where(Device.id == device_id))
    dev = result.scalars().first()
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found")
    if dev.device_type not in ("tapo_switch", "tapo_plug"):
        raise HTTPException(status_code=400, detail="Not a Tapo switch/plug")
    try:
        new_state = await turn_off_device(session, dev)
        await ws_manager.broadcast_device_update(dev)
        return {"state": "on" if new_state else "off"}
    except (ConnectionError, ValueError) as e:
        raise HTTPException(status_code=502, detail=str(e))
