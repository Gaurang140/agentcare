"""SSE timeline for a WorkflowRun: polls AuditEvent every second, heartbeats
every 15s while nothing new has happened, and closes with a final
`event: done` once the run reaches a terminal status.

The generator opens its own db session per poll (rather than holding the
route's injected session open for the whole stream) so it always sees
whatever another session - the background task executing the graph, the
scheduler jobs - has since committed; a single long-held session/transaction
would keep reading its original snapshot instead.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.auth.dependencies import ensure_owner_or_staff, get_current_user
from app.db import session as db_session_module
from app.db.session import get_db
from app.exceptions import NotFoundError
from app.models import AuditEvent, User, WorkflowRun

router = APIRouter(prefix="/workflows", tags=["workflows"])

_POLL_SECONDS = 1
_HEARTBEAT_SECONDS = 15
_TERMINAL_STATUSES = {"completed", "failed", "escalated"}


def _patient_metadata(metadata: dict | None) -> dict | None:
    """Drop the "error" key every specialist node's except branch writes into
    its exit-audit metadata (`{"error": str(exc)}`): raw exception text is
    staff-facing, and the portal timeline renders this metadata verbatim in
    an expandable view. The AuditEvent row keeps the full detail - staff read
    it from this same stream, ops from the log line the node wrote alongside."""
    if not isinstance(metadata, dict) or "error" not in metadata:
        return metadata
    return {key: value for key, value in metadata.items() if key != "error"}


def _serialize(event: AuditEvent, *, is_staff: bool) -> dict:
    return {
        "id": event.id,
        "action": event.action,
        "entity_type": event.entity_type,
        "entity_id": event.entity_id,
        "metadata": event.metadata_json if is_staff else _patient_metadata(event.metadata_json),
        "created_at": event.created_at.isoformat() if isinstance(event.created_at, datetime) else None,
    }


async def _event_stream(workflow_run_id: int, *, is_staff: bool):
    last_id = 0
    last_heartbeat = time.monotonic()

    while True:
        # Looked up at call time (not bound at import time) so the test
        # suite's db override, which patches this module attribute, reaches
        # this long-lived generator too - see routes_workflows.py's
        # run_workflow_background for the same reasoning.
        db = db_session_module.SessionLocal()
        try:
            new_events = (
                db.query(AuditEvent)
                .filter(
                    AuditEvent.entity_type == "workflow_run",
                    AuditEvent.entity_id == workflow_run_id,
                    AuditEvent.id > last_id,
                )
                .order_by(AuditEvent.id)
                .all()
            )
            for event in new_events:
                last_id = event.id
                yield f"data: {json.dumps(_serialize(event, is_staff=is_staff))}\n\n"

            run = db.get(WorkflowRun, workflow_run_id)
            terminal = run is not None and run.status in _TERMINAL_STATUSES
        finally:
            db.close()

        if terminal:
            yield "event: done\ndata: {}\n\n"
            return

        now = time.monotonic()
        if now - last_heartbeat >= _HEARTBEAT_SECONDS:
            yield ": ping\n\n"
            last_heartbeat = now

        await asyncio.sleep(_POLL_SECONDS)


@router.get("/{workflow_id}/events")
def workflow_events(
    workflow_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> StreamingResponse:
    run = db.get(WorkflowRun, workflow_id)
    if run is None:
        raise NotFoundError(f"WorkflowRun {workflow_id} not found")
    ensure_owner_or_staff(current_user, run.patient_id, db)

    # Staff and the owning patient both read this stream, so the internal
    # detail is gated on the role here rather than split across two
    # endpoints - same shape as get_workflow in routes_workflows.py.
    return StreamingResponse(
        _event_stream(workflow_id, is_staff=current_user.role == "staff"),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
