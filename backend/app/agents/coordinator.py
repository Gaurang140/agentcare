"""Coordinator: a pure decision node, no tools. Decides the next
administrative step from the current workflow state and appends it to
state["plan"] - the ordered trace of decisions the graph controller (task
11) walks to route between nodes.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agents.prompts import COORDINATOR
from app.agents.llm import chat_json
from app.agents.state import AgentState
from app.logging_setup import get_logger
from app.tools.audit_tools import write_audit

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


def _user_prompt(state: AgentState) -> str:
    return (
        f"Patient request: {state.get('request_text', '')}\n"
        f"Intent: {state.get('intent')}\n"
        f"Department: {state.get('department_name')}\n"
        f"Has appointment: {bool(state.get('appointment'))}\n"
        f"Documents result: {state.get('documents_result')}\n"
        f"Completed steps so far: {state.get('completed_steps') or []}\n"
        f"Prior decisions (plan): {state.get('plan') or []}\n"
        f"Error, if any: {state.get('error')}"
    )


def _exit_audit(db: Session, workflow_id: int | None, summary: dict) -> None:
    write_audit(db, None, "agent.coordinator.completed", "workflow_run", workflow_id, summary)
    db.commit()


def run(state: AgentState, db: Session) -> dict:
    """Decide the next step; append it to the running plan."""
    workflow_id = state.get("workflow_id")
    try:
        result = chat_json(COORDINATOR, _user_prompt(state), CoordinatorOutput)
        plan = [*(state.get("plan") or []), result.next_step]
        update = {"plan": plan, "completed_steps": ["coordinator"]}
        _exit_audit(db, workflow_id, {"next_step": result.next_step})
        return update
    except Exception as exc:  # noqa: BLE001 - node boundary must never crash the graph
        logger.error("coordinator_agent_failed", workflow_id=workflow_id, error=str(exc))
        db.rollback()
        _exit_audit(db, workflow_id, {"error": str(exc)})
        return {"error": f"coordinator agent failed: {exc}", "completed_steps": ["coordinator"]}
