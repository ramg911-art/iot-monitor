"""AES-CBC encryption for eWeLink LAN protocol (Sonoff R3/MiniR2/POWR2/DualR3)."""
import base64
import hashlib
import json

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad


def derive_key(apikey: str) -> bytes:
    return hashlib.md5(apikey.encode()).digest()


def derive_iv(device_id: str) -> bytes:
    """AES-CBC IV must be 16 bytes; pad device_id if shorter."""
    raw = device_id[:16].encode()
    return raw.ljust(16, b"\x00")[:16]


def encrypt_payload(device_id: str, apikey: str, payload_dict: dict) -> str:
    plain = json.dumps(payload_dict)
    key = derive_key(apikey)
    iv = derive_iv(device_id)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    padded = pad(plain.encode(), AES.block_size)
    encrypted = cipher.encrypt(padded)
    return base64.b64encode(encrypted).decode()


def decrypt_payload(device_id: str, apikey: str, encrypted_string: str) -> dict:
    encrypted = base64.b64decode(encrypted_string)
    key = derive_key(apikey)
    iv = derive_iv(device_id)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    decrypted = cipher.decrypt(encrypted)
    unpadded = unpad(decrypted, AES.block_size)
    return json.loads(unpadded.decode())
