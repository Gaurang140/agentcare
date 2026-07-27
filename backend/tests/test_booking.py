"""TDD for the booking tools: atomic slot claim, reschedule, cancel, plus
the department/patient lookups booking depends on.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.exceptions import ConflictError
from app.models import Appointment, AppointmentSlot, Department, Doctor, WorkflowRun
from app.tools.appointment_tools import (
    book_appointment,
    cancel_appointment,
    generate_slots_for_doctor,
    get_available_slots,
    list_patient_appointments,
    reschedule_appointment,
)
from app.tools.department_tools import find_department, list_departments


def _free_slot(db) -> AppointmentSlot:
    # Furthest-out free slot: seed data always runs ~2 weeks ahead of
    # "today", so this is guaranteed to be in the future regardless of what
    # time of day the suite happens to run at (unlike the earliest slot,
    # which could already be in the past by the afternoon).
    slot = (
        db.query(AppointmentSlot)
        .filter_by(status="free")
        .order_by(AppointmentSlot.start_time.desc())
        .first()
    )
    assert slot is not None
    return slot


def _workflow_run_id(db, patient_id: int = 1) -> int:
    """A real workflow_runs row for the one-booking-per-run tests to hang
    their appointments off. Sqlite runs with foreign keys off, so a made-up
    id works here and only breaks on Postgres, where the same tests run
    against a live FK - the row is cheap, so the tests carry one. Foreign
    keys go on for the rest of the connection too, so this stays true on
    sqlite rather than only on the deployment database."""
    db.execute(text("PRAGMA foreign_keys=ON"))
    run = WorkflowRun(
        user_id=patient_id,
        patient_id=patient_id,
        thread_id="",
        request_text="x",
        status="completed",
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run.id


def _cardiology_id(db) -> int:
    dept = db.query(Department).filter_by(name="Cardiology").first()
    assert dept is not None
    return dept.id


def test_double_booking_conflicts(db, seeded):
    slot = _free_slot(db)
    book_appointment(db, patient_id=1, slot_id=slot.id, reason="checkup")
    with pytest.raises(ConflictError):
        book_appointment(db, patient_id=2, slot_id=slot.id, reason="checkup")


def test_book_appointment_returns_confirmed_booking(db, seeded):
    slot = _free_slot(db)
    result = book_appointment(db, patient_id=1, slot_id=slot.id, reason="checkup")

    assert result["status"] == "confirmed"
    assert result["start_time"] == slot.start_time.isoformat()
    assert result["doctor"]
    assert result["department"]


def test_book_appointment_missing_slot_conflicts(db, seeded):
    with pytest.raises(ConflictError):
        book_appointment(db, patient_id=1, slot_id=999_999, reason="checkup")


def test_reschedule_frees_old_slot_and_claims_new(db, seeded):
    old_slot = _free_slot(db)
    booking = book_appointment(db, patient_id=1, slot_id=old_slot.id, reason="checkup")

    new_slot = (
        db.query(AppointmentSlot)
        .filter(AppointmentSlot.status == "free", AppointmentSlot.id != old_slot.id)
        .first()
    )
    assert new_slot is not None

    result = reschedule_appointment(db, appointment_id=booking["id"], new_slot_id=new_slot.id)

    db.refresh(old_slot)
    db.refresh(new_slot)
    assert old_slot.status == "free"
    assert new_slot.status == "booked"
    assert result["status"] == "confirmed"
    assert result["start_time"] == new_slot.start_time.isoformat()


def test_reschedule_conflict_leaves_original_booking_intact(db, seeded):
    old_slot = _free_slot(db)
    booking = book_appointment(db, patient_id=1, slot_id=old_slot.id, reason="checkup")

    contested = (
        db.query(AppointmentSlot)
        .filter(AppointmentSlot.status == "free", AppointmentSlot.id != old_slot.id)
        .first()
    )
    assert contested is not None
    book_appointment(db, patient_id=2, slot_id=contested.id, reason="checkup")

    with pytest.raises(ConflictError):
        reschedule_appointment(db, appointment_id=booking["id"], new_slot_id=contested.id)

    db.refresh(old_slot)
    assert old_slot.status == "booked"


def test_cancel_frees_slot(db, seeded):
    slot = _free_slot(db)
    booking = book_appointment(db, patient_id=1, slot_id=slot.id, reason="checkup")

    result = cancel_appointment(db, appointment_id=booking["id"])

    db.refresh(slot)
    assert slot.status == "free"
    assert result == {"id": booking["id"], "status": "cancelled"}


def test_cancelled_slot_can_be_rebooked(db, seeded):
    slot = _free_slot(db)
    first = book_appointment(db, patient_id=2, slot_id=slot.id, reason="checkup")
    cancel_appointment(db, appointment_id=first["id"])

    second = book_appointment(db, patient_id=2, slot_id=slot.id, reason="checkup again")

    assert second["status"] == "confirmed"
    assert second["id"] != first["id"]
    # The slot is claimed again, and the cancellation stays on the record as
    # its own row rather than being overwritten by the new booking.
    db.refresh(slot)
    assert slot.status == "booked"
    assert db.get(Appointment, first["id"]).status == "cancelled"


def test_cancelling_an_already_cancelled_appointment_leaves_the_new_holder_alone(db, seeded):
    """A cancel request that arrives twice - a double-clicked button, a retried
    HTTP call - must not free the slot a second time. Between the two the slot
    is rebookable, so the second cancel would hand another patient's confirmed
    slot back to the pool while that patient still holds the appointment.
    """
    slot = _free_slot(db)
    first = book_appointment(db, patient_id=1, slot_id=slot.id, reason="checkup")
    cancel_appointment(db, appointment_id=first["id"])
    second = book_appointment(db, patient_id=2, slot_id=slot.id, reason="checkup")

    with pytest.raises(ConflictError):
        cancel_appointment(db, appointment_id=first["id"])

    db.refresh(slot)
    assert slot.status == "booked"
    assert db.get(Appointment, second["id"]).status == "confirmed"


def test_rescheduling_a_cancelled_appointment_does_not_free_the_new_holders_slot(db, seeded):
    """The same stale request against the reschedule path. Moving a cancelled
    appointment would free whatever slot it still points at - now another
    patient's - and resurrect the cancelled row as confirmed.
    """
    old_slot = _free_slot(db)
    first = book_appointment(db, patient_id=1, slot_id=old_slot.id, reason="checkup")
    cancel_appointment(db, appointment_id=first["id"])
    second = book_appointment(db, patient_id=2, slot_id=old_slot.id, reason="checkup")

    target = (
        db.query(AppointmentSlot)
        .filter(AppointmentSlot.status == "free", AppointmentSlot.id != old_slot.id)
        .first()
    )
    assert target is not None

    with pytest.raises(ConflictError):
        reschedule_appointment(db, appointment_id=first["id"], new_slot_id=target.id)

    db.refresh(old_slot)
    db.refresh(target)
    assert old_slot.status == "booked"
    assert target.status == "free", "the failed reschedule must release the slot it claimed"
    assert db.get(Appointment, first["id"]).status == "cancelled"
    assert db.get(Appointment, second["id"]).status == "confirmed"


def test_one_run_cannot_hold_two_confirmed_bookings(db, seeded):
    slot = _free_slot(db)
    run_id = _workflow_run_id(db)
    book_appointment(db, patient_id=1, slot_id=slot.id, reason="checkup", workflow_run_id=run_id)

    other = (
        db.query(AppointmentSlot)
        .filter(AppointmentSlot.status == "free", AppointmentSlot.id != slot.id)
        .first()
    )
    with pytest.raises(ConflictError):
        book_appointment(db, patient_id=1, slot_id=other.id, reason="again", workflow_run_id=run_id)

    db.refresh(other)
    assert other.status == "free"


def test_rescheduling_into_a_second_confirmed_booking_for_one_run_conflicts(db, seeded):
    """The one-booking-per-run index fires inside the flush, and rescheduling
    stamps the run onto the row it moves. So moving a second appointment into
    a run that already booked one has to come back as a conflict, not as a raw
    IntegrityError. (A cancelled appointment cannot reach this at all any more:
    the status guard in the tool refuses it before the flush.)
    """
    run_id = _workflow_run_id(db)
    free_slots = (
        db.query(AppointmentSlot)
        .filter(AppointmentSlot.status == "free")
        .order_by(AppointmentSlot.id)
        .limit(3)
        .all()
    )
    assert len(free_slots) == 3

    book_appointment(
        db, patient_id=1, slot_id=free_slots[0].id, reason="checkup", workflow_run_id=run_id
    )
    other = book_appointment(db, patient_id=2, slot_id=free_slots[1].id, reason="checkup")

    with pytest.raises(ConflictError):
        reschedule_appointment(
            db, appointment_id=other["id"], new_slot_id=free_slots[2].id, workflow_run_id=run_id
        )

    db.refresh(free_slots[2])
    assert free_slots[2].status == "free"


def test_reschedule_stamps_the_run_and_stays_inside_the_one_booking_per_run_index(db, seeded):
    """The appointment node's replay guard reads `workflow_run_id`, so a
    reschedule has to stamp it. A run does exactly one appointment action, so
    the rescheduled row is the only confirmed row that run holds - and the
    index still refuses a second one, on sqlite, not in application code.
    """
    run_id = _workflow_run_id(db)
    first_slot = _free_slot(db)
    booking = book_appointment(db, patient_id=1, slot_id=first_slot.id, reason="checkup")
    free_slots = (
        db.query(AppointmentSlot)
        .filter(AppointmentSlot.status == "free", AppointmentSlot.id != first_slot.id)
        .order_by(AppointmentSlot.id)
        .limit(2)
        .all()
    )

    moved = reschedule_appointment(
        db, appointment_id=booking["id"], new_slot_id=free_slots[0].id, workflow_run_id=run_id
    )

    assert moved["status"] == "confirmed"
    assert db.get(Appointment, booking["id"]).workflow_run_id == run_id

    with pytest.raises(ConflictError):
        book_appointment(
            db, patient_id=1, slot_id=free_slots[1].id, reason="second", workflow_run_id=run_id
        )


def test_cancel_stamps_the_run_and_falls_outside_the_index(db, seeded):
    """A cancelled row drops out of the partial index, so the run id a cancel
    stamps can never compete with a booking for the same id."""
    run_id = _workflow_run_id(db)
    slot = _free_slot(db)
    booking = book_appointment(db, patient_id=1, slot_id=slot.id, reason="checkup")

    cancel_appointment(db, appointment_id=booking["id"], workflow_run_id=run_id)

    row = db.get(Appointment, booking["id"])
    assert row.workflow_run_id == run_id
    assert row.status == "cancelled"
    rebooked = book_appointment(
        db, patient_id=1, slot_id=slot.id, reason="again", workflow_run_id=run_id
    )
    assert rebooked["status"] == "confirmed"


def test_cancel_without_a_run_leaves_the_booking_run_stamp_alone(db, seeded):
    """The patient route cancels outside any workflow run (routes_patient.py),
    so it must not wipe the id of the run that booked the appointment."""
    run_id = _workflow_run_id(db)
    slot = _free_slot(db)
    booking = book_appointment(
        db, patient_id=1, slot_id=slot.id, reason="checkup", workflow_run_id=run_id
    )

    cancel_appointment(db, appointment_id=booking["id"])

    assert db.get(Appointment, booking["id"]).workflow_run_id == run_id


def test_one_booking_per_run_index_is_declared_on_confirmed_rows_only(db):
    """The model layer's half of the proof: the predicate both stamps rely on,
    read off the mapped index rather than assumed. Alembic revision
    8524b9522086 writes the same predicate on a real database."""
    index = next(
        i for i in Appointment.__table__.indexes if i.name == "uq_appointments_workflow_run"
    )

    assert index.unique
    assert list(index.columns.keys()) == ["workflow_run_id"]
    for dialect in ("sqlite", "postgresql"):
        assert str(index.dialect_options[dialect]["where"]) == (
            "workflow_run_id IS NOT NULL AND status = 'confirmed'"
        )


def test_an_unrelated_integrity_error_is_not_reported_as_a_slot_conflict(db, seeded):
    """A ConflictError tells the appointment agent to pick another slot and
    try again. Only the one-booking-per-run index means that; anything else
    the flush can raise (here a foreign key that does not resolve) is a bug
    and must surface as itself rather than send the agent round a retry loop
    that cannot help.
    """
    db.execute(text("PRAGMA foreign_keys=ON"))
    slot = _free_slot(db)

    with pytest.raises(IntegrityError):
        book_appointment(
            db, patient_id=1, slot_id=slot.id, reason="checkup", workflow_run_id=999_999
        )


def test_get_available_slots_filters_by_department_and_date_range(db, seeded):
    cardiology_id = _cardiology_id(db)
    today = date.today()

    slots = get_available_slots(
        db, department_id=cardiology_id, date_from=today, date_to=today + timedelta(days=30), limit=5
    )

    assert 0 < len(slots) <= 5
    assert all(s["start_time"] for s in slots)


def test_get_available_slots_excludes_inactive_doctors(db, seeded):
    # Staff can deactivate a doctor (set_doctor_active) without deleting the
    # calendar, so a deactivated doctor keeps free slots in the table. Those
    # slots must not be offered to patients any more.
    cardiology_id = _cardiology_id(db)
    inactive = Doctor(department_id=cardiology_id, name="Dr. Inaktiv Beispiel", active=False)
    db.add(inactive)
    db.commit()

    first_seeded_slot = (
        db.query(AppointmentSlot)
        .join(Doctor, AppointmentSlot.doctor_id == Doctor.id)
        .filter(Doctor.department_id == cardiology_id, AppointmentSlot.status == "free")
        .order_by(AppointmentSlot.start_time)
        .first()
    )
    assert first_seeded_slot is not None
    day = first_seeded_slot.start_time.date()

    # One weekday of free slots for the inactive doctor, same day (and so
    # same start times) as the active doctors' seeded slots.
    generated = generate_slots_for_doctor(db, doctor_id=inactive.id, date_from=day, date_to=day)
    assert generated, "the inactive doctor must actually own free slots for this to prove anything"

    # limit comfortably above the whole department's slot count for that day.
    slots = get_available_slots(
        db, department_id=cardiology_id, date_from=day, date_to=day, limit=100
    )

    doctor_ids = {s["doctor_id"] for s in slots}
    assert inactive.id not in doctor_ids
    assert doctor_ids, "active doctors' slots must still be offered"


def test_list_patient_appointments_reflects_bookings(db, seeded):
    slot = _free_slot(db)
    booking = book_appointment(db, patient_id=1, slot_id=slot.id, reason="checkup")

    appts = list_patient_appointments(db, patient_id=1)

    assert len(appts) == 1
    assert appts[0]["id"] == booking["id"]
    assert appts[0]["status"] == "confirmed"


def test_find_department_fuzzy_match(db, seeded):
    assert find_department(db, "cardiology")["name"] == "Cardiology"
    assert find_department(db, "I need a Cardiology appointment")["name"] == "Cardiology"
    assert find_department(db, "not-a-real-department") is None


def test_list_departments_returns_seeded_departments(db, seeded):
    names = {d["name"] for d in list_departments(db)}
    assert "Cardiology" in names
    assert len(names) >= 5
