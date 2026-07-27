"""Patient self-service: the department/slot catalog, and the patient's own
appointments and reminders. Every mutation goes through the same tools the
agents use, with its own audit event carrying the real actor_id.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth.dependencies import ensure_owner_or_staff, get_current_user
from app.db.session import get_db
from app.exceptions import NotFoundError
from app.models import Appointment, PatientProfile, Reminder, User
from app.schemas.appointment import (
    AppointmentOut,
    DepartmentOut,
    ReminderOut,
    RescheduleRequest,
    SlotOut,
)
from app.schemas.profile import ProfileOut, ProfileUpdateRequest
from app.tools.appointment_tools import (
    cancel_appointment,
    get_available_slots,
    list_patient_appointments,
    reschedule_appointment,
)
from app.tools.audit_tools import write_audit
from app.tools.department_tools import list_departments

router = APIRouter(tags=["patient"])

_DEFAULT_SLOT_WINDOW_DAYS = 14


def _own_profile(current_user: User, db: Session) -> PatientProfile:
    profile = db.query(PatientProfile).filter_by(user_id=current_user.id).first()
    if profile is None:
        raise NotFoundError(f"No patient profile for user {current_user.id}")
    return profile


def _profile_out(current_user: User, profile: PatientProfile) -> ProfileOut:
    return ProfileOut(
        name=current_user.full_name,
        email=current_user.email,
        date_of_birth=profile.date_of_birth,
        phone=profile.phone,
        preferred_language=profile.preferred_language,
        emergency_contact=profile.emergency_contact,
    )


@router.get("/profile", response_model=ProfileOut)
def get_profile(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ProfileOut:
    return _profile_out(current_user, _own_profile(current_user, db))


@router.patch("/profile", response_model=ProfileOut)
def update_profile(
    payload: ProfileUpdateRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ProfileOut:
    """Update the caller's own profile. Only provided fields change, and the
    audit row records which fields changed, never their values (phone and
    emergency contact are PII; the audit trail stores categories, not data)."""
    profile = _own_profile(current_user, db)
    changes = payload.model_dump(exclude_unset=True)
    for field_name, value in changes.items():
        setattr(profile, field_name, value)
    if changes:
        write_audit(
            db,
            current_user.id,
            "patient.profile_updated",
            "patient_profile",
            profile.id,
            {"updated_fields": sorted(changes.keys())},
        )
    db.commit()
    db.refresh(profile)
    return _profile_out(current_user, profile)


@router.get("/appointments", response_model=list[AppointmentOut])
def list_appointments(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[AppointmentOut]:
    return [AppointmentOut(**row) for row in list_patient_appointments(db, current_user.id)]


@router.post("/appointments/{appointment_id}/reschedule", response_model=AppointmentOut)
def reschedule(
    appointment_id: int,
    payload: RescheduleRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> AppointmentOut:
    appt = db.get(Appointment, appointment_id)
    if appt is None:
        raise NotFoundError(f"Appointment {appointment_id} not found")
    ensure_owner_or_staff(current_user, appt.patient_id)

    result = reschedule_appointment(db, appointment_id, payload.new_slot_id)
    write_audit(
        db,
        current_user.id,
        "appointment.reschedule_requested",
        "appointment",
        appointment_id,
        {"new_slot_id": payload.new_slot_id},
    )
    db.commit()
    return AppointmentOut(**result)


@router.post("/appointments/{appointment_id}/cancel", response_model=AppointmentOut)
def cancel(
    appointment_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> AppointmentOut:
    appt = db.get(Appointment, appointment_id)
    if appt is None:
        raise NotFoundError(f"Appointment {appointment_id} not found")
    ensure_owner_or_staff(current_user, appt.patient_id)

    result = cancel_appointment(db, appointment_id)
    write_audit(
        db, current_user.id, "appointment.cancel_requested", "appointment", appointment_id, {}
    )
    db.commit()
    return AppointmentOut(
        id=result["id"],
        doctor=appt.doctor.name,
        department=appt.doctor.department.name,
        start_time=appt.slot.start_time.isoformat() if appt.slot else None,
        status=result["status"],
        reason=appt.reason,
    )


@router.get("/departments", response_model=list[DepartmentOut])
def departments(
    _current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[DepartmentOut]:
    return [DepartmentOut(**row) for row in list_departments(db)]


@router.get("/departments/{department_id}/slots", response_model=list[SlotOut])
def department_slots(
    department_id: int,
    _current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 20,
) -> list[SlotOut]:
    start = date_from or date.today()
    end = date_to or (start + timedelta(days=_DEFAULT_SLOT_WINDOW_DAYS))
    return [SlotOut(**row) for row in get_available_slots(db, department_id, start, end, limit)]


@router.get("/reminders", response_model=list[ReminderOut])
def reminders(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[ReminderOut]:
    rows = (
        db.query(Reminder)
        .filter_by(patient_id=current_user.id)
        .order_by(Reminder.scheduled_at)
        .all()
    )
    return [
        ReminderOut(
            id=r.id,
            patient_id=r.patient_id,
            appointment_id=r.appointment_id,
            reminder_type=r.reminder_type,
            scheduled_at=r.scheduled_at,
            sent=r.sent,
        )
        for r in rows
    ]
