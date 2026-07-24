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

# What the patient reads once a staff member has decided a case that cannot
# go back to the agents: an approved agent_failure (a human is handling it by
# hand now) or any rejection. An approved uncertainty case produces no
# template at all - the run carries on and answers for itself.
_STAFF_APPROVED = {
    "en": "A staff member approved your request and it is being processed.",
    "de": "Ein Mitarbeiter hat Ihre Anfrage freigegeben. Sie wird bearbeitet.",
}

_STAFF_REJECTED = {
    "en": (
        "A staff member reviewed your request. It cannot be processed as "
        "submitted. The practice team will contact you."
    ),
    "de": (
        "Ein Mitarbeiter hat Ihre Anfrage geprüft. Sie kann so nicht "
        "bearbeitet werden. Das Praxisteam meldet sich bei Ihnen."
    ),
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


def staff_decision_response(db: Session, patient_id: int | None, approved: bool) -> str:
    """The closing message for a case a human has decided. Takes no note
    parameter on purpose: the reviewer's own words are staff-facing and stay
    on Escalation.resolution_note."""
    return _localized(db, patient_id, _STAFF_APPROVED if approved else _STAFF_REJECTED)


def emergency_response(db: Session, patient_id: int | None) -> str:
    """The deterministic emergency-screen message (call 112, staff notified)."""
    return _localized(db, patient_id, _EMERGENCY)


def medical_refusal_response(db: Session, patient_id: int | None) -> str:
    """The deterministic medical-advice refusal."""
    return _localized(db, patient_id, _MEDICAL_REFUSAL)
