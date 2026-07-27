"""PII-boundary coverage for PIIRedactor's five categories, the shared
`redact_for_llm` wrapper, and the routing, coordinator, appointment and
document nodes that build LLM prompts from patient-submitted request or
document text.
"""

from __future__ import annotations

import pytest
from presidio_analyzer import RecognizerResult

from app.agents import appointment, coordinator, document, routing
from app.models import AppointmentSlot, AuditEvent, Department, Doctor, PatientDocument
from app.safety import pii
from app.safety.pii import PIIRedactor, redact_for_llm, resolve_language

# --- PIIRedactor: one category at a time ------------------------------------


def test_email_redacted():
    text, counts = PIIRedactor().redact("contact me at jane.doe@example.com please")
    assert text == "contact me at [REDACTED_EMAIL] please"
    assert counts == {"email": 1}


def test_phone_international_redacted():
    text, counts = PIIRedactor().redact("call me at +49 176 12345678 today")
    assert text == "call me at [REDACTED_PHONE] today"
    assert counts == {"phone": 1}


def test_phone_german_national_redacted():
    text, counts = PIIRedactor().redact("call 0176-12345678 or (030) 12345678")
    assert text == "call [REDACTED_PHONE] or [REDACTED_PHONE]"
    assert counts == {"phone": 2}


def test_phone_generic_redacted():
    text, counts = PIIRedactor().redact("reach me at 555-123-4567")
    assert text == "reach me at [REDACTED_PHONE]"
    assert counts == {"phone": 1}


def test_dob_with_context_redacted_any_year():
    text, counts = PIIRedactor().redact("I was born 15.03.1990 in Munich")
    assert text == "I was [REDACTED_DOB] in Munich"
    assert counts == {"date_of_birth": 1}


def test_dob_german_geb_context_redacted():
    text, counts = PIIRedactor().redact("geb. 1990-03-15, patient record")
    assert text == "[REDACTED_DOB], patient record"
    assert counts == {"date_of_birth": 1}


def test_dob_standalone_in_plausible_birth_year_range_redacted():
    text, counts = PIIRedactor().redact("my date is 15.03.1990 on the form")
    assert text == "my date is [REDACTED_DOB] on the form"
    assert counts == {"date_of_birth": 1}


def test_health_insurance_number_redacted():
    text, counts = PIIRedactor().redact("my insurance number is A123456789 please note it")
    assert text == "my insurance number is [REDACTED_HEALTH_INSURANCE] please note it"
    assert counts == {"health_insurance": 1}


def test_iban_de_redacted():
    text, counts = PIIRedactor().redact("refund to DE89370400440532013000 thanks")
    assert text == "refund to [REDACTED_IBAN] thanks"
    assert counts == {"iban": 1}


def test_iban_generic_non_de_redacted():
    text, counts = PIIRedactor().redact("my account is AT611904300234573201")
    assert text == "my account is [REDACTED_IBAN]"
    assert counts == {"iban": 1}


# --- Mixed text and no-op cases ----------------------------------------------


def test_mixed_text_redacts_every_category_present():
    text = (
        "I'm Jane, born 15.03.1990, email jane.doe@example.com, phone "
        "+49 176 12345678, IBAN DE89370400440532013000, insurance A123456789."
    )
    redacted, counts = PIIRedactor().redact(text)
    assert "[REDACTED_DOB]" in redacted
    assert "[REDACTED_EMAIL]" in redacted
    assert "[REDACTED_PHONE]" in redacted
    assert "[REDACTED_IBAN]" in redacted
    assert "[REDACTED_HEALTH_INSURANCE]" in redacted
    assert counts == {
        "date_of_birth": 1,
        "email": 1,
        "phone": 1,
        "iban": 1,
        "health_insurance": 1,
    }
    # no raw PII survives
    assert "jane.doe@example.com" not in redacted
    assert "12345678" not in redacted
    assert "DE89370400440532013000" not in redacted
    assert "A123456789" not in redacted


def test_clean_admin_text_untouched_and_empty_counts():
    text = "Book me a cardiology appointment next week"
    redacted, counts = PIIRedactor().redact(text)
    assert redacted == text
    assert counts == {}


def test_future_appointment_date_not_redacted_as_dob():
    """A standalone date outside the 1900-2015 birth-year window (e.g. an
    ordinary future appointment ask) must survive untouched - this is the
    conservative half of the DOB rule, see safety/pii.py's module comment."""
    text, counts = PIIRedactor().redact("please book me for 15.08.2026")
    assert text == "please book me for 15.08.2026"
    assert counts == {}


def test_redact_for_llm_is_the_same_shared_pipeline():
    text, counts = redact_for_llm("email jane.doe@example.com")
    assert text == "email [REDACTED_EMAIL]"
    assert counts == {"email": 1}


# --- resolve_language: which model reads the text ----------------------------
# One precedence for every caller: a positive cue in the text wins, the stored
# preference breaks a no-cue tie, English is the last fallback. No engines
# needed - this is the regex cue test and nothing else.

_GERMAN_CUED = "Ich brauche einen Termin in der Kardiologie"
_NO_CUE = "cancel booking 4711"


def test_resolve_language_german_cue_beats_an_english_preference():
    """The reason this function exists: preferred_language defaults to "en"
    for every patient who never chose otherwise, and the English model reads
    "Termin" as a location."""
    assert resolve_language(_GERMAN_CUED, "en") == "de"


def test_resolve_language_german_cue_agrees_with_a_german_preference():
    assert resolve_language(_GERMAN_CUED, "de") == "de"


def test_resolve_language_english_text_under_a_german_preference_keeps_it():
    """The documented, accepted limitation: this module has a German-positive
    cue test and no English-positive one, so English text reads as a no-cue
    tie and the stored preference takes it. Over-redaction, never a leak."""
    assert resolve_language("Can I reschedule my appointment to next week?", "de") == "de"


def test_resolve_language_no_cue_falls_to_the_preference():
    assert resolve_language(_NO_CUE, "de") == "de"
    assert resolve_language(_NO_CUE, "en") == "en"


def test_resolve_language_no_cue_and_no_preference_falls_back_to_english():
    assert resolve_language(_NO_CUE, None) == "en"


def test_resolve_language_ignores_a_language_with_no_model():
    """A preference this module has no spaCy model for is not a language it
    can run, so it falls through to the same fallback as no preference."""
    assert resolve_language(_NO_CUE, "fr") == "en"
    assert resolve_language(_GERMAN_CUED, "fr") == "de"


# --- Node wiring: routing, coordinator, appointment, document ----------------
# Each node builds the user content passed to invoke_structured from
# patient-submitted text (request_text or a document's extracted_text). These
# tests assert the messages sent to the fake LLM carry the redacted token,
# never the raw value, and that a "safety.pii_redacted" audit row records counts
# (and nothing else) exactly once per node call.


def _pii_audit(db, workflow_id: int):
    return (
        db.query(AuditEvent)
        .filter_by(action="safety.pii_redacted", entity_type="workflow_run", entity_id=workflow_id)
        .all()
    )


def test_routing_node_redacts_request_text_before_the_llm_call(db, seeded, fake_llm):
    client = fake_llm(
        [{"intent": "book", "department": "Cardiology", "confidence": 0.92, "reason": "explicit ask"}]
    )
    state = {
        "workflow_id": 1,
        "user_id": 1,
        "patient_id": 1,
        "request_text": "Book cardiology, my email is jane.doe@example.com",
    }

    routing.run(state, db)

    sent_user_content = client.chat.completions.calls[0]["messages"][1]["content"]
    assert "[REDACTED_EMAIL]" in sent_user_content
    assert "jane.doe@example.com" not in sent_user_content

    rows = _pii_audit(db, 1)
    assert len(rows) == 1
    assert rows[0].metadata_json["counts"] == {"email": 1}
    assert "jane.doe@example.com" not in str(rows[0].metadata_json)


def test_appointment_node_redacts_request_text_before_the_llm_call(db, seeded, fake_llm):
    """The slot-picking prompt was the leak the Phase 4 verifier found: raw
    request_text went to the LLM while every other node redacted it. This
    pins the fix - the DB-stored appointment reason keeps the raw text, only
    the LLM-bound copy is redacted."""
    free_slot = (
        db.query(AppointmentSlot)
        .join(Doctor)
        .join(Department)
        .filter(Department.name == "Cardiology", AppointmentSlot.status == "free")
        .first()
    )
    client = fake_llm([{"slot_id": free_slot.id, "reason": "earliest"}])
    state = {
        "workflow_id": 1,
        "patient_id": 1,
        "intent": "book",
        "department_id": free_slot.doctor.department_id,
        "request_text": "Book me for next week, my number is +49 176 12345678",
    }

    result = appointment.run(state, db)

    sent_user_content = client.chat.completions.calls[0]["messages"][1]["content"]
    assert "[REDACTED_PHONE]" in sent_user_content
    assert "12345678" not in sent_user_content

    rows = _pii_audit(db, 1)
    assert len(rows) == 1
    assert rows[0].metadata_json == {"node": "appointment", "counts": {"phone": 1}}

    # The persisted appointment keeps the raw reason: redaction is only for
    # the provider boundary, the DB stays the system of record.
    assert result["appointment"]["status"] == "confirmed"


def test_coordinator_node_redacts_request_text_before_the_llm_call(db, seeded, fake_llm):
    client = fake_llm([{"next_step": "route_department", "reasoning": "start"}])
    state = {
        "workflow_id": 1,
        "request_text": "Reach me at +49 176 12345678 about my booking",
    }

    coordinator.run(state, db)

    sent_user_content = client.chat.completions.calls[0]["messages"][1]["content"]
    assert "[REDACTED_PHONE]" in sent_user_content
    assert "12345678" not in sent_user_content

    rows = _pii_audit(db, 1)
    assert len(rows) == 1
    assert rows[0].metadata_json["counts"] == {"phone": 1}


def _store_doc(db, *, patient_id=1, filename="doc.txt", text="") -> PatientDocument:
    doc = PatientDocument(
        patient_id=patient_id,
        filename=filename,
        document_type="other",
        checksum=f"chk-{filename}",
        storage_ref=f"local://{patient_id}/{filename}",
        extracted_text=text,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def test_document_node_redacts_extracted_text_before_the_llm_call(db, seeded, fake_llm):
    doc = _store_doc(
        db, filename="insurance.txt", text="Insurance card, contact jane.doe@example.com"
    )
    client = fake_llm([{"document_type": "insurance_card", "confidence": 0.8}])

    document.run({"workflow_id": 1, "patient_id": 1, "uploaded_document_ids": [doc.id]}, db)

    sent_user_content = client.chat.completions.calls[0]["messages"][1]["content"]
    assert "[REDACTED_EMAIL]" in sent_user_content
    assert "jane.doe@example.com" not in sent_user_content

    rows = _pii_audit(db, 1)
    assert len(rows) == 1
    assert rows[0].metadata_json["counts"] == {"email": 1}


def test_document_node_writes_one_audit_row_aggregating_counts_across_documents(
    db, seeded, fake_llm
):
    """Two documents with PII in the same node call must still produce
    exactly one "safety.pii_redacted" audit row, with counts summed across
    both documents - "once per node call", not once per document."""
    doc_a = _store_doc(db, filename="a.txt", text="email a@example.com")
    doc_b = _store_doc(db, filename="b.txt", text="email b@example.com")
    fake_llm(
        [
            {"document_type": "insurance_card", "confidence": 0.8},
            {"document_type": "referral_letter", "confidence": 0.8},
        ]
    )

    document.run(
        {"workflow_id": 1, "patient_id": 1, "uploaded_document_ids": [doc_a.id, doc_b.id]}, db
    )

    rows = _pii_audit(db, 1)
    assert len(rows) == 1
    assert rows[0].metadata_json["counts"] == {"email": 2}


def test_document_node_no_audit_row_when_nothing_to_redact(db, seeded, fake_llm):
    doc = _store_doc(db, filename="clean.txt", text="ECG report, sinus rhythm")
    fake_llm([{"document_type": "ecg_report", "confidence": 0.9}])

    document.run({"workflow_id": 1, "patient_id": 1, "uploaded_document_ids": [doc.id]}, db)

    assert _pii_audit(db, 1) == []


def test_routing_node_no_audit_row_when_nothing_to_redact(db, seeded, fake_llm):
    fake_llm(
        [{"intent": "book", "department": "Cardiology", "confidence": 0.92, "reason": "ask"}]
    )
    state = {
        "workflow_id": 1,
        "user_id": 1,
        "patient_id": 1,
        "request_text": "I need a cardiology appointment next week",
    }

    routing.run(state, db)

    assert _pii_audit(db, 1) == []


# --- Safety node: agent-composed confirmations are never redacted -----------


def test_safety_node_draft_reaches_llm_with_no_redaction_tokens(db, seeded, fake_llm):
    """redact_for_llm applies to inbound user text only (routing, coordinator,
    document). agents/safety.py composes its LLM-bound draft from freshly
    queried database rows (appointment status, doctor, department, date),
    never from request_text, so it must never call redact_for_llm - proven
    here by checking the exact message sent to the fake LLM carries no
    "[REDACTED_" token at all, and the appointment's raw ISO date string
    survives verbatim."""
    from app.agents import safety
    from app.models import AppointmentSlot
    from app.tools.appointment_tools import book_appointment

    slot = (
        db.query(AppointmentSlot)
        .filter_by(status="free")
        .order_by(AppointmentSlot.start_time)
        .first()
    )
    booking = book_appointment(db, patient_id=1, slot_id=slot.id, reason="checkup")
    client = fake_llm(
        [{"safe": True, "violations": [], "rewritten": "Your appointment is confirmed."}]
    )

    safety.run(
        {"workflow_id": 1, "patient_id": 1, "appointment": booking, "documents_result": None}, db
    )

    sent_user_content = client.chat.completions.calls[0]["messages"][1]["content"]
    assert "[REDACTED_" not in sent_user_content
    assert booking["start_time"] in sent_user_content
    assert _pii_audit(db, 1) == []


# --- Presidio pass ------------------------------------------------------------
# `redact_for_llm` runs the regex families first, then Presidio's analyzer over
# what is left, so names, locations and anything the regex families cannot
# describe are redacted too. `PIIRedactor.redact` stays the pure regex pass, so
# every test above this line still describes exactly one deterministic rule.


class _RecordingLogger:
    """Stands in for the module's structlog logger and keeps the event names.

    structlog is configured with a PrintLoggerFactory and cached bound loggers,
    so pytest's caplog sees nothing (same reason and same shape as
    tests/test_observability.py)."""

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


@pytest.fixture
def stub_presidio(monkeypatch):
    """Replace the lazily built Presidio engines with a test double.

    The engines are an expensive module-level singleton, so a test that swaps
    the builder has to clear the cache going in (or it keeps the real engines)
    and going out (or the next test keeps the double)."""

    def _install(builder):
        monkeypatch.setattr(pii, "_build_engines", builder)
        pii.reset_engines_for_tests()

    yield _install
    pii.reset_engines_for_tests()


@pytest.fixture
def recording_logger(monkeypatch):
    logger = _RecordingLogger()
    monkeypatch.setattr(pii, "logger", logger)
    return logger


def test_english_name_and_location_redacted():
    text, counts = redact_for_llm("My name is John Smith and I live in London.")
    assert text == "My name is [REDACTED_NAME] and I live in [REDACTED_LOCATION]."
    assert counts == {"name": 1, "location": 1}


def test_english_name_and_address_redacted():
    text, counts = redact_for_llm("Patient John Smith, 221B Baker Street, London, needs a slot.")
    assert "[REDACTED_NAME]" in text
    assert "[REDACTED_LOCATION]" in text
    assert "John Smith" not in text
    assert "London" not in text
    assert counts["name"] >= 1
    assert counts["location"] == 1


def test_german_sentence_name_and_location_redacted():
    text, counts = redact_for_llm("Mein Name ist Erika Mustermann und ich wohne in Berlin")
    assert "[REDACTED_NAME]" in text
    assert "[REDACTED_LOCATION]" in text
    assert "Erika Mustermann" not in text
    assert "Berlin" not in text
    assert counts["name"] >= 1
    assert counts["location"] == 1


def test_german_language_argument_runs_the_german_pipeline():
    text, counts = redact_for_llm(
        "Ich heisse Erika Mustermann, Hauptstrasse 5, Berlin.", language="de"
    )
    assert "[REDACTED_NAME]" in text
    assert "Erika Mustermann" not in text
    assert "Berlin" not in text
    assert counts["name"] >= 1


def test_english_text_is_not_read_with_the_german_model():
    """de_core_news_sm tags ordinary English admin words as names ("Book",
    "sinus rhythm"), so the German pipeline only runs on text that carries
    German cues or when the caller names the language. Without that rule an
    English request would come out half redacted."""
    text, counts = redact_for_llm("ECG report, sinus rhythm")
    assert text == "ECG report, sinus rhythm"
    assert counts == {}


def test_clean_admin_text_survives_both_passes():
    text, counts = redact_for_llm("Book me a cardiology appointment next week")
    assert text == "Book me a cardiology appointment next week"
    assert counts == {}


def test_german_booking_text_survives_both_passes():
    """The German half of the test above, on the auto path production takes.

    en_core_web_sm reading German tags "Termin" and "Kardiologie" as LOCATION at
    0.85, so a booking request would lose the department name before routing
    ever sees it and `routing.run` would escalate to staff review. Clean German
    booking prose must come out byte for byte."""
    text, counts = redact_for_llm("Ich brauche einen Termin in der Kardiologie naechste Woche.")
    assert text == "Ich brauche einen Termin in der Kardiologie naechste Woche."
    assert counts == {}


def test_german_booking_text_keeps_the_department_while_real_pii_goes():
    """Same sentence carrying a real phone number: the number is redacted and
    the two words routing needs survive. Detection on German text must not be
    the price of the rule above."""
    text, counts = redact_for_llm(
        "Ich brauche einen Termin in der Kardiologie, meine Nummer ist 0176 12345678."
    )
    assert "[REDACTED_PHONE]" in text
    assert "12345678" not in text
    assert "Termin" in text
    assert "Kardiologie" in text
    assert counts == {"phone": 1}


def test_regex_pass_wins_where_presidio_overlaps():
    """Presidio also recognizes emails. The regex pass ran first, so the value
    is already a token by then: exactly one token, counted exactly once, and
    the token itself is never redacted a second time."""
    text, counts = redact_for_llm("contact me at jane.doe@example.com please")
    assert text == "contact me at [REDACTED_EMAIL] please"
    assert text.count("[REDACTED_EMAIL]") == 1
    assert counts == {"email": 1}


def test_presidio_result_below_the_score_threshold_is_ignored(stub_presidio):
    """A weak guess never rewrites the text. The stub returns one result under
    `_PRESIDIO_SCORE_THRESHOLD` and one over it."""
    from presidio_anonymizer import AnonymizerEngine

    threshold = pii._PRESIDIO_SCORE_THRESHOLD

    class _StubAnalyzer:
        def analyze(self, text, language, entities):
            return [
                RecognizerResult(entity_type="PERSON", start=0, end=4, score=threshold - 0.01),
                RecognizerResult(entity_type="LOCATION", start=14, end=20, score=threshold),
            ]

    stub_presidio(
        lambda: pii._Engines(
            analyzer=_StubAnalyzer(),
            anonymizer=AnonymizerEngine(),
            operators=pii._build_operators(),
        )
    )

    text, counts = redact_for_llm("Alex lives in Berlin")

    assert text == "Alex lives in [REDACTED_LOCATION]"
    assert counts == {"location": 1}


def test_engine_build_failure_falls_back_to_the_regex_result(stub_presidio, recording_logger):
    def _raise():
        raise RuntimeError("no spacy model installed")

    stub_presidio(_raise)

    text, counts = redact_for_llm("My name is John Smith, email jane.doe@example.com")

    assert text == "My name is John Smith, email [REDACTED_EMAIL]"
    assert counts == {"email": 1}
    assert recording_logger.events == ["pii_presidio_unavailable"]


def test_engine_build_failure_is_logged_once_not_per_request(stub_presidio, recording_logger):
    """A broken install must not print a warning per patient request."""

    def _raise():
        raise RuntimeError("no spacy model installed")

    stub_presidio(_raise)

    for _ in range(3):
        redact_for_llm("My name is John Smith")

    assert recording_logger.events == ["pii_presidio_unavailable"]


def test_analysis_failure_falls_back_to_the_regex_result(stub_presidio, recording_logger):
    class _BrokenAnalyzer:
        def analyze(self, text, language, entities):
            raise RuntimeError("analysis blew up")

    stub_presidio(
        lambda: pii._Engines(analyzer=_BrokenAnalyzer(), anonymizer=None, operators={})
    )

    text, counts = redact_for_llm("My name is John Smith, email jane.doe@example.com")

    assert text == "My name is John Smith, email [REDACTED_EMAIL]"
    assert counts == {"email": 1}
    assert recording_logger.events == ["pii_presidio_failed"]


def test_routing_node_audit_counts_carry_the_new_categories(db, seeded, fake_llm):
    """The audit row keeps its shape: category counts only, no raw value."""
    client = fake_llm(
        [{"intent": "book", "department": "Cardiology", "confidence": 0.9, "reason": "ask"}]
    )
    state = {
        "workflow_id": 1,
        "user_id": 1,
        "patient_id": 1,
        "request_text": "Book cardiology for John Smith",
    }

    routing.run(state, db)

    sent_user_content = client.chat.completions.calls[0]["messages"][1]["content"]
    assert "[REDACTED_NAME]" in sent_user_content
    assert "John Smith" not in sent_user_content

    rows = _pii_audit(db, 1)
    assert len(rows) == 1
    assert rows[0].metadata_json["counts"] == {"name": 1}
    assert "John Smith" not in str(rows[0].metadata_json)
