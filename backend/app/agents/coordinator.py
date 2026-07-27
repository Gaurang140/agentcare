"""Coordinator: a pure decision node, no tools. Decides the next
administrative step from the current workflow state and appends it to
state["plan"] - the ordered trace the graph controller walks to route
between nodes.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agents.prompts import COORDINATOR
from app.agents.llm import chat_json
from app.agents.memory import build_system_prompt
from app.agents.state import AgentState
from app.agents.support import record_agent_exit, redact_request_for_agent
from app.logging_setup import get_logger

logger = get_logger(__name__)


class CoordinatorOutput(BaseModel):
    next_step: Literal[
        "route_department",
        "handle_appointment",
        "handle_documents",
        "schedule_followup",
        "finalize",
        "escalate",
    ]
    reasoning: str


def _user_prompt(state: AgentState, request_text: str) -> str:
    prompt = (
        f"Patient request: {request_text}\n"
        f"Intent: {state.get('intent')}\n"
        f"Department: {state.get('department_name')}\n"
        f"Has appointment: {bool(state.get('appointment'))}\n"
        f"Documents result: {state.get('documents_result')}\n"
        f"Completed steps so far: {state.get('completed_steps') or []}\n"
        f"Prior decisions (plan): {state.get('plan') or []}\n"
        f"Error, if any: {state.get('error')}"
    )
    # Only set on a run a staff member approved out of the escalate node
    # (agents/graph.py): the redacted copy of the note that unblocked it, so
    # the decision that follows uses what the human clarified rather than the
    # same ambiguity that stopped the run.
    guidance = state.get("staff_guidance")
    if guidance:
        prompt += f"\nStaff guidance: {guidance}"
    return prompt


def run(state: AgentState, db: Session) -> dict:
    """Decide the next step; append it to the running plan."""
    workflow_id = state.get("workflow_id")
    try:
        request_text = redact_request_for_agent(db, state, "coordinator")
        system = build_system_prompt(db, "coordinator", COORDINATOR)
        result = chat_json(system, _user_prompt(state, request_text), CoordinatorOutput)
        plan = [*(state.get("plan") or []), result.next_step]
        update = {"plan": plan, "completed_steps": ["coordinator"]}
        record_agent_exit(db, "coordinator", workflow_id, {"next_step": result.next_step})
        return update
    except Exception as exc:  # noqa: BLE001 - node boundary must never crash the graph
        logger.error("coordinator_agent_failed", workflow_id=workflow_id, error=str(exc))
        db.rollback()
        record_agent_exit(db, "coordinator", workflow_id, {"error": str(exc)})
        return {"error": f"coordinator agent failed: {exc}", "completed_steps": ["coordinator"]}
