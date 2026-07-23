"""Reminders and post-visit follow-up tasks (both stored as Reminder rows)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.exceptions import NotFoundError
from app.models import Appointment, Reminder
from app.tools.audit_tools import write_audit


def _naive_utcnow() -> datetime:
    """Timezone-aware now(), stripped back to naive - matches the naive
    (but UTC-convention) DateTime columns used throughout the schema.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def create_reminder(
    db: Session,
    patient_id: int,
    appointment_id: int | None,
    reminder_type: str,
    scheduled_at: datetime,
) -> dict:
    """Schedule one reminder for a patient (optionally tied to an appointment)."""
    reminder = Reminder(
        patient_id=patient_id,
        appointment_id=appointment_id,
        reminder_type=reminder_type,
        scheduled_at=scheduled_at,
    )
    db.add(reminder)
    db.flush()

    write_audit(
        db,
        None,
        "reminder.created",
        "reminder",
        reminder.id,
        {
            "patient_id": patient_id,
            "appointment_id": appointment_id,
            "reminder_type": reminder_type,
        },
    )
    db.commit()

    return {
        "id": reminder.id,
        "patient_id": reminder.patient_id,
        "appointment_id": reminder.appointment_id,
        "reminder_type": reminder.reminder_type,
        "scheduled_at": reminder.scheduled_at.isoformat(),
        "sent": reminder.sent,
    }


def create_followup_task(
    db: Session, patient_id: int, appointment_id: int, days_after: int = 14
) -> dict:
    """A "followup" reminder timed days_after the appointment's slot start."""
    appt = db.get(Appointment, appointment_id)
    if appt is None:
        raise NotFoundError(f"Appointment {appointment_id} not found")

    base_time = appt.slot.start_time if appt.slot else _naive_utcnow()
    scheduled_at = base_time + timedelta(days=days_after)

    return create_reminder(db, patient_id, appointment_id, "followup", scheduled_at)
