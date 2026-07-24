"""Deterministic patient-facing templates in the patient's preferred language.

The LLM-composed finalize path handles language inside the safety agent's
prompt; these templates cover the paths that must work with no model at all
(escalations). Escalation *reasons* stay English: they are staff-facing and
live on the Escalation row, not in the patient response.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import PatientProfile
from app.safety.guardrails import EMERGENCY_GUIDANCE, MEDICAL_REFUSAL

_STAFF_REVIEW = {
    "en": "a staff member will review your request",
    "de": "Ihre Anfrage wurde an das Praxisteam weitergeleitet. Ein Mitarbeiter wird sie prüfen.",
}

_EMERGENCY = {
    "en": EMERGENCY_GUIDANCE,
    "de": (
        "Das klingt dringend. Bitte rufen Sie jetzt die 112 an. "
        "Das Praxisteam wurde informiert."
    ),
}

_MEDICAL_REFUSAL = {
    "en": MEDICAL_REFUSAL,
    "de": (
        "Ich darf keine medizinischen Ratschläge geben. Bitte sprechen Sie "
        "mit einem Arzt oder einer Ärztin. Gerne helfe ich Ihnen, einen "
        "Termin zu buchen."
    ),
}


def _localized(db: Session, patient_id: int | None, templates: dict[str, str]) -> str:
    """Pick the template for the patient's preferred language; English when
    the patient is unknown, has no profile, or prefers a language without a
    template."""
    if patient_id is not None:
        profile = db.query(PatientProfile).filter_by(user_id=patient_id).first()
        if profile is not None and profile.preferred_language in templates:
            return templates[profile.preferred_language]
    return templates["en"]


def staff_review_response(db: Session, patient_id: int | None) -> str:
    """The no-LLM escalation message."""
    return _localized(db, patient_id, _STAFF_REVIEW)


def emergency_response(db: Session, patient_id: int | None) -> str:
    """The deterministic emergency-screen message (call 112, staff notified)."""
    return _localized(db, patient_id, _EMERGENCY)


def medical_refusal_response(db: Session, patient_id: int | None) -> str:
    """The deterministic medical-advice refusal."""
    return _localized(db, patient_id, _MEDICAL_REFUSAL)
