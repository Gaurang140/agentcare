"""Language-aware deterministic escalation responses (agents/responses.py).

The no-LLM escalation paths (routing uncertainty, appointment agent failure,
graph escalate node) must answer in the patient's preferred language; the
LLM-composed finalize path already handles language in the safety agent.
"""

from __future__ import annotations

from app.agents import routing
from app.agents.responses import staff_review_response


def test_german_preference_gets_german_response(db, seeded):
    # Seeded patient 1 (Max Mustermann) has preferred_language "de".
    assert "prüfen" in staff_review_response(db, 1)


def test_unknown_patient_falls_back_to_english(db, seeded):
    assert staff_review_response(db, None) == "a staff member will review your request"


def test_user_without_patient_profile_falls_back_to_english(db, seeded):
    # Seeded user 3 is the staff account and has no PatientProfile row.
    assert staff_review_response(db, 3) == "a staff member will review your request"


def test_routing_uncertainty_escalation_answers_in_german(db, seeded, fake_llm):
    fake_llm([{"intent": "other", "department": None, "confidence": 0.3, "reason": "unclear"}])
    state = {"workflow_id": 1, "patient_id": 1, "request_text": "irgendwas unklares bitte"}

    update = routing.run(state, db)

    assert update["escalation_id"] is not None
    assert "Praxisteam" in update["final_response"]
