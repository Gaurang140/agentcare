"""Follow-up agent: turns an LLM reminder plan into real Reminder rows -
one appointment reminder, one per missing document type, and the post-visit
follow-up task - once there is a confirmed appointment to hang them off.

Owns app.tools.followup_tools.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agents.prompts import FOLLOWUP
from app.agents.llm import chat_json
from app.agents.memory import build_system_prompt
from app.agents.state import AgentState
from app.logging_setup import get_logger
from app.models import Reminder
from app.tools.audit_tools import write_audit
from app.tools.followup_tools import create_followup_task, create_reminder, reminder_summary

logger = get_logger(__name__)

# Bounds on what the LLM's plan is allowed to turn into real rows. An
# unbounded reminder list floods the patient's schedule, a negative
# days_before_appointment lands the reminder after the appointment it is
# reminding about, and a very large one lands it in the past, where
# send_due_reminders marks it sent on its next tick. Enforced in code, not
# as pydantic Field(ge=..., le=...): Groq's strict json_schema mode rejects
# the "minimum"/"maximum" keywords those emit (AGENTS.md rule 3).
_MAX_REMINDERS = 5
_MAX_DAYS = 90


def _clamp_days(value: int) -> int:
    return min(max(value, 0), _MAX_DAYS)


class ReminderSpec(BaseModel):
    type: str
    days_before_appointment: int


class FollowupOutput(BaseModel):
    reminders: list[ReminderSpec]
    # No Python default: Groq strict mode requires every property in
    # "required" (see routing.py's RoutingOutput.department for the full
    # reasoning) - the LLM must always state the value, per the prompt's
    # "post-visit follow-up task 14 days after" instruction.
    followup_days_after: int


def _plan_prompt(appointment: dict, missing: list[str]) -> str:
    return (
        f"Confirmed appointment start: {appointment.get('start_time')}\n"
        f"Missing document types: {missing or '(none)'}"
    )


def _exit_audit(db: Session, workflow_id: int | None, summary: dict) -> None:
    write_audit(db, None, "agent.followup.completed", "workflow_run", workflow_id, summary)
    db.commit()


def _reminders_already_scheduled(db: Session, appointment_id: int) -> list[dict]:
    """The batch this node has already written for the appointment.

    Each reminder is committed as it is created, before LangGraph writes the
    checkpoint saying this node ran, so a process that dies in that window
    resumes into a node whose rows exist but whose run was never recorded.
    The whole appointment is the key. A per-reminder key cannot work: the
    resumed node re-plans from scratch, and the LLM's second plan names
    different types and different times for the same appointment, so every
    row it produced would look new.
    """
    rows = db.query(Reminder).filter_by(appointment_id=appointment_id).order_by(Reminder.id).all()
    return [reminder_summary(row) for row in rows]


def run(state: AgentState, db: Session) -> dict:
    """No-op without a confirmed appointment; otherwise schedule reminders
    from the LLM's plan plus the deterministic post-visit follow-up task."""
    workflow_id = state.get("workflow_id")
    appointment = state.get("appointment")
    if not appointment or appointment.get("status") != "confirmed":
        _exit_audit(db, workflow_id, {"skipped": True})
        return {"completed_steps": ["followup"]}

    try:
        patient_id = state["patient_id"]
        appointment_id = appointment["id"]

        existing = _reminders_already_scheduled(db, appointment_id)
        if existing:
            # Re-run after a crash-resume: the batch is already in the
            # database, so the whole creation block below is skipped rather
            # than deduplicated row by row.
            update = {"reminders": existing, "completed_steps": ["followup"]}
            _exit_audit(db, workflow_id, {"reminder_count": len(existing), "reused": True})
            return update

        missing = (state.get("documents_result") or {}).get("missing", [])

        system = build_system_prompt(db, "followup", FOLLOWUP)
        plan = chat_json(system, _plan_prompt(appointment, missing), FollowupOutput)
        start_time = datetime.fromisoformat(appointment["start_time"])

        created: list[dict] = []
        for spec in plan.reminders[:_MAX_REMINDERS]:
            scheduled_at = start_time - timedelta(days=_clamp_days(spec.days_before_appointment))
            created.append(
                create_reminder(db, patient_id, appointment_id, spec.type, scheduled_at)
            )
        created.append(
            create_followup_task(
                db, patient_id, appointment_id, days_after=_clamp_days(plan.followup_days_after)
            )
        )

        update = {"reminders": created, "completed_steps": ["followup"]}
        _exit_audit(db, workflow_id, {"reminder_count": len(created)})
        return update
    except Exception as exc:  # noqa: BLE001 - node boundary must never crash the graph
        logger.error("followup_agent_failed", workflow_id=workflow_id, error=str(exc))
        db.rollback()
        _exit_audit(db, workflow_id, {"error": str(exc)})
        return {"error": f"followup agent failed: {exc}", "completed_steps": ["followup"]}
