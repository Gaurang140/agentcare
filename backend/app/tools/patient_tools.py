"""Patient-facing summary composed from persisted rows (profile + counts)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.exceptions import NotFoundError
from app.models import Appointment, AppointmentSlot, Escalation, PatientDocument, User, WorkflowRun


def _naive_utcnow() -> datetime:
    """Timezone-aware now(), stripped back to naive - matches the naive
    (but UTC-convention) DateTime columns used throughout the schema.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_patient_summary(db: Session, patient_id: int) -> dict:
    """Profile fields plus counts of appointments, documents and open escalations."""
    user = db.get(User, patient_id)
    if user is None:
        raise NotFoundError(f"Patient {patient_id} not found")

    profile = user.patient_profile

    appointments_count = db.query(Appointment).filter_by(patient_id=patient_id).count()
    upcoming_appointments = (
        db.query(Appointment)
        .join(AppointmentSlot, Appointment.slot_id == AppointmentSlot.id)
        .filter(
            Appointment.patient_id == patient_id,
            Appointment.status.in_(["pending", "confirmed"]),
            AppointmentSlot.start_time >= _naive_utcnow(),
        )
        .count()
    )
    documents_count = db.query(PatientDocument).filter_by(patient_id=patient_id).count()
    open_escalations = (
        db.query(Escalation)
        .join(WorkflowRun, Escalation.workflow_run_id == WorkflowRun.id)
        .filter(WorkflowRun.patient_id == patient_id, Escalation.status == "open")
        .count()
    )

    return {
        "id": user.id,
        "full_name": user.full_name,
        "email": user.email,
        "date_of_birth": profile.date_of_birth.isoformat()
        if profile and profile.date_of_birth
        else None,
        "phone": profile.phone if profile else None,
        "preferred_language": profile.preferred_language if profile else "en",
        "appointments_count": appointments_count,
        "upcoming_appointments": upcoming_appointments,
        "documents_count": documents_count,
        "open_escalations": open_escalations,
    }
