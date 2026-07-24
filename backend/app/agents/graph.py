"""The LangGraph workflow graph: coordinator + five specialists, wired as a
loop that always returns to the coordinator until it picks a terminal step.

Node functions here are thin adapters: each specialist's real logic lives in
its own `app/agents/<name>.py::run(state, db)` (Tasks 7-10). The adapters
exist only because a compiled LangGraph node's signature is `(state,
config)`, not `(state, db)` - the db session for one workflow run travels
through `config["configurable"]["db"]` rather than through AgentState
(a SQLAlchemy Session isn't JSON-serializable and has no business being
checkpointed), one session per `graph.invoke()` call, supplied by
`workflow_service`.

Routing: the coordinator never picks a node directly - it appends one of six
plan words to `state["plan"]` (see `agents/coordinator.py`), and
`_route_from_coordinator` maps the latest plan entry to a graph node. Two
things override that mapping and force `escalate` directly, regardless of
what the coordinator last decided: a node's unrecovered failure
(`state["error"]` set - every specialist's own `run()` already catches its
exceptions and reports them this way rather than raising), and an escalation
a specialist already opened (`state["escalation_id"]` set). The graph must
never depend on the coordinator's own LLM call noticing either one: once a
case is with a human, the run stops rather than working on past the handoff
and answering over the top of it.

The escalate node is where it stops, on LangGraph's `interrupt()`: the run
is checkpointed mid-graph, its WorkflowRun goes to status `waiting_approval`
and nothing moves until a staff member decides
(`workflow_service.resume_with_decision`). Approving an uncertainty case
sends the run back to the coordinator with the reviewer's note as guidance,
so the work the patient asked for still happens; a rejection, or any run
whose agent had already failed, ends on a deterministic template instead.

The same applies to the ordering rules the coordinator prompt states
(prompts.py). `_step_allowed` enforces them against the completed-step
history instead of trusting the decision, and an out-of-order step routes to
escalate: a coordinator that jumps straight to finalize gets a human, not a
run marked completed that routed, booked and scheduled nothing.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from sqlalchemy.orm import Session

from app.agents import appointment, coordinator, document, followup, routing, safety
from app.agents.responses import staff_decision_response
from app.agents.state import AgentState
from app.config import settings
from app.logging_setup import get_logger
from app.models import Escalation
from app.tools.audit_tools import write_audit
from app.tools.escalation_tools import create_escalation

logger = get_logger(__name__)

# CoordinatorOutput.next_step (agents/coordinator.py) -> the graph node that
# handles it. Keys are exactly the six values in that Literal.
_STEP_TO_NODE: dict[str, str] = {
    "route_department": "routing",
    "handle_appointment": "appointment",
    "handle_documents": "document",
    "schedule_followup": "followup",
    "finalize": "safety_finalize",
    "escalate": "escalate",
}


def _db(config: RunnableConfig) -> Session:
    return config["configurable"]["db"]


def _coordinator_node(state: AgentState, config: RunnableConfig) -> dict:
    return coordinator.run(state, _db(config))


def _routing_node(state: AgentState, config: RunnableConfig) -> dict:
    return routing.run(state, _db(config))


def _appointment_node(state: AgentState, config: RunnableConfig) -> dict:
    return appointment.run(state, _db(config))


def _document_node(state: AgentState, config: RunnableConfig) -> dict:
    return document.run(state, _db(config))


def _followup_node(state: AgentState, config: RunnableConfig) -> dict:
    return followup.run(state, _db(config))


def _safety_finalize_node(state: AgentState, config: RunnableConfig) -> dict:
    return safety.run(state, _db(config))


def _existing_escalation(db: Session, state: AgentState) -> Escalation | None:
    """The open handoff this run is already having with staff, if any.

    Rows this run has already had back (approved, and the run carried on)
    are excluded: they are closed business, and reusing one would park the
    run against an escalation staff have no reason to look at again. The
    exclusion list is written by this node itself, so the pre-interrupt
    re-execution below - which happens before that write lands - still sees
    the row it just created and reuses it.
    """
    resolved = state.get("resolved_escalation_ids") or []
    query = db.query(Escalation).filter_by(workflow_run_id=state.get("workflow_id"))
    if resolved:
        query = query.filter(Escalation.id.notin_(resolved))
    return query.order_by(Escalation.id.desc()).first()


def _escalate_node(state: AgentState, config: RunnableConfig) -> dict:
    """The human handoff, for a coordinator-chosen escalate, a specialist's
    own escalation, or a specialist's unrecovered error.

    A specialist that already opened an escalation for this run (routing on
    low confidence, appointment on an unrecoverable booking failure) hands
    its id over on the state, and a run whose escalation row exists without
    that id is found by workflow_run_id: either way the row is reused, so
    one run never files two escalations for the same handoff. Only a run
    with no escalation yet gets a new one, with severity agent_failure when
    the state carries an error and uncertainty when the coordinator
    escalated a clean state (contradictory or out-of-scope request, per its
    prompt).

    Then the node stops on `interrupt()` and the run waits for a human.
    LangGraph re-executes a resumed node from its first line, so everything
    above the interrupt runs a second time when the decision arrives - which
    is exactly why the escalation is reused rather than created blind, and
    why nothing above the interrupt writes unconditionally. The create branch
    (and the escalation.created audit row that comes with it) is skipped on
    the second pass, which finds the row the first pass left.

    What the decision does depends on what the run was stopped for. An
    approved uncertainty case goes back to the coordinator with the staff
    note as guidance and no block left on the state, so the work the patient
    asked for actually happens. Anything else - a rejection, or a run whose
    agent already failed and has nothing to carry on with - ends here on a
    deterministic template. The escalation row survives either way, resolved,
    as the audit trail of who decided what.
    """
    db = _db(config)
    workflow_id = state.get("workflow_id")
    error = state.get("error")
    severity = "agent_failure" if error else "uncertainty"

    escalation_id = state.get("escalation_id")
    if escalation_id is None:
        existing = _existing_escalation(db, state)
        if existing is not None:
            escalation_id = existing.id
        else:
            reason = error or "coordinator escalated: contradictory or out-of-scope request"
            escalation = create_escalation(db, workflow_id, reason=reason, severity=severity)
            escalation_id = escalation["id"]

    # Audit the severity actually on the row: on a reuse that is the
    # escalating specialist's own severity (appointment files agent_failure
    # without setting state["error"]), not what this node would have picked.
    row = db.get(Escalation, escalation_id)
    if row is not None:
        severity = row.severity

    decision = interrupt({"escalation_id": escalation_id, "workflow_id": workflow_id})

    approved = bool(decision.get("approved"))
    guidance = (decision.get("note") or "").strip() or None
    write_audit(
        db,
        decision.get("reviewer_id"),
        "agent.escalate.resolved",
        "workflow_run",
        workflow_id,
        {"escalation_id": escalation_id, "approved": approved},
    )
    write_audit(
        db, None, "agent.escalate.completed", "workflow_run", workflow_id, {"severity": severity}
    )
    db.commit()

    if approved and not error:
        # escalation_id back to None so the router's short-circuit and
        # _final_status treat the continued run as an ordinary one. The row
        # itself stays, resolved, and is listed as handled so a later handoff
        # opens a fresh one.
        return {
            "escalation_id": None,
            "resolved_escalation_ids": [escalation_id],
            "final_response": None,
            "staff_guidance": guidance,
            "completed_steps": ["escalate"],
        }
    return {
        "escalation_id": escalation_id,
        "resolved_escalation_ids": [escalation_id],
        "final_response": staff_decision_response(db, state.get("patient_id"), approved),
        "completed_steps": ["escalate"],
    }


def _route_from_escalate(state: AgentState) -> str:
    """A decided case that produced an answer is finished; one that did not
    is an approved uncertainty case going back to the coordinator to do the
    work it was stopped before doing."""
    return END if state.get("final_response") else "coordinator"


def _ran(state: AgentState, step: str) -> bool:
    return step in (state.get("completed_steps") or [])


def _step_allowed(state: AgentState, step: str) -> bool:
    """The COORDINATOR prompt's three ordering rules, enforced in code
    (prompts.py: route_department before handle_appointment, handle_documents
    when the request carries uploads, schedule_followup then finalize). The
    argument is a coordinator plan word; the history it checks holds node
    names, which is why the two vocabularies differ here.

    History-based (has it run at least once), so crash-resume re-entry and
    legitimate re-visits stay legal. A booking whose department came back
    null (cancel, status, attach_documents) is legal too: the rule is that
    routing ran, not that it resolved a department."""
    if step in ("handle_appointment", "schedule_followup"):
        return _ran(state, "routing")
    if step == "finalize":
        if not _ran(state, "followup"):
            return False
        if state.get("uploaded_document_ids") and not _ran(state, "document"):
            return False
        return True
    return True


def _route_from_coordinator(state: AgentState) -> str:
    """An error or an already-created escalation always wins, regardless of
    the coordinator's own plan entry - a defensive fallback that doesn't
    depend on the LLM noticing either one itself. Then the ordering guard:
    a step the coordinator picked out of turn goes to a human instead of
    running, so an LLM that jumps straight to finalize cannot produce a
    "completed" run that did nothing. Otherwise follow the latest plan
    entry; an empty or unrecognized plan also falls back to escalate rather
    than crash the graph on a missing/bad decision."""
    if state.get("error") or state.get("escalation_id"):
        return "escalate"
    plan = state.get("plan") or []
    if not plan:
        return "escalate"
    step = plan[-1]
    if not _step_allowed(state, step):
        logger.warning(
            "coordinator_step_out_of_order",
            workflow_id=state.get("workflow_id"),
            step=step,
            completed_steps=state.get("completed_steps") or [],
        )
        return "escalate"
    return _STEP_TO_NODE.get(step, "escalate")


def build_graph(checkpointer) -> CompiledStateGraph:
    """Build and compile the six-agent workflow graph against an already-open
    checkpointer. Called once at process startup (see
    `app.services.workflow_service.get_graph`); the compiled graph is reused
    for every workflow run."""
    graph = StateGraph(AgentState)

    graph.add_node("coordinator", _coordinator_node)
    graph.add_node("routing", _routing_node)
    graph.add_node("appointment", _appointment_node)
    graph.add_node("document", _document_node)
    graph.add_node("followup", _followup_node)
    graph.add_node("safety_finalize", _safety_finalize_node)
    graph.add_node("escalate", _escalate_node)

    graph.add_edge(START, "coordinator")
    graph.add_conditional_edges(
        "coordinator",
        _route_from_coordinator,
        {
            "routing": "routing",
            "appointment": "appointment",
            "document": "document",
            "followup": "followup",
            "safety_finalize": "safety_finalize",
            "escalate": "escalate",
        },
    )
    graph.add_edge("routing", "coordinator")
    graph.add_edge("appointment", "coordinator")
    graph.add_edge("document", "coordinator")
    graph.add_edge("followup", "coordinator")
    graph.add_edge("safety_finalize", END)
    graph.add_conditional_edges(
        "escalate", _route_from_escalate, {END: END, "coordinator": "coordinator"}
    )

    return graph.compile(checkpointer=checkpointer)


def _psycopg_conninfo(database_url: str) -> str:
    """PostgresSaver.from_conn_string() hands the string straight to
    psycopg.Connection.connect(), whose libpq conninfo parser only
    understands plain `postgresql://` - not SQLAlchemy's `dialect+driver`
    syntax (e.g. `postgresql+psycopg://`, what settings.database_url is in
    docker-compose so the app engine also picks psycopg). Strip the driver
    suffix so both engines can share one DATABASE_URL."""
    scheme, sep, rest = database_url.partition("://")
    driver = scheme.split("+", 1)[0]
    return f"{driver}{sep}{rest}"


@contextlib.contextmanager
def open_checkpointer() -> Iterator[SqliteSaver | PostgresSaver]:
    """Open the LangGraph checkpointer selected by settings.database_url's
    scheme, as a context manager meant to stay open for the app's lifetime
    (see workflow_service.get_graph, entered via a module-level ExitStack).

    Sqlite uses a separate file (settings.checkpoint_db_path) from the app
    db to avoid file-locking conflicts between the two; SqliteSaver
    auto-creates its tables. Postgres reuses settings.database_url (same
    database is fine) and requires the one-time, idempotent .setup() call.
    """
    if settings.database_url.startswith("sqlite"):
        with SqliteSaver.from_conn_string(settings.checkpoint_db_path) as checkpointer:
            yield checkpointer
    else:
        conninfo = _psycopg_conninfo(settings.database_url)
        with PostgresSaver.from_conn_string(conninfo) as checkpointer:
            checkpointer.setup()
            yield checkpointer
