"""Department Routing agent: classifies intent, maps it to one department.

Owns app.tools.department_tools (list_departments, find_department) plus the
shared escalation/audit tools every node uses to hand off and report.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agents.prompts import ROUTING
from app.agents.llm import chat_json
from app.agents.state import AgentState
from app.logging_setup import get_logger
from app.safety.pii import redact_for_llm
from app.tools.audit_tools import write_audit
from app.tools.department_tools import find_department, list_departments
from app.tools.escalation_tools import create_escalation

logger = get_logger(__name__)

_CONFIDENCE_THRESHOLD = 0.7
_ESCALATION_RESPONSE = "a staff member will review your request"


class RoutingOutput(BaseModel):
    intent: Literal["book", "reschedule", "cancel", "attach_documents", "status", "other"]
    # No Python default: Groq/OpenAI strict json_schema mode requires every
    # property to appear in "required" (nullable != optional there), so a
    # field with a default drops out of "required" and the strict request
    # would 400. `str | None` with no default keeps it required-but-nullable.
    department: str | None
    confidence: float
    reason: str


def _user_prompt(request_text: str, departments: list[dict]) -> str:
    names = ", ".join(d["name"] for d in departments)
    return (
        f"Patient request: {request_text}\n"
        f"Available departments (choose exactly one of these, or null): {names}"
    )


def _exit_audit(db: Session, workflow_id: int | None, summary: dict) -> None:
    write_audit(db, None, "agent.routing.completed", "workflow_run", workflow_id, summary)
    db.commit()


def _redact_request_text(db: Session, workflow_id: int | None, request_text: str) -> str:
    """Redact `request_text` before it is embedded in the routing prompt;
    write one "safety.pii_redacted" audit row (counts only, no raw PII) when
    anything was found."""
    redacted, counts = redact_for_llm(request_text)
    if counts:
        write_audit(
            db,
            None,
            "safety.pii_redacted",
            "workflow_run",
            workflow_id,
            {"node": "routing", "counts": counts},
        )
    return redacted


def _escalate_uncertain(
    db: Session, workflow_id: int | None, intent: str, confidence: float, reason: str
) -> dict:
    escalation = create_escalation(db, workflow_id, reason=reason, severity="uncertainty")
    update = {
        "intent": intent,
        "department_id": None,
        "department_name": None,
        "routing_confidence": confidence,
        "escalation_id": escalation["id"],
        "final_response": _ESCALATION_RESPONSE,
        "completed_steps": ["routing"],
    }
    _exit_audit(db, workflow_id, {"escalated": True, "confidence": confidence})
    return update


def run(state: AgentState, db: Session) -> dict:
    """Classify the patient's intent and department; escalate on low
    confidence or a department that doesn't resolve, instead of guessing."""
    workflow_id = state.get("workflow_id")
    try:
        departments = list_departments(db)
        request_text = _redact_request_text(db, workflow_id, state.get("request_text", ""))
        result = chat_json(ROUTING, _user_prompt(request_text, departments), RoutingOutput)

        if result.confidence < _CONFIDENCE_THRESHOLD or not result.department:
            return _escalate_uncertain(
                db, workflow_id, result.intent, result.confidence, result.reason
            )

        department = find_department(db, result.department)
        if department is None:
            # The LLM was told to pick from the given list; a name that
            # doesn't resolve is treated the same as low confidence rather
            # than passed through.
            return _escalate_uncertain(
                db,
                workflow_id,
                result.intent,
                result.confidence,
                f"unresolvable department: {result.department}",
            )

        update = {
            "intent": result.intent,
            "department_id": department["id"],
            "department_name": department["name"],
            "routing_confidence": result.confidence,
            "completed_steps": ["routing"],
        }
        _exit_audit(
            db, workflow_id, {"department": department["name"], "confidence": result.confidence}
        )
        return update
    except Exception as exc:  # noqa: BLE001 - node boundary must never crash the graph
        logger.error("routing_agent_failed", workflow_id=workflow_id, error=str(exc))
        db.rollback()
        _exit_audit(db, workflow_id, {"error": str(exc)})
        return {"error": f"routing agent failed: {exc}", "completed_steps": ["routing"]}
