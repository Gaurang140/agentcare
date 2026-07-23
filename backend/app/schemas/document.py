"""Response schemas for document metadata and listing."""

from datetime import datetime

from pydantic import BaseModel


class DocumentMeta(BaseModel):
    id: int
    patient_id: int
    filename: str
    document_type: str
    created_at: datetime


class DocumentListResponse(BaseModel):
    """GET /documents: the patient's stored documents, plus - when
    `department_id` was given - the present/missing badges from
    `check_required_documents`."""

    documents: list[DocumentMeta]
    requirements: dict | None = None
