"""Document agent: classifies any upload still typed "other", then reports
which of the department's required document types are on file.

Owns app.tools.document_tools. Classification reads the already-extracted
text persisted on the row at upload time (document_tools.store_document
already ran extract_text and truncated it to 1500 chars) - there is no raw
file content to re-extract from at this point in the workflow, only the DB
row, which is the real-DB-work the tool layer already did.

Extracted text comes from a file a patient uploaded, so it is screened by
the prompt-injection guard (Task F1) before it goes into the classification
prompt. A poisoned document is left typed "other" and its own audit event is
written, but it never kills the run - the rest of the uploaded documents
still get classified and the workflow continues.

Whatever survives the injection guard is then redacted (Task F2,
`safety/pii.py::redact_for_llm`) before it reaches the classification
prompt, since extracted text is still patient-submitted content on its way
to the LLM provider. Counts are summed across every document processed in
one `run()` call and reported in a single "safety.pii_redacted" audit row,
not one row per document.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agents.prompts import DOCUMENT
from app.agents.llm import chat_json
from app.agents.memory import build_system_prompt
from app.agents.state import AgentState
from app.logging_setup import get_logger
from app.models import PatientDocument
from app.safety.injection_guard import screen_injection
from app.safety.pii import redact_for_llm
from app.tools.audit_tools import write_audit
from app.tools.document_tools import check_required_documents

logger = get_logger(__name__)

_UNKNOWN_TYPE = "other"

# A classification the model is not sure about is a guess, and a wrong
# document_type quietly changes what the department counts as missing. Under
# this threshold the guess stays in the audit trail for staff and never
# reaches the row. This is the document node's own number; routing.py sets a
# different one for a different decision.
_CONFIDENCE_THRESHOLD = 0.6


class DocumentOutput(BaseModel):
    document_type: Literal[
        "ecg_report", "blood_test", "referral_letter", "imaging_report", "insurance_card", "other"
    ]
    confidence: float


def _classification_prompt(filename: str, extracted_text: str) -> str:
    return f"Filename: {filename}\nExtracted text (may be empty): {extracted_text}"


def _exit_audit(db: Session, workflow_id: int | None, summary: dict) -> None:
    write_audit(db, None, "agent.document.completed", "workflow_run", workflow_id, summary)
    db.commit()


def _merge_counts(total: dict[str, int], counts: dict[str, int]) -> None:
    for category, count in counts.items():
        total[category] = total.get(category, 0) + count


def _audit_pii_redacted(db: Session, workflow_id: int | None, counts: dict[str, int]) -> None:
    """One audit row per `run()` call, aggregating counts across every
    document processed - never one row per document."""
    if counts:
        write_audit(
            db,
            None,
            "safety.pii_redacted",
            "workflow_run",
            workflow_id,
            {"node": "document", "counts": counts},
        )


def run(state: AgentState, db: Session) -> dict:
    """Classify unknown-type uploads, then report required-document coverage."""
    workflow_id = state.get("workflow_id")
    try:
        patient_id = state["patient_id"]
        doc_ids = state.get("uploaded_document_ids") or []
        system = build_system_prompt(db, "document", DOCUMENT)

        classified: list[dict] = []
        pii_counts: dict[str, int] = {}
        for doc_id in doc_ids:
            doc = db.get(PatientDocument, doc_id)
            if doc is None or doc.document_type != _UNKNOWN_TYPE:
                continue

            injection = screen_injection(doc.extracted_text or "")
            if injection.action == "block":
                doc.document_type = _UNKNOWN_TYPE
                db.flush()
                write_audit(
                    db,
                    None,
                    "safety.injection_blocked_document",
                    "patient_document",
                    doc.id,
                    {"matched": injection.matched, "via": injection.via},
                )
                continue

            redacted_text, counts = redact_for_llm(doc.extracted_text or "")
            _merge_counts(pii_counts, counts)
            result = chat_json(
                system, _classification_prompt(doc.filename, redacted_text), DocumentOutput
            )
            if result.confidence < _CONFIDENCE_THRESHOLD:
                write_audit(
                    db,
                    None,
                    "document.low_confidence",
                    "patient_document",
                    doc.id,
                    {"confidence": result.confidence, "suggested": result.document_type},
                )
                continue

            doc.document_type = result.document_type
            db.flush()
            classified.append(
                {"id": doc.id, "document_type": result.document_type, "confidence": result.confidence}
            )
        _audit_pii_redacted(db, workflow_id, pii_counts)
        db.commit()

        department_id = state.get("department_id")
        if department_id is not None:
            documents_result = check_required_documents(db, patient_id, department_id)
        else:
            documents_result = {"required": [], "present": [], "missing": []}
        documents_result["classified"] = classified

        update = {"documents_result": documents_result, "completed_steps": ["document"]}
        _exit_audit(db, workflow_id, {"classified_count": len(classified)})
        return update
    except Exception as exc:  # noqa: BLE001 - node boundary must never crash the graph
        logger.error("document_agent_failed", workflow_id=workflow_id, error=str(exc))
        db.rollback()
        _exit_audit(db, workflow_id, {"error": str(exc)})
        return {"error": f"document agent failed: {exc}", "completed_steps": ["document"]}
