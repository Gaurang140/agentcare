"""TDD for app.agents.coordinator.run: a pure decision node, no tools."""

from __future__ import annotations

from app.agents import coordinator
from app.safety.pii import resolve_language


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


def test_german_preference_pins_the_redaction_language(
    db, seeded, fake_llm, redaction_language
):
    """Same boundary as the routing node: patient 2 (Erika) prefers German and
    this request carries no German cue word for the redactor to detect."""
    seen = redaction_language(coordinator)
    fake_llm([{"next_step": "finalize", "reasoning": "nothing left"}])

    coordinator.run(
        {"workflow_id": 1, "patient_id": 2, "request_text": "cancel booking 4711"}, db
    )

    assert seen == ["de"]


def test_german_request_from_an_english_preferring_patient_runs_german(
    db, seeded, fake_llm, redaction_language
):
    """Same precedence as the routing node: patient 1 is stored as "en" and a
    German cue in the request still puts it on the German model."""
    seen = redaction_language(coordinator)
    fake_llm([{"next_step": "route_department", "reasoning": "nothing routed yet"}])

    coordinator.run(
        {
            "workflow_id": 1,
            "patient_id": 1,
            "request_text": "Ich brauche einen Termin in der Kardiologie",
        },
        db,
    )

    assert seen == ["de"]


def test_unknown_patient_leaves_the_language_to_the_redactor(
    db, seeded, fake_llm, redaction_language
):
    """No profile row, so nothing can outvote the text and the node passes the
    redactor's own reading of it rather than a default of its own."""
    seen = redaction_language(coordinator)
    fake_llm([{"next_step": "finalize", "reasoning": "nothing left"}])

    coordinator.run(
        {"workflow_id": 1, "patient_id": 999, "request_text": "cancel booking 4711"}, db
    )

    assert seen == [resolve_language("cancel booking 4711", None)]
    assert seen == ["en"]


def test_coordinator_llm_failure_returns_error_not_raise(db, seeded, fake_llm):
    fake_llm([RuntimeError("down")])

    result = coordinator.run({"workflow_id": 1, "request_text": "book cardiology"}, db)

    assert "error" in result
