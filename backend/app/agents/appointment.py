"""Appointment agent: picks a slot from the real available list and books,
reschedules, or cancels through the atomic appointment tools.

Owns app.tools.appointment_tools plus the shared escalation/audit tools.
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agents.prompts import APPOINTMENT
from app.agents.llm import invoke_structured
from app.agents.memory import build_system_prompt
from app.agents.responses import patient_language, staff_review_response
from app.agents.state import AgentState
from app.agents.support import record_agent_exit
from app.agents.time_window import RequestedWindow, parse_requested_window
from app.exceptions import ConflictError, NotFoundError
from app.logging_setup import get_logger
from app.models import Appointment
from app.safety.pii import redact_for_llm, resolve_language
from app.tools.appointment_tools import (
    appointment_summary,
    book_appointment,
    cancel_appointment,
    cancelled_summary,
    find_active_patient_appointment_in_window,
    get_available_slots,
    list_active_patient_appointments,
    reschedule_appointment,
)
from app.tools.audit_tools import write_audit
from app.tools.escalation_tools import create_escalation

logger = get_logger(__name__)

_SLOT_PICK_ATTEMPTS = 2
_SLOT_CANDIDATE_LIMIT = 50
_ADDITIONAL_APPOINTMENT = re.compile(
    r"\b(?:another|additional|second|extra|weiteren?|zweiten?|zus[aä]tzlichen?)\b",
    re.IGNORECASE,
)


class AppointmentOutput(BaseModel):
    # No Python default on slot_id, same reasoning as RoutingOutput.department
    # (see routing.py): Groq strict mode requires every property in
    # "required"; required-but-nullable is `int | None` with no default.
    slot_id: int | None
    reason: str


def _slot_prompt(
    llm_request_text: str,
    slots: list[dict],
    window: RequestedWindow,
) -> str:
    """`llm_request_text` must already be PII-redacted (see
    _handle_book_or_reschedule) - this string goes to the LLM provider."""
    if not slots:
        listing = "(no slots available)"
    else:
        listing = "\n".join(
            f"slot_id={s['slot_id']} doctor={s['doctor']} start={s['start_time']}" for s in slots
        )
    return (
        f"Today: {datetime.now().date().isoformat()}\n"
        f"Required scheduling window: {window.start.isoformat()} through "
        f"{window.end.isoformat()}\n"
        f"Patient timing preference: {llm_request_text}\n"
        f"Available slots (all already inside the required window):\n{listing}"
    )


def _pick_valid_slot(
    system: str,
    llm_request_text: str,
    slots: list[dict],
    window: RequestedWindow,
) -> int | None:
    """Ask the LLM for a slot id, attempting at most _SLOT_PICK_ATTEMPTS
    validated selections and never trusting an id it was not given. `system`
    is built once per run() call (see _handle_book_or_reschedule) and reused
    across every retry so a rules lookup does not repeat per attempt."""
    valid_ids = {s["slot_id"] for s in slots}
    for _ in range(_SLOT_PICK_ATTEMPTS):
        picked = invoke_structured(
            system,
            _slot_prompt(llm_request_text, slots, window),
            AppointmentOutput,
        )
        if picked.slot_id in valid_ids:
            return picked.slot_id
    return None


def _escalate_agent_failure(
    db: Session, workflow_id: int | None, patient_id: int | None, reason: str
) -> dict:
    escalation = create_escalation(db, workflow_id, reason=reason, severity="agent_failure")
    update = {
        "escalation_id": escalation["id"],
        "final_response": staff_review_response(db, patient_id),
        "completed_steps": ["appointment"],
    }
    record_agent_exit(db, "appointment", workflow_id, {"escalated": True, "reason": reason})
    return update


def _escalate_for_clarification(
    db: Session,
    workflow_id: int | None,
    patient_id: int,
    intent: str,
) -> dict:
    reason = f"multiple active appointments require clarification before {intent}"
    escalation = create_escalation(db, workflow_id, reason=reason, severity="uncertainty")
    update = {
        "escalation_id": escalation["id"],
        "final_response": staff_review_response(db, patient_id),
        "completed_steps": ["appointment"],
    }
    record_agent_exit(db, "appointment", workflow_id, {"escalated": True, "reason": reason})
    return update


def _active_appointment_for_action(
    db: Session,
    workflow_id: int | None,
    patient_id: int,
    intent: str,
) -> tuple[dict | None, dict | None]:
    active = list_active_patient_appointments(db, patient_id)
    if len(active) > 1:
        return None, _escalate_for_clarification(
            db,
            workflow_id,
            patient_id,
            intent,
        )
    return (active[0] if active else None), None


def _book_or_reschedule(
    db: Session,
    intent: str,
    patient_id: int,
    slot_id: int,
    request_text: str,
    existing_id: int | None,
    workflow_id: int | None,
    booking_window_key: str | None,
) -> dict:
    if intent == "book":
        return book_appointment(
            db,
            patient_id,
            slot_id,
            reason=request_text,
            workflow_run_id=workflow_id,
            booking_window_key=booking_window_key,
        )
    return reschedule_appointment(
        db,
        existing_id,
        slot_id,
        workflow_run_id=workflow_id,
        booking_window_key=booking_window_key,
    )


def _window_booking_key(
    department_id: int,
    window: RequestedWindow,
    request_text: str,
    workflow_id: int | None,
) -> str:
    key = (
        f"department:{department_id}:"
        f"{window.start.date().isoformat()}:{window.end.date().isoformat()}"
    )
    if _ADDITIONAL_APPOINTMENT.search(request_text):
        return f"{key}:additional:{workflow_id or 'direct'}"
    return key


def _reuse_window_booking(
    db: Session,
    workflow_id: int | None,
    appointment: Appointment,
    window: RequestedWindow,
) -> dict:
    result = appointment_summary(appointment)
    result["reused_existing"] = True
    write_audit(
        db,
        None,
        "appointment.duplicate_intent_reused",
        "appointment",
        appointment.id,
        {"workflow_run_id": workflow_id},
    )
    record_agent_exit(
        db,
        "appointment",
        workflow_id,
        {"action": "book", "appointment_id": appointment.id, "reused": True},
    )
    return {
        "appointment": result,
        "requested_window": window.as_state(),
        "scheduling_issue": (
            "An active appointment already satisfies this department and "
            "requested time window."
        ),
        "completed_steps": ["appointment"],
    }


# --- Replay guards -----------------------------------------------------------
# A node commits its rows before LangGraph writes the checkpoint saying it ran,
# so a process that dies in that window resumes into a node whose work is done
# but unrecorded, and re-executes it from the top. Every one of the three
# actions needs its own way back out of that, and all three use the same mark:
# the run's id, stamped on the appointment row by the tool that changed it.
#
# Booking and rescheduling leave the row confirmed, so they share one query and
# the partial unique index on models/appointment.py backs it up - the index
# makes a second confirmed row for the run impossible, and the query is what
# keeps the resumed run going instead of failing on a conflict it cannot
# resolve. Cancelling leaves the row cancelled, which falls outside that index,
# so it gets its own query below.


def _confirmed_appointment_from_this_run(
    db: Session, workflow_id: int | None
) -> Appointment | None:
    """The confirmed appointment this run booked or moved, if it already did."""
    if workflow_id is None:
        return None
    return db.query(Appointment).filter_by(workflow_run_id=workflow_id, status="confirmed").first()


def _cancellation_from_this_run(db: Session, workflow_id: int | None) -> Appointment | None:
    """The appointment this run cancelled, if it already cancelled one.

    Without this the second pass finds no active appointment left to cancel and
    reports a cancellation that succeeded as a failure.
    """
    if workflow_id is None:
        return None
    return db.query(Appointment).filter_by(workflow_run_id=workflow_id, status="cancelled").first()


def _reuse_existing(
    db: Session, workflow_id: int | None, appt: Appointment, intent: str, result: dict
) -> dict:
    """The shared exit for every replayed action: hand back the result the
    first pass produced and record that this pass reused it."""
    write_audit(
        db,
        None,
        "appointment.reused_existing",
        "appointment",
        appt.id,
        {"workflow_run_id": workflow_id},
    )
    update = {"appointment": result, "completed_steps": ["appointment"]}
    record_agent_exit(
        db,
        "appointment",
        workflow_id,
        {"action": intent, "appointment_id": result["id"], "reused": True},
    )
    return update


def _handle_cancel(db: Session, workflow_id: int | None, patient_id: int) -> dict:
    already_cancelled = _cancellation_from_this_run(db, workflow_id)
    if already_cancelled is not None:
        return _reuse_existing(
            db, workflow_id, already_cancelled, "cancel", cancelled_summary(already_cancelled)
        )

    existing, escalation = _active_appointment_for_action(
        db,
        workflow_id,
        patient_id,
        "cancellation",
    )
    if escalation is not None:
        return escalation
    if existing is None:
        raise NotFoundError("no active appointment to cancel")
    result = cancel_appointment(db, existing["id"], workflow_run_id=workflow_id)
    update = {"appointment": result, "completed_steps": ["appointment"]}
    record_agent_exit(db, "appointment", workflow_id, {"action": "cancel", "appointment_id": result["id"]})
    return update


def _handle_book_or_reschedule(
    db: Session, workflow_id: int | None, state: AgentState, intent: str
) -> dict:
    patient_id = state["patient_id"]

    already_confirmed = _confirmed_appointment_from_this_run(db, workflow_id)
    if already_confirmed is not None:
        return _reuse_existing(
            db, workflow_id, already_confirmed, intent, appointment_summary(already_confirmed)
        )

    department_id = state.get("department_id")
    if department_id is None:
        raise NotFoundError("no department resolved for appointment scheduling")

    existing_id: int | None = None
    if intent == "reschedule":
        existing, escalation = _active_appointment_for_action(
            db,
            workflow_id,
            patient_id,
            "rescheduling",
        )
        if escalation is not None:
            return escalation
        if existing is None:
            raise NotFoundError("no active appointment to reschedule")
        existing_id = existing["id"]

    system = build_system_prompt(db, "appointment", APPOINTMENT)

    # The raw request_text stays in the DB (appointment reason below); only
    # the copy embedded in the LLM prompt is redacted, same boundary as the
    # routing/coordinator/document nodes. The language is settled cue-first
    # (safety/pii.py::resolve_language): a German cue in the request decides,
    # and the patient's stored preference breaks the tie for a short booking
    # request that carries none.
    request_text = state.get("request_text", "")
    window = parse_requested_window(request_text)
    booking_window_key = _window_booking_key(
        department_id,
        window,
        request_text,
        workflow_id,
    )

    if intent == "book" and not _ADDITIONAL_APPOINTMENT.search(request_text):
        duplicate = find_active_patient_appointment_in_window(
            db,
            patient_id,
            department_id,
            window.start,
            window.end,
        )
        if duplicate is not None:
            return _reuse_window_booking(db, workflow_id, duplicate, window)

    llm_request_text, pii_counts = redact_for_llm(
        request_text,
        language=resolve_language(request_text, patient_language(db, patient_id)),
    )
    if pii_counts:
        write_audit(
            db,
            None,
            "safety.pii_redacted",
            "workflow_run",
            workflow_id,
            {"node": "appointment", "counts": pii_counts},
        )

    slots = get_available_slots(
        db,
        department_id,
        window.start.date(),
        window.end.date(),
        limit=_SLOT_CANDIDATE_LIMIT,
        patient_id=patient_id,
        exclude_appointment_id=existing_id,
        not_before=window.start,
    )
    if not slots:
        issue = f"No open slots are available in the requested {window.label} window."
        result = {
            "status": "unavailable",
            "requested_window": window.as_state(),
        }
        record_agent_exit(
            db,
            "appointment",
            workflow_id,
            {"action": intent, "available": False, "window": window.as_state()},
        )
        return {
            "appointment": result,
            "requested_window": window.as_state(),
            "scheduling_issue": issue,
            "completed_steps": ["appointment"],
        }

    slot_id = _pick_valid_slot(system, llm_request_text, slots, window)
    if slot_id is None:
        return _escalate_agent_failure(
            db,
            workflow_id,
            patient_id,
            "appointment agent could not select a valid slot from the list",
        )

    try:
        result = _book_or_reschedule(
            db,
            intent,
            patient_id,
            slot_id,
            request_text,
            existing_id,
            workflow_id,
            booking_window_key,
        )
    except ConflictError:
        if intent == "book":
            duplicate = find_active_patient_appointment_in_window(
                db,
                patient_id,
                department_id,
                window.start,
                window.end,
            )
            if duplicate is not None:
                return _reuse_window_booking(db, workflow_id, duplicate, window)
        slots = get_available_slots(
            db,
            department_id,
            window.start.date(),
            window.end.date(),
            limit=_SLOT_CANDIDATE_LIMIT,
            patient_id=patient_id,
            exclude_appointment_id=existing_id,
            not_before=window.start,
        )
        slot_id = _pick_valid_slot(system, llm_request_text, slots, window)
        if slot_id is None:
            return _escalate_agent_failure(
                db, workflow_id, patient_id, "no valid slot left after a booking conflict"
            )
        try:
            result = _book_or_reschedule(
                db,
                intent,
                patient_id,
                slot_id,
                request_text,
                existing_id,
                workflow_id,
                booking_window_key,
            )
        except ConflictError:
            return _escalate_agent_failure(
                db, workflow_id, patient_id, "repeated booking conflict on the same request"
            )

    update = {
        "appointment": result,
        "requested_window": window.as_state(),
        "completed_steps": ["appointment"],
    }
    record_agent_exit(db, "appointment", workflow_id, {"action": intent, "appointment_id": result["id"]})
    return update


def run(state: AgentState, db: Session) -> dict:
    """Book, reschedule, or cancel - a no-op for any other intent."""
    workflow_id = state.get("workflow_id")
    intent = state.get("intent")
    if intent not in ("book", "reschedule", "cancel", "status"):
        return {"completed_steps": ["appointment"]}

    try:
        if intent == "status":
            active = list_active_patient_appointments(db, state["patient_id"])
            appointments = [
                {key: value for key, value in row.items() if key != "reason"}
                for row in active
            ]
            appointment_context = {
                "status": "summary",
                "appointments": appointments,
            }
            record_agent_exit(
                db,
                "appointment",
                workflow_id,
                {
                    "action": "status",
                    "appointment_ids": [row["id"] for row in appointments],
                },
            )
            return {
                "appointment": appointment_context,
                "completed_steps": ["appointment"],
            }
        if intent == "cancel":
            return _handle_cancel(db, workflow_id, state["patient_id"])
        return _handle_book_or_reschedule(db, workflow_id, state, intent)
    except Exception as exc:  # noqa: BLE001 - node boundary must never crash the graph
        logger.error("appointment_agent_failed", workflow_id=workflow_id, error=str(exc))
        db.rollback()
        record_agent_exit(db, "appointment", workflow_id, {"error": str(exc)})
        return {"error": f"appointment agent failed: {exc}", "completed_steps": ["appointment"]}
