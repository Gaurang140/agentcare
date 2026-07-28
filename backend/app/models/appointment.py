"""Bookable calendar slots and the appointments made against them."""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DDL,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.catalog import Doctor
    from app.models.user import User

# Partial-index predicate: only live bookings that came from a workflow run
# take part in the one-booking-per-run rule. Cancelled rows drop out of the
# index, so the same run can rebook after a cancellation.
#
# The same predicate is written out again in alembic revision 8524b9522086
# (rework appointment slot linkage), which is what actually creates the index
# on a real database. Changing it here means changing it there too, in a new
# revision: a migrated database keeps whatever the migration said.
_ONE_CONFIRMED_PER_RUN = "workflow_run_id IS NOT NULL AND status = 'confirmed'"


class AppointmentSlot(Base):
    """A 30-minute calendar slot for one doctor. status: free|booked|blocked."""

    __tablename__ = "appointment_slots"
    __table_args__ = (
        UniqueConstraint("doctor_id", "start_time", name="uq_slot_doctor_start"),
        CheckConstraint("end_time > start_time", name="ck_appointment_slots_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="free", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    doctor: Mapped["Doctor"] = relationship(back_populates="slots")
    # One row per booking the slot has ever carried, cancellations included:
    # freeing a slot leaves its old Appointment behind as history, so a slot
    # that has been cancelled can be booked again.
    appointments: Mapped[list["Appointment"]] = relationship(back_populates="slot")


class Appointment(Base):
    """A patient's booking against one AppointmentSlot. status: pending|confirmed|cancelled|completed."""

    __tablename__ = "appointments"
    __table_args__ = (
        Index(
            "uq_appointments_workflow_run",
            "workflow_run_id",
            unique=True,
            sqlite_where=text(_ONE_CONFIRMED_PER_RUN),
            postgresql_where=text(_ONE_CONFIRMED_PER_RUN),
        ),
        Index(
            "uq_appointments_patient_booking_window",
            "patient_id",
            "booking_window_key",
            unique=True,
            sqlite_where=text(
                "booking_window_key IS NOT NULL AND status IN ('pending', 'confirmed')"
            ),
            postgresql_where=text(
                "booking_window_key IS NOT NULL AND status IN ('pending', 'confirmed')"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), nullable=False)
    # Not unique: the live claim on a slot is AppointmentSlot.status, held by
    # the conditional UPDATE in book_appointment. A unique slot_id would also
    # bar rebooking a slot whose earlier appointment was cancelled.
    slot_id: Mapped[int] = mapped_column(
        ForeignKey("appointment_slots.id"), nullable=False, index=True
    )
    workflow_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("workflow_runs.id"), nullable=True
    )
    booking_window_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    scheduled_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    scheduled_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    slot: Mapped["AppointmentSlot"] = relationship(back_populates="appointments")
    doctor: Mapped["Doctor"] = relationship()
    patient: Mapped["User"] = relationship()


Appointment.__table__.append_constraint(
    ExcludeConstraint(
        (Appointment.patient_id, "="),
        (
            func.tsrange(
                Appointment.scheduled_start,
                Appointment.scheduled_end,
                text("'[)'"),
            ),
            "&&",
        ),
        where=text("status IN ('pending', 'confirmed')"),
        using="gist",
        name="ex_appointments_patient_schedule",
    ).ddl_if(dialect="postgresql")
)
Appointment.__table__.append_constraint(
    CheckConstraint(
        "scheduled_end > scheduled_start",
        name="ck_appointments_scheduled_range",
    )
)
AppointmentSlot.__table__.append_constraint(
    ExcludeConstraint(
        (AppointmentSlot.doctor_id, "="),
        (
            func.tsrange(
                AppointmentSlot.start_time,
                AppointmentSlot.end_time,
                text("'[)'"),
            ),
            "&&",
        ),
        using="gist",
        name="ex_appointment_slots_doctor_schedule",
    ).ddl_if(dialect="postgresql")
)

# Production PostgreSQL receives the same extension from Alembic. This hook
# keeps direct metadata-created PostgreSQL databases equivalent as well.
event.listen(
    Base.metadata,
    "before_create",
    DDL("CREATE EXTENSION IF NOT EXISTS btree_gist").execute_if(dialect="postgresql"),
)

# SQLite has no exclusion constraints. Conditional DDL gives tests and local
# create_all databases the same half-open interval behavior as PostgreSQL.
event.listen(
    Appointment.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_appointments_patient_schedule_insert
        BEFORE INSERT ON appointments
        WHEN NEW.status IN ('pending', 'confirmed')
          AND EXISTS (
            SELECT 1
            FROM appointments AS existing
            WHERE existing.patient_id = NEW.patient_id
              AND existing.status IN ('pending', 'confirmed')
              AND existing.scheduled_start < NEW.scheduled_end
              AND existing.scheduled_end > NEW.scheduled_start
          )
        BEGIN
          SELECT RAISE(ABORT, 'ex_appointments_patient_schedule');
        END
        """
    ).execute_if(dialect="sqlite"),
)
event.listen(
    Appointment.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_appointments_patient_schedule_update
        BEFORE UPDATE ON appointments
        WHEN NEW.status IN ('pending', 'confirmed')
          AND EXISTS (
            SELECT 1
            FROM appointments AS existing
            WHERE existing.patient_id = NEW.patient_id
              AND existing.id != OLD.id
              AND existing.status IN ('pending', 'confirmed')
              AND existing.scheduled_start < NEW.scheduled_end
              AND existing.scheduled_end > NEW.scheduled_start
          )
        BEGIN
          SELECT RAISE(ABORT, 'ex_appointments_patient_schedule');
        END
        """
    ).execute_if(dialect="sqlite"),
)
event.listen(
    AppointmentSlot.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_appointment_slots_doctor_schedule_insert
        BEFORE INSERT ON appointment_slots
        WHEN EXISTS (
          SELECT 1
          FROM appointment_slots AS existing
          WHERE existing.doctor_id = NEW.doctor_id
            AND existing.start_time < NEW.end_time
            AND existing.end_time > NEW.start_time
        )
        BEGIN
          SELECT RAISE(ABORT, 'ex_appointment_slots_doctor_schedule');
        END
        """
    ).execute_if(dialect="sqlite"),
)
event.listen(
    AppointmentSlot.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_appointment_slots_doctor_schedule_update
        BEFORE UPDATE ON appointment_slots
        WHEN EXISTS (
          SELECT 1
          FROM appointment_slots AS existing
          WHERE existing.doctor_id = NEW.doctor_id
            AND existing.id != OLD.id
            AND existing.start_time < NEW.end_time
            AND existing.end_time > NEW.start_time
        )
        BEGIN
          SELECT RAISE(ABORT, 'ex_appointment_slots_doctor_schedule');
        END
        """
    ).execute_if(dialect="sqlite"),
)
