"""Sensor history for temperature, humidity, state, power."""
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.device import Device


class SensorHistory(Base):
    __tablename__ = "sensor_history"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True)

    temperature: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    humidity: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    state: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    power: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    voltage: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    current: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    device: Mapped["Device"] = relationship("Device", back_populates="history")
