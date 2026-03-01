"""Application configuration from environment variables."""
from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    app_name: str = "IoT Monitor"
    debug: bool = False

    # Database
    database_url: str = "sqlite+aiosqlite:///./iot_monitor.db"

    # JWT
    jwt_secret: str = "change-me-in-production-use-long-random-string"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24  # 24 hours

    # eWeLink
    ewelink_client_id: Optional[str] = None
    ewelink_client_secret: Optional[str] = None
    ewelink_redirect_uri: Optional[str] = None  # e.g. https://your-domain.com/api/ewelink/callback
    encryption_key: str = "change-me-32-bytes-base64!!"  # 32 bytes for Fernet

    # Polling intervals (seconds)
    camera_poll_interval: int = 10
    tapo_poll_interval: int = 30
    tapo_switch_poll_interval: int = 25  # 20-30 sec range, use 25

    # Default admin (single-user)
    admin_username: str = "admin"
    admin_password: str = "admin"  # Must change on first deploy

    # go2rtc
    go2rtc_url: str = "http://go2rtc:1984"  # Docker service name


@lru_cache
def get_settings() -> Settings:
    return Settings()
