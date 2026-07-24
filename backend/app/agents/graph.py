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
`_route_from_coordinator` maps the latest plan entry to a graph node. Any
node's unrecovered failure (`state["error"]` set - every specialist's own
`run()` already catches its exceptions and reports them this way rather than
raising) overrides that mapping and forces `escalate` directly, regardless of
what the coordinator last decided: the graph must never depend on the
coordinator's own LLM call correctly noticing an error to stay safe.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.orm import Session

from app.agents import appointment, coordinator, document, followup, routing, safety
from app.agents.responses import staff_review_response
from app.agents.state import AgentState
from app.config import settings
from app.logging_setup import get_logger
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


def _escalate_node(state: AgentState, config: RunnableConfig) -> dict:
    """Terminal node for either a coordinator-chosen escalate or a
    specialist's unrecovered error. severity is agent_failure when the
    state carries an error, uncertainty when the coordinator escalated a
    clean state (contradictory or out-of-scope request, per its prompt)."""
    db = _db(config)
    workflow_id = state.get("workflow_id")
    error = state.get("error")
    severity = "agent_failure" if error else "uncertainty"
    reason = error or "coordinator escalated: contradictory or out-of-scope request"

    escalation = create_escalation(db, workflow_id, reason=reason, severity=severity)
    write_audit(
        db, None, "agent.escalate.completed", "workflow_run", workflow_id, {"severity": severity}
    )
    db.commit()
    return {
        "escalation_id": escalation["id"],
        "final_response": staff_review_response(db, state.get("patient_id")),
        "completed_steps": ["escalate"],
    }


def _route_from_coordinator(state: AgentState) -> str:
    """An error on the state always wins, regardless of the coordinator's
    own plan entry - a defensive fallback that doesn't depend on the LLM
    noticing the error itself. Otherwise follow the latest plan entry; an
    empty or unrecognized plan also falls back to escalate rather than
    crash the graph on a missing/bad decision."""
    if state.get("error"):
        return "escalate"
    plan = state.get("plan") or []
    if not plan:
        return "escalate"
    return _STEP_TO_NODE.get(plan[-1], "escalate")


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
    graph.add_edge("escalate", END)

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
