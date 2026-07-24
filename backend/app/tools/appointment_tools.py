"""Appointment scheduling tools: availability, atomic booking, reschedule, cancel.

Booking and rescheduling claim a slot with a single conditional UPDATE
(`WHERE status = 'free'`) so two concurrent requests for the same slot can
never both succeed - the loser's UPDATE affects zero rows and raises
ConflictError, never a lost-update race.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.exceptions import ConflictError, NotFoundError
from app.models import Appointment, AppointmentSlot, Doctor
from app.tools.audit_tools import write_audit

_SLOT_START_HOUR = 9
_SLOT_END_HOUR = 17
_SLOT_MINUTES = 30


def get_available_slots(
    db: Session,
    department_id: int,
    date_from: date,
    date_to: date,
    limit: int = 10,
) -> list[dict]:
    """Free slots for any active doctor in department_id, within [date_from, date_to].

    Deactivating a doctor (staff action) leaves their calendar in place, so
    the doctor filter is what keeps those slots out of every patient-facing
    availability list.
    """
    range_start = datetime.combine(date_from, time.min)
    range_end = datetime.combine(date_to, time.max)

    slots = (
        db.query(AppointmentSlot)
        .join(Doctor, AppointmentSlot.doctor_id == Doctor.id)
        .filter(
            Doctor.department_id == department_id,
            Doctor.active.is_(True),
            AppointmentSlot.status == "free",
            AppointmentSlot.start_time >= range_start,
            AppointmentSlot.start_time <= range_end,
        )
        .order_by(AppointmentSlot.start_time)
        .limit(limit)
        .all()
    )
    return [
        {
            "slot_id": slot.id,
            "doctor_id": slot.doctor_id,
            "doctor": slot.doctor.name,
            "start_time": slot.start_time.isoformat(),
            "end_time": slot.end_time.isoformat(),
        }
        for slot in slots
    ]


def book_appointment(
    db: Session,
    patient_id: int,
    slot_id: int,
    reason: str,
    workflow_run_id: int | None = None,
) -> dict:
    """Atomically claim a free slot and create the confirmed appointment against it.

    Passing workflow_run_id ties the booking to the run that produced it.
    A partial unique index lets a run hold only one confirmed appointment,
    so a replayed or retried run cannot book the patient twice.
    """
    claimed = db.execute(
        update(AppointmentSlot)
        .where(AppointmentSlot.id == slot_id, AppointmentSlot.status == "free")
        .values(status="booked")
    )
    if claimed.rowcount == 0:
        raise ConflictError("Slot is no longer available")

    slot = db.get(AppointmentSlot, slot_id)
    appt = Appointment(
        patient_id=patient_id,
        doctor_id=slot.doctor_id,
        slot_id=slot_id,
        status="confirmed",
        reason=reason,
        workflow_run_id=workflow_run_id,
    )
    db.add(appt)
    try:
        db.flush()
    except IntegrityError as exc:
        # The one-confirmed-booking-per-run index fired. Roll back so the
        # slot claim above is released rather than left held by a booking
        # that never happened.
        db.rollback()
        raise ConflictError("This request already has a confirmed appointment") from exc
    write_audit(
        db,
        None,
        "appointment.booked",
        "appointment",
        appt.id,
        {"slot_id": slot_id, "patient_id": patient_id},
    )
    db.commit()
    return {
        "id": appt.id,
        "doctor": slot.doctor.name,
        "department": slot.doctor.department.name,
        "start_time": slot.start_time.isoformat(),
        "status": "confirmed",
    }


def reschedule_appointment(db: Session, appointment_id: int, new_slot_id: int) -> dict:
    """Claim new_slot_id, then free the old slot - both in one transaction.

    The new slot is claimed first: if that conditional UPDATE affects zero
    rows the function raises before touching the old slot, so a failed
    reschedule never leaves the patient without their original booking.
    """
    appt = db.get(Appointment, appointment_id)
    if appt is None:
        raise NotFoundError(f"Appointment {appointment_id} not found")

    claimed = db.execute(
        update(AppointmentSlot)
        .where(AppointmentSlot.id == new_slot_id, AppointmentSlot.status == "free")
        .values(status="booked")
    )
    if claimed.rowcount == 0:
        raise ConflictError("Slot is no longer available")

    old_slot_id = appt.slot_id
    db.execute(
        update(AppointmentSlot).where(AppointmentSlot.id == old_slot_id).values(status="free")
    )

    new_slot = db.get(AppointmentSlot, new_slot_id)
    appt.slot_id = new_slot_id
    appt.doctor_id = new_slot.doctor_id
    appt.status = "confirmed"
    db.flush()

    write_audit(
        db,
        None,
        "appointment.rescheduled",
        "appointment",
        appt.id,
        {"old_slot_id": old_slot_id, "new_slot_id": new_slot_id},
    )
    db.commit()
    return {
        "id": appt.id,
        "doctor": new_slot.doctor.name,
        "department": new_slot.doctor.department.name,
        "start_time": new_slot.start_time.isoformat(),
        "status": "confirmed",
    }


def cancel_appointment(db: Session, appointment_id: int) -> dict:
    """Free the slot and mark the appointment cancelled."""
    appt = db.get(Appointment, appointment_id)
    if appt is None:
        raise NotFoundError(f"Appointment {appointment_id} not found")

    db.execute(
        update(AppointmentSlot).where(AppointmentSlot.id == appt.slot_id).values(status="free")
    )
    appt.status = "cancelled"
    db.flush()

    write_audit(
        db,
        None,
        "appointment.cancelled",
        "appointment",
        appt.id,
        {"slot_id": appt.slot_id},
    )
    db.commit()
    return {"id": appt.id, "status": "cancelled"}


def generate_slots_for_doctor(
    db: Session, doctor_id: int, date_from: date, date_to: date
) -> list[dict]:
    """Weekday 09:00-17:00 30-minute slots for one doctor over [date_from,
    date_to], skipping any (doctor_id, start_time) pair that already exists
    (the unique constraint would otherwise raise) rather than duplicating
    it - so calling this twice over an overlapping range is safe.
    """
    doctor = db.get(Doctor, doctor_id)
    if doctor is None:
        raise NotFoundError(f"Doctor {doctor_id} not found")

    existing_starts = {
        start
        for (start,) in db.query(AppointmentSlot.start_time)
        .filter(AppointmentSlot.doctor_id == doctor_id)
        .all()
    }

    created: list[AppointmentSlot] = []
    day = date_from
    while day <= date_to:
        if day.weekday() < 5:  # Monday=0 ... Sunday=6
            day_start = datetime.combine(day, time(hour=_SLOT_START_HOUR))
            day_end = datetime.combine(day, time(hour=_SLOT_END_HOUR))
            step = timedelta(minutes=_SLOT_MINUTES)

            cursor = day_start
            while cursor + step <= day_end:
                if cursor not in existing_starts:
                    slot = AppointmentSlot(
                        doctor_id=doctor_id,
                        start_time=cursor,
                        end_time=cursor + step,
                        status="free",
                    )
                    db.add(slot)
                    created.append(slot)
                    existing_starts.add(cursor)
                cursor += step
        day += timedelta(days=1)

    db.flush()
    write_audit(
        db,
        None,
        "slots.generated",
        "doctor",
        doctor_id,
        {"count": len(created), "date_from": date_from.isoformat(), "date_to": date_to.isoformat()},
    )
    db.commit()

    return [
        {
            "slot_id": slot.id,
            "doctor_id": slot.doctor_id,
            "doctor": doctor.name,
            "start_time": slot.start_time.isoformat(),
            "end_time": slot.end_time.isoformat(),
        }
        for slot in created
    ]


def list_patient_appointments(db: Session, patient_id: int) -> list[dict]:
    """All of a patient's appointments, most recently created first."""
    appts = (
        db.query(Appointment)
        .filter_by(patient_id=patient_id)
        .order_by(Appointment.created_at.desc())
        .all()
    )
    return [
        {
            "id": appt.id,
            "doctor": appt.doctor.name,
            "department": appt.doctor.department.name,
            "start_time": appt.slot.start_time.isoformat() if appt.slot else None,
            "status": appt.status,
            "reason": appt.reason,
        }
        for appt in appts
    ]
