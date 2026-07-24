"""TDD for the booking tools: atomic slot claim, reschedule, cancel, plus
the department/patient lookups booking depends on.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.exceptions import ConflictError
from app.models import AppointmentSlot, Department
from app.tools.appointment_tools import (
    book_appointment,
    cancel_appointment,
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


def test_get_available_slots_filters_by_department_and_date_range(db, seeded):
    cardiology_id = _cardiology_id(db)
    today = date.today()

    slots = get_available_slots(
        db, department_id=cardiology_id, date_from=today, date_to=today + timedelta(days=30), limit=5
    )

    assert 0 < len(slots) <= 5
    assert all(s["start_time"] for s in slots)


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
