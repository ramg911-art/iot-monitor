"""Background polling tasks - Tapo, cameras, eWeLink LAN."""
import asyncio
import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session_maker
from app.models.device import (
    Device,
    DEVICE_TYPE_CAMERA,
    DEVICE_TYPE_TAPO_H100,
    DEVICE_TYPE_TAPO_SWITCH,
    DEVICE_TYPE_TAPO_PLUG,
)
from app.services.tapo_service import poll_tapo_device
from app.services.ewelink_lan_service import ewelink_lan_service
from app.websocket import ws_manager

logger = logging.getLogger(__name__)


async def _broadcast_device_update(device: Device) -> None:
    """Broadcast standardized device_update (only on state change)."""
    await ws_manager.broadcast_device_update(device)


async def _poll_single_tapo_device(
    session: AsyncSession,
    device: Device,
    device_id: int,
    device_name: str,
) -> None:
    """Poll a single Tapo device with timeout and error isolation."""
    try:
        await asyncio.wait_for(
            poll_tapo_device(session, device, broadcast_cb=_broadcast_device_update),
            timeout=15,
        )
    except asyncio.TimeoutError:
        logger.warning("Tapo poll timeout for device %s (%s)", device_name, device_id)
    except Exception as e:
        logger.warning("Tapo poll failed for %s: %s", device_name, e)
        try:
            await session.rollback()
            device.online = False
            await session.flush()
            await _broadcast_device_update(device)
        except Exception as inner:
            logger.warning("Could not mark device %s offline: %s", device_name, inner)
            try:
                await session.rollback()
            except Exception:
                pass


async def _poll_tapo_with_broadcast(session: AsyncSession) -> None:
    """Poll all Tapo devices (hubs, switches, plugs). Children are polled via hub."""
    # Only poll devices with IP (hubs and standalone plugs/switches). Children have no IP.
    stmt = select(Device).where(
        Device.device_type.in_(
            [DEVICE_TYPE_TAPO_H100, DEVICE_TYPE_TAPO_SWITCH, DEVICE_TYPE_TAPO_PLUG]
        ),
        Device.ip_address.isnot(None),
    )
    result = await session.execute(stmt)
    devices = result.scalars().all()

    # Load id/name into memory before polling (avoids lazy load if session is rolled back)
    device_infos = [(d.id, d.name) for d in devices]

    # Poll devices with small stagger to avoid overwhelming network
    for dev, (dev_id, dev_name) in zip(devices, device_infos):
        await _poll_single_tapo_device(session, dev, dev_id, dev_name)
        await asyncio.sleep(0.5)  # Stagger to avoid DDoS


async def tapo_poll_loop() -> None:
    """Poll Tapo H100 and WiFi switches every 30s. Errors do not crash the loop."""
    settings = get_settings()
    interval = settings.tapo_poll_interval
    logger.info("Tapo poll loop started (interval=%ds)", interval)
    while True:
        try:
            async with async_session_maker() as session:
                await _poll_tapo_with_broadcast(session)
        except asyncio.CancelledError:
            logger.info("Tapo poll loop cancelled")
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
    async def _broadcast_device(device: Device) -> None:
        await ws_manager.broadcast_device_update(device)

    ewelink_lan_service.set_broadcast_callback(_broadcast_device)
    await ewelink_lan_service.start()

    tasks = [
        asyncio.create_task(tapo_poll_loop()),
        asyncio.create_task(camera_poll_loop()),
    ]
    logger.info("Background tasks started")
    return tasks
