"""Runs one patient request through the LangGraph workflow: the deterministic
safety screen first (Task 7), then - only if it allows the request through -
the compiled graph (graph.py), with the outcome always landing on a
WorkflowRun row. `resume_workflow` re-enters the same thread from its last
checkpoint, for the crash-recovery demo.

The compiled graph is a module-level singleton (`get_graph`): it owns one
checkpointer connection for the process's life, opened lazily on first use
and closed by `close_graph()` (called from the FastAPI lifespan on shutdown,
and by tests wanting an isolated checkpoint file between runs). This module
is the only place that builds it - `start_workflow`/`resume_workflow` take no
graph parameter, per the Task 11 interface, so any caller (an HTTP route or a
test calling these functions directly) shares the same graph/checkpointer.
"""

from __future__ import annotations

import contextlib
from typing import Any

from sqlalchemy.orm import Session

from app.agents.graph import build_graph, open_checkpointer
from app.agents.state import AgentState
from app.config import settings
from app.exceptions import NotFoundError
from app.logging_setup import get_logger
from app.models import User, WorkflowRun
from app.safety.guardrails import ScreenResult, screen_request
from app.tools.audit_tools import write_audit
from app.tools.escalation_tools import create_escalation

logger = get_logger(__name__)

_graph_stack: contextlib.ExitStack | None = None
_graph: Any | None = None

_langfuse_client_ready = False


def get_graph() -> Any:
    """Lazily build the compiled graph once and cache it for reuse."""
    global _graph_stack, _graph
    if _graph is None:
        _graph_stack = contextlib.ExitStack()
        checkpointer = _graph_stack.enter_context(open_checkpointer())
        _graph = build_graph(checkpointer)
    return _graph


def close_graph() -> None:
    """Close the cached graph's checkpointer and clear the cache. Safe to
    call when nothing is cached. Used by the FastAPI lifespan on shutdown
    and by tests to force a fresh checkpoint file on the next get_graph()."""
    global _graph_stack, _graph, _langfuse_client_ready
    if _graph_stack is not None:
        _graph_stack.close()
    _graph_stack = None
    _graph = None
    _langfuse_client_ready = False


def _ensure_langfuse_client() -> None:
    global _langfuse_client_ready
    if _langfuse_client_ready:
        return
    from langfuse import Langfuse  # deferred: only needed when keys are set

    Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host or None,
    )
    _langfuse_client_ready = True


@contextlib.contextmanager
def _observability(config: dict[str, Any], workflow_id: int):
    """Attach Langfuse tracing to this graph invocation when both
    settings.langfuse_public_key/secret_key are set; a no-op otherwise.
    langfuse.langchain (which additionally requires `langchain`, not pinned
    in requirements.txt) is only imported on the keys-set path, so the
    default/CI path never touches it."""
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        yield
        return

    _ensure_langfuse_client()
    from langfuse import propagate_attributes
    from langfuse.langchain import CallbackHandler

    config.setdefault("callbacks", []).append(CallbackHandler())
    with propagate_attributes(metadata={"workflow_id": workflow_id}):
        yield


def _final_status(state: dict) -> str:
    if state.get("error"):
        return "failed"
    if state.get("escalation_id"):
        return "escalated"
    return "completed"


def _apply_final_state(workflow_run: WorkflowRun, state: dict) -> None:
    workflow_run.state = dict(state)
    completed_steps = state.get("completed_steps") or []
    if completed_steps:
        workflow_run.current_step = completed_steps[-1]
    workflow_run.status = _final_status(state)


def _new_workflow_run(db: Session, user: User, request_text: str, status: str) -> WorkflowRun:
    workflow_run = WorkflowRun(
        user_id=user.id,
        patient_id=user.id,
        thread_id="",
        request_text=request_text,
        status=status,
    )
    db.add(workflow_run)
    db.flush()  # assign workflow_run.id for the thread_id below
    workflow_run.thread_id = f"wf-{workflow_run.id}"
    return workflow_run


def _screened_run(
    db: Session, user: User, request_text: str, screen: ScreenResult, *, status: str, action: str
) -> WorkflowRun:
    """Pre-graph screening path (emergency or medical refusal): no graph, no
    LLM call, per screen_request's own judgment alone."""
    workflow_run = _new_workflow_run(db, user, request_text, status)
    workflow_run.current_step = "safety_screen"
    workflow_run.state = {"final_response": screen.reason, "safety_flags": screen.matched}

    if screen.action == "escalate_emergency":
        create_escalation(db, workflow_run.id, reason=screen.reason, severity="emergency")

    write_audit(
        db,
        user.id,
        f"workflow.{action}",
        "workflow_run",
        workflow_run.id,
        {"matched": screen.matched},
    )
    db.commit()
    return workflow_run


def _invoke_graph(db: Session, workflow_run: WorkflowRun, graph_input: dict | None) -> dict | None:
    """Shared invoke wrapper for both start and resume: never lets a graph
    exception become a 500. Any exception that escapes graph.invoke()
    (a real bug, not a node-level LLM/tool failure - those are already
    caught inside each node and surface as state["error"] instead) is
    caught here, logged, and turned into a failed WorkflowRun with its own
    agent_failure escalation."""
    graph = get_graph()
    config: dict[str, Any] = {"configurable": {"thread_id": workflow_run.thread_id, "db": db}}

    try:
        with _observability(config, workflow_run.id):
            return graph.invoke(graph_input, config)
    except Exception as exc:  # noqa: BLE001 - last-resort safety net, never a 500
        logger.error("workflow_graph_crashed", workflow_id=workflow_run.id, error=str(exc))
        db.rollback()
        create_escalation(
            db, workflow_run.id, reason=f"workflow graph crashed: {exc}", severity="agent_failure"
        )
        workflow_run.status = "failed"
        workflow_run.current_step = "crashed"
        db.commit()
        return None


def start_workflow(
    db: Session, user: User, request_text: str, document_ids: list[int] | None = None
) -> WorkflowRun:
    """Screen the request first; only a request the screen allows ever
    reaches the graph/LLM. Emergency and medical-refusal paths create their
    WorkflowRun and (for emergency) Escalation directly, with zero LLM
    calls - never invoking the graph at all."""
    document_ids = document_ids or []
    screen = screen_request(request_text)

    if screen.action == "escalate_emergency":
        return _screened_run(
            db, user, request_text, screen, status="escalated", action="escalated_emergency"
        )
    if screen.action == "refuse_medical":
        return _screened_run(
            db, user, request_text, screen, status="completed", action="refused_medical"
        )

    workflow_run = _new_workflow_run(db, user, request_text, status="running")
    write_audit(
        db, user.id, "workflow.started", "workflow_run", workflow_run.id, {"request_text": request_text}
    )
    db.commit()

    initial_state: AgentState = {
        "workflow_id": workflow_run.id,
        "user_id": user.id,
        "patient_id": user.id,
        "request_text": request_text,
        "uploaded_document_ids": document_ids,
    }

    final_state = _invoke_graph(db, workflow_run, initial_state)
    if final_state is not None:
        _apply_final_state(workflow_run, final_state)
        db.commit()
    return workflow_run


def resume_workflow(db: Session, workflow_run_id: int) -> WorkflowRun:
    """Re-enter the same thread from its last checkpoint (the restart demo):
    `graph.invoke(None, config)`. A no-op on an already-completed thread -
    nothing left to resume, so no node re-executes and nothing duplicates."""
    workflow_run = db.get(WorkflowRun, workflow_run_id)
    if workflow_run is None:
        raise NotFoundError(f"WorkflowRun {workflow_run_id} not found")

    final_state = _invoke_graph(db, workflow_run, None)
    if final_state:
        _apply_final_state(workflow_run, final_state)
        db.commit()
    return workflow_run
