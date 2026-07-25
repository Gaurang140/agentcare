"""TDD for app.agents.document.run: classify unknown-type uploads via the
LLM, persist the classification, then report required-document coverage.
"""

from __future__ import annotations

from app.agents import document
from app.models import AuditEvent, Department, PatientDocument


def _cardiology_id(db) -> int:
    dept = db.query(Department).filter_by(name="Cardiology").first()
    assert dept is not None
    return dept.id


def _store_doc(db, *, patient_id=1, document_type="other", filename="scan.pdf", text="some text") -> PatientDocument:
    doc = PatientDocument(
        patient_id=patient_id,
        filename=filename,
        document_type=document_type,
        checksum=f"chk-{filename}-{document_type}",
        storage_ref=f"local://{patient_id}/{filename}",
        extracted_text=text,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def _state(**overrides) -> dict:
    base = {"workflow_id": 1, "patient_id": 1, "uploaded_document_ids": []}
    base.update(overrides)
    return base


def test_classifies_unknown_type_document_and_persists_it(db, seeded, fake_llm):
    doc = _store_doc(db, document_type="other")
    client = fake_llm([{"document_type": "blood_test", "confidence": 0.88}])

    result = document.run(_state(uploaded_document_ids=[doc.id]), db)

    db.refresh(doc)
    assert doc.document_type == "blood_test"
    assert result["documents_result"]["classified"] == [
        {"id": doc.id, "document_type": "blood_test", "confidence": 0.88}
    ]
    assert len(client.chat.completions.calls) == 1


def test_low_confidence_classification_is_not_persisted(db, seeded, fake_llm):
    """Under the confidence threshold the guess is recorded in the audit
    trail for staff, never written onto the document row."""
    doc = _store_doc(db, document_type="other")
    client = fake_llm([{"document_type": "blood_test", "confidence": 0.3}])

    result = document.run(_state(uploaded_document_ids=[doc.id]), db)

    db.refresh(doc)
    assert doc.document_type == "other"
    assert result["documents_result"]["classified"] == []
    assert len(client.chat.completions.calls) == 1

    audit = (
        db.query(AuditEvent)
        .filter_by(
            action="document.low_confidence",
            entity_type="patient_document",
            entity_id=doc.id,
        )
        .first()
    )
    assert audit is not None
    assert audit.metadata_json == {"confidence": 0.3, "suggested": "blood_test"}


def test_already_typed_document_is_not_reclassified(db, seeded, fake_llm):
    doc = _store_doc(db, document_type="insurance_card")
    client = fake_llm([])  # no LLM call expected

    result = document.run(_state(uploaded_document_ids=[doc.id]), db)

    db.refresh(doc)
    assert doc.document_type == "insurance_card"
    assert result["documents_result"]["classified"] == []
    assert len(client.chat.completions.calls) == 0


def test_reports_missing_required_documents_for_department(db, seeded, fake_llm):
    dept_id = _cardiology_id(db)  # requires ecg_report, blood_test
    doc = _store_doc(db, document_type="other", filename="ecg.pdf")
    fake_llm([{"document_type": "ecg_report", "confidence": 0.9}])

    result = document.run(
        _state(uploaded_document_ids=[doc.id], department_id=dept_id), db
    )

    dr = result["documents_result"]
    assert dr["required"] == ["ecg_report", "blood_test"]
    assert dr["present"] == ["ecg_report"]
    assert dr["missing"] == ["blood_test"]


def _prompt_of(client, index: int = 0) -> str:
    """The user message of the index-th classification call."""
    return client.chat.completions.calls[index]["messages"][1]["content"]


def test_filename_pii_is_redacted_before_it_reaches_the_prompt(db, seeded, fake_llm):
    """The filename is patient-supplied text on its way to the provider, so
    it crosses the same PII boundary the extracted text does. The stored row
    keeps the original."""
    doc = _store_doc(db, filename="scan-erika@example.com.pdf", text="ECG report")
    client = fake_llm([{"document_type": "ecg_report", "confidence": 0.9}])

    document.run(_state(uploaded_document_ids=[doc.id]), db)

    prompt = _prompt_of(client)
    assert "[REDACTED_EMAIL]" in prompt
    assert "erika@example.com" not in prompt

    db.refresh(doc)
    assert doc.filename == "scan-erika@example.com.pdf"

    audit = (
        db.query(AuditEvent)
        .filter_by(action="safety.pii_redacted", entity_type="workflow_run", entity_id=1)
        .first()
    )
    assert audit is not None
    assert audit.metadata_json["counts"]["email"] == 1


def test_patient_name_in_the_filename_is_redacted(db, seeded, fake_llm):
    """The common real case: whoever scanned the document named it after the
    patient. Underscores are read as word separators, so the NER pass sees a
    name where the raw string shows one token."""
    doc = _store_doc(db, filename="max_mustermann_ekg.pdf", text="ECG report, sinus rhythm")
    client = fake_llm([{"document_type": "ecg_report", "confidence": 0.9}])

    document.run(_state(uploaded_document_ids=[doc.id]), db)

    prompt = _prompt_of(client)
    assert "[REDACTED_NAME]" in prompt
    assert "mustermann" not in prompt.lower()


_ENGLISH_BODY = (
    "ECG report. Patient: Erika Mustermann, resident in Munich. Sinus rhythm, "
    "no further action. Insurance card on file. Book the follow-up appointment "
    "with the care team."
)


def test_german_cue_in_the_filename_does_not_switch_an_english_body(db, seeded, fake_llm):
    """The filename and the body are redacted in one pass, so the language the
    NER pass runs with is pinned from the body alone. A German word in the
    filename must not send an English report to the German model, which reads
    "Sinus rhythm" and "Book the" as person names."""
    doc = _store_doc(db, filename="befund_der_untersuchung.pdf", text=_ENGLISH_BODY)
    client = fake_llm([{"document_type": "ecg_report", "confidence": 0.9}])

    document.run(_state(uploaded_document_ids=[doc.id]), db)

    prompt = _prompt_of(client)
    assert "Sinus rhythm, no further action" in prompt
    assert "Book the follow-up appointment" in prompt
    # The body's real PII still goes, and the filename still reaches the model.
    assert "Mustermann" not in prompt
    assert "[REDACTED_NAME]" in prompt
    assert "Filename: befund der untersuchung.pdf" in prompt


def test_single_german_stopword_in_the_filename_does_not_switch_the_body(db, seeded, fake_llm):
    """One stopword is all the language heuristic needs, and a spaced filename
    hands it several. Same pin, the smallest cue."""
    doc = _store_doc(db, filename="ecg-und-labor.pdf", text=_ENGLISH_BODY)
    client = fake_llm([{"document_type": "ecg_report", "confidence": 0.9}])

    document.run(_state(uploaded_document_ids=[doc.id]), db)

    prompt = _prompt_of(client)
    assert "Sinus rhythm, no further action" in prompt
    assert "Book the follow-up appointment" in prompt
    assert "Mustermann" not in prompt


def test_german_body_is_still_read_with_the_german_model(db, seeded, fake_llm):
    """The other direction is unchanged: the body decides, so a German report
    keeps its German-model catches and its classification vocabulary."""
    doc = _store_doc(
        db,
        filename="report_2.pdf",
        text=(
            "Impfpass und Befund der Untersuchung. Patient: Erika Mustermann, "
            "wohnhaft in Muenchen. Bitte einen Termin in der Kardiologie "
            "vereinbaren. Die Untersuchung ergab einen normalen Sinusrhythmus."
        ),
    )
    client = fake_llm([{"document_type": "other", "confidence": 0.9}])

    document.run(_state(uploaded_document_ids=[doc.id]), db)

    prompt = _prompt_of(client)
    assert "Mustermann" not in prompt
    assert "[REDACTED_NAME]" in prompt
    assert "[REDACTED_LOCATION]" in prompt
    assert "Kardiologie" in prompt
    assert "Untersuchung" in prompt


def test_ordinary_filename_reaches_the_prompt_unchanged(db, seeded, fake_llm):
    doc = _store_doc(db, filename="impfpass.pdf", text="Impfpass")
    client = fake_llm([{"document_type": "other", "confidence": 0.9}])

    document.run(_state(uploaded_document_ids=[doc.id]), db)

    assert "Filename: impfpass.pdf\n" in _prompt_of(client)


def test_filename_is_normalized_before_use(db, seeded, fake_llm):
    """Zero-width and control characters go, whitespace collapses, word
    separators become spaces so the pattern layer can read the words, and the
    result is capped."""
    doc = _store_doc(db, filename="  blood_test\u200b\treport-2.pdf ", text="lab values")
    client = fake_llm([{"document_type": "blood_test", "confidence": 0.9}])

    document.run(_state(uploaded_document_ids=[doc.id]), db)

    assert "Filename: blood test report 2.pdf\n" in _prompt_of(client)


def test_overlong_filename_is_capped(db, seeded, fake_llm):
    """A filename can never be the bulk of the prompt, and what is past the
    cap never reaches the model."""
    doc = _store_doc(db, filename="befund_" * 40 + "ende.pdf", text="lab values")
    client = fake_llm([{"document_type": "blood_test", "confidence": 0.9}])

    document.run(_state(uploaded_document_ids=[doc.id]), db)

    prompt = _prompt_of(client)
    filename_line = prompt.splitlines()[0]
    assert len(filename_line) <= len("Filename: ") + 120
    assert "ende.pdf" not in prompt


def test_no_uploaded_documents_still_reports_required_documents(db, seeded, fake_llm):
    dept_id = _cardiology_id(db)
    client = fake_llm([])

    result = document.run(_state(department_id=dept_id), db)

    assert result["documents_result"]["missing"] == ["ecg_report", "blood_test"]
    assert len(client.chat.completions.calls) == 0
