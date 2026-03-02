"""eWeLink LAN service - UDP discovery, per-device WebSocket, LAN control with cloud fallback."""
import asyncio
import json
import logging
import random
import socket
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

import websockets
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_maker
from app.models.device import DEVICE_TYPE_EWELINK, Device
from app.services.encryption_service import decrypt_token

logger = logging.getLogger(__name__)

LAN_WS_PORT = 8081
DISCOVERY_INTERVAL = 60
UDP_PROBE_PORT = 6666
UDP_LISTEN_TIMEOUT = 3


def _get_local_broadcast_address() -> Optional[str]:
    """Get local subnet broadcast address from server IP. Blocking - run in executor."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
        finally:
            s.close()
        parts = local_ip.split(".")
        if len(parts) == 4:
            return f"{parts[0]}.{parts[1]}.{parts[2]}.255"
    except Exception:
        pass
    return None


async def _get_broadcast_address() -> Optional[str]:
    """Async: get broadcast address without blocking event loop."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _get_local_broadcast_address)


@dataclass
class DiscoveredDevice:
    deviceid: str
    ip: str
    model: Optional[str] = None
    firmware: Optional[str] = None


def _user_online_payload(apikey: Optional[str] = None) -> dict:
    return {
        "action": "userOnline",
        "apikey": apikey or "apikey",
        "nonce": "".join(str(random.randint(0, 9)) for _ in range(15)),
        "ts": str(int(time.time())),
        "version": 6,
        "sequence": str(time.time()).replace(".", ""),
    }


def _update_payload(device_id: str, params: dict, apikey: Optional[str] = None) -> dict:
    return {
        "action": "update",
        "userAgent": "app",
        "params": params,
        "apikey": apikey or "apikey",
        "deviceid": device_id,
        "sequence": str(time.time()).replace(".", ""),
        "controlType": 4,
        "ts": 0,
    }


class _UDPDiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, devices: list, seen: set):
        self._devices = devices
        self._seen = seen

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        try:
            msg = json.loads(data.decode(errors="ignore"))
            device_id = msg.get("deviceid") or msg.get("deviceId") or msg.get("id")
            if not device_id:
                return
            key = f"{addr[0]}:{device_id}"
            if key in self._seen:
                return
            self._seen.add(key)
            self._devices.append(
                DiscoveredDevice(
                    deviceid=str(device_id),
                    ip=addr[0],
                    model=msg.get("model"),
                    firmware=msg.get("fwVersion") or msg.get("firmware"),
                )
            )
        except (json.JSONDecodeError, TypeError):
            pass


async def _udp_discover() -> list[DiscoveredDevice]:
    """Broadcast UDP probe and listen for Sonoff device responses."""
    devices: list[DiscoveredDevice] = []
    seen: set[str] = set()
    broadcast_addr = await _get_broadcast_address()
    if not broadcast_addr:
        logger.warning("Could not determine local broadcast address, skipping UDP discovery")
        return devices

    probe = json.dumps({"action": "probe", "ts": str(int(time.time()))}).encode()

    try:
        transport, _ = await asyncio.get_event_loop().create_datagram_endpoint(
            lambda: _UDPDiscoveryProtocol(devices, seen),
            local_addr=("0.0.0.0", 0),
            allow_broadcast=True,
        )
        try:
            transport.sendto(probe, (broadcast_addr, UDP_PROBE_PORT))
            await asyncio.sleep(UDP_LISTEN_TIMEOUT)
        finally:
            transport.close()
    except Exception as e:
        logger.debug("UDP discovery error: %s", e)

    return devices


async def discover_lan_devices() -> list[DiscoveredDevice]:
    """Discover Sonoff/eWeLink devices on LAN via UDP broadcast only."""
    return await _udp_discover()


async def _upsert_discovered(
    session: AsyncSession, dev: DiscoveredDevice, broadcast_cb: Optional[Callable] = None
) -> Optional[Device]:
    """Update existing Device from discovery. Only updates lan_ip/lan_online for known devices."""
    result = await session.execute(
        select(Device).where(Device.ewelink_device_id == dev.deviceid)
    )
    d = result.scalars().first()
    if not d:
        return None

    prev_lan = d.lan_online
    d.lan_ip = dev.ip
    d.lan_online = True
    d.last_seen = datetime.utcnow()
    if dev.model or dev.firmware:
        try:
            extra = json.loads(d.extra_data or "{}")
            if dev.model:
                extra["model"] = dev.model
            if dev.firmware:
                extra["firmware"] = dev.firmware
            d.extra_data = json.dumps(extra)
        except Exception:
            pass
    await session.flush()
    if broadcast_cb and (not prev_lan or prev_lan != d.lan_online):
        await broadcast_cb(d)
    return d


class LanDeviceConnection:
    """Maintains WebSocket connection to a single Sonoff device."""

    def __init__(
        self,
        device_id: str,
        ip: str,
        apikey: Optional[str],
        on_state: Callable,
        on_offline: Callable,
    ):
        self.device_id = device_id
        self.ip = ip
        self.apikey = apikey
        self.on_state = on_state
        self.on_offline = on_offline
        self._ws = None
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._online = False

    @property
    def online(self) -> bool:
        return self._online

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        self._online = False

    async def send_command(self, params: dict) -> bool:
        if not self._ws or self._ws.closed:
            return False
        try:
            msg = json.dumps(_update_payload(self.device_id, params, self.apikey))
            await self._ws.send(msg)
            return True
        except Exception:
            return False

    async def _run(self) -> None:
        uri = f"ws://{self.ip}:{LAN_WS_PORT}/"
        backoff = 5
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    uri,
                    subprotocols=["chat"],
                    close_timeout=2,
                    open_timeout=10,
                    ping_interval=25,
                    ping_timeout=10,
                ) as ws:
                    self._ws = ws
                    self._online = True
                    backoff = 5
                    await ws.send(json.dumps(_user_online_payload(self.apikey)))
                    resp = await ws.recv()
                    data = json.loads(resp)
                    if data.get("error") == 0 and "params" in data:
                        await self.on_state(data.get("params", {}))

                    while not self._stop.is_set():
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=60)
                            data = json.loads(msg)
                            if "params" in data:
                                await self.on_state(data["params"])
                        except asyncio.TimeoutError:
                            pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("LAN WS %s (%s): %s", self.device_id, self.ip, e)
            finally:
                self._ws = None
                self._online = False
                await self.on_offline()

            if self._stop.is_set():
                break
            await asyncio.sleep(min(backoff, 60))
            backoff = min(backoff * 1.5, 60)


class EweLinkLanService:
    """Singleton service: discovery loop, per-device connections."""

    def __init__(self):
        self._connections: dict[str, LanDeviceConnection] = {}
        self._discovery_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._broadcast_cb: Optional[Callable] = None

    def set_broadcast_callback(self, cb: Callable) -> None:
        self._broadcast_cb = cb

    async def _broadcast_device(self, device: Device) -> None:
        if self._broadcast_cb:
            await self._broadcast_cb(device)

    async def _on_lan_state(self, device_id: str, params: dict) -> None:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Device).where(Device.ewelink_device_id == device_id)
            )
            dev = result.scalars().first()
            if dev:
                switch = params.get("switch")
                if switch:
                    dev.state = "on" if switch == "on" else "off"
                switches = params.get("switches")
                if switches and isinstance(switches, list) and len(switches) > 0:
                    first = switches[0].get("switch", "off")
                    dev.state = "on" if first == "on" else "off"
                if "power" in params:
                    try:
                        dev.power = float(params["power"])
                    except (TypeError, ValueError):
                        pass
                dev.extra_data = json.dumps(params) if params else None
                await session.commit()
                await self._broadcast_device(dev)

    async def _on_lan_offline(self, device_id: str) -> None:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Device).where(Device.ewelink_device_id == device_id)
            )
            dev = result.scalars().first()
            if dev:
                dev.lan_online = False
                await session.commit()
                await self._broadcast_device(dev)

    async def run_discovery(self) -> list[DiscoveredDevice]:
        """Run discovery and update DB. Returns list of discovered devices."""
        found = await discover_lan_devices()
        matched_ids: set[str] = set()
        async with async_session_maker() as session:
            for d in found:
                dev = await _upsert_discovered(
                    session, d, broadcast_cb=self._broadcast_device
                )
                if dev:
                    matched_ids.add(d.deviceid)

            # Set lan_online=False for eWeLink devices not found this cycle
            result = await session.execute(
                select(Device).where(
                    Device.device_type == DEVICE_TYPE_EWELINK,
                    Device.ewelink_device_id.isnot(None),
                    Device.lan_online == True,
                )
            )
            for dev in result.scalars().all():
                if dev.ewelink_device_id and dev.ewelink_device_id not in matched_ids:
                    dev.lan_online = False
                    dev.lan_ip = None
                    await self._broadcast_device(dev)

            await session.commit()

        to_mark_offline: list[str] = []
        async with self._lock:
            # Start connections only for devices we have in DB (with apikey)
            for d in found:
                if d.deviceid not in matched_ids:
                    continue
                if d.deviceid not in self._connections:
                    apikey = None
                    async with async_session_maker() as sess:
                        res = await sess.execute(
                            select(Device).where(Device.ewelink_device_id == d.deviceid)
                        )
                        dev_row = res.scalars().first()
                        if dev_row and getattr(dev_row, "ewelink_apikey_encrypted", None):
                            apikey = decrypt_token(dev_row.ewelink_apikey_encrypted)
                    conn = LanDeviceConnection(
                        d.deviceid,
                        d.ip,
                        apikey,
                        lambda p, did=d.deviceid: self._on_lan_state(did, p),
                        lambda did=d.deviceid: self._on_lan_offline(did),
                    )
                    self._connections[d.deviceid] = conn
                    asyncio.create_task(conn.start())

            # Stop connections for devices not found this cycle
            for did, conn in list(self._connections.items()):
                if did not in matched_ids:
                    await conn.stop()
                    del self._connections[did]
                    to_mark_offline.append(did)

        for did in to_mark_offline:
            async with async_session_maker() as session:
                result = await session.execute(
                    select(Device).where(Device.ewelink_device_id == did)
                )
                dev = result.scalars().first()
                if dev:
                    dev.lan_online = False
                    dev.lan_ip = None
                    await session.commit()
                    await self._broadcast_device(dev)

        return found

    async def discovery_loop(self) -> None:
        """Background task: run discovery every DISCOVERY_INTERVAL seconds."""
        logger.info("eWeLink LAN discovery loop started (interval=%ds)", DISCOVERY_INTERVAL)
        while True:
            try:
                await self.run_discovery()
            except asyncio.CancelledError:
                logger.info("eWeLink LAN discovery loop cancelled")
                break
            except Exception as e:
                logger.exception("eWeLink LAN discovery error: %s", e)
            await asyncio.sleep(DISCOVERY_INTERVAL)

    async def start(self) -> None:
        """Start discovery loop and initial discovery."""
        self._discovery_task = asyncio.create_task(self.discovery_loop())

    async def stop(self) -> None:
        """Cancel discovery and all device connections."""
        if self._discovery_task:
            self._discovery_task.cancel()
            try:
                await self._discovery_task
            except asyncio.CancelledError:
                pass
        async with self._lock:
            for conn in self._connections.values():
                await conn.stop()
            self._connections.clear()

    async def send_lan_command(
        self, device_id: str, params: dict
    ) -> tuple[bool, Optional[str]]:
        """Send command via LAN. Returns (success, error_msg)."""
        async with self._lock:
            conn = self._connections.get(device_id)
        if not conn:
            return False, "Device not connected via LAN"
        if not conn.online:
            return False, "LAN connection offline"
        ok = await conn.send_command(params)
        return ok, None if ok else "Send failed"


ewelink_lan_service = EweLinkLanService()
