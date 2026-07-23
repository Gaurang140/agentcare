"""SSE first-chunk smoke test for GET /api/workflows/{id}/events.

Uses a screened emergency request (workflow_service.create_run) rather than
a full graph run: it's synchronous, needs no fake LLM, and lands its own
"workflow.escalated_emergency" AuditEvent plus a terminal status - which is
exactly what the very first poll iteration of the SSE generator needs to
have something to stream immediately, with no 1s wait involved.
"""

from __future__ import annotations

from app.models import User
from app.services import workflow_service


def test_sse_stream_headers_and_first_event(patient_client, db_session):
    user = db_session.query(User).filter_by(email="patient@example.com").first()
    assert user is not None

    run = workflow_service.create_run(db_session, user, "severe chest pain right now")
    assert run.status == "escalated"

    with patient_client.stream("GET", f"/api/workflows/{run.id}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"

        first_chunk = next(response.iter_bytes())
        assert first_chunk.startswith(b"data:")


def test_sse_stream_requires_ownership(
    patient_client, independent_staff_client, db_session, other_patient_doc
):
    from app.models import WorkflowRun

    run = WorkflowRun(
        user_id=other_patient_doc.patient_id,
        patient_id=other_patient_doc.patient_id,
        thread_id=f"wf-sse-other-{other_patient_doc.id}",
        request_text="not mine",
        status="completed",
    )
    db_session.add(run)
    db_session.commit()

    assert patient_client.get(f"/api/workflows/{run.id}/events").status_code == 403

    with independent_staff_client.stream("GET", f"/api/workflows/{run.id}/events") as response:
        assert response.status_code == 200
