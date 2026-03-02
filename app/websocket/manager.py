"""WebSocket connection manager with JWT auth and broadcast."""
import asyncio
import json
import logging
from typing import Any, Optional

from fastapi import WebSocket, WebSocketDisconnect

from app.services.auth_service import decode_token

logger = logging.getLogger(__name__)


def _build_device_update_payload(device: Any) -> dict:
    """Build standardized device_update payload. device must have id, device_type, state, power, etc."""
    return {
        "type": "device_update",
        "device_id": device.id,
        "device_type": device.device_type,
        "data": {
            "state": device.state,
            "power": device.power,
            "temperature": device.temperature,
            "humidity": device.humidity,
            "battery": device.battery if hasattr(device, "battery") else None,
            "online": device.online,
            "lan_ip": getattr(device, "lan_ip", None),
            "lan_online": getattr(device, "lan_online", False),
            "prefer_lan": getattr(device, "prefer_lan", True),
        },
    }


class ConnectionManager:
    def __init__(self):
        self._connections: dict[str, WebSocket] = {}
        self._user_ids: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def connect(
        self,
        websocket: WebSocket,
        token: Optional[str] = None,
    ) -> Optional[int]:
        """
        Accept connection, verify JWT. Returns user_id or None.
        """
        await websocket.accept()
        if not token:
            await websocket.close(code=4001, reason="Missing token")
            return None
        payload = decode_token(token)
        if not payload or "sub" not in payload:
            await websocket.close(code=4001, reason="Invalid token")
            return None
        user_id = int(payload["sub"])
        conn_id = id(websocket)
        async with self._lock:
            self._connections[str(conn_id)] = websocket
            self._user_ids[str(conn_id)] = user_id
        logger.info("WebSocket connected: user_id=%s", user_id)
        return user_id

    async def disconnect(self, websocket: WebSocket) -> None:
        conn_id = str(id(websocket))
        async with self._lock:
            self._connections.pop(conn_id, None)
            self._user_ids.pop(conn_id, None)
        logger.debug("WebSocket disconnected: %s", conn_id)

    async def broadcast(self, event_type: str, payload: dict[str, Any]) -> None:
        """Broadcast to all connected clients. Uses {type, data} format."""
        msg = json.dumps({"type": event_type, "data": payload})
        async with self._lock:
            dead = []
            for cid, ws in self._connections.items():
                try:
                    await ws.send_text(msg)
                except Exception as e:
                    logger.debug("Broadcast failed to %s: %s", cid, e)
                    dead.append(cid)
            for cid in dead:
                self._connections.pop(cid, None)
                self._user_ids.pop(cid, None)

    async def broadcast_device_update(self, device: Any) -> None:
        """Broadcast standardized device_update. Sends type=device_update with device_id, device_type, data."""
        payload = _build_device_update_payload(device)
        msg = json.dumps(payload)
        async with self._lock:
            dead = []
            for cid, ws in self._connections.items():
                try:
                    await ws.send_text(msg)
                except Exception as e:
                    logger.debug("Broadcast failed to %s: %s", cid, e)
                    dead.append(cid)
            for cid in dead:
                self._connections.pop(cid, None)
                self._user_ids.pop(cid, None)

    @property
    def connection_count(self) -> int:
        return len(self._connections)


ws_manager = ConnectionManager()


async def heartbeat_loop(websocket: WebSocket, interval: float = 30) -> None:
    """Send periodic pings to keep connection alive."""
    try:
        while True:
            await asyncio.sleep(interval)
            await websocket.send_text(json.dumps({"type": "heartbeat", "data": {}}))
    except (WebSocketDisconnect, Exception):
        pass
