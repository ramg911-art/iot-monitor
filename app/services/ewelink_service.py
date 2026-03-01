"""eWeLink Cloud API integration - OAuth, device sync, toggle, webhook."""
import json
import logging
import secrets
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.device import DEVICE_TYPE_EWELINK, SOURCE_EWELINK, Device
from app.models.ewelink_token import EwelinkToken
from app.services.encryption_service import decrypt_token, encrypt_token

logger = logging.getLogger(__name__)

# eWeLink regional API bases
API_BASE = {
    "cn": "https://cn-apia.coolkit.cn",
    "as": "https://as-apia.coolkit.cc",
    "us": "https://us-apia.coolkit.cc",
    "eu": "https://eu-apia.coolkit.cc",
}
# OAuth (based on eWeLink dev platform - exact URLs may vary by region)
OAUTH_AUTH_BASE = {
    "cn": "https://oauth2.coolkit.cn",
    "as": "https://oauth2.coolkit.cc",
    "us": "https://oauth2.coolkit.cc",
    "eu": "https://oauth2.coolkit.cc",
}
DEFAULT_REGION = "eu"


def _get_oauth_auth_url(region: str = DEFAULT_REGION) -> str:
    base = OAUTH_AUTH_BASE.get(region, OAUTH_AUTH_BASE["eu"])
    return f"{base}/oauth2/authorize"


def _get_oauth_token_url(region: str) -> str:
    base = API_BASE.get(region, API_BASE["eu"])
    return f"{base}/v2/user/oauth/getToken"


def create_oauth_login_url(state: Optional[str] = None) -> str:
    """Generate eWeLink OAuth authorization URL."""
    settings = get_settings()
    if not settings.ewelink_client_id or not settings.ewelink_redirect_uri:
        raise ValueError("eWeLink client_id and redirect_uri must be configured")
    state = state or secrets.token_urlsafe(16)
    params = {
        "client_id": settings.ewelink_client_id,
        "redirect_uri": settings.ewelink_redirect_uri,
        "response_type": "code",
        "state": state,
        "grant_type": "authorization_code",
    }
    # Try EU region first - user will be redirected if wrong region
    url = _get_oauth_auth_url("eu")
    return f"{url}?{urllib.parse.urlencode(params)}"


async def exchange_oauth_code(
    session: AsyncSession,
    code: str,
    region: str,
    user_id: int,
) -> tuple[bool, str]:
    """
    Exchange OAuth code for tokens. Store encrypted. Returns (success, message).
    """
    settings = get_settings()
    if not settings.ewelink_client_id or not settings.ewelink_client_secret or not settings.ewelink_redirect_uri:
        return False, "eWeLink not configured"

    token_url = _get_oauth_token_url(region)
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(
                token_url,
                json={
                    "client_id": settings.ewelink_client_id,
                    "client_secret": settings.ewelink_client_secret,
                    "redirect_uri": settings.ewelink_redirect_uri,
                    "code": code,
                    "grant_type": "authorization_code",
                },
                headers={"Content-Type": "application/json", "X-CK-Appid": settings.ewelink_client_id},
            )
            data = resp.json()
        except Exception as e:
            logger.exception("eWeLink token exchange failed")
            return False, str(e)

    if data.get("error", 0) != 0:
        return False, data.get("msg", "Unknown error")

    at = data.get("data", {}).get("at")
    rt = data.get("data", {}).get("rt")
    if not at or not rt:
        return False, "No tokens in response"

    # Default 30 days
    expires_at = datetime.utcnow() + timedelta(days=28)

    # Upsert token for user (single binding)
    result = await session.execute(select(EwelinkToken).where(EwelinkToken.user_id == user_id))
    token_row = result.scalars().first()
    if token_row:
        token_row.encrypted_access_token = encrypt_token(at)
        token_row.encrypted_refresh_token = encrypt_token(rt)
        token_row.region = region
        token_row.expires_at = expires_at
    else:
        token_row = EwelinkToken(
            user_id=user_id,
            encrypted_access_token=encrypt_token(at),
            encrypted_refresh_token=encrypt_token(rt),
            region=region,
            expires_at=expires_at,
        )
        session.add(token_row)
    await session.flush()
    return True, "eWeLink connected successfully"


async def get_valid_access_token(session: AsyncSession, user_id: int) -> tuple[Optional[str], Optional[str]]:
    """
    Get valid access token, refreshing if needed.
    Returns (access_token, region) or (None, None).
    """
    result = await session.execute(select(EwelinkToken).where(EwelinkToken.user_id == user_id))
    token_row = result.scalars().first()
    if not token_row:
        return None, None

    at = decrypt_token(token_row.encrypted_access_token)
    if not at:
        return None, None

    # Refresh if within 2 days of expiry
    if token_row.expires_at and (token_row.expires_at - datetime.utcnow()) < timedelta(days=2):
        rt = decrypt_token(token_row.encrypted_refresh_token)
        if rt:
            new_at, new_rt = await _refresh_tokens(session, token_row, rt)
            if new_at:
                return new_at, token_row.region
        return None, None

    return at, token_row.region


async def _refresh_tokens(
    session: AsyncSession, token_row: EwelinkToken, rt: str
) -> tuple[Optional[str], Optional[str]]:
    """Refresh access token. Returns (new_at, new_rt) or (None, None)."""
    settings = get_settings()
    url = f"{API_BASE.get(token_row.region, API_BASE['eu'])}/v2/user/refresh"
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(
                url,
                json={"rt": rt},
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {rt}",
                    "X-CK-Appid": settings.ewelink_client_id or "",
                },
            )
            data = resp.json()
        except Exception as e:
            logger.warning("eWeLink token refresh failed: %s", e)
            return None, None

    if data.get("error", 0) != 0:
        logger.warning("eWeLink refresh error: %s", data.get("msg"))
        return None, None

    new_at = data.get("data", {}).get("at")
    new_rt = data.get("data", {}).get("rt", rt)
    if not new_at:
        return None, None

    token_row.encrypted_access_token = encrypt_token(new_at)
    token_row.encrypted_refresh_token = encrypt_token(new_rt)
    token_row.expires_at = datetime.utcnow() + timedelta(days=28)
    await session.flush()
    return new_at, new_rt


async def ewelink_api_request(
    session: AsyncSession,
    user_id: int,
    method: str,
    path: str,
    region: Optional[str] = None,
    json_body: Optional[dict] = None,
) -> tuple[Optional[dict], Optional[str]]:
    """Make authenticated eWeLink API request. Returns (data, error_msg)."""
    at, reg = await get_valid_access_token(session, user_id)
    if not at:
        return None, "eWeLink not connected or token expired"
    reg = region or reg
    base = API_BASE.get(reg, API_BASE["eu"])
    url = f"{base}{path}"
    settings = get_settings()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {at}",
        "X-CK-Appid": settings.ewelink_client_id or "",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            if method == "GET":
                resp = await client.get(url, headers=headers)
            elif method == "POST":
                resp = await client.post(url, headers=headers, json=json_body or {})
            elif method == "PUT":
                resp = await client.put(url, headers=headers, json=json_body or {})
            else:
                return None, "Unsupported method"
            data = resp.json()
        except Exception as e:
            return None, str(e)

    if data.get("error", 0) != 0:
        return None, data.get("msg", "API error")
    return data.get("data", {}), None


async def sync_ewelink_devices(
    session: AsyncSession,
    user_id: int,
    broadcast_cb: Optional[Callable] = None,
) -> tuple[list[dict], Optional[str]]:
    """
    Fetch device list from eWeLink and upsert into DB.
    Returns (list of device dicts, error_msg).
    """
    data, err = await ewelink_api_request(session, user_id, "GET", "/v2/device/thing?num=0")
    if err:
        return [], err

    things = data.get("thingList", [])
    devices_out = []
    for item in things:
        itype = item.get("itemType", 0)
        if itype == 3:  # Skip groups
            continue
        thing = item.get("itemData", {})
        device_id = thing.get("deviceid") or thing.get("id")
        if not device_id:
            continue

        result = await session.execute(select(Device).where(Device.ewelink_device_id == device_id))
        dev = result.scalars().first()
        if not dev:
            dev = Device(
                name=thing.get("name", device_id),
                device_type=DEVICE_TYPE_EWELINK,
                source=SOURCE_EWELINK,
                ewelink_device_id=device_id,
            )
            session.add(dev)
            await session.flush()

        params = thing.get("params", {}) or {}
        switch = params.get("switch")
        if switch:
            dev.state = "on" if switch == "on" else "off"
        switches = params.get("switches")
        if switches and isinstance(switches, list) and len(switches) > 0:
            first = switches[0].get("switch", "off")
            dev.state = "on" if first == "on" else "off"
        power = params.get("power") or params.get("load")
        if power is not None:
            dev.power = float(power) if isinstance(power, (int, float, str)) else None
        dev.online = thing.get("online", True)
        dev.extra_data = json.dumps(params) if params else None
        dev.name = thing.get("name", dev.name)
        dev.last_seen = datetime.utcnow()
        await session.flush()
        devices_out.append(_device_to_dict(dev))
        if broadcast_cb:
            await broadcast_cb("device", dev)

    return devices_out, None


async def toggle_ewelink_device(
    session: AsyncSession,
    user_id: int,
    device_id: str,
    turn_on: bool,
) -> tuple[bool, Optional[str]]:
    """Toggle eWeLink device. Returns (success, error_msg)."""
    params_key = "switch"
    params_value = "on" if turn_on else "off"
    # Some devices use "switches" array
    data, err = await ewelink_api_request(
        session,
        user_id,
        "PUT",
        "/v2/device/thing/status",
        json_body={
            "type": 1,
            "id": device_id,
            "params": {params_key: params_value},
        },
    )
    if err:
        return False, err

    # Update local device
    result = await session.execute(select(Device).where(Device.ewelink_device_id == device_id))
    dev = result.scalars().first()
    if dev:
        dev.state = params_value
        await session.flush()
    return True, None


def _device_to_dict(d: Device) -> dict:
    return {
        "id": d.id,
        "name": d.name,
        "device_type": d.device_type,
        "state": d.state,
        "online": d.online,
        "power": d.power,
        "ewelink_device_id": d.ewelink_device_id,
    }


def parse_webhook_payload(body: dict) -> Optional[dict]:
    """
    Parse eWeLink webhook POST body.
    User must create a webhook scene in eWeLink that sends to our URL.
    Payload structure varies - we accept common patterns.
    Returns dict with deviceid, state, etc. or None.
    """
    if not body:
        return None
    # Common fields from eWeLink webhook
    device_id = body.get("deviceid") or body.get("deviceId")
    if device_id:
        return {
            "deviceid": device_id,
            "state": body.get("switch") or body.get("state", "off"),
            "params": body.get("params", body),
        }
    # Alternative nested structure
    data = body.get("data", body)
    if isinstance(data, dict):
        return parse_webhook_payload(data)
    return None
