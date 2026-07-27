"""Shared persistence and privacy boundaries for model-assisted agents."""

from sqlalchemy.orm import Session

from app.agents.responses import patient_language
from app.agents.state import AgentState
from app.safety.pii import redact_for_llm, resolve_language
from app.tools.audit_tools import write_audit


def record_agent_exit(
    db: Session,
    agent_name: str,
    workflow_id: int | None,
    summary: dict,
) -> None:
    write_audit(
        db,
        None,
        f"agent.{agent_name}.completed",
        "workflow_run",
        workflow_id,
        summary,
    )
    db.commit()


def redact_request_for_agent(
    db: Session,
    state: AgentState,
    agent_name: str,
) -> str:
    """Redact the request copy that crosses into a model prompt."""
    return redact_text_for_agent(
        db,
        state,
        state.get("request_text", ""),
        agent_name,
    )


def redact_text_for_agent(
    db: Session,
    state: AgentState,
    text: str,
    agent_name: str,
) -> str:
    """Redact patient-related text before graph state or a model prompt.

    Text-language cues win; the stored patient preference is only a
    no-cue tie-breaker, because a default preference must not override the
    language the text actually uses. When redaction finds PII, its audit
    metadata records the responsible node and category counts only, never
    raw patient or staff-entered data.
    """
    redacted, counts = redact_for_llm(
        text,
        language=resolve_language(
            text,
            patient_language(db, state.get("patient_id")),
        ),
    )
    if counts:
        write_audit(
            db,
            None,
            "safety.pii_redacted",
            "workflow_run",
            state.get("workflow_id"),
            {"node": agent_name, "counts": counts},
        )
    return redacted
