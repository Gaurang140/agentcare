"""Appointment scheduling tools: availability, atomic booking, reschedule, cancel.

Booking and rescheduling claim a slot with a single conditional UPDATE
(`WHERE status = 'free'`) so two concurrent requests for the same slot can
never both succeed - the loser's UPDATE affects zero rows and raises
ConflictError, never a lost-update race.

Cancelling conditionally changes an active appointment's status. Rescheduling
uses a compare-and-swap update that additionally requires the slot id the
caller originally observed. Without those guards, a repeated cancel or stale
reschedule could free a slot another patient now holds. The guards live in the
tools rather than in the agent, so the patient routes in api/routes_patient.py
are covered by the same rule.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.exceptions import ConflictError, NotFoundError
from app.models import Appointment, AppointmentSlot, Doctor
from app.tools.audit_tools import write_audit

_SLOT_START_HOUR = 9
_SLOT_END_HOUR = 17
_SLOT_MINUTES = 30

# How each backend names the known booking conflicts in the IntegrityError it
# raises. Postgres quotes constraint/index names; sqlite names the workflow-run
# indexed column but uses the explicit trigger messages for range conflicts.
# Anything else the flush can raise - a foreign key that does not resolve,
# some future constraint - is a bug and stays an IntegrityError rather than
# becoming a retryable booking conflict.
_RUN_INDEX_MARKERS = ("uq_appointments_workflow_run", "appointments.workflow_run_id")
_PATIENT_OVERLAP_MARKER = "ex_appointments_patient_schedule"
_DOCTOR_OVERLAP_MARKER = "ex_appointment_slots_doctor_schedule"
_NAMED_CONFLICT_MARKERS = (
    *_RUN_INDEX_MARKERS,
    _PATIENT_OVERLAP_MARKER,
    _DOCTOR_OVERLAP_MARKER,
)

# The statuses that still hold a slot. Anything else - today only "cancelled" -
# has already given its slot back, so it must not be moved or freed again.
_ACTIVE_STATUSES = ("pending", "confirmed")


def _is_named_booking_conflict(exc: IntegrityError) -> bool:
    message = str(exc.orig)
    return any(marker in message for marker in _NAMED_CONFLICT_MARKERS)


def _active_patient_appointment_query(
    db: Session, patient_id: int, exclude_appointment_id: int | None = None
):
    query = db.query(Appointment).filter(
        Appointment.patient_id == patient_id,
        Appointment.status.in_(_ACTIVE_STATUSES),
    )
    if exclude_appointment_id is not None:
        query = query.filter(Appointment.id != exclude_appointment_id)
    return query


def _patient_has_active_overlap(
    db: Session,
    patient_id: int,
    target_start: datetime,
    target_end: datetime,
    *,
    exclude_appointment_id: int | None = None,
) -> bool:
    return (
        _active_patient_appointment_query(db, patient_id, exclude_appointment_id)
        .filter(
            Appointment.scheduled_start < target_end,
            Appointment.scheduled_end > target_start,
        )
        .first()
        is not None
    )


def _claim_active_appointment(db: Session, appointment_id: int, new_status: str) -> int:
    """Move an appointment out of an active status with one conditional UPDATE.

    The same shape as the slot claim in book_appointment, and for the same
    reason: reading the status and then writing it leaves a window where a
    second request sees the row still active and acts on it too. A cancel that
    arrives twice would free the slot twice, and the slot is rebookable in
    between, so the second free hands another patient's confirmed slot back to
    the pool. Zero rows affected means the row was already cancelled (or gone),
    which is a conflict rather than a silent no-op.

    Returns the slot id the appointment holds, re-read after the claim won:
    a concurrent reschedule may have moved the row since the caller loaded it,
    and freeing the slot it used to point at would strand the new one.
    """
    claimed = db.execute(
        update(Appointment)
        .where(Appointment.id == appointment_id, Appointment.status.in_(_ACTIVE_STATUSES))
        .values(status=new_status)
    )
    if claimed.rowcount == 0:
        raise ConflictError(f"Appointment {appointment_id} is not active")

    slot_id = db.execute(
        select(Appointment.slot_id).where(Appointment.id == appointment_id)
    ).scalar_one()
    return slot_id


def appointment_summary(appt: Appointment, slot: AppointmentSlot | None = None) -> dict:
    """The dict shape every appointment path hands back to the agents.

    `slot` is given explicitly by callers that have just moved the booking:
    the appointment's own `slot` relationship can still hold the row it was
    loaded with, while the caller already has the one it just claimed.
    """
    slot = slot if slot is not None else appt.slot
    return {
        "id": appt.id,
        "doctor": slot.doctor.name,
        "department": slot.doctor.department.name,
        "start_time": slot.start_time.isoformat(),
        "status": appt.status,
    }


def cancelled_summary(appt: Appointment) -> dict:
    """The dict shape the cancel path hands back. Deliberately smaller than
    `appointment_summary`: a cancelled appointment's slot is free again, so
    naming a doctor and a time would read as a booking that still stands.
    """
    return {"id": appt.id, "status": appt.status}


def get_available_slots(
    db: Session,
    department_id: int,
    date_from: date,
    date_to: date,
    limit: int = 10,
    patient_id: int | None = None,
    exclude_appointment_id: int | None = None,
) -> list[dict]:
    """Free slots for any active doctor in department_id, within [date_from, date_to].

    Deactivating a doctor (staff action) leaves their calendar in place, so
    the doctor filter is what keeps those slots out of every patient-facing
    availability list.
    """
    range_start = datetime.combine(date_from, time.min)
    range_end = datetime.combine(date_to, time.max)

    query = (
        db.query(AppointmentSlot)
        .join(Doctor, AppointmentSlot.doctor_id == Doctor.id)
        .filter(
            Doctor.department_id == department_id,
            Doctor.active.is_(True),
            AppointmentSlot.status == "free",
            AppointmentSlot.start_time >= range_start,
            AppointmentSlot.start_time <= range_end,
        )
    )
    if patient_id is not None:
        patient_overlap = (
            select(Appointment.id)
            .where(
                Appointment.patient_id == patient_id,
                Appointment.status.in_(_ACTIVE_STATUSES),
                Appointment.scheduled_start < AppointmentSlot.end_time,
                Appointment.scheduled_end > AppointmentSlot.start_time,
            )
            .correlate(AppointmentSlot)
        )
        if exclude_appointment_id is not None:
            patient_overlap = patient_overlap.where(
                Appointment.id != exclude_appointment_id
            )
        query = query.filter(~patient_overlap.exists())

    slots = query.order_by(AppointmentSlot.start_time).limit(limit).all()
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
    slot = db.get(AppointmentSlot, slot_id)
    if slot is None:
        db.rollback()
        raise ConflictError("Slot is no longer available")

    if _patient_has_active_overlap(
        db,
        patient_id,
        slot.start_time,
        slot.end_time,
    ):
        db.rollback()
        raise ConflictError("Patient already has an appointment at this time")

    try:
        claimed = db.execute(
            update(AppointmentSlot)
            .where(AppointmentSlot.id == slot_id, AppointmentSlot.status == "free")
            .values(status="booked")
        )
        if claimed.rowcount == 0:
            raise ConflictError("Slot is no longer available")

        appt = Appointment(
            patient_id=patient_id,
            doctor_id=slot.doctor_id,
            slot_id=slot_id,
            status="confirmed",
            scheduled_start=slot.start_time,
            scheduled_end=slot.end_time,
            reason=reason,
            workflow_run_id=workflow_run_id,
        )
        db.add(appt)
        db.flush()
        write_audit(
            db,
            None,
            "appointment.booked",
            "appointment",
            appt.id,
            {"slot_id": slot_id, "patient_id": patient_id},
        )
        db.commit()
    except Exception as exc:
        # Audit writes flush too, and commit may surface deferred database
        # constraints. Every failure after the slot claim must release it.
        db.rollback()
        if isinstance(exc, IntegrityError) and _is_named_booking_conflict(exc):
            raise ConflictError("Appointment conflicts with an existing booking") from exc
        raise
    return appointment_summary(appt, slot)


def reschedule_appointment(
    db: Session, appointment_id: int, new_slot_id: int, workflow_run_id: int | None = None
) -> dict:
    """Claim new_slot_id, then free the old slot - both in one transaction.

    The new slot is claimed first: if that conditional UPDATE affects zero
    rows the function raises before touching the old slot, so a failed
    reschedule never leaves the patient without their original booking.

    Only an active appointment can be moved. Rescheduling a cancelled one
    would free whatever slot it still points at - by now possibly another
    patient's - and resurrect the row as confirmed, so it raises ConflictError
    and releases the slot it had just claimed.

    Passing workflow_run_id stamps the run that moved the appointment onto the
    row, the same tie book_appointment writes, so a replayed run recognizes the
    move it already made instead of swapping slots a second time
    (agents/appointment.py). The row stays confirmed, so it takes part in the
    one-booking-per-run index: a run holds one confirmed appointment, and this
    is it. Passing None leaves whatever stamp the row already carries, which is
    what the patient's own reschedule route (api/routes_patient.py) wants -
    that call is not part of any run.
    """
    appt = db.get(Appointment, appointment_id)
    if appt is None:
        db.rollback()
        raise NotFoundError(f"Appointment {appointment_id} not found")

    expected_old_slot_id = appt.slot_id
    new_slot = db.get(AppointmentSlot, new_slot_id)
    if new_slot is None:
        db.rollback()
        raise ConflictError("Slot is no longer available")

    if _patient_has_active_overlap(
        db,
        appt.patient_id,
        new_slot.start_time,
        new_slot.end_time,
        exclude_appointment_id=appointment_id,
    ):
        db.rollback()
        raise ConflictError("Patient already has an appointment at this time")

    appointment_values = {
        "slot_id": new_slot_id,
        "doctor_id": new_slot.doctor_id,
        "scheduled_start": new_slot.start_time,
        "scheduled_end": new_slot.end_time,
        "status": "confirmed",
    }
    if workflow_run_id is not None:
        appointment_values["workflow_run_id"] = workflow_run_id

    try:
        claimed = db.execute(
            update(AppointmentSlot)
            .where(AppointmentSlot.id == new_slot_id, AppointmentSlot.status == "free")
            .values(status="booked")
        )
        if claimed.rowcount == 0:
            raise ConflictError("Slot is no longer available")

        moved = db.execute(
            update(Appointment)
            .where(
                Appointment.id == appointment_id,
                Appointment.slot_id == expected_old_slot_id,
                Appointment.status.in_(_ACTIVE_STATUSES),
            )
            .values(**appointment_values)
            .execution_options(synchronize_session=False)
        )
        if moved.rowcount == 0:
            raise ConflictError(f"Appointment {appointment_id} changed concurrently")

        db.execute(
            update(AppointmentSlot)
            .where(AppointmentSlot.id == expected_old_slot_id)
            .values(status="free")
        )
        db.refresh(appt)
        write_audit(
            db,
            None,
            "appointment.rescheduled",
            "appointment",
            appt.id,
            {"old_slot_id": expected_old_slot_id, "new_slot_id": new_slot_id},
        )
        db.commit()
    except Exception as exc:
        # Restore the new/old slot claims and the appointment CAS for any
        # failure, including the audit flush and commit itself.
        db.rollback()
        if isinstance(exc, IntegrityError) and _is_named_booking_conflict(exc):
            raise ConflictError("Appointment conflicts with an existing booking") from exc
        raise
    return appointment_summary(appt, new_slot)


def cancel_appointment(
    db: Session, appointment_id: int, workflow_run_id: int | None = None
) -> dict:
    """Free the slot and mark the appointment cancelled.

    Only an active appointment can be cancelled; cancelling one that is
    already cancelled raises ConflictError instead of freeing its old slot a
    second time.

    Passing workflow_run_id stamps the run that cancelled onto the row, which
    is how the appointment node recognizes its own completed cancel on a replay
    (agents/appointment.py). A cancelled row sits outside the
    one-booking-per-run index, so the stamp can never collide with a booking
    holding the same id.

    When a different run booked the appointment, the stamp overwrites that
    run's id. The audit trail is what keeps the booking run readable: the
    "appointment.booked" row for this appointment id is still there, alongside
    the "appointment.cancelled" row written below. Passing None leaves the
    stamp untouched, which is what the patient's own cancel route
    (api/routes_patient.py) wants - it is not part of any run.
    """
    appt = db.get(Appointment, appointment_id)
    if appt is None:
        raise NotFoundError(f"Appointment {appointment_id} not found")

    slot_id = _claim_active_appointment(db, appointment_id, "cancelled")
    db.execute(update(AppointmentSlot).where(AppointmentSlot.id == slot_id).values(status="free"))
    if workflow_run_id is not None:
        appt.workflow_run_id = workflow_run_id
    db.flush()

    write_audit(
        db,
        None,
        "appointment.cancelled",
        "appointment",
        appt.id,
        {"slot_id": slot_id},
    )
    db.commit()
    return cancelled_summary(appt)


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


def list_active_patient_appointments(
    db: Session,
    patient_id: int,
    *,
    exclude_appointment_id: int | None = None,
) -> list[dict]:
    """A patient's active appointments, optionally excluding one being moved."""
    appts = (
        _active_patient_appointment_query(db, patient_id, exclude_appointment_id)
        .order_by(Appointment.created_at.desc())
        .all()
    )
    return [
        {
            "id": appt.id,
            "doctor": appt.doctor.name,
            "department": appt.doctor.department.name,
            "start_time": appt.scheduled_start.isoformat(),
            "status": appt.status,
            "reason": appt.reason,
        }
        for appt in appts
    ]
