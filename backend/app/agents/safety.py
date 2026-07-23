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
"""

from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agents.prompts import SAFETY
from app.agents.llm import chat_json
from app.agents.state import AgentState
from app.logging_setup import get_logger
from app.models import Appointment, PatientDocument, Reminder
from app.safety.guardrails import sanitize_agent_output
from app.tools.audit_tools import write_audit

logger = get_logger(__name__)

_NO_APPOINTMENT_LINE = "No appointment has been booked yet."
_REMINDER_LOOKBACK = 10


class SafetyOutput(BaseModel):
    safe: bool
    # No Python default on violations: Groq strict mode requires every
    # property in "required" (see routing.py's RoutingOutput.department for
    # the full reasoning); the LLM must always emit the key, empty if none.
    violations: list[str]
    rewritten: str


def _compose_draft(db: Session, patient_id: int, appointment_ref: dict | None) -> str:
    lines: list[str] = []

    appt_row = None
    if appointment_ref and appointment_ref.get("id"):
        appt_row = db.get(Appointment, appointment_ref["id"])
    if appt_row is not None:
        when = appt_row.slot.start_time.isoformat() if appt_row.slot else "an unscheduled time"
        lines.append(
            f"Your appointment with {appt_row.doctor.name} "
            f"({appt_row.doctor.department.name}) is {appt_row.status} for {when}."
        )
    else:
        lines.append(_NO_APPOINTMENT_LINE)

    docs = db.query(PatientDocument).filter_by(patient_id=patient_id).all()
    if docs:
        types = ", ".join(sorted({d.document_type for d in docs}))
        lines.append(f"Documents on file: {types}.")

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
        lines.append(f"Reminders scheduled: {summary}.")

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
        draft = _compose_draft(db, patient_id, state.get("appointment"))

        candidate = draft
        violations: list[str] = []
        llm_ok = True
        try:
            llm_result = chat_json(SAFETY, draft, SafetyOutput)
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
