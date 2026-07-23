"""Bookable calendar slots and the appointments made against them."""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.catalog import Doctor
    from app.models.user import User


class AppointmentSlot(Base):
    """A 30-minute calendar slot for one doctor. status: free|booked|blocked."""

    __tablename__ = "appointment_slots"
    __table_args__ = (UniqueConstraint("doctor_id", "start_time", name="uq_slot_doctor_start"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="free", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    doctor: Mapped["Doctor"] = relationship(back_populates="slots")
    appointment: Mapped["Appointment | None"] = relationship(back_populates="slot", uselist=False)


class Appointment(Base):
    """A patient's booking against one AppointmentSlot. status: pending|confirmed|cancelled|completed."""

    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), nullable=False)
    slot_id: Mapped[int] = mapped_column(ForeignKey("appointment_slots.id"), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    slot: Mapped["AppointmentSlot"] = relationship(back_populates="appointment")
    doctor: Mapped["Doctor"] = relationship()
    patient: Mapped["User"] = relationship()
