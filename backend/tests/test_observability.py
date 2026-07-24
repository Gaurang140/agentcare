"""Langfuse tracing is optional and must never take a run down with it.

`langfuse` itself is pinned in requirements.txt, but the callback handler in
`langfuse.langchain` imports `langchain`, which is not pinned. Setting the two
Langfuse keys on a machine without `langchain` therefore used to fail every
workflow through `_invoke_graph`'s last-resort handler. These tests script the
same booking run as tests/test_graph_e2e.py and force that import to fail the
way a missing install does.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Department, User
from app.services import workflow_service
from app.tools.appointment_tools import get_available_slots

_SLOT_WINDOW_DAYS = 14

_MISSING_DEPENDENCY_EVENT = "langfuse_disabled_missing_dependency"


@pytest.fixture(autouse=True)
def _isolated_checkpointer(tmp_path, monkeypatch):
    """A fresh checkpoint file per test, for the reason spelled out in
    tests/test_graph_e2e.py: WorkflowRun ids restart at 1 in every test's own
    `db` fixture, so thread_id "wf-1" would otherwise collide inside the one
    checkpoint file the module-level graph singleton keeps open."""
    monkeypatch.setattr(settings, "checkpoint_db_path", str(tmp_path / "checkpoints.db"))
    workflow_service.close_graph()
    yield
    workflow_service.close_graph()


class _RecordingLogger:
    """Stands in for the module's structlog logger and keeps the event names.

    structlog is configured with a PrintLoggerFactory and cached bound
    loggers, so pytest's caplog sees nothing. Swapping the module attribute is
    the deterministic way to assert on a log line.
    """

    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict]] = []

    def warning(self, event: str, **kwargs) -> None:
        self.warnings.append((event, kwargs))

    def info(self, event: str, **kwargs) -> None:
        pass

    def error(self, event: str, **kwargs) -> None:
        pass

    def debug(self, event: str, **kwargs) -> None:
        pass

    @property
    def events(self) -> list[str]:
        return [event for event, _ in self.warnings]


def _patient_user(db: Session) -> User:
    user = db.query(User).filter_by(email="patient@agentcare-demo.com").first()
    assert user is not None
    return user


def _first_free_slot_id(db: Session) -> int:
    dept = db.query(Department).filter_by(name="Cardiology").first()
    assert dept is not None
    today = date.today()
    slots = get_available_slots(db, dept.id, today, today + timedelta(days=_SLOT_WINDOW_DAYS))
    assert slots
    return slots[0]["slot_id"]


def _full_booking_script(slot_id: int) -> list[dict]:
    """The 9 scripted LLM responses of one complete booking run, in call
    order (see tests/test_graph_e2e.py for the node-by-node walk)."""
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


def _record_logger(monkeypatch) -> _RecordingLogger:
    recorder = _RecordingLogger()
    monkeypatch.setattr(workflow_service, "logger", recorder)
    return recorder


def test_workflow_completes_untraced_when_langchain_is_missing(db, seeded, fake_llm, monkeypatch):
    monkeypatch.setattr(settings, "langfuse_public_key", "pk-lf-test")
    monkeypatch.setattr(settings, "langfuse_secret_key", "sk-lf-test")
    # Exactly what a machine without `langchain` does to the handler import.
    monkeypatch.setitem(sys.modules, "langfuse.langchain", None)

    def _no_client() -> None:
        raise AssertionError("no Langfuse client while tracing cannot attach")

    # Tracing that cannot attach must not build a client either: with fake
    # keys that would start a real exporter thread from a unit test.
    monkeypatch.setattr(workflow_service, "_ensure_langfuse_client", _no_client)
    recorder = _record_logger(monkeypatch)
    client = fake_llm(_full_booking_script(_first_free_slot_id(db)))

    run = workflow_service.start_workflow(
        db, _patient_user(db), "I need a cardiology appointment next week", []
    )

    assert run.status == "completed"
    assert _MISSING_DEPENDENCY_EVENT in recorder.events
    assert len(client.chat.completions.calls) == 9


def test_tracing_stays_silent_when_no_langfuse_keys_are_set(db, seeded, fake_llm, monkeypatch):
    monkeypatch.setattr(settings, "langfuse_public_key", "")
    monkeypatch.setattr(settings, "langfuse_secret_key", "")
    recorder = _record_logger(monkeypatch)
    fake_llm(_full_booking_script(_first_free_slot_id(db)))

    run = workflow_service.start_workflow(
        db, _patient_user(db), "I need a cardiology appointment next week", []
    )

    assert run.status == "completed"
    assert recorder.events == []
