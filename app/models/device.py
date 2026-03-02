"""Device model for cameras, Tapo, eWeLink."""
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.sensor_history import SensorHistory


# Device types
DEVICE_TYPE_CAMERA = "camera"
DEVICE_TYPE_TAPO_H100 = "tapo_h100"
DEVICE_TYPE_TAPO_SWITCH = "tapo_switch"
DEVICE_TYPE_TAPO_PLUG = "tapo_plug"
DEVICE_TYPE_EWELINK = "ewelink"
DEVICE_TYPE_DOOR = "door"  # H100 door sensor T110 (child of tapo_h100)
DEVICE_TYPE_TEMP_SENSOR = "temp_sensor"  # H100 temp/humidity sensor T310
DEVICE_TYPE_NVR = "nvr"  # Dahua NVR
DEVICE_TYPE_NVR_CAMERA = "nvr_camera"  # NVR channel (child of nvr)

# Source for device origin
SOURCE_TAPO_H100 = "tapo_h100"
SOURCE_TAPO_WIFI = "tapo_wifi"
SOURCE_EWELINK = "ewelink"
SOURCE_MANUAL = "manual"
SOURCE_DAHUA = "dahua"


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    device_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default=SOURCE_MANUAL)

    # Connection / identity
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)  # IPv6 support
    rtsp_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    go2rtc_stream_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    ewelink_device_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)  # deviceid
    tapo_device_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    parent_device_id: Mapped[Optional[int]] = mapped_column(ForeignKey("devices.id"), nullable=True)
    # eWeLink LAN auth - encrypted, from cloud sync
    ewelink_apikey_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # NVR / camera
    channel_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    stream_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    total_channels: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # NVR parent only
    nvr_username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    nvr_password_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # LAN mode (eWeLink/Sonoff local control)
    lan_ip: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    lan_online: Mapped[bool] = mapped_column(default=False)
    prefer_lan: Mapped[bool] = mapped_column(default=True)

    # Current state (denormalized for quick access)
    state: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # on, off, open, closed
    temperature: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    humidity: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    power: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Watts
    voltage: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    current: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    battery: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # % for sensors
    online: Mapped[bool] = mapped_column(default=True)

    # Metadata
    extra_data: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON
    last_seen: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    parent: Mapped[Optional["Device"]] = relationship(
        "Device", remote_side="Device.id", foreign_keys=[parent_device_id], back_populates="children"
    )
    children: Mapped[list["Device"]] = relationship("Device", back_populates="parent")
    history: Mapped[list["SensorHistory"]] = relationship(
        "SensorHistory", back_populates="device", cascade="all, delete-orphan", order_by="SensorHistory.timestamp.desc()"
    )
