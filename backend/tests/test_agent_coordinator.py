"""TDD for app.agents.coordinator.run: a pure decision node, no tools."""

from __future__ import annotations

from app.agents import coordinator


def test_coordinator_appends_decision_to_plan(db, seeded, fake_llm):
    fake_llm([{"next_step": "route_department", "reasoning": "nothing routed yet"}])

    result = coordinator.run({"workflow_id": 1, "request_text": "book cardiology"}, db)

    assert result["plan"] == ["route_department"]
    assert result["completed_steps"] == ["coordinator"]


def test_coordinator_appends_to_existing_plan(db, seeded, fake_llm):
    fake_llm([{"next_step": "finalize", "reasoning": "everything is done"}])

    result = coordinator.run(
        {
            "workflow_id": 1,
            "request_text": "book cardiology",
            "plan": ["route_department", "handle_appointment"],
            "completed_steps": ["routing", "appointment", "document", "followup", "safety"],
        },
        db,
    )

    assert result["plan"] == ["route_department", "handle_appointment", "finalize"]


def test_coordinator_llm_failure_returns_error_not_raise(db, seeded, fake_llm):
    fake_llm([RuntimeError("down")])

    result = coordinator.run({"workflow_id": 1, "request_text": "book cardiology"}, db)

    assert "error" in result
