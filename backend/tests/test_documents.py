"""TDD for document storage: checksum dedup, filename classification, text
extraction, and per-department requirement checks.
"""

from __future__ import annotations

import pytest

from app.models import Department, PatientDocument
from app.tools.document_tools import check_required_documents, extract_text, store_document


@pytest.fixture(autouse=True)
def _local_upload_dir(tmp_path, monkeypatch):
    """Redirect LocalStorage at a per-test tmp dir instead of the real uploads/."""
    from app import config as config_module

    monkeypatch.setattr(config_module.settings, "upload_dir", str(tmp_path))


def _cardiology_id(db) -> int:
    dept = db.query(Department).filter_by(name="Cardiology").first()
    assert dept is not None
    return dept.id


def test_duplicate_document_detected(db, seeded):
    r1 = store_document(db, 1, "ecg_2024.pdf", b"%PDF-1.4 fake", "ecg_report")
    r2 = store_document(db, 1, "ecg_copy.pdf", b"%PDF-1.4 fake", "ecg_report")

    assert r1["duplicate"] is False and r2["duplicate"] is True
    assert r2["existing_id"] == r1["id"]


def test_duplicate_detection_is_scoped_per_patient(db, seeded):
    """Same bytes, different patient: not a duplicate - dedup key is (patient_id, checksum)."""
    r1 = store_document(db, 1, "ecg_2024.pdf", b"%PDF-1.4 fake", "ecg_report")
    r2 = store_document(db, 2, "ecg_2024.pdf", b"%PDF-1.4 fake", "ecg_report")

    assert r1["duplicate"] is False
    assert r2["duplicate"] is False
    assert r1["id"] != r2["id"]


def test_store_document_only_writes_bytes_once_for_duplicates(db, seeded, tmp_path):
    store_document(db, 1, "ecg_2024.pdf", b"%PDF-1.4 fake", "ecg_report")
    store_document(db, 1, "ecg_copy.pdf", b"%PDF-1.4 fake", "ecg_report")

    stored_files = list(tmp_path.rglob("*"))
    written = [p for p in stored_files if p.is_file()]
    assert len(written) == 1
    assert db.query(PatientDocument).filter_by(patient_id=1).count() == 1


def test_filename_classification_wins_over_caller_type(db, seeded):
    result = store_document(db, 1, "blutbild_2024.pdf", b"content", "referral_letter")
    assert result["document_type"] == "blood_test"


def test_missing_documents_for_cardiology(db, seeded):
    r = check_required_documents(db, patient_id=1, department_id=_cardiology_id(db))
    assert "blood_test" in r["missing"]
    assert "ecg_report" in r["missing"]


def test_check_required_documents_marks_present_after_upload(db, seeded):
    store_document(db, 1, "ecg_2024.pdf", b"%PDF-1.4 fake", None)

    r = check_required_documents(db, patient_id=1, department_id=_cardiology_id(db))
    assert "ecg_report" in r["present"]
    assert "ecg_report" not in r["missing"]
    assert "blood_test" in r["missing"]


def test_extract_text_reads_txt_with_utf8_ignore_errors():
    assert extract_text("notes.txt", "héllo".encode("utf-8")) == "héllo"
    # Invalid utf-8 byte sequence: errors="ignore" drops it instead of raising.
    assert extract_text("notes.txt", b"ok \xff bytes") == "ok  bytes"


def test_extract_text_returns_empty_for_unknown_extension():
    assert extract_text("image.png", b"\x89PNG\r\n") == ""


def test_extract_text_handles_invalid_pdf_bytes_gracefully():
    assert extract_text("broken.pdf", b"not a real pdf") == ""
