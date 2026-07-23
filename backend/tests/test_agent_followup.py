"""TDD for app.agents.followup.run: turns an LLM reminder plan into real
Reminder rows (appointment reminder + one per missing document + the
post-visit follow-up task), only once there's a confirmed appointment.
"""

from __future__ import annotations

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


def test_skips_without_a_confirmed_appointment(db, seeded, fake_llm):
    client = fake_llm([])

    result = followup.run(
        {"workflow_id": 1, "patient_id": 1, "appointment": None, "documents_result": None}, db
    )

    assert db.query(Reminder).count() == 0
    assert len(client.chat.completions.calls) == 0
    assert result["completed_steps"] == ["followup"]
