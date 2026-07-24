"""Deterministic patient-facing templates in the patient's preferred language.

The LLM-composed finalize path handles language inside the safety agent's
prompt; these templates cover the paths that must work with no model at all
(escalations). Escalation *reasons* stay English: they are staff-facing and
live on the Escalation row, not in the patient response.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import PatientProfile

_STAFF_REVIEW = {
    "en": "a staff member will review your request",
    "de": "Ihre Anfrage wurde an das Praxisteam weitergeleitet. Ein Mitarbeiter wird sie prüfen.",
}


def staff_review_response(db: Session, patient_id: int | None) -> str:
    """The no-LLM escalation message, in the patient's preferred language.

    Falls back to English when the patient is unknown, has no profile, or
    prefers a language without a template.
    """
    if patient_id is not None:
        profile = db.query(PatientProfile).filter_by(user_id=patient_id).first()
        if profile is not None and profile.preferred_language in _STAFF_REVIEW:
            return _STAFF_REVIEW[profile.preferred_language]
    return _STAFF_REVIEW["en"]
