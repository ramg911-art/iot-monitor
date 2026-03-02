"""eWeLink LAN service - UDP discovery, per-device WebSocket, LAN control with cloud fallback."""
import asyncio
import ipaddress
import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

import websockets
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_maker
from app.models.device import DEVICE_TYPE_EWELINK, SOURCE_EWELINK, Device
from app.services.encryption_service import decrypt_token

logger = logging.getLogger(__name__)

LAN_WS_PORT = 8081
DISCOVERY_INTERVAL = 60
UDP_PROBE_PORT = 7000
COMMON_SUBNETS = ["192.168.0.0/24", "192.168.1.0/24", "192.168.178.0/24"]
SCAN_TIMEOUT = 1.5
WS_CONNECT_TIMEOUT = 5


@dataclass
class DiscoveredDevice:
    deviceid: str
    ip: str
    model: Optional[str] = None
    firmware: Optional[str] = None


def _user_online_payload(apikey: Optional[str] = None) -> dict:
    return {
        "action": "userOnline",
        "userAgent": "app",
        "version": 6,
        "nonce": "".join(str(random.randint(0, 9)) for _ in range(15)),
        "apkVesrion": "1.8",
        "os": "ios",
        "at": "at",
        "apikey": apikey or "apikey",
        "ts": str(int(time.time())),
        "model": "iPhone10,6",
        "romVersion": "11.1.2",
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


async def _udp_discover() -> list[DiscoveredDevice]:
    """Broadcast UDP probe and listen for Sonoff device responses."""
    devices: list[DiscoveredDevice] = []
    probe = json.dumps({"action": "probe", "ts": str(int(time.time()))}).encode()
    seen: set[str] = set()

    try:
        transport, protocol = await asyncio.get_event_loop().create_datagram_endpoint(
            lambda: _UDPDiscoveryProtocol(probe, devices, seen),
            local_addr=("0.0.0.0", 0),
            allow_broadcast=True,
        )
        try:
            # Broadcast to common broadcast addresses
            for subnet in COMMON_SUBNETS:
                base = subnet.split("/")[0]
                parts = base.split(".")
                if len(parts) == 4:
                    broadcast = ".".join(parts[:3] + ["255"])
                    transport.sendto(probe, (broadcast, UDP_PROBE_PORT))
                    transport.sendto(probe, (broadcast, LAN_WS_PORT))
            transport.sendto(probe, ("255.255.255.255", UDP_PROBE_PORT))
            transport.sendto(probe, ("255.255.255.255", LAN_WS_PORT))
            await asyncio.sleep(3)
        finally:
            transport.close()
    except Exception as e:
        logger.debug("UDP discovery error: %s", e)

    return devices


class _UDPDiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, _probe: bytes, devices: list, seen: set):
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


async def _tcp_scan_and_probe() -> list[DiscoveredDevice]:
    """Scan subnets for port 8081, connect WebSocket to get deviceid."""
    devices: list[DiscoveredDevice] = []
    seen_ids: set[str] = set()
    sem = asyncio.Semaphore(32)

    async def probe_ip(ip_str: str) -> Optional[DiscoveredDevice]:
        async with sem:
            try:
                uri = f"ws://{ip_str}:{LAN_WS_PORT}/"
                async with asyncio.timeout(WS_CONNECT_TIMEOUT):
                    async with websockets.connect(
                        uri,
                        subprotocols=["chat"],
                        close_timeout=1,
                        open_timeout=WS_CONNECT_TIMEOUT,
                    ) as ws:
                        await ws.send(json.dumps(_user_online_payload()))
                        resp = await asyncio.wait_for(ws.recv(), timeout=3)
                        data = json.loads(resp)
                        device_id = data.get("deviceid")
                        if device_id and device_id not in seen_ids:
                            seen_ids.add(device_id)
                            return DiscoveredDevice(
                                deviceid=device_id,
                                ip=ip_str,
                                model=data.get("model"),
                                firmware=data.get("fwVersion"),
                            )
            except Exception:
                pass
            return None

    all_ips: list[str] = []
    for subnet in COMMON_SUBNETS:
        try:
            net = ipaddress.ipv4_network(subnet, strict=False)
            all_ips.extend(str(ip) for ip in net.hosts())
        except Exception:
            pass

    # Check port 8081 first (async connect)
    tasks = []
    for ip in all_ips:
        tasks.append(probe_ip(ip))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, DiscoveredDevice):
            devices.append(r)
        elif isinstance(r, Exception):
            logger.debug("Probe error: %s", r)

    return devices


async def discover_lan_devices() -> list[DiscoveredDevice]:
    """Discover Sonoff/eWeLink devices on LAN. UDP first, then TCP scan fallback."""
    found = await _udp_discover()
    if not found:
        found = await _tcp_scan_and_probe()
    return found


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

            # Stop connections for devices not found or no longer matched
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
