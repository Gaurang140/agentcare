"""TDD for the two scheduler job bodies (plain functions, called directly -
no APScheduler involved here) and the scheduler's own TESTING guard.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import AuditEvent, Escalation, Reminder, WorkflowRun
from app.scheduler import start_scheduler, stop_scheduler
from app.services.workflow_service import escalate_stalled_workflows
from app.tools.followup_tools import send_due_reminders


def _naive_utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_send_due_reminders_marks_sent_and_writes_audit(db, seeded):
    reminder = Reminder(
        patient_id=1,
        appointment_id=None,
        reminder_type="appointment",
        scheduled_at=_naive_utcnow() - timedelta(minutes=5),
        sent=False,
    )
    db.add(reminder)
    db.commit()

    result = send_due_reminders(db)

    assert result == {"sent_count": 1, "reminder_ids": [reminder.id]}
    db.refresh(reminder)
    assert reminder.sent is True

    audit = db.query(AuditEvent).filter_by(action="reminder.sent", entity_id=reminder.id).first()
    assert audit is not None
    assert audit.entity_type == "reminder"


def test_send_due_reminders_ignores_future_and_already_sent(db, seeded):
    future = Reminder(
        patient_id=1,
        reminder_type="appointment",
        scheduled_at=_naive_utcnow() + timedelta(days=1),
        sent=False,
    )
    already_sent = Reminder(
        patient_id=1,
        reminder_type="appointment",
        scheduled_at=_naive_utcnow() - timedelta(minutes=5),
        sent=True,
    )
    db.add_all([future, already_sent])
    db.commit()

    result = send_due_reminders(db)

    assert result == {"sent_count": 0, "reminder_ids": []}


def test_escalate_stalled_workflows_marks_escalated_and_creates_escalation(db, seeded):
    stale_run = WorkflowRun(
        user_id=1,
        patient_id=1,
        thread_id="wf-stale",
        request_text="stuck request",
        status="running",
    )
    db.add(stale_run)
    db.commit()
    # Explicitly set updated_at back 31 minutes: an explicit value in the
    # flush wins over the onupdate=func.now() server default, so this
    # sticks instead of being overwritten back to "now".
    stale_run.updated_at = _naive_utcnow() - timedelta(minutes=31)
    db.commit()

    result = escalate_stalled_workflows(db)

    assert result["escalated_count"] == 1
    assert result["workflow_run_ids"] == [stale_run.id]
    db.refresh(stale_run)
    assert stale_run.status == "escalated"

    escalation = db.query(Escalation).filter_by(workflow_run_id=stale_run.id).first()
    assert escalation is not None
    assert escalation.severity == "agent_failure"
    assert escalation.status == "open"


def test_escalate_stalled_workflows_ignores_recent_and_non_running(db, seeded):
    """A run waiting for staff is the case this job must not touch: it is
    idle by design, for as long as it takes a human to look at it, and
    force-escalating it would take the decision out of their hands and leave
    the graph parked at an interrupt nobody can answer."""
    fresh_run = WorkflowRun(
        user_id=1, patient_id=1, thread_id="wf-fresh", request_text="ok", status="running"
    )
    old_but_done = WorkflowRun(
        user_id=1, patient_id=1, thread_id="wf-done", request_text="ok", status="completed"
    )
    old_but_waiting = WorkflowRun(
        user_id=1, patient_id=1, thread_id="wf-waiting", request_text="ok",
        status="waiting_approval",
    )
    db.add_all([fresh_run, old_but_done, old_but_waiting])
    db.commit()
    old_but_done.updated_at = _naive_utcnow() - timedelta(hours=2)
    old_but_waiting.updated_at = _naive_utcnow() - timedelta(hours=2)
    db.commit()

    result = escalate_stalled_workflows(db)

    assert result == {"escalated_count": 0, "workflow_run_ids": []}
    db.refresh(fresh_run)
    db.refresh(old_but_done)
    db.refresh(old_but_waiting)
    assert fresh_run.status == "running"
    assert old_but_done.status == "completed"
    assert old_but_waiting.status == "waiting_approval"


def test_start_scheduler_is_a_noop_under_testing_env():
    # conftest.py sets TESTING=1 before importing app.main, so this must
    # never actually start a BackgroundScheduler during the test suite.
    assert start_scheduler() is None
    stop_scheduler()  # must not raise even though nothing started
