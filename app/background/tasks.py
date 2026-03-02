"""Background polling tasks - Tapo, cameras, eWeLink LAN."""
import asyncio
import logging
from types import SimpleNamespace
from typing import Optional

from sqlalchemy import select

from app.config import get_settings
from app.database import async_session_maker, commit_with_retry, db_write_lock
from app.models.device import (
    Device,
    DEVICE_TYPE_TAPO_H100,
    DEVICE_TYPE_TAPO_SWITCH,
    DEVICE_TYPE_TAPO_PLUG,
)
from app.services.tapo_service import poll_tapo_device
from app.services.ewelink_lan_service import ewelink_lan_service
from app.websocket import ws_manager

logger = logging.getLogger(__name__)


def _device_to_broadcast_payload(dev: Device) -> SimpleNamespace:
    """Extract attributes for broadcast; avoids passing ORM object across session boundary."""
    return SimpleNamespace(
        id=dev.id,
        device_type=dev.device_type,
        state=getattr(dev, "state", None),
        power=getattr(dev, "power", None),
        temperature=getattr(dev, "temperature", None),
        humidity=getattr(dev, "humidity", None),
        battery=getattr(dev, "battery", None),
        online=dev.online,
        lan_ip=getattr(dev, "lan_ip", None),
        lan_online=getattr(dev, "lan_online", False),
        prefer_lan=getattr(dev, "prefer_lan", True),
    )


async def _broadcast_device_update(device_or_payload) -> None:
    """Broadcast device update. Accepts Device or broadcast payload."""
    await ws_manager.broadcast_device_update(device_or_payload)


async def _mark_device_offline(device_id: int) -> None:
    """Mark device offline using own session, db_write_lock, and retry."""
    async with db_write_lock:
        async with async_session_maker() as session:
            result = await session.execute(select(Device).where(Device.id == device_id))
            dev = result.scalars().first()
            if dev:
                dev.online = False
                await commit_with_retry(session)
                payload = _device_to_broadcast_payload(dev)
                await _broadcast_device_update(payload)


async def _poll_single_tapo_device(device_id: int, device_name: str, ip_address: str) -> None:
    """
    Poll a single Tapo device. Uses own session per poll.
    No ORM reuse across sessions. No flush in tasks.
    """
    try:
        await asyncio.wait_for(
            _do_tapo_poll(device_id, device_name, ip_address),
            timeout=15,
        )
    except asyncio.TimeoutError:
        logger.warning("Tapo poll timeout for device %s (%s)", device_name, device_id)
        await _mark_device_offline(device_id)
    except Exception as e:
        logger.warning("Tapo poll failed for %s: %s", device_name, e)
        await _mark_device_offline(device_id)


async def _do_tapo_poll(device_id: int, device_name: str, ip_address: str) -> None:
    """Execute Tapo poll with own session and db_write_lock."""

    async def broadcast_cb(dev: Device) -> None:
        payload = _device_to_broadcast_payload(dev)
        await _broadcast_device_update(payload)

    async with db_write_lock:
        async with async_session_maker() as session:
            result = await session.execute(select(Device).where(Device.id == device_id))
            dev = result.scalars().first()
            if not dev or not dev.ip_address:
                return
            await poll_tapo_device(session, dev, broadcast_cb=broadcast_cb)
            await commit_with_retry(session)


async def _poll_tapo_with_broadcast() -> None:
    """Poll all Tapo devices. Each device gets its own session."""
    async with async_session_maker() as session:
        stmt = select(Device.id, Device.name, Device.ip_address).where(
            Device.device_type.in_(
                [DEVICE_TYPE_TAPO_H100, DEVICE_TYPE_TAPO_SWITCH, DEVICE_TYPE_TAPO_PLUG]
            ),
            Device.ip_address.isnot(None),
        )
        result = await session.execute(stmt)
        rows = result.all()

    for row in rows:
        device_id, device_name, ip_address = row[0], row[1], row[2]
        await _poll_single_tapo_device(device_id, device_name or "Device", ip_address)
        await asyncio.sleep(0.5)


async def tapo_poll_loop() -> None:
    """Poll Tapo H100 and WiFi switches every 30s. Errors do not crash the loop."""
    settings = get_settings()
    interval = settings.tapo_poll_interval
    logger.info("Tapo poll loop started (interval=%ds)", interval)
    while True:
        try:
            await _poll_tapo_with_broadcast()
        except asyncio.CancelledError:
            logger.info("Tapo poll loop cancelled")
            break
        except Exception as e:
            logger.exception("Tapo poll loop error: %s", e)
        await asyncio.sleep(interval)


async def camera_poll_loop() -> None:
    """Poll cameras - verify go2rtc streams. No DB writes."""
    settings = get_settings()
    interval = settings.camera_poll_interval
    while True:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{settings.go2rtc_url}/api/streams")
                if resp.status_code == 200:
                    pass
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug("Camera poll: %s", e)
        await asyncio.sleep(interval)


async def start_background_tasks() -> list[asyncio.Task]:
    """Start polling tasks."""

    async def _broadcast_device(device) -> None:
        await ws_manager.broadcast_device_update(device)

    ewelink_lan_service.set_broadcast_callback(_broadcast_device)
    await ewelink_lan_service.start()

    tasks = [
        asyncio.create_task(tapo_poll_loop()),
        asyncio.create_task(camera_poll_loop()),
    ]
    logger.info("Background tasks started")
    return tasks
