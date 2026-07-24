"""TDD for app.agents.followup.run: turns an LLM reminder plan into real
Reminder rows (appointment reminder + one per missing document + the
post-visit follow-up task), only once there's a confirmed appointment.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.agents import followup
from app.models import AppointmentSlot, Reminder
from app.tools.appointment_tools import book_appointment


def _booked_state(db, **overrides) -> dict:
    slot = db.query(AppointmentSlot).filter_by(status="free").order_by(AppointmentSlot.start_time).first()
    booking = book_appointment(db, patient_id=1, slot_id=slot.id, reason="checkup")
    base = {
        "workflow_id": 1,
        "patient_id": 1,
        "appointment": booking,
        "documents_result": {"missing": ["ecg_report"]},
    }
    base.update(overrides)
    return base


def test_creates_reminder_and_followup_task_rows(db, seeded, fake_llm):
    state = _booked_state(db)
    fake_llm(
        [
            {
                "reminders": [
                    {"type": "appointment_reminder", "days_before_appointment": 1},
                    {"type": "missing_document:ecg_report", "days_before_appointment": 3},
                ],
                "followup_days_after": 14,
            }
        ]
    )

    result = followup.run(state, db)

    reminders = db.query(Reminder).filter_by(patient_id=1).all()
    types = sorted(r.reminder_type for r in reminders)
    assert types == ["appointment_reminder", "followup", "missing_document:ecg_report"]
    assert len(result["reminders"]) == 3
    assert result["completed_steps"] == ["followup"]


def test_caps_reminder_count_and_clamps_days_before_appointment(db, seeded, fake_llm):
    """An LLM that returns a long reminder list with out-of-range days must
    not be able to flood the schedule, put a reminder after the appointment
    (negative days) or park one far in the past (huge days), where
    send_due_reminders would mark it sent immediately."""
    state = _booked_state(db)
    start_time = datetime.fromisoformat(state["appointment"]["start_time"])
    fake_llm(
        [
            {
                "reminders": [
                    {"type": f"appointment_reminder_{index}", "days_before_appointment": days}
                    for index, days in enumerate([-3, 400, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
                ],
                "followup_days_after": 14,
            }
        ]
    )

    result = followup.run(state, db)

    scheduled = (
        db.query(Reminder).filter(Reminder.patient_id == 1, Reminder.reminder_type != "followup").all()
    )
    assert len(scheduled) == 5
    earliest = start_time - timedelta(days=90)
    for reminder in scheduled:
        assert earliest <= reminder.scheduled_at <= start_time
    # the five capped reminders plus the post-visit follow-up task
    assert len(result["reminders"]) == 6


def test_clamps_followup_days_after(db, seeded, fake_llm):
    state = _booked_state(db)
    start_time = datetime.fromisoformat(state["appointment"]["start_time"])
    fake_llm([{"reminders": [], "followup_days_after": 400}])

    followup.run(state, db)

    task = db.query(Reminder).filter_by(patient_id=1, reminder_type="followup").one()
    assert task.scheduled_at == start_time + timedelta(days=90)


def test_skips_without_a_confirmed_appointment(db, seeded, fake_llm):
    client = fake_llm([])

    result = followup.run(
        {"workflow_id": 1, "patient_id": 1, "appointment": None, "documents_result": None}, db
    )

    assert db.query(Reminder).count() == 0
    assert len(client.chat.completions.calls) == 0
    assert result["completed_steps"] == ["followup"]
