"""TDD for app.agents.followup.run: turns an LLM reminder plan into real
Reminder rows (appointment reminder + one per missing document + the
post-visit follow-up task), only once there's a confirmed appointment.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.agents import followup
from app.models import AppointmentSlot, AuditEvent, Reminder
from app.tools import followup_tools
from app.tools.appointment_tools import book_appointment
from app.tools.followup_tools import ReminderItem, create_reminders_batch


def _booked_state(db, **overrides) -> dict:
    slot = (
        db.query(AppointmentSlot)
        .filter_by(status="free")
        .order_by(AppointmentSlot.start_time.desc())
        .first()
    )
    booking = book_appointment(db, patient_id=1, slot_id=slot.id, reason="checkup")
    base = {
        "workflow_id": 1,
        "patient_id": 1,
        "appointment": booking,
        "documents_result": {"missing": ["ecg_report"]},
    }
    base.update(overrides)
    return base


def test_creates_reminder_and_followup_task_rows(db, seeded, fake_llm):
    state = _booked_state(db)
    fake_llm(
        [
            {
                "reminders": [
                    {"type": "appointment_reminder", "days_before_appointment": 1},
                    {"type": "missing_document:ecg_report", "days_before_appointment": 3},
                ],
                "followup_days_after": 14,
            }
        ]
    )

    result = followup.run(state, db)

    reminders = db.query(Reminder).filter_by(patient_id=1).all()
    types = sorted(r.reminder_type for r in reminders)
    assert types == ["appointment_reminder", "followup", "missing_document:ecg_report"]
    assert len(result["reminders"]) == 3
    assert result["completed_steps"] == ["followup"]


def test_re_running_the_node_keeps_the_reminder_batch_it_already_wrote(db, seeded, fake_llm):
    """Same crash window as the appointment node: the reminder rows are
    committed before LangGraph checkpoints the node, so a resumed run
    re-executes it. The second pass must leave the batch alone.

    Deduping on the plan's content or times cannot work, which the second
    scripted plan shows: the same request re-planned comes back with
    different types and different days, so every row would look new. The
    node skips the creation block on the appointment it already has
    reminders for instead, and never asks for that second plan at all.
    """
    state = _booked_state(db)
    client = fake_llm(
        [
            {
                "reminders": [{"type": "appointment_reminder", "days_before_appointment": 1}],
                "followup_days_after": 14,
            },
            {
                "reminders": [{"type": "prepare_documents", "days_before_appointment": 5}],
                "followup_days_after": 21,
            },
        ]
    )

    first = followup.run(state, db)
    created = db.query(Reminder).count()
    assert created == 2

    second = followup.run(state, db)

    assert db.query(Reminder).count() == created
    assert second["completed_steps"] == ["followup"]
    assert [r["id"] for r in second["reminders"]] == [r["id"] for r in first["reminders"]]
    assert len(client.chat.completions.calls) == 1


def test_caps_reminder_count_and_clamps_days_before_appointment(db, seeded, fake_llm):
    """An LLM that returns a long reminder list with out-of-range days must
    not be able to flood the schedule, put a reminder after the appointment
    (negative days) or park one far in the past (huge days), where
    send_due_reminders would mark it sent immediately."""
    state = _booked_state(db)
    start_time = datetime.fromisoformat(state["appointment"]["start_time"])
    fake_llm(
        [
            {
                "reminders": [
                    {"type": f"appointment_reminder_{index}", "days_before_appointment": days}
                    for index, days in enumerate([-3, 400, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
                ],
                "followup_days_after": 14,
            }
        ]
    )

    result = followup.run(state, db)

    scheduled = (
        db.query(Reminder).filter(Reminder.patient_id == 1, Reminder.reminder_type != "followup").all()
    )
    assert 1 <= len(scheduled) <= 5
    earliest = start_time - timedelta(days=90)
    for reminder in scheduled:
        assert max(earliest, datetime.now()) < reminder.scheduled_at <= start_time
    assert len(result["reminders"]) == len(scheduled) + 1


def test_clamps_followup_days_after(db, seeded, fake_llm):
    state = _booked_state(db)
    start_time = datetime.fromisoformat(state["appointment"]["start_time"])
    fake_llm([{"reminders": [], "followup_days_after": 400}])

    followup.run(state, db)

    task = db.query(Reminder).filter_by(patient_id=1, reminder_type="followup").one()
    assert task.scheduled_at == start_time + timedelta(days=90)


def test_expired_preappointment_reminders_are_not_created(
    db, seeded, fake_llm
):
    state = _booked_state(db)
    soon = datetime.now() + timedelta(minutes=30)
    state["appointment"]["start_time"] = soon.isoformat()
    fake_llm(
        [
            {
                "reminders": [
                    {
                        "type": "appointment_reminder",
                        "days_before_appointment": 1,
                    }
                ],
                "followup_days_after": 14,
            }
        ]
    )

    result = followup.run(state, db)

    reminders = db.query(Reminder).filter_by(patient_id=1).all()
    assert [row.reminder_type for row in reminders] == ["followup"]
    assert all(row.scheduled_at > datetime.now() for row in reminders)
    assert [row["reminder_type"] for row in result["reminders"]] == ["followup"]


# --- The batch is one transaction -------------------------------------------
# The node's resume guard skips the whole batch when the appointment already
# has any reminder, so a batch that can survive halfway would teach the
# resumed node that a one-row batch was the finished job.


def _reminder_audit_count(db) -> int:
    return db.query(AuditEvent).filter_by(action="reminder.created").count()


def _appointment_id(db) -> int:
    return _booked_state(db)["appointment"]["id"]


def test_a_failing_item_leaves_no_reminder_rows_and_no_audit_rows(db, seeded):
    appointment_id = _appointment_id(db)
    items = [
        ReminderItem("appointment_reminder", datetime(2026, 8, 1, 9, 0)),
        ReminderItem("appointment_reminder_2", datetime(2026, 8, 2, 9, 0)),
        # Rejected by the DateTime column, the way any bad row would be.
        ReminderItem("broken", "not-a-datetime"),
    ]

    with pytest.raises(Exception):  # noqa: B017 - the driver's error type is not the point
        create_reminders_batch(db, patient_id=1, appointment_id=appointment_id, items=items)

    assert db.query(Reminder).count() == 0
    assert _reminder_audit_count(db) == 0


def test_a_successful_batch_commits_exactly_once(db, seeded, monkeypatch):
    appointment_id = _appointment_id(db)
    items = [
        ReminderItem("appointment_reminder", datetime(2026, 8, 1, 9, 0)),
        ReminderItem("missing_document:ecg_report", datetime(2026, 8, 2, 9, 0)),
        ReminderItem("followup", datetime(2026, 8, 20, 9, 0)),
    ]
    commits: list[int] = []
    real_commit = db.commit

    def counting_commit() -> None:
        commits.append(1)
        real_commit()

    monkeypatch.setattr(db, "commit", counting_commit)

    summaries = create_reminders_batch(db, patient_id=1, appointment_id=appointment_id, items=items)

    assert len(commits) == 1
    assert len(summaries) == 3
    assert db.query(Reminder).count() == 3
    assert _reminder_audit_count(db) == 3
    assert [summary["reminder_type"] for summary in summaries] == [item[0] for item in items]


def test_the_post_visit_task_is_part_of_the_same_transaction(db, seeded, fake_llm, monkeypatch):
    """The last row the node writes is the post-visit follow-up task. Losing
    the process there must not leave the reminders before it behind."""
    state = _booked_state(db)
    fake_llm(
        [
            {
                "reminders": [
                    {"type": "appointment_reminder", "days_before_appointment": 1},
                    {"type": "missing_document:ecg_report", "days_before_appointment": 3},
                ],
                "followup_days_after": 14,
            }
        ]
    )
    real_write_audit = followup_tools.write_audit
    seen: list[int] = []

    def dying_write_audit(*args, **kwargs):
        seen.append(1)
        if len(seen) == 3:  # the post-visit task, written last
            raise RuntimeError("process died writing the last row")
        return real_write_audit(*args, **kwargs)

    monkeypatch.setattr(followup_tools, "write_audit", dying_write_audit)

    result = followup.run(state, db)

    assert "error" in result
    assert db.query(Reminder).count() == 0
    assert _reminder_audit_count(db) == 0


def test_a_batch_that_died_halfway_does_not_block_the_resumed_node(db, seeded, fake_llm, monkeypatch):
    """The crash-resume case end to end: the first pass dies mid-batch, the
    resumed node finds nothing to reuse, re-plans, and writes one complete
    batch with no duplicates."""
    state = _booked_state(db)
    client = fake_llm(
        [
            {
                "reminders": [
                    {"type": "appointment_reminder", "days_before_appointment": 1},
                    {"type": "missing_document:ecg_report", "days_before_appointment": 3},
                ],
                "followup_days_after": 14,
            },
            {
                "reminders": [{"type": "prepare_documents", "days_before_appointment": 5}],
                "followup_days_after": 21,
            },
        ]
    )
    real_write_audit = followup_tools.write_audit
    seen: list[int] = []

    def dying_write_audit(*args, **kwargs):
        seen.append(1)
        if len(seen) == 2:
            raise RuntimeError("process died mid-batch")
        return real_write_audit(*args, **kwargs)

    monkeypatch.setattr(followup_tools, "write_audit", dying_write_audit)
    first = followup.run(state, db)
    assert "error" in first
    assert db.query(Reminder).count() == 0

    monkeypatch.undo()
    second = followup.run(state, db)

    reminders = db.query(Reminder).order_by(Reminder.id).all()
    assert [row.reminder_type for row in reminders] == ["prepare_documents", "followup"]
    assert [row["id"] for row in second["reminders"]] == [row.id for row in reminders]
    assert len(client.chat.completions.calls) == 2


def test_skips_without_a_confirmed_appointment(db, seeded, fake_llm):
    client = fake_llm([])

    result = followup.run(
        {"workflow_id": 1, "patient_id": 1, "appointment": None, "documents_result": None}, db
    )

    assert db.query(Reminder).count() == 0
    assert len(client.chat.completions.calls) == 0
    assert result["completed_steps"] == ["followup"]
