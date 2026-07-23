"""TDD for app.agents.document.run: classify unknown-type uploads via the
LLM, persist the classification, then report required-document coverage.
"""

from __future__ import annotations

from app.agents import document
from app.models import Department, PatientDocument


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


def test_no_uploaded_documents_still_reports_required_documents(db, seeded, fake_llm):
    dept_id = _cardiology_id(db)
    client = fake_llm([])

    result = document.run(_state(department_id=dept_id), db)

    assert result["documents_result"]["missing"] == ["ecg_report", "blood_test"]
    assert len(client.chat.completions.calls) == 0
