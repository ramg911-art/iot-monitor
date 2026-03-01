"""Background polling tasks - Tapo, cameras."""
import asyncio
import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session_maker
from app.models.device import Device, DEVICE_TYPE_CAMERA, DEVICE_TYPE_TAPO_H100, DEVICE_TYPE_TAPO_SWITCH, DEVICE_TYPE_TAPO_PLUG
from app.services.tapo_service import poll_tapo_device
from app.websocket import ws_manager

logger = logging.getLogger(__name__)


async def _broadcast_device(device: Device) -> None:
    await ws_manager.broadcast("device", {
        "id": device.id,
        "name": device.name,
        "device_type": device.device_type,
        "state": device.state,
        "temperature": device.temperature,
        "humidity": device.humidity,
        "power": device.power,
        "online": device.online,
    })


async def _poll_tapo_with_broadcast(session: AsyncSession) -> None:
    async def bc(_, dev):
        await _broadcast_device(dev)
    stmt = select(Device).where(
        Device.device_type.in_([DEVICE_TYPE_TAPO_H100, DEVICE_TYPE_TAPO_SWITCH, DEVICE_TYPE_TAPO_PLUG]),
        Device.ip_address.isnot(None),
    )
    result = await session.execute(stmt)
    devices = result.scalars().all()
    for dev in devices:
        try:
            await poll_tapo_device(session, dev, broadcast_cb=bc)
        except Exception as e:
            logger.warning("Tapo poll failed for %s: %s", dev.name, e)
        await asyncio.sleep(0.5)  # Stagger to avoid DDoS


async def tapo_poll_loop() -> None:
    """Poll Tapo H100 and WiFi switches every 30s."""
    settings = get_settings()
    interval = settings.tapo_poll_interval
    while True:
        try:
            async with async_session_maker() as session:
                await _poll_tapo_with_broadcast(session)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception("Tapo poll loop error: %s", e)
        await asyncio.sleep(interval)


async def camera_poll_loop() -> None:
    """
    Poll cameras every 10s - just verify go2rtc streams.
    Cameras are typically configured in go2rtc, we don't store camera state in DB
    for each stream. This task can optionally sync stream list to DB if needed.
    """
    settings = get_settings()
    interval = settings.camera_poll_interval
    while True:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{settings.go2rtc_url}/api/streams")
                if resp.status_code == 200:
                    # Optionally update camera devices in DB
                    pass
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug("Camera poll: %s", e)
        await asyncio.sleep(interval)


async def start_background_tasks() -> list[asyncio.Task]:
    """Start polling tasks. Returns list of tasks."""
    tasks = [
        asyncio.create_task(tapo_poll_loop()),
        asyncio.create_task(camera_poll_loop()),
    ]
    logger.info("Background tasks started")
    return tasks
