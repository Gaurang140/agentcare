"""Reminders and post-visit follow-up tasks (both stored as Reminder rows).

Two ways in, on purpose. `create_reminder` writes one row and commits it,
which is what a caller scheduling a single reminder wants.
`create_reminders_batch` writes a whole set in one transaction: every row is
flushed, then one commit closes the lot, and any failure rolls the set back
so nothing survives.

The batch exists because `agents/followup.py` resumes by appointment. That
node skips its entire creation block when the appointment already has any
reminder, since a re-planned batch names different types at different times
and could never be deduplicated row by row. Row-at-a-time commits would let
a process die with one row written, and the resumed node would read that one
row as the finished batch. All-or-nothing is what makes the skip correct.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import NamedTuple

from sqlalchemy.orm import Session

from app.models import Reminder
from app.tools.audit_tools import write_audit


def _naive_utcnow() -> datetime:
    """Timezone-aware now(), stripped back to naive - matches the naive
    (but UTC-convention) DateTime columns used throughout the schema.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def reminder_summary(reminder: Reminder) -> dict:
    """The dict shape every reminder path hands back to the agents."""
    return {
        "id": reminder.id,
        "patient_id": reminder.patient_id,
        "appointment_id": reminder.appointment_id,
        "reminder_type": reminder.reminder_type,
        "scheduled_at": reminder.scheduled_at.isoformat(),
        "sent": reminder.sent,
    }


def _add_reminder(
    db: Session,
    patient_id: int,
    appointment_id: int | None,
    reminder_type: str,
    scheduled_at: datetime,
) -> Reminder:
    """Construct, flush, and audit one reminder without owning its transaction."""
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
    return reminder


def create_reminder(
    db: Session,
    patient_id: int,
    appointment_id: int | None,
    reminder_type: str,
    scheduled_at: datetime,
) -> dict:
    """Schedule one reminder for a patient (optionally tied to an appointment)."""
    reminder = _add_reminder(
        db,
        patient_id,
        appointment_id,
        reminder_type,
        scheduled_at,
    )
    db.commit()

    return reminder_summary(reminder)


class ReminderItem(NamedTuple):
    """One row `create_reminders_batch` will write."""

    reminder_type: str
    scheduled_at: datetime


def create_reminders_batch(
    db: Session,
    patient_id: int,
    appointment_id: int | None,
    items: Sequence[ReminderItem],
) -> list[dict]:
    """Schedule a whole set of reminders as one transaction: all of the rows
    or none of them.

    Every row and its audit event is flushed inside the loop, and exactly one
    commit follows the loop. Anything raising on the way through rolls the
    session back and propagates, so the caller sees the failure and the
    database keeps no part of the batch (see the module docstring for why a
    half-written batch is worse here than no batch at all).
    """
    summaries: list[dict] = []
    try:
        for reminder_type, scheduled_at in items:
            reminder = _add_reminder(
                db,
                patient_id,
                appointment_id,
                reminder_type,
                scheduled_at,
            )
            # Read before the commit expires the instance, so the summaries
            # cost no extra queries.
            summaries.append(reminder_summary(reminder))
        db.commit()
    except Exception:
        db.rollback()
        raise

    return summaries


def send_due_reminders(db: Session) -> dict:
    """Scheduler job1's body: every unsent Reminder whose scheduled_at has
    passed becomes sent, each with its own "reminder.sent" AuditEvent. Also
    the body POST /api/internal/reminders/run-due calls on demand."""
    due = (
        db.query(Reminder)
        .filter(Reminder.sent.is_(False), Reminder.scheduled_at <= _naive_utcnow())
        .all()
    )

    sent_ids: list[int] = []
    for reminder in due:
        reminder.sent = True
        db.flush()
        write_audit(
            db,
            None,
            "reminder.sent",
            "reminder",
            reminder.id,
            {"patient_id": reminder.patient_id, "reminder_type": reminder.reminder_type},
        )
        sent_ids.append(reminder.id)
    db.commit()

    return {"sent_count": len(sent_ids), "reminder_ids": sent_ids}
