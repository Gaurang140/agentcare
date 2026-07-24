"""SSE first-chunk smoke test for GET /api/workflows/{id}/events.

Uses a screened emergency request (workflow_service.create_run) rather than
a full graph run: it's synchronous, needs no fake LLM, and lands its own
"workflow.escalated_emergency" AuditEvent plus a terminal status - which is
exactly what the very first poll iteration of the SSE generator needs to
have something to stream immediately, with no 1s wait involved.
"""

from __future__ import annotations

import json

import pytest

from app.api.routes_workflows import run_workflow_background
from app.config import settings
from app.models import AuditEvent, User
from app.services import workflow_service


@pytest.fixture(autouse=True)
def _isolated_checkpointer(tmp_path, monkeypatch):
    """A fresh checkpoint file per test - see test_graph_e2e.py's fixture of
    the same name for why. Only the failed-run tests below invoke the graph,
    but the module-level graph singleton is process-wide, so the fixture is
    autouse here as it is there."""
    monkeypatch.setattr(settings, "checkpoint_db_path", str(tmp_path / "checkpoints.db"))
    workflow_service.close_graph()
    yield
    workflow_service.close_graph()


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


# --- Patient-safe event metadata ---------------------------------------------
# Every specialist node writes {"error": str(exc)} into its exit-audit metadata,
# and the portal timeline renders that metadata verbatim in an expandable view.
# The rows keep the detail for staff and for the log; the patient's stream must
# not carry it.

# Deliberately shaped like something that would hurt to leak, and unlike
# anything the request text itself contains.
_CRASH_MARKER = "psycopg-conn-refused-at-10.1.2.3"


def _routing_failure_script() -> list:
    """Coordinator routes, the routing node's LLM call blows up (routing's own
    except writes `{"error": str(exc)}` into its "agent.routing.completed"
    audit row), the coordinator runs once more and the graph forces escalate
    off the error - the run ends terminal, so the stream closes on its first
    poll and every read below returns immediately."""
    return [
        {"next_step": "route_department", "reasoning": "nothing routed yet"},
        RuntimeError(_CRASH_MARKER),
        {"next_step": "escalate", "reasoning": "routing failed"},
    ]


def _failed_run_id(patient_client) -> int:
    resp = patient_client.post(
        "/api/requests", data={"text": "I need a cardiology appointment next week"}
    )
    assert resp.status_code == 202, resp.text
    workflow_id = resp.json()["workflow_id"]
    run_workflow_background(workflow_id, [])
    return workflow_id


def _streamed_metadata(test_client, workflow_id: int) -> list[dict]:
    """Every `data:` payload the terminal stream emits, decoded down to its
    metadata dict. The closing `event: done` frame carries `{}` and drops out
    here, having no metadata key."""
    response = test_client.get(f"/api/workflows/{workflow_id}/events")
    assert response.status_code == 200, response.text
    payloads = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    return [payload["metadata"] for payload in payloads if "metadata" in payload]


def test_stream_hides_raw_exception_text_from_the_patient(
    patient_client, db_session, fake_llm
):
    fake_llm(_routing_failure_script())
    workflow_id = _failed_run_id(patient_client)

    db_session.expire_all()
    stored = [
        event.metadata_json or {}
        for event in db_session.query(AuditEvent)
        .filter_by(entity_type="workflow_run", entity_id=workflow_id)
        .all()
    ]
    # The rows themselves keep the detail; only the stream is masked.
    assert any(_CRASH_MARKER in str(metadata.get("error", "")) for metadata in stored)

    streamed = _streamed_metadata(patient_client, workflow_id)
    assert streamed, "expected the terminal stream to emit at least one event"
    assert all("error" not in metadata for metadata in streamed)
    assert all(_CRASH_MARKER not in json.dumps(metadata) for metadata in streamed)


def test_staff_still_see_the_raw_exception_text_on_the_same_stream(
    patient_client, independent_staff_client, fake_llm
):
    """Staff read the same route as the portal, so masking has to be
    role-gated rather than applied to every caller's payload."""
    fake_llm(_routing_failure_script())
    workflow_id = _failed_run_id(patient_client)

    streamed = _streamed_metadata(independent_staff_client, workflow_id)
    errors = [metadata["error"] for metadata in streamed if "error" in metadata]

    assert errors
    assert any(_CRASH_MARKER in error for error in errors)
