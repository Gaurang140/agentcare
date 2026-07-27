"""TDD for app.agents.routing.run: department classification + confidence gate."""

from __future__ import annotations

from app.agents import routing, support
from app.models import Escalation
from app.safety.pii import resolve_language


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
    # Patient 1 (Max) prefers English; the no-LLM escalation template
    # follows the profile language (agents/responses.py).
    assert result["final_response"] == "a staff member will review your request"
    escalation = db.get(Escalation, result["escalation_id"])
    assert escalation.severity == "uncertainty"
    assert escalation.workflow_run_id == 1


def test_null_department_escalates_for_booking_intent(db, seeded, fake_llm):
    """Booking needs a department to work with, so a null one escalates."""
    fake_llm(
        [{"intent": "book", "department": None, "confidence": 0.95, "reason": "unclear ask"}]
    )

    result = routing.run(_state(request_text="I need an appointment somewhere"), db)

    assert result["department_id"] is None
    assert result["escalation_id"] is not None
    escalation = db.get(Escalation, result["escalation_id"])
    assert escalation.severity == "uncertainty"


def test_cancel_without_department_passes_through(db, seeded, fake_llm):
    """Cancelling works off the patient's existing appointment, so a null
    department is not a reason to hand a well-formed request to staff."""
    fake_llm(
        [{"intent": "cancel", "department": None, "confidence": 0.9, "reason": "clear cancel"}]
    )

    result = routing.run(_state(request_text="please cancel my appointment"), db)

    assert result["intent"] == "cancel"
    assert result["department_id"] is None
    assert result["department_name"] is None
    assert result["routing_confidence"] == 0.9
    assert "escalation_id" not in result
    assert result["completed_steps"] == ["routing"]
    assert db.query(Escalation).count() == 0


def test_attach_documents_without_department_passes_through(db, seeded, fake_llm):
    """The document node degrades to classify-only without a department, so
    document uploads route through without one."""
    fake_llm(
        [
            {
                "intent": "attach_documents",
                "department": None,
                "confidence": 0.9,
                "reason": "upload only",
            }
        ]
    )

    result = routing.run(_state(request_text="here is my insurance card"), db)

    assert result["intent"] == "attach_documents"
    assert result["department_id"] is None
    assert result["department_name"] is None
    assert "escalation_id" not in result
    assert db.query(Escalation).count() == 0


def test_other_intent_escalates_even_with_department_and_confidence(db, seeded, fake_llm):
    """"other" means the request is outside the supported administrative
    intents. That escalates deterministically here, without depending on the
    coordinator to notice."""
    fake_llm(
        [{"intent": "other", "department": "Cardiology", "confidence": 0.95, "reason": "off topic"}]
    )

    result = routing.run(_state(request_text="what's the weather like"), db)

    assert result["intent"] == "other"
    assert result["department_id"] is None
    assert result["escalation_id"] is not None
    assert result["final_response"] == "a staff member will review your request"
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


# --- Redaction language ------------------------------------------------------
# One precedence, the same one every other call site uses
# (safety/pii.py::resolve_language): a German cue in the request wins, the
# patient's stored preferred_language breaks the tie when the request carries
# no cue, and English is the last fallback. The preference cannot outvote the
# text because it defaults to "en" for every patient who never chose German,
# and the English model reads "Termin" as a location (safety/pii.py explains
# what that costs).

_CUE_FREE_REQUEST = "cancel booking 4711"
_GERMAN_REQUEST = "Ich brauche einen Termin in der Kardiologie"


def test_german_request_from_an_english_preferring_patient_runs_german(
    db, seeded, fake_llm, redaction_language
):
    """Patient 1 (Max) is stored as "en", the column default every patient who
    never chose German carries. His German booking request still goes to the
    German model, so "Termin" and the department name reach this node."""
    seen = redaction_language(support)
    fake_llm([{"intent": "book", "department": "Cardiology", "confidence": 0.9, "reason": "clear"}])

    routing.run(_state(patient_id=1, request_text=_GERMAN_REQUEST), db)

    assert seen == ["de"]


def test_english_request_from_a_german_preferring_patient_keeps_the_preference(
    db, seeded, fake_llm, redaction_language
):
    """The documented, accepted limitation: safety/pii.py has a German-positive
    cue set and no English-positive one, so English text reads as a no-cue tie
    and patient 2's stored "de" takes it."""
    seen = redaction_language(support)
    fake_llm([{"intent": "reschedule", "department": "Cardiology", "confidence": 0.9, "reason": "clear"}])

    routing.run(
        _state(patient_id=2, request_text="Can I reschedule my appointment to next week?"), db
    )

    assert seen == ["de"]


def test_german_preference_pins_the_redaction_language(
    db, seeded, fake_llm, redaction_language
):
    """Patient 2 (Erika) prefers German. This request carries no German cue
    word at all, so the stored preference is the only thing that can put it on
    the German model."""
    seen = redaction_language(support)
    fake_llm([{"intent": "cancel", "department": None, "confidence": 0.9, "reason": "clear"}])

    routing.run(_state(patient_id=2, request_text=_CUE_FREE_REQUEST), db)

    assert seen == ["de"]


def test_unknown_patient_leaves_the_language_to_the_redactor(
    db, seeded, fake_llm, redaction_language
):
    """No profile row means no stored preference, and the redactor's own
    reading of the text stays the fallback rather than being replaced by a
    default of this node's own."""
    seen = redaction_language(support)
    fake_llm([{"intent": "cancel", "department": None, "confidence": 0.9, "reason": "clear"}])

    routing.run(_state(patient_id=999, request_text=_CUE_FREE_REQUEST), db)

    assert seen == [resolve_language(_CUE_FREE_REQUEST, None)]
    assert seen == ["en"]


def test_routing_llm_failure_returns_error_not_raise(db, seeded, fake_llm):
    # RuntimeError isn't a retried transport error, so chat_json propagates
    # it on the first call - routing.run must still not raise.
    fake_llm([RuntimeError("boom")])

    result = routing.run(_state(), db)

    assert "error" in result
    assert result["completed_steps"] == ["routing"]
