"""Hospital catalog: departments, doctors, and per-department required documents."""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.appointment import AppointmentSlot


class Department(Base):
    """A hospital department, e.g. Cardiology."""

    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    doctors: Mapped[list["Doctor"]] = relationship(back_populates="department")
    required_documents: Mapped[list["RequiredDocument"]] = relationship(back_populates="department")


class Doctor(Base):
    """A doctor belonging to one department; owns a calendar of AppointmentSlots."""

    __tablename__ = "doctors"

    id: Mapped[int] = mapped_column(primary_key=True)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    department: Mapped["Department"] = relationship(back_populates="doctors")
    slots: Mapped[list["AppointmentSlot"]] = relationship(back_populates="doctor")


class RequiredDocument(Base):
    """A document type a department requires before an appointment (e.g. ecg_report)."""

    __tablename__ = "required_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"), nullable=False)
    document_type: Mapped[str] = mapped_column(String(50), nullable=False)

    department: Mapped["Department"] = relationship(back_populates="required_documents")
