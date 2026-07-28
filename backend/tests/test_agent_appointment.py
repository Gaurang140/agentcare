"""TDD for app.agents.appointment.run: slot selection validated against the
real available-slots list, plus conflict retry/escalate behavior.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from app.agents import appointment
from app.exceptions import ConflictError
from app.models import Appointment, AppointmentSlot, AuditEvent, Department, Escalation
from app.tools.appointment_tools import book_appointment, get_available_slots


def _cardiology_id(db) -> int:
    dept = db.query(Department).filter_by(name="Cardiology").first()
    assert dept is not None
    return dept.id


def _free_slots(db, limit=5):
    return (
        db.query(AppointmentSlot)
        .filter(
            AppointmentSlot.status == "free",
            AppointmentSlot.start_time >= datetime.now(),
        )
        .order_by(AppointmentSlot.start_time)
        .limit(limit)
        .all()
    )


def _next_non_overlapping_slot(db, slot: AppointmentSlot) -> AppointmentSlot:
    candidate = (
        db.query(AppointmentSlot)
        .filter(
            AppointmentSlot.status == "free",
            AppointmentSlot.start_time >= slot.end_time,
        )
        .order_by(AppointmentSlot.start_time)
        .first()
    )
    assert candidate is not None
    return candidate


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


def test_next_week_only_offers_and_books_slots_in_next_week(db, seeded, fake_llm):
    dept_id = _cardiology_id(db)
    today = date.today()
    next_monday = today + timedelta(days=(7 - today.weekday()))
    target = (
        db.query(AppointmentSlot)
        .filter(
            AppointmentSlot.status == "free",
            AppointmentSlot.start_time >= datetime.combine(next_monday, time.min),
            AppointmentSlot.start_time
            < datetime.combine(next_monday + timedelta(days=7), time.min),
        )
        .order_by(AppointmentSlot.start_time)
        .first()
    )
    assert target is not None
    client = fake_llm([{"slot_id": target.id, "reason": "inside requested week"}])

    result = appointment.run(
        _state(
            department_id=dept_id,
            request_text="Book a cardiology appointment next week",
        ),
        db,
    )

    booked = datetime.fromisoformat(result["appointment"]["start_time"])
    assert next_monday <= booked.date() <= next_monday + timedelta(days=6)
    prompt = client.chat.completions.calls[0]["messages"][-1]["content"]
    candidate_listing = prompt.split("Available slots", maxsplit=1)[1]
    assert today.isoformat() not in candidate_listing


def test_explicit_window_with_no_slots_does_not_fall_back_or_call_llm(
    db, seeded, fake_llm
):
    client = fake_llm([])

    result = appointment.run(
        _state(
            department_id=_cardiology_id(db),
            request_text="Book a cardiology appointment on 30 September 2030",
        ),
        db,
    )

    assert result["appointment"]["status"] == "unavailable"
    assert result["scheduling_issue"]
    assert client.chat.completions.calls == []


def test_repeated_booking_intent_reuses_active_department_window_booking(
    db, seeded, fake_llm
):
    dept_id = _cardiology_id(db)
    target = _free_slots(db)[0]
    client = fake_llm([{"slot_id": target.id, "reason": "first booking"}])

    first = appointment.run(
        _state(workflow_id=101, department_id=dept_id),
        db,
    )
    second = appointment.run(
        _state(workflow_id=102, department_id=dept_id),
        db,
    )

    assert second["appointment"]["id"] == first["appointment"]["id"]
    assert second["appointment"]["reused_existing"] is True
    assert db.query(Appointment).count() == 1
    assert len(client.chat.completions.calls) == 1


def test_re_running_the_node_reuses_the_booking_instead_of_booking_twice(db, seeded, fake_llm):
    """A node commits its rows before LangGraph writes the checkpoint that
    records it ran, so a process killed in that window re-executes the whole
    node on resume. The second pass must return the appointment this run
    already booked, not book a second one against a different slot - and it
    must not ask the LLM for another slot to do it.

    The second scripted pick is what an un-guarded node would consume: it
    stays queued to prove the node stopped before slot selection rather than
    that the script ran out.
    """
    dept_id = _cardiology_id(db)
    slots = _free_slots(db, limit=2)
    client = fake_llm(
        [
            {"slot_id": slots[0].id, "reason": "first pass"},
            {"slot_id": slots[1].id, "reason": "the re-planned second pass"},
        ]
    )
    state = _state(department_id=dept_id)

    first = appointment.run(state, db)
    second = appointment.run(state, db)

    assert second["appointment"] == first["appointment"]
    assert second["completed_steps"] == ["appointment"]
    assert db.query(Appointment).count() == 1
    db.refresh(slots[1])
    assert slots[1].status == "free"
    assert db.query(AuditEvent).filter_by(action="appointment.reused_existing").count() == 1
    assert len(client.chat.completions.calls) == 1


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

    def _flaky_book(
        db_,
        patient_id,
        slot_id,
        reason,
        workflow_run_id=None,
        booking_window_key=None,
    ):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConflictError("slot taken by someone else")
        return real_book(
            db_,
            patient_id,
            slot_id,
            reason,
            workflow_run_id,
            booking_window_key,
        )

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

    def _always_conflict(
        db_,
        patient_id,
        slot_id,
        reason,
        workflow_run_id=None,
        booking_window_key=None,
    ):
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


def test_explicit_additional_request_can_book_after_previous_workflow(
    db, seeded, fake_llm
):
    dept_id = _cardiology_id(db)
    first_slot = _free_slots(db)[0]
    second_slot = _next_non_overlapping_slot(db, first_slot)
    client = fake_llm(
        [
            {"slot_id": first_slot.id, "reason": "first workflow"},
            {"slot_id": second_slot.id, "reason": "second workflow"},
        ]
    )

    first = appointment.run(_state(workflow_id=1, department_id=dept_id), db)
    second = appointment.run(
        _state(
            workflow_id=2,
            department_id=dept_id,
            request_text="Book another cardiology appointment",
        ),
        db,
    )

    assert first["appointment"]["start_time"] == first_slot.start_time.isoformat()
    assert second["appointment"]["start_time"] == second_slot.start_time.isoformat()
    second_candidate_list = client.chat.completions.calls[1]["messages"][1]["content"]
    assert first_slot.start_time.isoformat() not in second_candidate_list


def test_reschedule_excludes_appointment_being_moved_from_conflict_filter(
    db, seeded, fake_llm, monkeypatch
):
    dept_id = _cardiology_id(db)
    old_slot = _free_slots(db)[0]
    same_time_slot = (
        db.query(AppointmentSlot)
        .filter(
            AppointmentSlot.status == "free",
            AppointmentSlot.id != old_slot.id,
            AppointmentSlot.start_time == old_slot.start_time,
            AppointmentSlot.doctor.has(department_id=dept_id),
        )
        .one()
    )
    booking = book_appointment(db, patient_id=1, slot_id=old_slot.id, reason="checkup")
    fake_llm([{"slot_id": same_time_slot.id, "reason": "different doctor"}])
    availability_context: list[tuple[int | None, int | None]] = []

    def _capturing_slots(
        db_,
        department_id,
        date_from,
        date_to,
        limit=10,
        patient_id=None,
        exclude_appointment_id=None,
        not_before=None,
    ):
        availability_context.append((patient_id, exclude_appointment_id))
        return get_available_slots(
            db_,
            department_id,
            date_from,
            date_to,
            limit,
            patient_id,
            exclude_appointment_id,
            not_before,
        )

    monkeypatch.setattr(appointment, "get_available_slots", _capturing_slots)

    result = appointment.run(_state(intent="reschedule", department_id=dept_id), db)

    assert result["appointment"]["start_time"] == same_time_slot.start_time.isoformat()
    assert availability_context == [(1, booking["id"])]


def test_cancel_with_multiple_active_appointments_escalates_for_clarification(
    db, seeded, fake_llm
):
    first_slot = _free_slots(db)[0]
    second_slot = _next_non_overlapping_slot(db, first_slot)
    first = book_appointment(db, patient_id=1, slot_id=first_slot.id, reason="first")
    second = book_appointment(db, patient_id=1, slot_id=second_slot.id, reason="second")

    result = appointment.run(_state(intent="cancel"), db)

    assert result.get("appointment") is None
    escalation = db.get(Escalation, result["escalation_id"])
    assert escalation.severity == "uncertainty"
    assert "multiple active appointments" in escalation.reason
    assert db.get(Appointment, first["id"]).status == "confirmed"
    assert db.get(Appointment, second["id"]).status == "confirmed"


def test_reschedule_with_multiple_active_appointments_escalates_for_clarification(
    db, seeded, fake_llm
):
    dept_id = _cardiology_id(db)
    first_slot = _free_slots(db)[0]
    second_slot = _next_non_overlapping_slot(db, first_slot)
    first = book_appointment(db, patient_id=1, slot_id=first_slot.id, reason="first")
    second = book_appointment(db, patient_id=1, slot_id=second_slot.id, reason="second")
    client = fake_llm(
        [{"slot_id": _next_non_overlapping_slot(db, second_slot).id, "reason": "move"}]
    )

    result = appointment.run(_state(intent="reschedule", department_id=dept_id), db)

    assert result.get("appointment") is None
    escalation = db.get(Escalation, result["escalation_id"])
    assert escalation.severity == "uncertainty"
    assert "multiple active appointments" in escalation.reason
    assert db.get(Appointment, first["id"]).status == "confirmed"
    assert db.get(Appointment, second["id"]).status == "confirmed"
    assert len(client.chat.completions.calls) == 0


def test_status_intent_returns_current_sql_backed_appointments_without_llm_call(
    db, seeded, fake_llm
):
    slot = _free_slots(db)[0]
    booking = book_appointment(db, patient_id=1, slot_id=slot.id, reason="private symptom")
    client = fake_llm([])

    result = appointment.run(_state(intent="status"), db)

    assert result["appointment"] == {
        "status": "summary",
        "appointments": [
            {
                "id": booking["id"],
                "doctor": booking["doctor"],
                    "department": booking["department"],
                    "start_time": booking["start_time"],
                    "end_time": booking["end_time"],
                    "status": "confirmed",
            }
        ],
    }
    assert result["completed_steps"] == ["appointment"]
    assert len(client.chat.completions.calls) == 0


def test_status_with_multiple_active_appointments_returns_all_without_clarification(
    db, seeded, fake_llm
):
    first_slot = _free_slots(db)[0]
    second_slot = _next_non_overlapping_slot(db, first_slot)
    first = book_appointment(db, patient_id=1, slot_id=first_slot.id, reason="private first")
    second = book_appointment(db, patient_id=1, slot_id=second_slot.id, reason="private second")
    client = fake_llm([])

    result = appointment.run(_state(intent="status"), db)

    assert result["appointment"]["status"] == "summary"
    appointments_by_id = {
        row["id"]: row for row in result["appointment"]["appointments"]
    }
    assert appointments_by_id == {
        first["id"]: {
            "id": first["id"],
            "doctor": first["doctor"],
                "department": first["department"],
                "start_time": first["start_time"],
                "end_time": first["end_time"],
                "status": "confirmed",
        },
        second["id"]: {
            "id": second["id"],
            "doctor": second["doctor"],
                "department": second["department"],
                "start_time": second["start_time"],
                "end_time": second["end_time"],
                "status": "confirmed",
        },
    }
    assert result.get("escalation_id") is None
    assert all("reason" not in row for row in result["appointment"]["appointments"])
    assert len(client.chat.completions.calls) == 0


def test_status_without_active_appointments_returns_empty_summary(db, seeded, fake_llm):
    client = fake_llm([])

    result = appointment.run(_state(intent="status"), db)

    assert result["appointment"] == {"status": "summary", "appointments": []}
    assert result["completed_steps"] == ["appointment"]
    assert len(client.chat.completions.calls) == 0


# --- Redaction language ------------------------------------------------------
# The booking prompt carries the patient's own request text. A German cue in it
# decides ("Termin 4711 bitte verschieben" carries one, "4711" does not), and
# the stored preference breaks the tie when there is no cue - the same
# precedence the routing and coordinator nodes use
# (safety/pii.py::resolve_language).


def test_german_request_from_an_english_preferring_patient_runs_german(
    db, seeded, fake_llm, redaction_language
):
    """Patient 1 is stored as "en". A German cue in the request outranks that,
    so the booking prompt keeps "Termin" instead of losing it to the English
    model as a location."""
    seen = redaction_language(appointment)
    dept_id = _cardiology_id(db)
    target = _free_slots(db)[0]
    fake_llm([{"slot_id": target.id, "reason": "earliest match"}])

    appointment.run(
        _state(
            patient_id=1,
            department_id=dept_id,
            request_text="Ich brauche einen Termin in der Kardiologie",
        ),
        db,
    )

    assert seen == ["de"]


def test_booking_redaction_runs_with_the_patient_language(
    db, seeded, fake_llm, redaction_language
):
    """Patient 2 (Erika) prefers German, so the redaction of her booking
    request runs with the German model rather than the redactor's own guess."""
    seen = redaction_language(appointment)
    dept_id = _cardiology_id(db)
    target = _free_slots(db)[0]
    fake_llm([{"slot_id": target.id, "reason": "earliest match"}])

    appointment.run(_state(patient_id=2, department_id=dept_id), db)

    assert seen == ["de"]


def test_english_preference_reaches_the_booking_redaction_too(
    db, seeded, fake_llm, redaction_language
):
    """The other stored preference reaches it as well, so the assertion above
    is about the language being threaded, not about a German default."""
    seen = redaction_language(appointment)
    dept_id = _cardiology_id(db)
    target = _free_slots(db)[0]
    fake_llm([{"slot_id": target.id, "reason": "earliest match"}])

    appointment.run(_state(patient_id=1, department_id=dept_id), db)

    assert seen == ["en"]


# --- Replay after a completed cancel / reschedule ----------------------------
# Same crash window as the booking replay above: the node commits its rows
# before LangGraph writes the checkpoint saying it ran, so a process killed in
# between resumes into a node whose work is done and re-executes it from the
# top.


def test_cancel_replay_returns_the_same_success_instead_of_a_not_found_error(
    db, seeded, fake_llm
):
    """Without a guard the second pass finds no active appointment left and
    turns a cancellation that succeeded into a reported failure."""
    slot = _free_slots(db)[0]
    booking = book_appointment(db, patient_id=1, slot_id=slot.id, reason="checkup")
    state = _state(intent="cancel")

    first = appointment.run(state, db)
    second = appointment.run(state, db)

    assert first["appointment"] == {"id": booking["id"], "status": "cancelled"}
    assert second["appointment"] == first["appointment"]
    assert "error" not in second
    assert db.get(Appointment, booking["id"]).status == "cancelled"
    db.refresh(slot)
    assert slot.status == "free"
    assert db.query(AuditEvent).filter_by(action="appointment.reused_existing").count() == 1


def test_reschedule_replay_keeps_the_slot_it_already_moved_to(db, seeded, fake_llm):
    """The second pass must hand back the appointment this run already moved,
    with no second slot swap - and without asking the LLM for another slot.

    The second scripted pick stays queued to prove the node stopped before
    slot selection rather than that the script ran out.
    """
    dept_id = _cardiology_id(db)
    old_slot, new_slot, spare = _free_slots(db, limit=3)
    book_appointment(db, patient_id=1, slot_id=old_slot.id, reason="checkup")
    client = fake_llm(
        [
            {"slot_id": new_slot.id, "reason": "prefers later"},
            {"slot_id": spare.id, "reason": "the re-planned second pass"},
        ]
    )
    state = _state(intent="reschedule", department_id=dept_id)

    first = appointment.run(state, db)
    second = appointment.run(state, db)

    assert second["appointment"] == first["appointment"]
    assert second["appointment"]["start_time"] == new_slot.start_time.isoformat()
    for slot, expected in ((old_slot, "free"), (new_slot, "booked"), (spare, "free")):
        db.refresh(slot)
        assert slot.status == expected
    assert db.query(Appointment).count() == 1
    assert len(client.chat.completions.calls) == 1
