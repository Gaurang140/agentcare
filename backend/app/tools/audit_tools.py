"""Append-only audit trail writer. Every other tool and mutating route calls
this so the SSE timeline and staff audit view have a full record.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import AuditEvent


def write_audit(
    db: Session,
    actor_id: int | None,
    action: str,
    entity_type: str,
    entity_id: int | None,
    metadata: dict | None = None,
) -> None:
    """Add one AuditEvent row to the session.

    Only flushes, never commits: the caller owns the transaction boundary
    so the audit row lands atomically with whatever it documents.
    """
    db.add(
        AuditEvent(
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            metadata_json=metadata,
        )
    )
    db.flush()
