"""Language-aware deterministic templates (agents/responses.py).

The no-LLM paths (routing uncertainty, appointment agent failure, graph
escalate node, emergency screen, medical refusal) must answer in the
patient's preferred language; the LLM-composed finalize path already handles
language in the safety agent. Seeded Max (patient 1) prefers "en", Erika
(patient 2) "de".
"""

from __future__ import annotations

from app.agents import routing
from app.agents.responses import (
    emergency_response,
    medical_refusal_response,
    staff_review_response,
)
from app.services.workflow_service import start_workflow
from app.models import User


def test_german_preference_gets_german_staff_review(db, seeded):
    assert "prüfen" in staff_review_response(db, 2)


def test_english_preference_gets_english_staff_review(db, seeded):
    assert staff_review_response(db, 1) == "a staff member will review your request"


def test_unknown_patient_falls_back_to_english(db, seeded):
    assert staff_review_response(db, None) == "a staff member will review your request"


def test_user_without_patient_profile_falls_back_to_english(db, seeded):
    # Seeded user 3 is the staff account and has no PatientProfile row.
    assert staff_review_response(db, 3) == "a staff member will review your request"


def test_emergency_template_localized(db, seeded):
    assert "112" in emergency_response(db, 1)
    german = emergency_response(db, 2)
    assert "112" in german and "Praxisteam" in german


def test_medical_refusal_template_localized(db, seeded):
    assert "medical advice" in medical_refusal_response(db, 1)
    assert "Ratschläge" in medical_refusal_response(db, 2)


def test_routing_uncertainty_escalation_answers_in_german(db, seeded, fake_llm):
    fake_llm([{"intent": "other", "department": None, "confidence": 0.3, "reason": "unclear"}])
    state = {"workflow_id": 1, "patient_id": 2, "request_text": "irgendwas unklares bitte"}

    update = routing.run(state, db)

    assert update["escalation_id"] is not None
    assert "Praxisteam" in update["final_response"]


def test_emergency_screen_answers_in_german_for_german_patient(db, seeded):
    erika = db.get(User, 2)

    run = start_workflow(db, erika, "Ich habe starke Brustschmerzen", [])

    assert run.status == "escalated"
    assert "112" in run.state["final_response"]
    assert "Praxisteam" in run.state["final_response"]
