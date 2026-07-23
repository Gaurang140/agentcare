"""TDD for app.agents.appointment.run: slot selection validated against the
real available-slots list, plus conflict retry/escalate behavior.
"""

from __future__ import annotations

from app.agents import appointment
from app.exceptions import ConflictError
from app.models import AppointmentSlot, Department, Escalation
from app.tools.appointment_tools import book_appointment


def _cardiology_id(db) -> int:
    dept = db.query(Department).filter_by(name="Cardiology").first()
    assert dept is not None
    return dept.id


def _free_slots(db, limit=5):
    return (
        db.query(AppointmentSlot)
        .filter_by(status="free")
        .order_by(AppointmentSlot.start_time)
        .limit(limit)
        .all()
    )


def _state(**overrides) -> dict:
    base = {
        "workflow_id": 1,
        "patient_id": 1,
        "request_text": "earliest available please",
        "intent": "book",
    }
    base.update(overrides)
    return base


def test_book_picks_valid_slot_and_confirms(db, seeded, fake_llm):
    dept_id = _cardiology_id(db)
    target = _free_slots(db)[0]
    fake_llm([{"slot_id": target.id, "reason": "earliest match"}])

    result = appointment.run(_state(department_id=dept_id), db)

    assert result["appointment"]["status"] == "confirmed"
    assert result["appointment"]["start_time"] == target.start_time.isoformat()
    db.refresh(target)
    assert target.status == "booked"
    assert result["completed_steps"] == ["appointment"]


def test_invented_slot_id_retries_once_then_escalates(db, seeded, fake_llm):
    dept_id = _cardiology_id(db)
    client = fake_llm(
        [
            {"slot_id": 999_999, "reason": "made up"},
            {"slot_id": 999_998, "reason": "still made up"},
        ]
    )

    result = appointment.run(_state(department_id=dept_id), db)

    assert result.get("appointment") is None
    assert result["escalation_id"] is not None
    escalation = db.get(Escalation, result["escalation_id"])
    assert escalation.severity == "agent_failure"
    assert len(client.chat.completions.calls) == 2


def test_conflict_retries_with_refreshed_slots_then_books(db, seeded, fake_llm, monkeypatch):
    dept_id = _cardiology_id(db)
    slots = _free_slots(db, limit=2)
    contested, backup = slots[0], slots[1]
    fake_llm(
        [
            {"slot_id": contested.id, "reason": "first pick"},
            {"slot_id": backup.id, "reason": "second pick after refresh"},
        ]
    )

    real_book = appointment.book_appointment
    calls = {"n": 0}

    def _flaky_book(db_, patient_id, slot_id, reason):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConflictError("slot taken by someone else")
        return real_book(db_, patient_id, slot_id, reason)

    monkeypatch.setattr(appointment, "book_appointment", _flaky_book)

    result = appointment.run(_state(department_id=dept_id), db)

    assert result["appointment"]["status"] == "confirmed"
    assert calls["n"] == 2


def test_repeated_conflict_escalates_agent_failure(db, seeded, fake_llm, monkeypatch):
    dept_id = _cardiology_id(db)
    slots = _free_slots(db, limit=2)
    fake_llm(
        [
            {"slot_id": slots[0].id, "reason": "first"},
            {"slot_id": slots[1].id, "reason": "retry"},
        ]
    )

    def _always_conflict(db_, patient_id, slot_id, reason):
        raise ConflictError("always taken")

    monkeypatch.setattr(appointment, "book_appointment", _always_conflict)

    result = appointment.run(_state(department_id=dept_id), db)

    assert result.get("appointment") is None
    assert result["escalation_id"] is not None
    escalation = db.get(Escalation, result["escalation_id"])
    assert escalation.severity == "agent_failure"


def test_cancel_intent_cancels_latest_active_appointment(db, seeded, fake_llm):
    slot = _free_slots(db)[0]
    booking = book_appointment(db, patient_id=1, slot_id=slot.id, reason="checkup")

    result = appointment.run(_state(intent="cancel"), db)

    assert result["appointment"]["status"] == "cancelled"
    assert result["appointment"]["id"] == booking["id"]


def test_reschedule_intent_moves_to_new_slot(db, seeded, fake_llm):
    dept_id = _cardiology_id(db)
    slots = _free_slots(db, limit=2)
    old_slot, new_slot = slots[0], slots[1]
    book_appointment(db, patient_id=1, slot_id=old_slot.id, reason="checkup")
    fake_llm([{"slot_id": new_slot.id, "reason": "prefers later"}])

    result = appointment.run(_state(intent="reschedule", department_id=dept_id), db)

    assert result["appointment"]["status"] == "confirmed"
    assert result["appointment"]["start_time"] == new_slot.start_time.isoformat()
    db.refresh(old_slot)
    assert old_slot.status == "free"


def test_irrelevant_intent_is_a_no_op(db, seeded, fake_llm):
    result = appointment.run(_state(intent="status"), db)

    assert result == {"completed_steps": ["appointment"]}
