"""TDD for app.agents.safety.run: the draft is composed from re-queried DB
rows (never the in-memory state dict), and the deterministic sanitizer wins
over whatever the LLM claims - even a poisoned "safe: true" response gets
its unsafe sentence stripped.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.agents import safety
from app.models import AppointmentSlot
from app.safety.guardrails import SANITIZED_SENTENCE
from app.tools.appointment_tools import book_appointment, cancel_appointment
from app.tools.followup_tools import create_reminder


def _booked_state(db, **overrides) -> dict:
    slot = db.query(AppointmentSlot).filter_by(status="free").order_by(AppointmentSlot.start_time).first()
    booking = book_appointment(db, patient_id=1, slot_id=slot.id, reason="checkup")
    create_reminder(
        db,
        patient_id=1,
        appointment_id=booking["id"],
        reminder_type="appointment_reminder",
        scheduled_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1),
    )
    base = {
        "workflow_id": 1,
        "patient_id": 1,
        "appointment": booking,
        "documents_result": {"missing": ["ecg_report"]},
    }
    base.update(overrides)
    return base


def test_safe_llm_response_passes_through_after_sanitizer(db, seeded, fake_llm):
    state = _booked_state(db)
    fake_llm(
        [
            {
                "safe": True,
                "violations": [],
                "rewritten": "Your appointment is confirmed. Please bring your ecg_report.",
            }
        ]
    )

    result = safety.run(state, db)

    assert "diagnosis" not in result["final_response"].lower()
    assert result["final_response"] == "Your appointment is confirmed. Please bring your ecg_report."
    assert result["completed_steps"] == ["safety"]


def test_deterministic_sanitizer_strips_poisoned_response_even_when_llm_says_safe(db, seeded, fake_llm):
    state = _booked_state(db)
    fake_llm(
        [
            {
                "safe": True,
                "violations": [],
                "rewritten": (
                    "Your appointment is confirmed. You have diabetes and should "
                    "take 500mg metformin daily."
                ),
            }
        ]
    )

    result = safety.run(state, db)

    final = result["final_response"]
    assert "diabetes" not in final
    assert "metformin" not in final
    assert SANITIZED_SENTENCE in final
    assert "Your appointment is confirmed." in final
    assert "deterministic_sanitizer_rewrote_output" in result["safety_flags"]


def test_llm_failure_falls_back_to_deterministic_only_path(db, seeded, fake_llm):
    state = _booked_state(db)
    fake_llm([RuntimeError("llm endpoint down")])

    result = safety.run(state, db)

    assert "error" not in result
    assert result["final_response"]
    assert "confirmed" in result["final_response"].lower()


def test_draft_reflects_live_db_status_not_stale_state_dict(db, seeded, fake_llm):
    """state["appointment"]["status"] was captured as "confirmed" at booking
    time. Cancel the appointment directly in the db afterwards (simulating
    staleness) and force the deterministic-only fallback path (LLM down) so
    the composed draft itself becomes final_response - proving the draft was
    built from the live row, not the cached dict still sitting in state."""
    state = _booked_state(db)
    cancel_appointment(db, state["appointment"]["id"])
    fake_llm([RuntimeError("llm down")])

    result = safety.run(state, db)

    assert "cancelled" in result["final_response"].lower()
    assert "confirmed" not in result["final_response"].lower()
