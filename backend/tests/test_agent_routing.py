"""TDD for app.agents.routing.run: department classification + confidence gate."""

from __future__ import annotations

from app.agents import routing
from app.models import Escalation


def _state(**overrides) -> dict:
    base = {
        "workflow_id": 1,
        "user_id": 1,
        "patient_id": 1,
        "request_text": "I need to book a cardiology appointment",
    }
    base.update(overrides)
    return base


def test_confident_routing_resolves_department(db, seeded, fake_llm):
    client = fake_llm(
        [{"intent": "book", "department": "Cardiology", "confidence": 0.92, "reason": "explicit ask"}]
    )

    result = routing.run(_state(), db)

    assert result["intent"] == "book"
    assert result["department_name"] == "Cardiology"
    assert result["department_id"] is not None
    assert result["routing_confidence"] == 0.92
    assert "escalation_id" not in result
    assert result["completed_steps"] == ["routing"]
    assert len(client.chat.completions.calls) == 1


def test_low_confidence_escalates_instead_of_routing(db, seeded, fake_llm):
    fake_llm(
        [{"intent": "book", "department": "Cardiology", "confidence": 0.4, "reason": "vague request"}]
    )

    result = routing.run(_state(request_text="something about my heart maybe"), db)

    assert result["department_id"] is None
    assert result["escalation_id"] is not None
    assert result["final_response"] == "a staff member will review your request"
    escalation = db.get(Escalation, result["escalation_id"])
    assert escalation.severity == "uncertainty"
    assert escalation.workflow_run_id == 1


def test_null_department_escalates(db, seeded, fake_llm):
    fake_llm(
        [{"intent": "other", "department": None, "confidence": 0.95, "reason": "unclear ask"}]
    )

    result = routing.run(_state(request_text="what's the weather like"), db)

    assert result["department_id"] is None
    assert result["escalation_id"] is not None
    escalation = db.get(Escalation, result["escalation_id"])
    assert escalation.severity == "uncertainty"


def test_unknown_department_name_from_llm_escalates(db, seeded, fake_llm):
    """The LLM is instructed to pick from the given list, but a hallucinated
    department name that doesn't resolve must not silently pass through -
    it's treated the same as low confidence."""
    fake_llm(
        [{"intent": "book", "department": "Neurosurgery", "confidence": 0.9, "reason": "ask"}]
    )

    result = routing.run(_state(), db)

    assert result["department_id"] is None
    assert result["escalation_id"] is not None


def test_routing_llm_failure_returns_error_not_raise(db, seeded, fake_llm):
    # RuntimeError isn't a retried transport error, so chat_json propagates
    # it on the first call - routing.run must still not raise.
    fake_llm([RuntimeError("boom")])

    result = routing.run(_state(), db)

    assert "error" in result
    assert result["completed_steps"] == ["routing"]
