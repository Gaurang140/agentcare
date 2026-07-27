"""Runs one patient request through the LangGraph workflow: the deterministic
safety screen first, then the prompt-injection guard, then - only if both
allow the request through - the compiled graph (graph.py), with the outcome
always landing on a WorkflowRun row. `resume_workflow` re-enters the same
thread from its last checkpoint for crash recovery.

The compiled graph is a module-level singleton (`get_graph`): it owns one
checkpointer connection for the process's life, opened lazily on first use
and closed by `close_graph()` (called from the FastAPI lifespan on shutdown,
and by tests wanting an isolated checkpoint file between runs). This module
is the only place that builds it - `start_workflow`/`resume_workflow` take no
graph parameter, so any caller (an HTTP route or a test calling these
functions directly) shares the same graph/checkpointer.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
from typing import Any

from langgraph.types import Command
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.agents.graph import build_graph, open_checkpointer
from app.agents.responses import emergency_response, medical_refusal_response
from app.agents.state import AgentState
from app.agents.support import redact_text_for_agent
from app.config import settings
from app.exceptions import NotFoundError, ValidationError
from app.logging_setup import get_logger
from app.models import Escalation, User, WorkflowRun
from app.safety.guardrails import ScreenResult, screen_request
from app.safety.injection_guard import InjectionResult, screen_injection
from app.tools.audit_tools import write_audit
from app.tools.escalation_tools import create_escalation

logger = get_logger(__name__)

# Shown to the patient when the injection guard blocks a request. Deliberately
# uninformative about why - explaining the block would hand an attacker a map
# of what the guard catches.
INJECTION_BLOCKED_RESPONSE = (
    "Your request could not be processed automatically and has been sent to "
    "staff for review."
)

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
    The Langfuse LangChain callback is imported only on the keys-set path, so
    the default/CI path never touches tracing infrastructure.

    Tracing is optional infrastructure, so an import failure downgrades the
    run to untraced instead of failing it. The import is attempted before
    anything else on this path, and its failure leaves both the Langfuse
    client and callback out of the invocation."""
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        yield
        return

    try:
        from langfuse import propagate_attributes
        from langfuse.langchain import CallbackHandler
    except ImportError as exc:
        logger.warning("langfuse_disabled_missing_dependency", error=str(exc))
        yield
        return

    _ensure_langfuse_client()
    config.setdefault("callbacks", []).append(CallbackHandler())
    with propagate_attributes(metadata={"workflow_id": workflow_id}):
        yield


# The key LangGraph adds to invoke()'s result when the run stopped on an
# interrupt() instead of finishing. Its value is a list of Interrupt objects,
# which are not JSON-serializable and must not reach WorkflowRun.state.
_INTERRUPT_KEY = "__interrupt__"


def _interrupt_payload(result: dict) -> dict:
    """What the escalate node handed to `interrupt()`: the escalation and
    workflow ids of the handoff the run is now waiting on."""
    interrupts = result.get(_INTERRUPT_KEY) or ()
    value = getattr(interrupts[0], "value", None) if interrupts else None
    return value if isinstance(value, dict) else {}


def _paused_state(result: dict, escalation_id: int | None) -> dict:
    """The checkpointed state to store on a run parked at the interrupt.

    `final_response` is cleared: a run waiting for a human has no answer
    yet, and the handoff line the escalating specialist wrote on its way out
    is not one. The escalation id comes from the interrupt payload rather
    than the state, because a coordinator-chosen escalation is opened inside
    the paused node and its own writes never landed.
    """
    state = {key: value for key, value in result.items() if key != _INTERRUPT_KEY}
    state["final_response"] = None
    if escalation_id is not None:
        state["escalation_id"] = escalation_id
    return state


def _apply_pause(db: Session, workflow_run: WorkflowRun, result: dict) -> bool:
    """Park the run at the escalate node's interrupt, and say whether that is
    what happened. Commits, because a paused run has to survive the process
    that paused it - the decision arrives in some later request."""
    if not result.get(_INTERRUPT_KEY):
        return False

    escalation_id = _interrupt_payload(result).get("escalation_id")
    workflow_run.status = "waiting_approval"
    workflow_run.current_step = "escalate"
    workflow_run.state = _paused_state(result, escalation_id)
    write_audit(
        db,
        None,
        "workflow.waiting_approval",
        "workflow_run",
        workflow_run.id,
        {"escalation_id": escalation_id},
    )
    db.commit()
    return True


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
    LLM call, per screen_request's own judgment alone. The patient-facing
    response is localized (agents/responses.py); the escalation reason below
    stays English because it is staff-facing."""
    if screen.action == "escalate_emergency":
        final_response = emergency_response(db, user.id)
    else:
        final_response = medical_refusal_response(db, user.id)

    workflow_run = _new_workflow_run(db, user, request_text, status)
    workflow_run.current_step = "safety_screen"
    workflow_run.state = {"final_response": final_response, "safety_flags": screen.matched}

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


def _injection_blocked_run(
    db: Session, user: User, request_text: str, injection: InjectionResult
) -> WorkflowRun:
    """Prompt-injection screening runs after screen_request has allowed the
    request through, so - like `_screened_run` - this path makes no graph or
    coordinator LLM call."""
    workflow_run = _new_workflow_run(db, user, request_text, status="escalated")
    workflow_run.current_step = "injection_screen"
    workflow_run.state = {
        "final_response": INJECTION_BLOCKED_RESPONSE,
        "safety_flags": injection.matched,
    }

    create_escalation(
        db,
        workflow_run.id,
        reason=f"prompt injection detected ({injection.via}): {', '.join(injection.matched)}",
        severity="safety",
    )
    # The patient timeline streams AuditEvent rows straight through, so the
    # matched patterns stay out of the metadata: telling an attacker which
    # pattern fired is the same map the blocked-response text refuses to hand
    # over. Ops still get the full list from this log line, and staff from the
    # escalation reason above.
    logger.warning(
        "injection_blocked",
        workflow_id=workflow_run.id,
        via=injection.via,
        matched=injection.matched,
    )
    write_audit(
        db,
        user.id,
        "safety.injection_blocked",
        "workflow_run",
        workflow_run.id,
        {"via": injection.via},
    )
    db.commit()
    return workflow_run


def _invoke_graph(
    db: Session, workflow_run: WorkflowRun, graph_input: dict | Command | None
) -> dict | None:
    """Shared invoke wrapper for start, crash-resume and the staff decision:
    never lets a graph exception become a 500. Any exception that escapes
    graph.invoke() (a real bug, not a node-level LLM/tool failure - those are
    already caught inside each node and surface as state["error"] instead) is
    caught here, logged, and turned into a failed WorkflowRun with its own
    agent_failure escalation. A stop on `interrupt()` is not an exception:
    LangGraph returns normally with a "__interrupt__" key, handled by the
    callers through `_apply_pause`."""
    graph = get_graph()
    config: dict[str, Any] = {
        "configurable": {"thread_id": workflow_run.thread_id, "db": db},
        # Operational cap on the coordinator loop. LangGraph's own default is
        # far higher; a real run takes about 7 supersteps, so a graph that
        # keeps bouncing back to the coordinator is a bug and should stop
        # early rather than burn LLM calls until the default trips. The exact
        # default is deliberately not quoted here: it is the library's number
        # to change, and this cap does not depend on what it is.
        "recursion_limit": 50,
    }

    try:
        with _observability(config, workflow_run.id):
            # Persist each checkpoint before the next super-step runs. The
            # default mode writes asynchronously, which leaves a window where a
            # crash loses the step that just finished. A booking that already
            # claimed a slot must not come back as a run that never took it.
            return graph.invoke(graph_input, config, durability="sync")
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


def _fail_before_graph(
    db: Session,
    workflow_run: WorkflowRun,
    *,
    reason: str,
    log_event: str,
    error: Exception,
) -> WorkflowRun:
    """Contain work that fails after a run is claimed but before invoke()."""
    logger.error(
        log_event,
        workflow_id=workflow_run.id,
        error_type=type(error).__name__,
    )
    db.rollback()
    create_escalation(
        db,
        workflow_run.id,
        reason=reason,
        severity="agent_failure",
    )
    workflow_run.status = "failed"
    workflow_run.current_step = "crashed"
    db.commit()
    return workflow_run


def create_run(db: Session, user: User, request_text: str) -> WorkflowRun:
    """Screen the request and create its WorkflowRun row - synchronously,
    with zero LLM calls or graph invocation either way. An emergency,
    medical-refusal or injection-blocked request is already terminal on
    return (its own escalation, if any, already created via `_screened_run`
    or `_injection_blocked_run`); a request both screens allow comes back
    with status "running" and nothing executed yet. Callers that want the
    graph to actually run call `execute_workflow` next (only when status is
    still "running") - split out so an HTTP route can hand that part to a
    background task instead of blocking the request on it."""
    screen = screen_request(request_text)

    if screen.action == "escalate_emergency":
        return _screened_run(
            db, user, request_text, screen, status="escalated", action="escalated_emergency"
        )
    if screen.action == "refuse_medical":
        return _screened_run(
            db, user, request_text, screen, status="completed", action="refused_medical"
        )

    injection = screen_injection(request_text)
    if injection.action == "block":
        return _injection_blocked_run(db, user, request_text, injection)

    workflow_run = _new_workflow_run(db, user, request_text, status="running")
    write_audit(
        db, user.id, "workflow.started", "workflow_run", workflow_run.id, {"request_text": request_text}
    )
    db.commit()
    return workflow_run


def execute_workflow(
    db: Session, workflow_run_id: int, document_ids: list[int] | None = None
) -> WorkflowRun | None:
    """Invoke the graph for a WorkflowRun `create_run` already put in
    status "running". Meant to be safe from a background task holding its
    own fresh db session, never the request's: it only reads/writes rows
    reachable through the `db` it was given. A no-op returning the run
    unchanged if the id is missing or the run is no longer "running" (an
    already-screened/terminal run, or a duplicate call after the graph
    already finished it)."""
    workflow_run = db.get(WorkflowRun, workflow_run_id)
    if workflow_run is None or workflow_run.status != "running":
        return workflow_run

    initial_state: AgentState = {
        "workflow_id": workflow_run.id,
        "user_id": workflow_run.user_id,
        "patient_id": workflow_run.patient_id,
        "request_text": workflow_run.request_text,
        "uploaded_document_ids": document_ids or [],
    }

    final_state = _invoke_graph(db, workflow_run, initial_state)
    if final_state is not None and not _apply_pause(db, workflow_run, final_state):
        _apply_final_state(workflow_run, final_state)
        db.commit()
    return workflow_run


def start_workflow(
    db: Session, user: User, request_text: str, document_ids: list[int] | None = None
) -> WorkflowRun:
    """Synchronous convenience for tests and any non-HTTP caller: `create_run`
    then `execute_workflow`, back to back in the same db session. The HTTP
    route calls the two halves separately, running `execute_workflow` in a
    FastAPI BackgroundTasks callback with its own session so the request
    returns immediately after `create_run`."""
    document_ids = document_ids or []
    workflow_run = create_run(db, user, request_text)
    if workflow_run.status == "running":
        execute_workflow(db, workflow_run.id, document_ids)
    return workflow_run


def resume_workflow(db: Session, workflow_run_id: int) -> WorkflowRun:
    """Re-enter the same thread from its last checkpoint (the restart demo):
    `graph.invoke(None, config)`. A no-op on an already-completed thread -
    nothing left to resume, so no node re-executes and nothing duplicates.

    Refuses a run waiting for staff. That thread does resume, but only into
    the same interrupt it is already stopped on, and only a decision
    (`resume_with_decision`) gets it past there. Answering the patient's own
    resume with a 400 says so, rather than looking like a retry that quietly
    changed nothing."""
    workflow_run = db.get(WorkflowRun, workflow_run_id)
    if workflow_run is None:
        raise NotFoundError(f"WorkflowRun {workflow_run_id} not found")
    if workflow_run.status == "waiting_approval":
        raise ValidationError("This request is waiting for staff review")

    final_state = _invoke_graph(db, workflow_run, None)
    if final_state and not _apply_pause(db, workflow_run, final_state):
        _apply_final_state(workflow_run, final_state)
        db.commit()
    return workflow_run


def resume_with_decision(
    db: Session,
    workflow_run_id: int,
    escalation_id: int,
    approved: bool,
    note: str | None,
    reviewer_id: int,
) -> WorkflowRun:
    """Hand a staff decision to a run parked at the escalate node's
    interrupt, and let the graph carry it out: an approved uncertainty case
    goes back to the coordinator and finishes the patient's request, anything
    else closes on a template (see `agents/graph.py::_escalate_node`).

    A no-op on a run that is not waiting, which makes a double-clicked
    approve harmless. The invoke goes through `_invoke_graph`, so a crash
    while resuming ends as a failed run with its own escalation, exactly like
    a crash on the first pass, and never as a 500 out of the staff route.

    The run claim and escalation decision are one transaction. The status
    moves waiting_approval -> running and the matching open escalation gets
    its reviewer, note and decision before either change commits. Two staff
    members can both read the open case, but only one can claim the run; the
    loser cannot overwrite the decision that actually drove the graph.

    The same transaction persists the raw staff note on
    Escalation.resolution_note. Only an approved uncertainty case needs it as
    agent guidance, so that copy is PII-redacted before Command construction.
    Raw note text therefore never enters LangGraph's checkpoint input.
    """
    workflow_run = db.get(WorkflowRun, workflow_run_id)
    if workflow_run is None:
        raise NotFoundError(f"WorkflowRun {workflow_run_id} not found")
    if workflow_run.status != "waiting_approval":
        return workflow_run

    escalation = db.get(Escalation, escalation_id)
    if escalation is None or escalation.workflow_run_id != workflow_run_id:
        raise NotFoundError(f"Escalation {escalation_id} not found for this workflow")
    severity = escalation.severity

    claimed = db.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == workflow_run_id, WorkflowRun.status == "waiting_approval")
        .values(status="running"),
        execution_options={"synchronize_session": False},
    )
    if claimed.rowcount == 0:
        db.commit()
        return workflow_run

    decided = db.execute(
        update(Escalation)
        .where(
            Escalation.id == escalation_id,
            Escalation.workflow_run_id == workflow_run_id,
            Escalation.status == "open",
        )
        .values(
            status="approved" if approved else "rejected",
            reviewed_by=reviewer_id,
            resolution_note=note,
        ),
        execution_options={"synchronize_session": False},
    )
    if decided.rowcount == 0:
        db.rollback()
        raise ValidationError("This escalation has already been resolved")

    write_audit(
        db,
        reviewer_id,
        "escalation.resolved",
        "escalation",
        escalation_id,
        {"approve": approved},
    )
    db.commit()
    db.refresh(workflow_run)

    raw_guidance = (note or "").strip()
    guidance = None
    if approved and severity == "uncertainty" and raw_guidance:
        try:
            redaction_state: AgentState = dict(workflow_run.state or {})
            redaction_state.setdefault("workflow_id", workflow_run.id)
            redaction_state.setdefault("patient_id", workflow_run.patient_id)
            guidance = redact_text_for_agent(
                db,
                redaction_state,
                raw_guidance,
                "escalate",
            )
            db.commit()
        except Exception as exc:  # noqa: BLE001 - pre-invoke boundary
            return _fail_before_graph(
                db,
                workflow_run,
                reason="staff guidance redaction failed",
                log_event="staff_guidance_redaction_failed",
                error=exc,
            )

    resume = Command(
        resume={
            "approved": approved,
            "guidance": guidance,
            "reviewer_id": reviewer_id,
        }
    )
    final_state = _invoke_graph(db, workflow_run, resume)
    if final_state is None:
        return workflow_run
    # A resumed run can reach the escalate node a second time: a legitimate
    # second handoff, on its own escalation row, waiting for staff again.
    if not _apply_pause(db, workflow_run, final_state):
        _apply_final_state(workflow_run, final_state)
        db.commit()
    return workflow_run


_STALL_THRESHOLD_MINUTES = 30


def _naive_utcnow() -> datetime:
    """Timezone-aware now(), stripped back to naive - matches the naive
    (but UTC-convention) DateTime columns used throughout the schema."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def escalate_stalled_workflows(db: Session) -> dict:
    """Scheduler job2's body: any WorkflowRun still "running" whose
    updated_at hasn't moved in _STALL_THRESHOLD_MINUTES gets its own
    agent_failure escalation and moves to status "escalated". A run only
    stays "running" this long if whatever was supposed to execute it (a
    background task, a crashed process) never got the chance to finish -
    this is the safety net that keeps such a run from being silently stuck
    forever."""
    threshold = _naive_utcnow() - timedelta(minutes=_STALL_THRESHOLD_MINUTES)
    stalled = (
        db.query(WorkflowRun)
        .filter(WorkflowRun.status == "running", WorkflowRun.updated_at <= threshold)
        .all()
    )

    escalated_ids: list[int] = []
    for run in stalled:
        create_escalation(
            db, run.id, reason="workflow stalled: no progress in 30 minutes", severity="agent_failure"
        )
        run.status = "escalated"
        db.flush()
        escalated_ids.append(run.id)
    db.commit()

    return {"escalated_count": len(escalated_ids), "workflow_run_ids": escalated_ids}
