"""Department Routing agent: classifies intent, maps it to one department.

Owns app.tools.department_tools (list_departments, find_department) plus the
shared escalation/audit tools every node uses to hand off and report.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agents.prompts import ROUTING
from app.agents.llm import invoke_structured
from app.agents.memory import build_system_prompt
from app.agents.responses import staff_review_response
from app.agents.schema_types import Probability
from app.agents.state import AgentState
from app.agents.support import record_agent_exit, redact_request_for_agent
from app.logging_setup import get_logger
from app.tools.department_tools import find_department, list_departments
from app.tools.escalation_tools import create_escalation

logger = get_logger(__name__)

_CONFIDENCE_THRESHOLD = 0.7

# Only these two intents cannot be served without a department: booking and
# rescheduling need one to look up slots. Cancel works off the patient's own
# appointment, status reads it back, and the document node degrades to
# classify-only when no department is set - so a null department there is a
# well-formed request, not a reason to hand the patient to staff.
_DEPARTMENT_REQUIRED_INTENTS = {"book", "reschedule"}


class RoutingOutput(BaseModel):
    intent: Literal["book", "reschedule", "cancel", "attach_documents", "status", "other"]
    # No Python default: Groq/OpenAI strict json_schema mode requires every
    # property to appear in "required" (nullable != optional there), so a
    # field with a default drops out of "required" and the strict request
    # would 400. `str | None` with no default keeps it required-but-nullable.
    department: str | None
    confidence: Probability
    reason: str


def _user_prompt(request_text: str, departments: list[dict], guidance: str | None = None) -> str:
    names = ", ".join(d["name"] for d in departments)
    prompt = (
        f"Patient request: {request_text}\n"
        f"Available departments (choose exactly one of these, or null): {names}"
    )
    # Set only on a run staff approved out of the escalate node. This is the
    # note's redacted copy; without the human's clarification this node would
    # usually land in the same uncertainty again.
    if guidance:
        prompt += f"\nStaff guidance: {guidance}"
    return prompt


def _escalate_uncertain(
    db: Session,
    workflow_id: int | None,
    patient_id: int | None,
    intent: str,
    confidence: float,
    reason: str,
) -> dict:
    escalation = create_escalation(db, workflow_id, reason=reason, severity="uncertainty")
    update = {
        "intent": intent,
        "department_id": None,
        "department_name": None,
        "routing_confidence": confidence,
        "escalation_id": escalation["id"],
        "final_response": staff_review_response(db, patient_id),
        "completed_steps": ["routing"],
    }
    record_agent_exit(db, "routing", workflow_id, {"escalated": True, "confidence": confidence})
    return update


def run(state: AgentState, db: Session) -> dict:
    """Classify the patient's intent and department; escalate on an
    unsupported intent, low confidence, a department that is missing where the
    intent needs one, or a department that doesn't resolve, instead of
    guessing."""
    workflow_id = state.get("workflow_id")
    try:
        departments = list_departments(db)
        request_text = redact_request_for_agent(db, state, "routing")
        system = build_system_prompt(db, "routing", ROUTING)
        result = invoke_structured(
            system,
            _user_prompt(request_text, departments, state.get("staff_guidance")),
            RoutingOutput,
        )

        if result.intent == "other":
            # Nothing downstream can serve a request outside the supported
            # administrative intents, so it goes to staff here rather than
            # depending on the coordinator to notice.
            return _escalate_uncertain(
                db,
                workflow_id,
                state.get("patient_id"),
                result.intent,
                result.confidence,
                "request outside supported administrative intents",
            )

        needs_department = result.intent in _DEPARTMENT_REQUIRED_INTENTS
        if result.confidence < _CONFIDENCE_THRESHOLD or (needs_department and not result.department):
            return _escalate_uncertain(
                db, workflow_id, state.get("patient_id"), result.intent, result.confidence, result.reason
            )

        department_id: int | None = None
        department_name: str | None = None
        if result.department:
            department = find_department(db, result.department)
            if department is None:
                # The LLM was told to pick from the given list; a name that
                # doesn't resolve is treated the same as low confidence rather
                # than passed through.
                return _escalate_uncertain(
                    db,
                    workflow_id,
                    state.get("patient_id"),
                    result.intent,
                    result.confidence,
                    f"unresolvable department: {result.department}",
                )
            department_id, department_name = department["id"], department["name"]

        update = {
            "intent": result.intent,
            "department_id": department_id,
            "department_name": department_name,
            "routing_confidence": result.confidence,
            "completed_steps": ["routing"],
        }
        record_agent_exit(
            db, "routing", workflow_id, {"department": department_name, "confidence": result.confidence}
        )
        return update
    except Exception as exc:  # noqa: BLE001 - node boundary must never crash the graph
        logger.error("routing_agent_failed", workflow_id=workflow_id, error=str(exc))
        db.rollback()
        record_agent_exit(db, "routing", workflow_id, {"error": str(exc)})
        return {"error": f"routing agent failed: {exc}", "completed_steps": ["routing"]}
