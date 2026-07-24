"""Safety agent: composes the patient-facing confirmation from freshly
re-queried DB rows (never the in-memory state dict, which may be stale),
then runs it past both the LLM safety reviewer and the deterministic
sanitizer. The deterministic sanitizer always has the final word: an LLM
that claims "safe: true" over a poisoned sentence does not get to publish
it, and an LLM outage never blocks finalize (MOSAIC fallback pattern) - the
deterministic sanitizer alone is enough to answer safely.

Owns no domain tools - it only reviews. Its DB reads go straight through the
session, mirroring how patient_tools.get_patient_summary composes a
response from persisted rows without a dedicated tool wrapper.

Language preference (PatientProfile.preferred_language) is read fresh here
too: the LLM path gets a "Respond in <language>." instruction appended to
its user content, and the deterministic draft itself is composed in that
same language, so the MOSAIC fallback path never silently reverts to
English for a German-preferring patient.
"""

from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agents.prompts import SAFETY
from app.agents.llm import chat_json
from app.agents.memory import build_system_prompt
from app.agents.state import AgentState
from app.logging_setup import get_logger
from app.models import Appointment, PatientDocument, PatientProfile, Reminder
from app.safety.guardrails import sanitize_agent_output
from app.tools.audit_tools import write_audit

logger = get_logger(__name__)

_REMINDER_LOOKBACK = 10

# Only these two are supported; anything else (missing profile, an
# unrecognized code) falls back to English.
_DEFAULT_LANGUAGE = "en"
_LANGUAGE_INSTRUCTIONS = {
    "de": "Respond in German (de).",
    "en": "Respond in English (en).",
}
_NO_APPOINTMENT_LINE = {
    "en": "No appointment has been booked yet.",
    "de": "Es ist noch kein Termin gebucht.",
}
_APPOINTMENT_STATUS_DE = {
    "pending": "ausstehend",
    "confirmed": "bestätigt",
    "cancelled": "storniert",
    "completed": "abgeschlossen",
}


class SafetyOutput(BaseModel):
    safe: bool
    # No Python default on violations: Groq strict mode requires every
    # property in "required" (see routing.py's RoutingOutput.department for
    # the full reasoning); the LLM must always emit the key, empty if none.
    violations: list[str]
    rewritten: str


def _preferred_language(db: Session, patient_id: int) -> str:
    """"de" or "en", matching PatientProfile.preferred_language; "en" when
    the patient has no profile row (mirrors
    patient_tools.get_patient_summary) or the stored value isn't one of the
    two supported languages."""
    profile = db.query(PatientProfile).filter_by(user_id=patient_id).first()
    language = profile.preferred_language if profile else _DEFAULT_LANGUAGE
    return language if language in _LANGUAGE_INSTRUCTIONS else _DEFAULT_LANGUAGE


def _compose_draft(db: Session, patient_id: int, appointment_ref: dict | None, language: str) -> str:
    """The deterministic draft, in `language` ("en" or "de") - this is what
    ships as final_response outright whenever the LLM review step fails
    (the MOSAIC fallback path), so it must never silently stay English for
    a German-preferring patient."""
    lines: list[str] = []

    appt_row = None
    if appointment_ref and appointment_ref.get("id"):
        appt_row = db.get(Appointment, appointment_ref["id"])
    if appt_row is not None:
        if appt_row.slot:
            when = appt_row.slot.start_time.isoformat()
        else:
            when = "einen noch nicht festgelegten Zeitpunkt" if language == "de" else "an unscheduled time"
        if language == "de":
            status = _APPOINTMENT_STATUS_DE.get(appt_row.status, appt_row.status)
            lines.append(
                f"Ihr Termin bei {appt_row.doctor.name} "
                f"({appt_row.doctor.department.name}) ist {status} für {when}."
            )
        else:
            lines.append(
                f"Your appointment with {appt_row.doctor.name} "
                f"({appt_row.doctor.department.name}) is {appt_row.status} for {when}."
            )
    else:
        lines.append(_NO_APPOINTMENT_LINE[language])

    docs = db.query(PatientDocument).filter_by(patient_id=patient_id).all()
    if docs:
        types = ", ".join(sorted({d.document_type for d in docs}))
        line = f"Vorliegende Dokumente: {types}." if language == "de" else f"Documents on file: {types}."
        lines.append(line)

    reminders = (
        db.query(Reminder)
        .filter_by(patient_id=patient_id)
        .order_by(Reminder.id.desc())
        .limit(_REMINDER_LOOKBACK)
        .all()
    )
    if reminders:
        summary = ", ".join(
            f"{r.reminder_type} on {r.scheduled_at.date().isoformat()}" for r in reminders
        )
        line = (
            f"Geplante Erinnerungen: {summary}."
            if language == "de"
            else f"Reminders scheduled: {summary}."
        )
        lines.append(line)

    return " ".join(lines)


def _exit_audit(db: Session, workflow_id: int | None, summary: dict) -> None:
    write_audit(db, None, "agent.safety.completed", "workflow_run", workflow_id, summary)
    db.commit()


def run(state: AgentState, db: Session) -> dict:
    """Compose, review, sanitize, and set final_response - never raising on
    an LLM failure, since a safe deterministic answer must always ship."""
    workflow_id = state.get("workflow_id")
    try:
        patient_id = state["patient_id"]
        language = _preferred_language(db, patient_id)
        draft = _compose_draft(db, patient_id, state.get("appointment"), language)

        candidate = draft
        violations: list[str] = []
        llm_ok = True
        try:
            system = build_system_prompt(db, "safety", SAFETY)
            user_content = f"{draft}\n\n{_LANGUAGE_INSTRUCTIONS[language]}"
            llm_result = chat_json(system, user_content, SafetyOutput)
            candidate = llm_result.rewritten or draft
            violations = list(llm_result.violations)
        except Exception as exc:  # noqa: BLE001 - MOSAIC fallback: never block finalize on an LLM error
            llm_ok = False
            logger.warning("safety_llm_failed_using_deterministic_only", error=str(exc))

        final_text, flagged = sanitize_agent_output(candidate)
        safety_flags = list(violations)
        if flagged:
            safety_flags.append("deterministic_sanitizer_rewrote_output")

        update = {
            "final_response": final_text,
            "safety_flags": safety_flags,
            "completed_steps": ["safety"],
        }
        _exit_audit(
            db, workflow_id, {"llm_ok": llm_ok, "sanitizer_flagged": flagged, "violations": violations}
        )
        return update
    except Exception as exc:  # noqa: BLE001 - node boundary must never crash the graph
        logger.error("safety_agent_failed", workflow_id=workflow_id, error=str(exc))
        db.rollback()
        _exit_audit(db, workflow_id, {"error": str(exc)})
        return {"error": f"safety agent failed: {exc}", "completed_steps": ["safety"]}
