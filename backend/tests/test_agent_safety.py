"""TDD for app.agents.safety.run: the draft is composed from re-queried DB
rows (never the in-memory state dict), and the deterministic sanitizer wins
over whatever the LLM claims - even a poisoned "safe: true" response gets
its unsafe sentence stripped.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.agents import safety
from app.models import Appointment, AppointmentSlot, AuditEvent, PatientProfile
from app.safety.guardrails import SANITIZED_SENTENCE
from app.tools.appointment_tools import book_appointment, cancel_appointment
from app.tools.followup_tools import create_reminder


def _set_language(db, patient_id: int, language: str) -> None:
    """Seeded Max (patient 1) prefers "en", Erika (patient 2) "de"
    (app/db/seed.py) - tests pin the language they exercise explicitly
    instead of relying on the seed incidentally."""
    profile = db.query(PatientProfile).filter_by(user_id=patient_id).first()
    profile.preferred_language = language
    db.flush()


def _booked_state(db, **overrides) -> dict:
    slot = (
        db.query(AppointmentSlot)
        .filter_by(status="free")
        .order_by(AppointmentSlot.start_time.desc())
        .first()
    )
    booking = book_appointment(db, patient_id=1, slot_id=slot.id, reason="checkup")
    create_reminder(
        db,
        patient_id=1,
        appointment_id=booking["id"],
        reminder_type="appointment_reminder",
        scheduled_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1),
    )
    base = {
        "workflow_id": 1,
        "patient_id": 1,
        "appointment": booking,
        "documents_result": {"missing": ["ecg_report"]},
    }
    base.update(overrides)
    return base


def _status_summary_state(db) -> tuple[dict, list[dict]]:
    first_slot = (
        db.query(AppointmentSlot)
        .filter_by(status="free")
        .order_by(AppointmentSlot.start_time)
        .first()
    )
    second_slot = (
        db.query(AppointmentSlot)
        .filter(
            AppointmentSlot.status == "free",
            AppointmentSlot.start_time >= first_slot.end_time,
        )
        .order_by(AppointmentSlot.start_time)
        .first()
    )
    first = book_appointment(db, patient_id=1, slot_id=first_slot.id, reason="private first")
    second = book_appointment(db, patient_id=1, slot_id=second_slot.id, reason="private second")
    state = {
        "workflow_id": 1,
        "patient_id": 1,
        "appointment": {
            "status": "summary",
            "appointments": [
                {"id": first["id"], "doctor": "forged state doctor", "reason": "state secret"},
                {"id": second["id"], "doctor": "another forged doctor", "reason": "state secret"},
            ],
        },
        "documents_result": None,
    }
    return state, [first, second]


def test_safe_llm_verdict_keeps_canonical_sql_draft(db, seeded, fake_llm):
    state = _booked_state(db)
    fake_llm(
        [
            {
                "safe": True,
                "violations": [],
                "rewritten": "Your appointment is confirmed. Please bring your ecg_report.",
            }
        ]
    )

    result = safety.run(state, db)

    assert "diagnosis" not in result["final_response"].lower()
    assert state["appointment"]["doctor"] in result["final_response"]
    assert state["appointment"]["start_time"] in result["final_response"]
    assert "Please bring your ecg_report" not in result["final_response"]
    assert result["completed_steps"] == ["safety"]


def test_safe_llm_cannot_rewrite_authoritative_sql_appointment_facts(
    db, seeded, fake_llm
):
    state = _booked_state(db)
    booking = state["appointment"]
    fake_llm(
        [
            {
                "safe": True,
                "violations": [],
                "rewritten": (
                    "Your appointment is with Dr. Fabricated tomorrow at midnight."
                ),
            }
        ]
    )

    result = safety.run(state, db)

    assert booking["doctor"] in result["final_response"]
    assert booking["start_time"] in result["final_response"]
    assert "Dr. Fabricated" not in result["final_response"]
    assert "midnight" not in result["final_response"]


def test_draft_excludes_reminders_from_unrelated_appointments(
    db, seeded, fake_llm
):
    state = _booked_state(db)
    first_start = datetime.fromisoformat(state["appointment"]["start_time"])
    other_slot = (
        db.query(AppointmentSlot)
        .filter(
            AppointmentSlot.status == "free",
            AppointmentSlot.end_time <= first_start,
        )
        .order_by(AppointmentSlot.start_time.desc())
        .first()
    )
    other = book_appointment(
        db,
        patient_id=1,
        slot_id=other_slot.id,
        reason="unrelated appointment",
    )
    create_reminder(
        db,
        patient_id=1,
        appointment_id=other["id"],
        reminder_type="unrelated_secret_reminder",
        scheduled_at=datetime.now(timezone.utc).replace(tzinfo=None)
        + timedelta(days=2),
    )
    fake_llm([RuntimeError("use deterministic draft")])

    result = safety.run(state, db)

    assert "appointment_reminder" in result["final_response"]
    assert "unrelated_secret_reminder" not in result["final_response"]


def test_poisoned_llm_rewrite_is_ignored_even_when_llm_says_safe(
    db, seeded, fake_llm
):
    state = _booked_state(db)
    fake_llm(
        [
            {
                "safe": True,
                "violations": [],
                "rewritten": (
                    "Your appointment is confirmed. You have diabetes and should "
                    "take 500mg metformin daily."
                ),
            }
        ]
    )

    result = safety.run(state, db)

    final = result["final_response"]
    assert "diabetes" not in final
    assert "metformin" not in final
    assert state["appointment"]["doctor"] in final
    assert "deterministic_sanitizer_rewrote_output" not in result["safety_flags"]


def test_llm_failure_falls_back_to_deterministic_only_path(db, seeded, fake_llm):
    state = _booked_state(db)
    _set_language(db, 1, "en")
    fake_llm([RuntimeError("llm endpoint down")])

    result = safety.run(state, db)

    assert "error" not in result
    assert result["final_response"]
    assert "confirmed" in result["final_response"].lower()


def test_draft_reflects_live_db_status_not_stale_state_dict(db, seeded, fake_llm):
    """state["appointment"]["status"] was captured as "confirmed" at booking
    time. Cancel the appointment directly in the db afterwards (simulating
    staleness) and force the deterministic-only fallback path (LLM down) so
    the composed draft itself becomes final_response - proving the draft was
    built from the live row, not the cached dict still sitting in state."""
    state = _booked_state(db)
    _set_language(db, 1, "en")
    cancel_appointment(db, state["appointment"]["id"])
    fake_llm([RuntimeError("llm down")])

    result = safety.run(state, db)

    assert "cancelled" in result["final_response"].lower()
    assert "confirmed" not in result["final_response"].lower()


def test_status_summary_draft_renders_every_sql_appointment_and_ignores_state_text(
    db, seeded, fake_llm
):
    state, bookings = _status_summary_state(db)
    _set_language(db, 1, "en")
    fake_llm([RuntimeError("llm down")])

    result = safety.run(state, db)

    final = result["final_response"]
    for booking in bookings:
        assert booking["doctor"] in final
        assert booking["start_time"] in final
    assert final.count("Your appointment with") == 2
    assert "forged state doctor" not in final
    assert "state secret" not in final
    assert "private first" not in final
    assert "private second" not in final
    assert "No appointment has been booked yet." not in final


def test_status_summary_draft_renders_every_sql_appointment_in_german(
    db, seeded, fake_llm
):
    state, bookings = _status_summary_state(db)
    _set_language(db, 1, "de")
    fake_llm([RuntimeError("llm down")])

    result = safety.run(state, db)

    final = result["final_response"]
    for booking in bookings:
        assert booking["doctor"] in final
        assert booking["start_time"] in final
    assert final.count("Ihr Termin bei") == 2
    assert final.count("bestätigt") == 2
    assert "Es ist noch kein Termin gebucht." not in final


# --- Language preference (PatientProfile.preferred_language) ----------------
# Seeded Max (patient 1) prefers "en"; the German-path tests pin "de" via
# _set_language so they don't depend on which seed account they run as.


def test_safety_prompt_carries_german_language_instruction_for_llm_path(db, seeded, fake_llm):
    state = _booked_state(db)
    _set_language(db, 1, "de")
    client = fake_llm(
        [{"safe": True, "violations": [], "rewritten": "Ihr Termin ist bestätigt."}]
    )

    safety.run(state, db)

    sent_user_content = client.chat.completions.calls[0]["messages"][1]["content"]
    assert "Respond in German (de)." in sent_user_content


def test_safety_prompt_carries_english_language_instruction_when_preferred(db, seeded, fake_llm):
    state = _booked_state(db)
    _set_language(db, 1, "en")
    client = fake_llm(
        [{"safe": True, "violations": [], "rewritten": "Your appointment is confirmed."}]
    )

    safety.run(state, db)

    sent_user_content = client.chat.completions.calls[0]["messages"][1]["content"]
    assert "Respond in English (en)." in sent_user_content


# --- Model Armor screens the draft, the deterministic sanitizer still last ---


_CONFIRMATION = "Your appointment is confirmed."


def _armor_audit(db) -> AuditEvent | None:
    return db.query(AuditEvent).filter_by(action="safety.model_armor_blocked").first()


def test_model_armor_flagged_draft_becomes_the_referral_text_with_an_audit_row(
    db, seeded, fake_llm, model_armor_on, fake_model_armor
):
    state = _booked_state(db)
    fake_llm([{"safe": True, "violations": [], "rewritten": _CONFIRMATION}])
    fake_model_armor(response={"pi_and_jailbreak": True})

    result = safety.run(state, db)

    assert result["final_response"] == SANITIZED_SENTENCE
    row = _armor_audit(db)
    assert row is not None
    assert row.metadata_json == {"categories": ["pi_and_jailbreak"]}


def test_model_armor_audit_row_carries_categories_and_not_the_draft(
    db, seeded, fake_llm, model_armor_on, fake_model_armor
):
    state = _booked_state(db)
    fake_llm(
        [{"safe": True, "violations": [], "rewritten": "Max Mustermann, you have diabetes."}]
    )
    fake_model_armor(response={"pi_and_jailbreak": True})

    safety.run(state, db)

    row = _armor_audit(db)
    assert "Mustermann" not in str(row.metadata_json)
    assert "diabetes" not in str(row.metadata_json)


def test_clean_model_armor_verdict_leaves_the_draft_untouched(
    db, seeded, fake_llm, model_armor_on, fake_model_armor
):
    state = _booked_state(db)
    expected = safety._compose_draft(db, 1, state["appointment"], "en")
    fake_llm([{"safe": True, "violations": [], "rewritten": _CONFIRMATION}])
    client = fake_model_armor(response={"pi_and_jailbreak": False, "malicious_uris": False})

    result = safety.run(state, db)

    assert result["final_response"] == expected
    assert _armor_audit(db) is None
    assert client.response_calls[0]["request"].model_response_data.text == expected


def test_model_armor_failure_leaves_the_draft_to_the_deterministic_sanitizer(
    db, seeded, fake_llm, model_armor_on, fake_model_armor
):
    """No opinion is not a block. A screening outage must not turn every
    confirmation into a referral."""
    state = _booked_state(db)
    expected = safety._compose_draft(db, 1, state["appointment"], "en")
    fake_llm([{"safe": True, "violations": [], "rewritten": _CONFIRMATION}])
    fake_model_armor(response=RuntimeError("deadline exceeded"))

    result = safety.run(state, db)

    assert result["final_response"] == expected
    assert _armor_audit(db) is None


def test_deterministic_sanitizer_still_has_the_last_word_after_a_clean_armor_verdict(
    db, seeded, fake_llm, model_armor_on, fake_model_armor
):
    """Model Armor saying nothing is wrong does not publish a diagnosis."""
    state = _booked_state(db)
    appointment_row = db.get(Appointment, state["appointment"]["id"])
    appointment_row.doctor.name = "Dr. Unsafe. You have diabetes"
    db.commit()
    fake_llm([{"safe": True, "violations": [], "rewritten": _CONFIRMATION}])
    fake_model_armor(response={"pi_and_jailbreak": False})

    result = safety.run(state, db)

    assert "diabetes" not in result["final_response"]
    assert SANITIZED_SENTENCE in result["final_response"]
    assert "deterministic_sanitizer_rewrote_output" in result["safety_flags"]


def test_disabled_model_armor_never_screens_the_draft(db, seeded, fake_llm, fake_model_armor):
    """With no template configured the path is what it was: no call, no audit
    row, the same answer."""
    state = _booked_state(db)
    expected = safety._compose_draft(db, 1, state["appointment"], "en")
    fake_llm([{"safe": True, "violations": [], "rewritten": _CONFIRMATION}])
    client = fake_model_armor(response={"pi_and_jailbreak": True})

    result = safety.run(state, db)

    assert result["final_response"] == expected
    assert client.response_calls == []
    assert _armor_audit(db) is None


def test_deterministic_fallback_uses_german_template_for_preferred_language_de(
    db, seeded, fake_llm
):
    """The MOSAIC fallback path (LLM down) must not silently answer in
    English for a German-preferring patient."""
    state = _booked_state(db)
    _set_language(db, 1, "de")
    fake_llm([RuntimeError("llm down")])

    result = safety.run(state, db)

    final = result["final_response"]
    assert "Ihr Termin bei" in final
    assert "bestätigt" in final
    assert "confirmed" not in final.lower()
