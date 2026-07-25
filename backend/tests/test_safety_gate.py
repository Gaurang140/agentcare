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


# --- German output sanitizing ------------------------------------------------
# The clinic is German, so a model can state a diagnosis or a dosage in German
# just as easily as in English. Each test below is the German twin of one of
# the four English shapes the sanitizer already knew.


def test_german_diagnosis_as_fact_is_rewritten():
    text, flagged = sanitize_agent_output(
        "Sie haben wahrscheinlich Vorhofflimmern. Ihr Termin ist bestätigt."
    )
    assert flagged
    assert "Vorhofflimmern" not in text
    assert "bestätigt" in text


def test_german_diagnosis_statement_is_rewritten():
    text, flagged = sanitize_agent_output("Die Diagnose lautet Bluthochdruck.")
    assert flagged
    assert "Bluthochdruck" not in text


def test_german_dosage_instruction_is_rewritten():
    text, flagged = sanitize_agent_output("Nehmen Sie 5 mg pro Tag.")
    assert flagged
    assert "5 mg" not in text


def test_german_treatment_recommendation_is_rewritten():
    text, flagged = sanitize_agent_output("Ich empfehle die Einnahme von Betablockern.")
    assert flagged
    assert "Betablockern" not in text


def test_clean_german_confirmation_passes_through_untouched():
    """The administrative German the safety agent actually composes must not
    trip any of the new patterns."""
    original = (
        "Ihr Termin bei Dr. Weber (Kardiologie) ist bestätigt für Dienstag um 10 Uhr. "
        "Bitte bringen Sie Ihre Versichertenkarte mit."
    )
    text, flagged = sanitize_agent_output(original)
    assert flagged is False
    assert text == original


# --- Normalization before output matching ------------------------------------


def test_zero_width_characters_inside_a_forbidden_phrase_are_caught():
    """Zero-width characters render as nothing, so "ha<zwsp>ve" reads as
    "have" to the patient while matching no pattern. Normalization strips them
    before the patterns run.
    """
    text, flagged = sanitize_agent_output("You ha​ve arrhythmia.")
    assert flagged
    assert "arrhythmia" not in text


def test_compatibility_spelling_and_odd_whitespace_are_caught():
    """NFKC folds a compatibility spelling onto its plain form and turns a
    non-breaking space into an ordinary one, so a dosage instruction spelled
    either way still matches."""
    text, flagged = sanitize_agent_output("Please ｔａｋｅ 500 mg tonight.")
    assert flagged
    assert "500" not in text
