"""Deterministic safety gate: keyword screening + output sanitizing.

Pure-function module, no app fixtures needed (no LLM, no DB).
"""

from app.safety.guardrails import screen_request, sanitize_agent_output


def test_emergency_detected():
    r = screen_request("I have severe chest pain right now")
    assert r.action == "escalate_emergency"


def test_medical_advice_refused():
    r = screen_request("Welches Medikament soll ich nehmen?")
    assert r.action == "refuse_medical"


def test_admin_request_allowed():
    r = screen_request("I need a cardiology appointment next week")
    assert r.action == "allow"


def test_output_sanitizer_strips_diagnosis():
    text, flagged = sanitize_agent_output(
        "You probably have arrhythmia. Your appointment is booked."
    )
    assert flagged and "arrhythmia" not in text and "booked" in text


def test_german_emergency_detected():
    """Emergency keywords must fire in German too, not just English."""
    r = screen_request("Ich habe seit heute Morgen starke Atemnot")
    assert r.action == "escalate_emergency"
    assert "atemnot" in r.matched


def test_mixed_admin_and_medical_request_refuses():
    """An admin ask (appointment) plus a medical ask (diagnose) in the same
    message must refuse, not allow: the medical ask wins.
    """
    r = screen_request(
        "I need a cardiology appointment next week, and can you diagnose "
        "what's wrong with my heart?"
    )
    assert r.action == "refuse_medical"
    assert "diagnose" in r.matched


def test_clean_output_passes_through_untouched():
    """Text with no forbidden pattern is returned exactly as given."""
    original = (
        "Your appointment is confirmed for Tuesday at 10am. "
        "Please arrive 15 minutes early."
    )
    text, flagged = sanitize_agent_output(original)
    assert flagged is False
    assert text == original


def test_diagnostics_department_not_flagged_as_medical_advice():
    """'diagnose'/'diagnosis' must match as whole words only, so an unrelated
    admin phrase like a department name doesn't trip a medical-advice refusal.
    """
    r = screen_request("Where is the diagnostics department located?")
    assert r.action == "allow"
