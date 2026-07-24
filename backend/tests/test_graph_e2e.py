"""End-to-end TDD for the LangGraph workflow: app.agents.graph.build_graph,
driven through app.services.workflow_service.start_workflow/resume_workflow.

Every LLM call in a run is scripted via the shared `fake_llm` fixture (see
conftest.py) in the exact order the graph will call it. The coordinator's own
next_step decisions ARE the script here, so the graph's path through the six
nodes is fully deterministic and known in advance - never inferred from what
the real system prompts would make an LLM choose.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.agents import followup
from app.agents.responses import staff_decision_response
from app.api.routes_workflows import _patient_state
from app.config import settings
from app.models import (
    Appointment,
    AuditEvent,
    Department,
    Escalation,
    PatientDocument,
    Reminder,
    User,
    WorkflowRun,
)
from app.services import workflow_service
from app.tools.appointment_tools import get_available_slots
from app.tools.escalation_tools import resolve_escalation

_SLOT_WINDOW_DAYS = 14

# Seeded user 3 is the staff account (see tests/test_responses.py).
_REVIEWER_ID = 3


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
    user = db.query(User).filter_by(email="patient@agentcare-demo.com").first()
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


def _uncertainty_pause_script() -> list[dict]:
    """The three scripted calls that park a run at the escalate node's
    interrupt: the coordinator routes, routing gives up on an ambiguous
    request and opens its own uncertainty escalation, and the coordinator
    burns one more decision before the graph's short-circuit sends the run
    to escalate regardless of what it picked."""
    return [
        {"next_step": "route_department", "reasoning": "nothing routed yet"},
        {"intent": "other", "department": None, "confidence": 0.4, "reason": "ambiguous"},
        {"next_step": "finalize", "reasoning": "coordinator missed the escalation"},
    ]


def _staff_decision(db, run, *, approved: bool, note: str):
    """The two steps routes_staff.resolve_escalation_route takes, in its
    order: record the decision on the escalation row, then hand it to the
    graph waiting at the interrupt."""
    escalation = (
        db.query(Escalation)
        .filter_by(workflow_run_id=run.id)
        .order_by(Escalation.id.desc())
        .first()
    )
    assert escalation is not None
    resolve_escalation(db, escalation.id, _REVIEWER_ID, approved, note)
    return workflow_service.resume_with_decision(
        db, run.id, approved=approved, note=note, reviewer_id=_REVIEWER_ID
    )


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
    regardless of the now-stale plan, so the run still ends cleanly - with
    its own agent_failure escalation and a human, never a raised exception
    out of start_workflow (never a 500 upstream). It reaches the human
    first: the run parks at the escalate node's interrupt and only the
    staff decision closes it, failed."""
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
    assert run.status == "waiting_approval"

    resumed = _staff_decision(db, run, approved=True, note="taking this one by hand")

    assert resumed.status == "failed"
    assert db.query(Escalation).filter_by(severity="agent_failure").count() == 1
    assert len(client.chat.completions.calls) == 3


def test_specialist_escalation_stops_the_run_at_the_escalate_node(db, seeded, fake_llm):
    """Routing escalates on an intent outside the supported administrative
    ones ("other") and the coordinator's next decision ignores it
    ("finalize"). The graph must not take that decision:
    an escalation_id on the state routes straight to the escalate node, so
    the safety agent never runs and the escalate node reuses the row routing
    already opened instead of filing a second one for the same run. The
    answer the patient ends up with comes from the staff decision, not from
    the specialist that gave up."""
    client = fake_llm(
        [
            {"next_step": "route_department", "reasoning": "nothing routed yet"},
            {"intent": "other", "department": None, "confidence": 0.4, "reason": "ambiguous"},
            {"next_step": "finalize", "reasoning": "coordinator missed the escalation"},
        ]
    )

    run = workflow_service.start_workflow(
        db, _patient_user(db), "something about an appointment, maybe", []
    )
    assert run.status == "waiting_approval"

    resumed = _staff_decision(db, run, approved=False, note="nothing bookable here")

    assert resumed.status == "escalated"
    assert db.query(Escalation).count() == 1
    assert resumed.state["final_response"] == staff_decision_response(db, resumed.patient_id, False)
    assert db.query(AuditEvent).filter_by(action="agent.safety.completed").count() == 0
    assert len(client.chat.completions.calls) == 3


def test_appointment_escalation_is_not_filed_twice(db, seeded, fake_llm):
    """The appointment agent gives up after two invented slot ids and files
    its own agent_failure escalation without setting state["error"]. The run
    stops there for staff, nothing is booked, and the escalate node reuses
    that row - so the staff queue shows one escalation for the run, still
    carrying the severity the appointment agent chose. The node writes its
    exit audit once, on the far side of the interrupt, even though the
    resumed node re-executes everything above it."""
    client = fake_llm(
        [
            {"next_step": "route_department", "reasoning": "nothing routed yet"},
            {"intent": "book", "department": "Cardiology", "confidence": 0.95, "reason": "routing"},
            {"next_step": "handle_appointment", "reasoning": "department resolved"},
            {"slot_id": 999_999, "reason": "invented id"},
            {"slot_id": 999_998, "reason": "invented again"},
            {"next_step": "handle_documents", "reasoning": "coordinator missed the escalation"},
        ]
    )

    run = workflow_service.start_workflow(
        db, _patient_user(db), "I need a cardiology appointment next week", []
    )
    assert run.status == "waiting_approval"

    resumed = _staff_decision(db, run, approved=False, note="no slot to give")

    assert resumed.status == "escalated"
    assert db.query(Appointment).count() == 0
    escalations = db.query(Escalation).all()
    assert [e.severity for e in escalations] == ["agent_failure"]
    audit = db.query(AuditEvent).filter_by(action="agent.escalate.completed").one()
    assert audit.metadata_json == {"severity": "agent_failure"}
    assert len(client.chat.completions.calls) == 6


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


def test_crash_between_a_node_commit_and_its_checkpoint_does_not_duplicate_work(
    db, seeded, fake_llm, monkeypatch
):
    """The crash-resume case that actually re-executes a node, unlike the
    test above it.

    Every specialist commits its rows before LangGraph writes the checkpoint
    that records the node ran, so a process killed in that window resumes
    with the work done but unrecorded, and re-runs the node from its first
    line. The followup node stands in for the kill here: it does its real
    work, commits the reminder batch, and then raises once, exactly as a
    process death between the two would look to the resumed run.

    The resumed node has to find that batch and skip the creation block. The
    9-response script proves it without counting rows twice: the run needs
    all 9 calls to finish, and a re-planning followup node would take the
    coordinator's finalize response as its reminder plan and derail the run.
    """
    dept_id = _cardiology_id(db)
    slot_id = _first_free_slot_id(db, dept_id)
    script = _full_booking_script(slot_id)
    client = fake_llm(script)

    real_run = followup.run
    passes = {"n": 0}

    def _commit_then_crash(state, session):
        result = real_run(state, session)
        passes["n"] += 1
        if passes["n"] == 1:
            raise RuntimeError("process killed after the reminders were committed")
        return result

    monkeypatch.setattr(followup, "run", _commit_then_crash)

    run = workflow_service.start_workflow(
        db, _patient_user(db), "I need a cardiology appointment next week", []
    )

    assert run.status == "failed"
    reminders_before_resume = db.query(Reminder).count()
    assert reminders_before_resume > 0
    assert db.query(Appointment).filter_by(patient_id=1, status="confirmed").count() == 1

    resumed = workflow_service.resume_workflow(db, run.id)

    assert resumed.status == "completed"
    assert passes["n"] == 2, "the resumed run must actually re-execute the killed node"
    assert db.query(Reminder).count() == reminders_before_resume
    assert db.query(Appointment).filter_by(patient_id=1, status="confirmed").count() == 1
    assert len(client.chat.completions.calls) == len(script)


def test_finalize_before_followup_escalates_instead_of_completing_empty(db, seeded, fake_llm):
    """The coordinator's very first decision is "finalize". Its own prompt
    forbids that (every workflow ends with schedule_followup then finalize),
    but a prompt is not enforcement: without the transition guard the graph
    would run the safety agent and mark the run completed while nothing was
    routed, booked or scheduled. The guard sends an out-of-order finalize to
    the escalate node instead, so a human sees the run.

    The safety response stays in the script on purpose: it is what the
    unguarded graph would consume, and leaving it queued proves the safety
    node never ran rather than only that the script ran out."""
    client = fake_llm(
        [
            {"next_step": "finalize", "reasoning": "coordinator skipped the whole workflow"},
            {"safe": True, "violations": [], "rewritten": "All done."},
        ]
    )

    run = workflow_service.start_workflow(
        db, _patient_user(db), "I need a cardiology appointment next week", []
    )
    assert run.status == "waiting_approval"
    # The escalation the staff queue shows was opened by the escalate node
    # itself here, before the interrupt, so it is durable while the run waits.
    assert [e.severity for e in db.query(Escalation).all()] == ["uncertainty"]

    resumed = _staff_decision(db, run, approved=False, note="agent skipped the workflow")

    assert resumed.status == "escalated"
    assert [e.severity for e in db.query(Escalation).all()] == ["uncertainty"]
    assert resumed.state["final_response"] == staff_decision_response(db, resumed.patient_id, False)
    assert db.query(Appointment).count() == 0
    assert db.query(Reminder).count() == 0
    assert db.query(AuditEvent).filter_by(action="agent.safety.completed").count() == 0
    assert len(client.chat.completions.calls) == 1


def test_finalize_with_unhandled_uploads_escalates(db, seeded, fake_llm):
    """The request carries an upload, so the coordinator prompt's second
    ordering rule applies: handle_documents must run. This coordinator goes
    routing -> appointment -> followup -> finalize and never handles the
    document. The guard blocks that finalize, so the run ends with a staff
    handoff rather than a "completed" answer that quietly ignored the file
    the patient sent."""
    doc = PatientDocument(
        patient_id=1,
        filename="referral.pdf",
        document_type="other",
        checksum="chk-referral",
        storage_ref="local://1/referral.pdf",
        extracted_text="referral letter",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    dept_id = _cardiology_id(db)
    slot_id = _first_free_slot_id(db, dept_id)
    client = fake_llm(
        [
            {"next_step": "route_department", "reasoning": "nothing routed yet"},
            {"intent": "book", "department": "Cardiology", "confidence": 0.95, "reason": "routing"},
            {"next_step": "handle_appointment", "reasoning": "department resolved"},
            {"slot_id": slot_id, "reason": "earliest match"},
            {"next_step": "schedule_followup", "reasoning": "skips the upload"},
            {
                "reminders": [{"type": "appointment", "days_before_appointment": 1}],
                "followup_days_after": 14,
            },
            {"next_step": "finalize", "reasoning": "coordinator forgot the document"},
            {"safe": True, "violations": [], "rewritten": "Your appointment is confirmed."},
        ]
    )

    run = workflow_service.start_workflow(
        db, _patient_user(db), "I need a cardiology appointment next week", [doc.id]
    )
    assert run.status == "waiting_approval"

    resumed = _staff_decision(db, run, approved=False, note="the upload was never read")

    assert resumed.status == "escalated"
    assert [e.severity for e in db.query(Escalation).all()] == ["uncertainty"]
    assert resumed.state.get("documents_result") is None
    assert db.query(AuditEvent).filter_by(action="agent.document.completed").count() == 0
    assert db.query(AuditEvent).filter_by(action="agent.safety.completed").count() == 0
    assert len(client.chat.completions.calls) == 7


# --- Pause and approve (the real human-in-the-loop) --------------------------
# The escalate node calls LangGraph's interrupt(), so a run that reaches it
# stops mid-graph instead of ending. Staff then decide, and only an approved
# uncertainty case picks the work back up: approve on a run whose agent
# already failed, or any rejection, closes with a template.


def test_uncertainty_escalation_pauses_the_run_for_staff(db, seeded, fake_llm):
    """The escalate node hands the case over and stops. Nothing is final
    yet: no answer, no safety pass, and the escalation staff will see is
    still open."""
    client = fake_llm(_uncertainty_pause_script())

    run = workflow_service.start_workflow(
        db, _patient_user(db), "something about an appointment, maybe", []
    )

    assert run.status == "waiting_approval"
    assert run.current_step == "escalate"
    assert run.state.get("final_response") is None
    assert [(e.severity, e.status) for e in db.query(Escalation).all()] == [
        ("uncertainty", "open")
    ]
    assert db.query(AuditEvent).filter_by(action="agent.safety.completed").count() == 0
    assert len(client.chat.completions.calls) == 3


def test_staff_approval_resumes_the_run_and_books_the_appointment(db, seeded, fake_llm):
    """The demo beat: an ambiguous request parks, a staff member approves it
    with a note naming the department, and the run picks up where it stopped
    and books the appointment it was asked for. The note steers the agents
    and lands on the escalation row; it never becomes something the patient
    reads."""
    slot_id = _first_free_slot_id(db, _cardiology_id(db))
    note = "patient means cardiology"
    client = fake_llm(
        [
            *_uncertainty_pause_script(),
            # Everything below runs only because staff approved.
            {"next_step": "route_department", "reasoning": "staff named the department"},
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
    )

    run = workflow_service.start_workflow(
        db, _patient_user(db), "something about an appointment, maybe", []
    )
    assert run.status == "waiting_approval"

    resumed = _staff_decision(db, run, approved=True, note=note)

    assert resumed.status == "completed"
    assert db.query(Appointment).filter_by(patient_id=1, status="confirmed").count() == 1
    # One row for one handoff, kept for the audit trail and now resolved.
    assert [(e.status, e.resolution_note) for e in db.query(Escalation).all()] == [
        ("approved", note)
    ]
    # The note reached the agents as guidance ...
    assert any(note in json.dumps(call) for call in client.chat.completions.calls)
    # ... and reaches the patient through nothing at all: not the answer, and
    # not the state projection the portal renders (routes_workflows).
    assert note not in (resumed.state["final_response"] or "")
    assert note not in json.dumps(_patient_state(resumed.state))
    assert len(client.chat.completions.calls) == 12


def test_approved_run_cannot_book_an_appointment_without_a_department(db, seeded, fake_llm):
    """The one state a resumed run can be in that no ordering rule covered:
    routing has run, so `handle_appointment` looks legal, but routing gave up
    before it resolved a department - it escalated on a booking request with
    no department, which is what put the run in front of staff in the first
    place. Approval clears the block and the coordinator picks
    `handle_appointment` off a state whose department_id is still None.

    The appointment node has nothing to book against there, so the guard has
    to stop the step rather than let the node fail into an agent_failure
    escalation: the case goes back to the same human, still an uncertainty,
    and no half-run booking attempt sits in the audit trail between them.
    """
    client = fake_llm(
        [
            {"next_step": "route_department", "reasoning": "nothing routed yet"},
            {"intent": "book", "department": None, "confidence": 0.95, "reason": "no department"},
            {"next_step": "handle_appointment", "reasoning": "coordinator missed the escalation"},
            # The decision after the approval, off a state that still has no
            # department: the one this test is about.
            {"next_step": "handle_appointment", "reasoning": "routing ran, so this looks legal"},
        ]
    )

    run = workflow_service.start_workflow(
        db, _patient_user(db), "I want to book something next week", []
    )
    assert run.status == "waiting_approval"
    assert run.state.get("intent") == "book"
    assert run.state.get("department_id") is None

    resumed = _staff_decision(db, run, approved=True, note="please book this one")

    assert resumed.status == "waiting_approval"
    assert db.query(AuditEvent).filter_by(action="agent.appointment.completed").count() == 0
    assert db.query(Appointment).count() == 0
    # Back to a human as the same kind of case, not as a failed agent.
    assert [e.severity for e in db.query(Escalation).all()] == ["uncertainty", "uncertainty"]
    assert len(client.chat.completions.calls) == 4


def test_staff_rejection_closes_the_run_with_the_template(db, seeded, fake_llm):
    """A rejected case does not go back to the agents. It ends on the
    deterministic rejection template, with the staff member's own words kept
    on the escalation row where only staff read them."""
    client = fake_llm(_uncertainty_pause_script())
    note = "not eligible"

    run = workflow_service.start_workflow(
        db, _patient_user(db), "something about an appointment, maybe", []
    )
    assert run.status == "waiting_approval"

    resumed = _staff_decision(db, run, approved=False, note=note)

    assert resumed.status == "escalated"
    assert resumed.state["final_response"] == staff_decision_response(db, resumed.patient_id, False)
    assert note not in resumed.state["final_response"]
    assert db.query(Appointment).count() == 0
    assert [(e.status, e.resolution_note) for e in db.query(Escalation).all()] == [
        ("rejected", note)
    ]
    # Nothing ran after the decision: the three calls are the ones from before it.
    assert len(client.chat.completions.calls) == 3


def test_agent_failure_escalation_closes_even_when_staff_approves(db, seeded, fake_llm):
    """Approval means "carry on" only where there is something to carry on
    with. This run's routing agent failed, so the state carries an error and
    no amount of staff approval can hand it back to the agents: it closes on
    the template and stays failed, with the case in a human's hands."""
    client = fake_llm(
        [
            {"next_step": "route_department", "reasoning": "nothing routed yet"},
            RuntimeError("llm endpoint down"),
            {"next_step": "escalate", "reasoning": "routing failed"},
        ]
    )
    note = "called the patient, handled by phone"

    run = workflow_service.start_workflow(
        db, _patient_user(db), "I need a cardiology appointment next week", []
    )
    assert run.status == "waiting_approval"

    resumed = _staff_decision(db, run, approved=True, note=note)

    assert resumed.status == "failed"
    assert resumed.state["final_response"] == staff_decision_response(db, resumed.patient_id, True)
    assert note not in resumed.state["final_response"]
    assert [e.severity for e in db.query(Escalation).all()] == ["agent_failure"]
    assert db.query(Appointment).count() == 0
    assert len(client.chat.completions.calls) == 3


def test_second_decision_on_the_same_run_never_invokes_the_graph_twice(
    db, seeded, fake_llm, monkeypatch
):
    """Two staff members deciding the same case at once must not resume one
    thread twice: two resumed runs off one checkpoint can book twice.

    The loser is simulated the way the race actually produces it. It read the
    run while the row still said `waiting_approval`, and by the time it acts
    the winner has claimed the row. `resume_with_decision` claims the run with
    a conditional UPDATE rather than trusting that read, so the loser's claim
    matches no row and the graph is never invoked a second time.
    """
    fake_llm(_uncertainty_pause_script())
    run = workflow_service.start_workflow(
        db, _patient_user(db), "something about an appointment, maybe", []
    )
    assert run.status == "waiting_approval"

    # The winner's claim, written straight to the row: the loser's session
    # keeps the run cached exactly as it read it a moment earlier.
    db.execute(
        update(WorkflowRun).where(WorkflowRun.id == run.id).values(status="running"),
        execution_options={"synchronize_session": False},
    )
    assert db.get(WorkflowRun, run.id).status == "waiting_approval"

    invoked: list = []

    class _CountingGraph:
        def invoke(self, graph_input, config):
            invoked.append(graph_input)
            return {}

    monkeypatch.setattr(workflow_service, "get_graph", lambda: _CountingGraph())

    workflow_service.resume_with_decision(
        db, run.id, approved=True, note="mine", reviewer_id=_REVIEWER_ID
    )

    assert invoked == []


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
