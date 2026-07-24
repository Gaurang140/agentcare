"""TDD for the booking tools: atomic slot claim, reschedule, cancel, plus
the department/patient lookups booking depends on.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.exceptions import ConflictError
from app.models import Appointment, AppointmentSlot, Department, Doctor
from app.tools.appointment_tools import (
    book_appointment,
    cancel_appointment,
    generate_slots_for_doctor,
    get_available_slots,
    list_patient_appointments,
    reschedule_appointment,
)
from app.tools.department_tools import find_department, list_departments
from app.tools.patient_tools import get_patient_summary


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


def test_one_run_cannot_hold_two_confirmed_bookings(db, seeded):
    slot = _free_slot(db)
    book_appointment(db, patient_id=1, slot_id=slot.id, reason="checkup", workflow_run_id=7)

    other = (
        db.query(AppointmentSlot)
        .filter(AppointmentSlot.status == "free", AppointmentSlot.id != slot.id)
        .first()
    )
    with pytest.raises(ConflictError):
        book_appointment(db, patient_id=1, slot_id=other.id, reason="again", workflow_run_id=7)

    db.refresh(other)
    assert other.status == "free"


def test_rescheduling_into_a_second_confirmed_booking_for_one_run_conflicts(db, seeded):
    """Cancelled rows fall outside the one-booking-per-run index, so a run
    that cancelled and rebooked can reach the index through the back door:
    rescheduling the cancelled appointment sets it confirmed again, which
    would give the run two live bookings. That has to come back as a
    conflict, not as a raw IntegrityError out of the flush.
    """
    first_slot = _free_slot(db)
    first = book_appointment(
        db, patient_id=1, slot_id=first_slot.id, reason="checkup", workflow_run_id=7
    )
    cancel_appointment(db, appointment_id=first["id"])

    free_slots = (
        db.query(AppointmentSlot)
        .filter(AppointmentSlot.status == "free", AppointmentSlot.id != first_slot.id)
        .order_by(AppointmentSlot.id)
        .limit(2)
        .all()
    )
    assert len(free_slots) == 2
    book_appointment(
        db, patient_id=1, slot_id=free_slots[0].id, reason="rebooked", workflow_run_id=7
    )

    with pytest.raises(ConflictError):
        reschedule_appointment(db, appointment_id=first["id"], new_slot_id=free_slots[1].id)

    db.refresh(free_slots[1])
    assert free_slots[1].status == "free"


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


def test_get_patient_summary_counts_reflect_activity(db, seeded):
    slot = _free_slot(db)
    book_appointment(db, patient_id=1, slot_id=slot.id, reason="checkup")

    summary = get_patient_summary(db, patient_id=1)

    assert summary["id"] == 1
    assert summary["appointments_count"] == 1
    assert summary["upcoming_appointments"] == 1
