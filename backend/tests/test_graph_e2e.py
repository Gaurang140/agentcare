"""End-to-end TDD for the LangGraph workflow: app.agents.graph.build_graph,
driven through app.services.workflow_service.start_workflow/resume_workflow.

Every LLM call in a run is scripted via the shared `fake_llm` fixture (see
conftest.py) in the exact order the graph will call it. The coordinator's own
next_step decisions ARE the script here, so the graph's path through the six
nodes is fully deterministic and known in advance - never inferred from what
the real system prompts would make an LLM choose.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Appointment, AuditEvent, Department, Escalation, Reminder, User
from app.services import workflow_service
from app.tools.appointment_tools import get_available_slots

_SLOT_WINDOW_DAYS = 14


@pytest.fixture(autouse=True)
def _isolated_checkpointer(tmp_path, monkeypatch):
    """A fresh checkpoint file per test: WorkflowRun ids restart at 1 in
    every test's own fresh in-memory `db` fixture, so without this,
    thread_id "wf-1" in one test would collide with "wf-1" in another test
    inside the one checkpoint file the module-level graph singleton keeps
    open for the whole process. Resetting the cached graph before and
    after each test makes it rebuild against the fresh path."""
    monkeypatch.setattr(settings, "checkpoint_db_path", str(tmp_path / "checkpoints.db"))
    workflow_service.close_graph()
    yield
    workflow_service.close_graph()


def _patient_user(db: Session) -> User:
    user = db.query(User).filter_by(email="patient@demo.agentcare.local").first()
    assert user is not None
    return user


def _cardiology_id(db: Session) -> int:
    dept = db.query(Department).filter_by(name="Cardiology").first()
    assert dept is not None
    return dept.id


def _first_free_slot_id(db: Session, department_id: int) -> int:
    today = date.today()
    slots = get_available_slots(db, department_id, today, today + timedelta(days=_SLOT_WINDOW_DAYS))
    assert slots
    return slots[0]["slot_id"]


def _full_booking_script(slot_id: int) -> list[dict]:
    """The 9 scripted LLM responses for one full booking run, in call
    order: coordinator, routing, coordinator, appointment, coordinator,
    coordinator, followup, coordinator, safety. (The document node runs
    between the "handle_documents" and "schedule_followup" coordinator
    calls but makes no LLM call itself - no uploaded documents to
    classify.)"""
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


def test_full_booking_workflow_persists_everything(db, seeded, fake_llm):
    dept_id = _cardiology_id(db)
    slot_id = _first_free_slot_id(db, dept_id)
    script = _full_booking_script(slot_id)
    client = fake_llm(script)

    run = workflow_service.start_workflow(
        db, _patient_user(db), "I need a cardiology appointment next week", []
    )

    assert run.status == "completed"
    assert db.query(Appointment).filter_by(patient_id=1, status="confirmed").count() == 1
    assert db.query(Reminder).count() >= 1
    assert db.query(AuditEvent).filter_by(entity_type="workflow_run").count() >= 4
    assert run.state["department_name"] == "Cardiology"
    assert len(client.chat.completions.calls) == len(script)


def test_emergency_short_circuits_to_escalation(db, seeded, fake_llm):
    client = fake_llm([])

    run = workflow_service.start_workflow(db, _patient_user(db), "severe chest pain right now", [])

    assert run.status == "escalated"
    assert db.query(Escalation).filter_by(severity="emergency").count() == 1
    assert client.chat.completions.calls == []


def test_medical_refusal_short_circuits_without_graph(db, seeded, fake_llm):
    client = fake_llm([])

    run = workflow_service.start_workflow(
        db, _patient_user(db), "what medication should i take for this", []
    )

    assert run.status == "completed"
    assert run.state["final_response"]
    assert client.chat.completions.calls == []


def test_node_failure_escalates_and_marks_run_failed(db, seeded, fake_llm):
    """The 3rd LLM call (the coordinator's second decision) raises. The
    coordinator node itself catches it and reports state["error"] instead
    of crashing the graph; the graph's own routing then forces escalate
    regardless of the now-stale plan, so the run still ends cleanly -
    failed, with its own agent_failure escalation, never a raised
    exception out of start_workflow (never a 500 upstream)."""
    client = fake_llm(
        [
            {"next_step": "route_department", "reasoning": "nothing routed yet"},
            {"intent": "book", "department": "Cardiology", "confidence": 0.95, "reason": "routing"},
            RuntimeError("llm endpoint down"),
        ]
    )

    run = workflow_service.start_workflow(
        db, _patient_user(db), "I need a cardiology appointment next week", []
    )

    assert run.status == "failed"
    assert db.query(Escalation).filter_by(severity="agent_failure").count() == 1
    assert len(client.chat.completions.calls) == 3


def test_resume_after_completion_is_idempotent(db, seeded, fake_llm):
    """Crash-resume in reverse: nothing to crash-recover from once a run
    has reached completed, so invoke(None, config) must be a no-op - not
    re-run any node, not book a second appointment or write a second
    batch of reminders. The scripted fake LLM enforces this by construction:
    it has exactly 9 responses queued, one per call in the first run: any
    extra call from a resumed node re-executing would raise "called more
    times than scripted"."""
    dept_id = _cardiology_id(db)
    slot_id = _first_free_slot_id(db, dept_id)
    script = _full_booking_script(slot_id)
    client = fake_llm(script)

    run = workflow_service.start_workflow(
        db, _patient_user(db), "I need a cardiology appointment next week", []
    )
    assert run.status == "completed"
    appt_count = db.query(Appointment).filter_by(patient_id=1, status="confirmed").count()
    reminder_count = db.query(Reminder).count()
    assert appt_count == 1

    resumed = workflow_service.resume_workflow(db, run.id)

    assert resumed.id == run.id
    assert resumed.status == "completed"
    assert db.query(Appointment).filter_by(patient_id=1, status="confirmed").count() == appt_count
    assert db.query(Reminder).count() == reminder_count
    assert len(client.chat.completions.calls) == len(script)


def test_resume_unknown_workflow_run_raises_not_found(db, seeded, fake_llm):
    from app.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        workflow_service.resume_workflow(db, 999_999)


def test_graph_exception_creates_escalation_and_marks_run_failed(db, seeded, fake_llm, monkeypatch):
    """A genuine exception escaping graph.invoke() itself (a real bug, not
    a node-level LLM/tool failure - those are already caught inside each
    node and never raise) must never surface as a 500: workflow_service's
    own safety net catches it, opens its own agent_failure escalation, and
    marks the run failed."""
    fake_llm([])  # the crash happens before any node would run

    class _ExplodingGraph:
        def invoke(self, *args, **kwargs):
            raise RuntimeError("boom: unexpected graph bug")

    monkeypatch.setattr(workflow_service, "get_graph", lambda: _ExplodingGraph())

    run = workflow_service.start_workflow(
        db, _patient_user(db), "I need a cardiology appointment next week", []
    )

    assert run.status == "failed"
    assert run.current_step == "crashed"
    escalation = (
        db.query(Escalation)
        .filter_by(workflow_run_id=run.id, severity="agent_failure")
        .first()
    )
    assert escalation is not None
