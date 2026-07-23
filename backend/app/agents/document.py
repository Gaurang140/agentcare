"""Document agent: classifies any upload still typed "other", then reports
which of the department's required document types are on file.

Owns app.tools.document_tools. Classification reads the already-extracted
text persisted on the row at upload time (document_tools.store_document
already ran extract_text and truncated it to 1500 chars) - there is no raw
file content to re-extract from at this point in the workflow, only the DB
row, which is the real-DB-work the tool layer already did.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agents.prompts import DOCUMENT
from app.agents.llm import chat_json
from app.agents.state import AgentState
from app.logging_setup import get_logger
from app.models import PatientDocument
from app.tools.audit_tools import write_audit
from app.tools.document_tools import check_required_documents

logger = get_logger(__name__)

_UNKNOWN_TYPE = "other"


class DocumentOutput(BaseModel):
    document_type: Literal[
        "ecg_report", "blood_test", "referral_letter", "imaging_report", "insurance_card", "other"
    ]
    confidence: float


def _classification_prompt(doc: PatientDocument) -> str:
    return f"Filename: {doc.filename}\nExtracted text (may be empty): {doc.extracted_text or ''}"


def _exit_audit(db: Session, workflow_id: int | None, summary: dict) -> None:
    write_audit(db, None, "agent.document.completed", "workflow_run", workflow_id, summary)
    db.commit()


def run(state: AgentState, db: Session) -> dict:
    """Classify unknown-type uploads, then report required-document coverage."""
    workflow_id = state.get("workflow_id")
    try:
        patient_id = state["patient_id"]
        doc_ids = state.get("uploaded_document_ids") or []

        classified: list[dict] = []
        for doc_id in doc_ids:
            doc = db.get(PatientDocument, doc_id)
            if doc is None or doc.document_type != _UNKNOWN_TYPE:
                continue
            result = chat_json(DOCUMENT, _classification_prompt(doc), DocumentOutput)
            doc.document_type = result.document_type
            db.flush()
            classified.append(
                {"id": doc.id, "document_type": result.document_type, "confidence": result.confidence}
            )
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
