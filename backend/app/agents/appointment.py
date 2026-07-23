"""Appointment agent: picks a slot from the real available list and books,
reschedules, or cancels through the atomic appointment tools.

Owns app.tools.appointment_tools plus the shared escalation/audit tools.
"""

from __future__ import annotations

from datetime import date, timedelta

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agents.prompts import APPOINTMENT
from app.agents.llm import chat_json
from app.agents.state import AgentState
from app.exceptions import ConflictError, NotFoundError
from app.logging_setup import get_logger
from app.models import Appointment
from app.tools.appointment_tools import (
    book_appointment,
    cancel_appointment,
    get_available_slots,
    reschedule_appointment,
)
from app.tools.audit_tools import write_audit
from app.tools.escalation_tools import create_escalation

logger = get_logger(__name__)

_SLOT_WINDOW_DAYS = 14
_SLOT_PICK_ATTEMPTS = 2
_ESCALATION_RESPONSE = "a staff member will review your request"


class AppointmentOutput(BaseModel):
    # No Python default on slot_id, same reasoning as RoutingOutput.department
    # (see routing.py): Groq strict mode requires every property in
    # "required"; required-but-nullable is `int | None` with no default.
    slot_id: int | None
    reason: str


def _slot_prompt(state: AgentState, slots: list[dict]) -> str:
    if not slots:
        listing = "(no slots available)"
    else:
        listing = "\n".join(
            f"slot_id={s['slot_id']} doctor={s['doctor']} start={s['start_time']}" for s in slots
        )
    return f"Patient timing preference: {state.get('request_text', '')}\nAvailable slots:\n{listing}"


def _pick_valid_slot(state: AgentState, slots: list[dict]) -> int | None:
    """Ask the LLM for a slot id, validated against the real list. Retries
    once on an invented id, per the brief; never trusts an id it didn't hand
    the LLM in the first place."""
    valid_ids = {s["slot_id"] for s in slots}
    for _ in range(_SLOT_PICK_ATTEMPTS):
        picked = chat_json(APPOINTMENT, _slot_prompt(state, slots), AppointmentOutput)
        if picked.slot_id in valid_ids:
            return picked.slot_id
    return None


def _latest_active_appointment(db: Session, patient_id: int) -> Appointment | None:
    return (
        db.query(Appointment)
        .filter(Appointment.patient_id == patient_id, Appointment.status.in_(["pending", "confirmed"]))
        .order_by(Appointment.created_at.desc())
        .first()
    )


def _exit_audit(db: Session, workflow_id: int | None, summary: dict) -> None:
    write_audit(db, None, "agent.appointment.completed", "workflow_run", workflow_id, summary)
    db.commit()


def _escalate_agent_failure(db: Session, workflow_id: int | None, reason: str) -> dict:
    escalation = create_escalation(db, workflow_id, reason=reason, severity="agent_failure")
    update = {
        "escalation_id": escalation["id"],
        "final_response": _ESCALATION_RESPONSE,
        "completed_steps": ["appointment"],
    }
    _exit_audit(db, workflow_id, {"escalated": True, "reason": reason})
    return update


def _book_or_reschedule(
    db: Session, intent: str, patient_id: int, slot_id: int, request_text: str, existing_id: int | None
) -> dict:
    if intent == "book":
        return book_appointment(db, patient_id, slot_id, reason=request_text)
    return reschedule_appointment(db, existing_id, slot_id)


def _handle_cancel(db: Session, workflow_id: int | None, patient_id: int) -> dict:
    existing = _latest_active_appointment(db, patient_id)
    if existing is None:
        raise NotFoundError("no active appointment to cancel")
    result = cancel_appointment(db, existing.id)
    update = {"appointment": result, "completed_steps": ["appointment"]}
    _exit_audit(db, workflow_id, {"action": "cancel", "appointment_id": result["id"]})
    return update


def _handle_book_or_reschedule(
    db: Session, workflow_id: int | None, state: AgentState, intent: str
) -> dict:
    patient_id = state["patient_id"]
    department_id = state.get("department_id")
    if department_id is None:
        raise NotFoundError("no department resolved for appointment scheduling")

    existing_id: int | None = None
    if intent == "reschedule":
        existing = _latest_active_appointment(db, patient_id)
        if existing is None:
            raise NotFoundError("no active appointment to reschedule")
        existing_id = existing.id

    today = date.today()
    slots = get_available_slots(db, department_id, today, today + timedelta(days=_SLOT_WINDOW_DAYS))
    slot_id = _pick_valid_slot(state, slots)
    if slot_id is None:
        return _escalate_agent_failure(
            db, workflow_id, "appointment agent could not select a valid slot from the list"
        )

    request_text = state.get("request_text", "")
    try:
        result = _book_or_reschedule(db, intent, patient_id, slot_id, request_text, existing_id)
    except ConflictError:
        slots = get_available_slots(db, department_id, today, today + timedelta(days=_SLOT_WINDOW_DAYS))
        slot_id = _pick_valid_slot(state, slots)
        if slot_id is None:
            return _escalate_agent_failure(
                db, workflow_id, "no valid slot left after a booking conflict"
            )
        try:
            result = _book_or_reschedule(db, intent, patient_id, slot_id, request_text, existing_id)
        except ConflictError:
            return _escalate_agent_failure(
                db, workflow_id, "repeated booking conflict on the same request"
            )

    update = {"appointment": result, "completed_steps": ["appointment"]}
    _exit_audit(db, workflow_id, {"action": intent, "appointment_id": result["id"]})
    return update


def run(state: AgentState, db: Session) -> dict:
    """Book, reschedule, or cancel - a no-op for any other intent."""
    workflow_id = state.get("workflow_id")
    intent = state.get("intent")
    if intent not in ("book", "reschedule", "cancel"):
        return {"completed_steps": ["appointment"]}

    try:
        if intent == "cancel":
            return _handle_cancel(db, workflow_id, state["patient_id"])
        return _handle_book_or_reschedule(db, workflow_id, state, intent)
    except Exception as exc:  # noqa: BLE001 - node boundary must never crash the graph
        logger.error("appointment_agent_failed", workflow_id=workflow_id, error=str(exc))
        db.rollback()
        _exit_audit(db, workflow_id, {"error": str(exc)})
        return {"error": f"appointment agent failed: {exc}", "completed_steps": ["appointment"]}
