"""Fernet encryption for eWeLink token storage."""
import base64
import logging
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from app.config import get_settings

logger = logging.getLogger(__name__)


def _get_fernet() -> Fernet:
    settings = get_settings()
    key = settings.encryption_key
    if len(key) == 44 and key.endswith("="):
        try:
            return Fernet(key.encode() if isinstance(key, str) else key)
        except Exception:
            pass
    # Derive key from passphrase
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"iot_monitor_ewelink",
        iterations=480000,
    )
    derived = base64.urlsafe_b64encode(kdf.derive(key.encode() if isinstance(key, str) else key))
    return Fernet(derived)


def encrypt_token(plain: str) -> str:
    f = _get_fernet()
    return f.encrypt(plain.encode()).decode()


def decrypt_token(encrypted: str) -> Optional[str]:
    try:
        f = _get_fernet()
        return f.decrypt(encrypted.encode()).decode()
    except InvalidToken:
        logger.warning("Failed to decrypt token - invalid or corrupted")
        return None
