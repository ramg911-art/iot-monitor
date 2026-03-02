"""eWeLink LAN service - encrypted WebSocket, per-device connections, cloud fallback."""
import asyncio
import json
import logging
import time
from types import SimpleNamespace
from typing import Callable, Optional

import websockets
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_maker
from app.models.device import DEVICE_TYPE_EWELINK, Device
from app.services.encryption_service import decrypt_token
from app.services.ewelink_lan_crypto import decrypt_payload, encrypt_payload

logger = logging.getLogger(__name__)

LAN_WS_PORT = 8081
DISCOVERY_INTERVAL = 60


def _build_encrypted_message(
    device_id: str,
    apikey: str,
    action: str,
    params_dict: dict,
) -> dict:
    """Build WebSocket message with encrypted params."""
    encrypted = encrypt_payload(device_id, apikey, params_dict)
    return {
        "action": action,
        "deviceid": device_id,
        "apikey": apikey,
        "encrypt": True,
        "sequence": str(time.time()).replace(".", ""),
        "params": encrypted,
    }


def _user_online_params() -> dict:
    return {
        "ts": str(int(time.time())),
        "version": 6,
    }


def _device_to_broadcast_payload(dev: Device) -> SimpleNamespace:
    """Extract attributes while session is active. Prevents greenlet errors when broadcasting."""
    return SimpleNamespace(
        id=dev.id,
        device_type=dev.device_type,
        state=getattr(dev, "state", None),
        power=getattr(dev, "power", None),
        temperature=getattr(dev, "temperature", None),
        humidity=getattr(dev, "humidity", None),
        battery=getattr(dev, "battery", None),
        online=getattr(dev, "online", True),
        lan_ip=getattr(dev, "lan_ip", None),
        lan_online=getattr(dev, "lan_online", False),
        prefer_lan=getattr(dev, "prefer_lan", True),
    )


class LanDeviceConnection:
    """Maintains encrypted WebSocket connection to a single Sonoff device."""

    def __init__(
        self,
        device_id: str,
        ip: str,
        apikey: Optional[str],
        on_state: Callable,
        on_offline: Callable,
        on_online: Optional[Callable] = None,
    ):
        self.device_id = device_id
        self.ip = ip
        self.apikey = apikey or ""
        self.on_state = on_state
        self.on_offline = on_offline
        self.on_online = on_online
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

    def _parse_incoming(self, data: dict) -> Optional[dict]:
        """Extract params from message; decrypt if encrypt==true."""
        if data.get("encrypt"):
            enc = data.get("params")
            if enc and self.apikey:
                try:
                    return decrypt_payload(self.device_id, self.apikey, enc)
                except Exception as e:
                    logger.debug("LAN decrypt error %s: %s", self.device_id, e)
                    return None
        return data.get("params")

    async def send_command(self, params: dict) -> bool:
        if not self._ws or self._ws.closed:
            return False
        if not self.apikey:
            return False
        try:
            msg = _build_encrypted_message(
                self.device_id, self.apikey, "update", params
            )
            await self._ws.send(json.dumps(msg))
            return True
        except Exception:
            return False

    async def _run(self) -> None:
        if not self.apikey:
            logger.debug("LAN %s: no apikey, skipping", self.device_id)
            return

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

                    user_online_msg = _build_encrypted_message(
                        self.device_id,
                        self.apikey,
                        "userOnline",
                        _user_online_params(),
                    )
                    await ws.send(json.dumps(user_online_msg))
                    resp = await ws.recv()
                    data = json.loads(resp)
                    params = self._parse_incoming(data)
                    if data.get("error") == 0:
                        if self.on_online:
                            await self.on_online()
                        if params:
                            await self.on_state(params)

                    while not self._stop.is_set():
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=60)
                            data = json.loads(msg)
                            params = self._parse_incoming(data)
                            if params:
                                await self.on_state(params)
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
    """Singleton service: connection loop, per-device encrypted WebSocket."""

    def __init__(self):
        self._connections: dict[str, LanDeviceConnection] = {}
        self._loop_task: Optional[asyncio.Task] = None
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
                payload = _device_to_broadcast_payload(dev)
                await session.commit()
                await self._broadcast_device(payload)

    async def _on_lan_offline(self, device_id: str) -> None:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Device).where(Device.ewelink_device_id == device_id)
            )
            dev = result.scalars().first()
            if dev:
                dev.lan_online = False
                payload = _device_to_broadcast_payload(dev)
                await session.commit()
                await self._broadcast_device(payload)

    async def _set_lan_online(self, device_id: str, online: bool) -> None:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Device).where(Device.ewelink_device_id == device_id)
            )
            dev = result.scalars().first()
            if dev:
                dev.lan_online = online
                payload = _device_to_broadcast_payload(dev)
                await session.commit()
                await self._broadcast_device(payload)

    async def run_discovery(self) -> list[dict]:
        """Load eWeLink devices with lan_ip from DB and manage connections."""
        async with async_session_maker() as session:
            result = await session.execute(
                select(Device).where(
                    Device.device_type == DEVICE_TYPE_EWELINK,
                    Device.ewelink_device_id.isnot(None),
                    Device.lan_ip.isnot(None),
                )
            )
            devices = result.scalars().all()

        target: dict[str, tuple[str, Optional[str]]] = {}
        for d in devices:
            if d.ewelink_device_id and d.lan_ip:
                apikey = None
                if getattr(d, "ewelink_apikey_encrypted", None):
                    apikey = decrypt_token(d.ewelink_apikey_encrypted)
                target[d.ewelink_device_id] = (d.lan_ip, apikey)

        to_stop: list[str] = []
        async with self._lock:
            for did, (ip, apikey) in target.items():
                if did not in self._connections:
                    conn = LanDeviceConnection(
                        did,
                        ip,
                        apikey,
                        lambda p, device_id=did: self._on_lan_state(device_id, p),
                        lambda device_id=did: self._on_lan_offline(device_id),
                        lambda device_id=did: self._set_lan_online(device_id, True),
                    )
                    self._connections[did] = conn
                    asyncio.create_task(conn.start())

            for did in list(self._connections.keys()):
                if did not in target:
                    to_stop.append(did)

        for did in to_stop:
            async with self._lock:
                conn = self._connections.pop(did, None)
            if conn:
                await conn.stop()
            async with async_session_maker() as session:
                result = await session.execute(
                    select(Device).where(Device.ewelink_device_id == did)
                )
                dev = result.scalars().first()
                if dev:
                    dev.lan_online = False
                    payload = _device_to_broadcast_payload(dev)
                    await session.commit()
                    await self._broadcast_device(payload)

        return [{"deviceid": did, "ip": ip} for did, (ip, _) in target.items()]

    async def _connection_loop(self) -> None:
        """Periodically sync connections from DB."""
        logger.info("eWeLink LAN connection loop started (interval=%ds)", DISCOVERY_INTERVAL)
        while True:
            try:
                await self.run_discovery()
            except asyncio.CancelledError:
                logger.info("eWeLink LAN connection loop cancelled")
                break
            except Exception as e:
                logger.exception("eWeLink LAN connection loop error: %s", e)
            await asyncio.sleep(DISCOVERY_INTERVAL)

    async def start(self) -> None:
        """Start connection loop and initial run."""
        await self.run_discovery()
        self._loop_task = asyncio.create_task(self._connection_loop())

    async def stop(self) -> None:
        """Cancel loop and all device connections."""
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        async with self._lock:
            for conn in self._connections.values():
                await conn.stop()
            self._connections.clear()

    async def send_lan_command(
        self, device_id: str, params: dict
    ) -> tuple[bool, Optional[str]]:
        """Send command via LAN (encrypted). Returns (success, error_msg)."""
        async with self._lock:
            conn = self._connections.get(device_id)
        if not conn:
            return False, "Device not connected via LAN"
        if not conn.online:
            return False, "LAN connection offline"
        ok = await conn.send_command(params)
        return ok, None if ok else "Send failed"


ewelink_lan_service = EweLinkLanService()
