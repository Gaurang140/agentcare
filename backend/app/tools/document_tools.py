"""Document storage, checksum dedup, text extraction, and requirement checks."""

from __future__ import annotations

import hashlib
import io

from pypdf import PdfReader
from sqlalchemy.orm import Session

from app.models import PatientDocument, RequiredDocument
from app.services.storage import get_storage
from app.tools.audit_tools import write_audit

_PDF_EXTRACT_CHARS = 1500

# Filename substring -> document_type, checked before any caller-supplied
# LLM classification. The first matching hint group wins.
_FILENAME_TYPE_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("ecg",), "ecg_report"),
    (("blut", "blood"), "blood_test"),
    (("überweisung", "ueberweisung", "referral"), "referral_letter"),
)


def _classify_by_filename(filename: str) -> str | None:
    lowered = filename.lower()
    for hints, document_type in _FILENAME_TYPE_HINTS:
        if any(hint in lowered for hint in hints):
            return document_type
    return None


def extract_text(filename: str, content: bytes) -> str:
    """First ~1500 chars of PDF text, decoded .txt content, or "" otherwise.

    A .pdf that fails to parse (corrupt bytes, not actually a PDF) yields ""
    rather than raising - extraction is best-effort, never a hard failure
    for the caller storing the document.
    """
    lowered = filename.lower()
    if lowered.endswith(".pdf"):
        try:
            reader = PdfReader(io.BytesIO(content))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:  # noqa: BLE001 - extraction is best-effort
            return ""
        return text[:_PDF_EXTRACT_CHARS]
    if lowered.endswith(".txt"):
        return content.decode("utf-8", errors="ignore")
    return ""


def store_document(
    db: Session,
    patient_id: int,
    filename: str,
    content: bytes,
    document_type: str | None,
) -> dict:
    """Store new bytes, or flag a duplicate by (patient_id, checksum) without
    writing the bytes again.
    """
    checksum = hashlib.sha256(content).hexdigest()

    existing = (
        db.query(PatientDocument).filter_by(patient_id=patient_id, checksum=checksum).first()
    )
    if existing is not None:
        write_audit(
            db,
            None,
            "document.duplicate_detected",
            "patient_document",
            existing.id,
            {"patient_id": patient_id, "filename": filename},
        )
        db.commit()
        return {
            "id": existing.id,
            "duplicate": True,
            "existing_id": existing.id,
            "document_type": existing.document_type,
        }

    resolved_type = _classify_by_filename(filename) or document_type or "other"
    storage_ref = get_storage().save(patient_id, filename, content)
    extracted_text = extract_text(filename, content) or None

    doc = PatientDocument(
        patient_id=patient_id,
        filename=filename,
        document_type=resolved_type,
        checksum=checksum,
        storage_ref=storage_ref,
        extracted_text=extracted_text,
    )
    db.add(doc)
    db.flush()

    write_audit(
        db,
        None,
        "document.stored",
        "patient_document",
        doc.id,
        {"patient_id": patient_id, "document_type": resolved_type},
    )
    db.commit()

    return {
        "id": doc.id,
        "duplicate": False,
        "existing_id": None,
        "document_type": resolved_type,
    }


def check_required_documents(db: Session, patient_id: int, department_id: int) -> dict:
    """Which of department_id's required document types the patient has on file."""
    required_types = [
        req.document_type
        for req in db.query(RequiredDocument).filter_by(department_id=department_id).all()
    ]
    present_types = {
        doc.document_type
        for doc in db.query(PatientDocument).filter_by(patient_id=patient_id).all()
    }

    return {
        "required": required_types,
        "present": [t for t in required_types if t in present_types],
        "missing": [t for t in required_types if t not in present_types],
    }
