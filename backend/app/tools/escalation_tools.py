"""Handing a case to a human, and recording the human's decision."""

from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.exceptions import NotFoundError
from app.models import Escalation
from app.tools.audit_tools import write_audit


def create_escalation(
    db: Session, workflow_run_id: int | None, reason: str, severity: str
) -> dict:
    """Open an escalation. severity: emergency|safety|uncertainty|agent_failure."""
    escalation = Escalation(
        workflow_run_id=workflow_run_id,
        reason=reason,
        severity=severity,
        status="open",
    )
    db.add(escalation)
    db.flush()

    write_audit(
        db,
        None,
        "escalation.created",
        "escalation",
        escalation.id,
        {"workflow_run_id": workflow_run_id, "severity": severity},
    )
    db.commit()

    return {
        "id": escalation.id,
        "workflow_run_id": escalation.workflow_run_id,
        "reason": escalation.reason,
        "severity": escalation.severity,
        "status": escalation.status,
    }


def resolve_escalation(
    db: Session, escalation_id: int, reviewer_id: int, approve: bool, note: str
) -> dict:
    """Record the first staff decision; later attempts are idempotent."""
    escalation = db.get(Escalation, escalation_id)
    if escalation is None:
        raise NotFoundError(f"Escalation {escalation_id} not found")

    decided = db.execute(
        update(Escalation)
        .where(Escalation.id == escalation_id, Escalation.status == "open")
        .values(
            status="approved" if approve else "rejected",
            reviewed_by=reviewer_id,
            resolution_note=note,
        ),
        execution_options={"synchronize_session": False},
    )

    if decided.rowcount:
        write_audit(
            db,
            reviewer_id,
            "escalation.resolved",
            "escalation",
            escalation.id,
            {"approve": approve},
        )
    db.commit()
    db.refresh(escalation)

    return {
        "id": escalation.id,
        "status": escalation.status,
        "reviewed_by": escalation.reviewed_by,
        "resolution_note": escalation.resolution_note,
    }
