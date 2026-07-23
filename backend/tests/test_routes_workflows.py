"""TDD for POST /api/requests, GET /api/workflows/{id}, and POST
/api/workflows/{id}/resume: the HTTP surface over workflow_service, exercised
through the shared session-scoped `client`/`patient_client` db (not the
throwaway `db`/`seeded` fixtures the tools-layer tests use), since these are
route-level tests.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.api.routes_workflows import run_workflow_background
from app.config import settings
from app.db.seed import seed
from app.models import Appointment, AuditEvent, Department, PatientDocument, User, WorkflowRun
from app.services import workflow_service
from app.tools.appointment_tools import get_available_slots

_SLOT_WINDOW_DAYS = 14


@pytest.fixture(autouse=True)
def _isolated_checkpointer(tmp_path, monkeypatch):
    """A fresh checkpoint file per test - see test_graph_e2e.py's fixture of
    the same name for why: otherwise every test in this module would share
    thread_id collisions through the one checkpoint file the module-level
    graph singleton keeps open for the whole process."""
    monkeypatch.setattr(settings, "checkpoint_db_path", str(tmp_path / "checkpoints.db"))
    workflow_service.close_graph()
    yield
    workflow_service.close_graph()


def _cardiology_slot(db_session) -> int:
    """A free Cardiology slot that has never had any Appointment (confirmed
    or cancelled) against it.

    Appointment.slot_id is unique, but cancel_appointment/reschedule_
    appointment only flip AppointmentSlot.status back to "free" - they
    never remove the old Appointment row. So a slot this whole shared-db
    test session has ever cancelled or rescheduled away from still shows up
    as "free" via get_available_slots, yet a fresh book_appointment against
    it raises a raw sqlite3.IntegrityError instead of the tool's own
    ConflictError (a real, pre-existing gap in Task 3/7's model - see the
    Task 12 report). Filtering those out here keeps this file's tests
    independent of test_routes_patient.py's cancel/reschedule tests
    (test order is otherwise incidental) without touching that model.
    """
    seed(db_session)
    dept = db_session.query(Department).filter_by(name="Cardiology").first()
    today = date.today()
    slots = get_available_slots(
        db_session, dept.id, today, today + timedelta(days=_SLOT_WINDOW_DAYS), limit=200
    )
    ever_used = {row[0] for row in db_session.query(Appointment.slot_id).all()}
    for slot in slots:
        if slot["slot_id"] not in ever_used:
            return slot["slot_id"]
    raise AssertionError("no untouched free Cardiology slot available")


def _full_booking_script(slot_id: int) -> list[dict]:
    """Same 9-call script as test_graph_e2e.py's _full_booking_script."""
    return [
        {"next_step": "route_department", "reasoning": "nothing routed yet"},
        {"intent": "book", "department": "Cardiology", "confidence": 0.95, "reason": "routing"},
        {"next_step": "handle_appointment", "reasoning": "department resolved"},
        {"slot_id": slot_id, "reason": "earliest match"},
        {"next_step": "handle_documents", "reasoning": "appointment booked"},
        {"next_step": "schedule_followup", "reasoning": "documents checked"},
        {
            "reminders": [{"type": "appointment", "days_before_appointment": 1}],
            "followup_days_after": 14,
        },
        {"next_step": "finalize", "reasoning": "reminders scheduled"},
        {"safe": True, "violations": [], "rewritten": "Your appointment is confirmed."},
    ]


def test_request_creates_running_workflow_then_background_completes_it(
    patient_client, db_session, fake_llm
):
    slot_id = _cardiology_slot(db_session)
    script = _full_booking_script(slot_id)
    llm = fake_llm(script)

    resp = patient_client.post(
        "/api/requests", data={"text": "I need a cardiology appointment next week"}
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    # The response is built right after create_run, before any graph/LLM
    # call - it must always be "running" for a request the safety screen
    # allows through, regardless of whether TestClient's own background-task
    # timing has already kicked in underneath.
    assert body["status"] == "running"
    workflow_id = body["workflow_id"]

    # Deterministic per the brief: call the background function directly
    # rather than depend on TestClient's background-task timing. Safe even
    # if TestClient already ran it - execute_workflow no-ops on a run that
    # is no longer "running".
    run_workflow_background(workflow_id, [])

    detail = patient_client.get(f"/api/workflows/{workflow_id}")
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["status"] == "completed"
    assert payload["appointment"] is not None
    assert payload["appointment"]["department"] == "Cardiology"

    assert len(llm.chat.completions.calls) == len(script)


def test_emergency_request_short_circuits_with_no_background_execution(patient_client, fake_llm):
    llm = fake_llm([])

    resp = patient_client.post("/api/requests", data={"text": "severe chest pain right now"})

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "escalated"
    assert llm.chat.completions.calls == []


def test_upload_rejects_disallowed_extension(patient_client, db_session):
    before = db_session.query(PatientDocument).count()

    resp = patient_client.post(
        "/api/requests",
        data={"text": "please see attached"},
        files={"files": ("malware.exe", b"MZ fake binary", "application/octet-stream")},
    )

    assert resp.status_code == 400
    assert db_session.query(PatientDocument).count() == before


def test_upload_rejects_oversized_file(patient_client, db_session):
    before = db_session.query(PatientDocument).count()
    oversized = b"a" * (10 * 1024 * 1024 + 1)

    resp = patient_client.post(
        "/api/requests",
        data={"text": "please see attached"},
        files={"files": ("scan.pdf", oversized, "application/pdf")},
    )

    assert resp.status_code == 400
    assert db_session.query(PatientDocument).count() == before


def test_upload_rejects_one_bad_file_even_with_a_good_one_alongside(patient_client, db_session):
    """One bad file in a multi-file upload must reject the whole request -
    never leaving the good file's document stored on its own."""
    before = db_session.query(PatientDocument).count()

    resp = patient_client.post(
        "/api/requests",
        data={"text": "please see attached"},
        files=[
            ("files", ("good.txt", b"hello", "text/plain")),
            ("files", ("bad.exe", b"MZ", "application/octet-stream")),
        ],
    )

    assert resp.status_code == 400
    assert db_session.query(PatientDocument).count() == before


def test_get_workflow_denied_for_non_owner_non_staff(
    patient_client, independent_staff_client, db_session, other_patient_doc
):
    run = WorkflowRun(
        user_id=other_patient_doc.patient_id,
        patient_id=other_patient_doc.patient_id,
        thread_id=f"wf-other-{other_patient_doc.id}",
        request_text="a request belonging to someone else",
        status="completed",
    )
    db_session.add(run)
    db_session.commit()

    assert patient_client.get(f"/api/workflows/{run.id}").status_code == 403
    assert independent_staff_client.get(f"/api/workflows/{run.id}").status_code == 200


def test_get_workflow_missing_id_is_404(patient_client):
    assert patient_client.get("/api/workflows/999999").status_code == 404


def test_resume_route_returns_status_and_writes_audit_with_real_actor(
    patient_client, db_session, fake_llm
):
    slot_id = _cardiology_slot(db_session)
    script = _full_booking_script(slot_id)
    fake_llm(script)

    user = db_session.query(User).filter_by(email="patient@example.com").first()
    run = workflow_service.start_workflow(
        db_session, user, "I need a cardiology appointment next week", []
    )
    assert run.status == "completed"

    resp = patient_client.post(f"/api/workflows/{run.id}/resume")
    assert resp.status_code == 200
    assert resp.json() == {"id": run.id, "status": "completed"}

    audit = (
        db_session.query(AuditEvent)
        .filter_by(action="workflow.resumed", entity_type="workflow_run", entity_id=run.id)
        .first()
    )
    assert audit is not None
    assert audit.actor_id == user.id
