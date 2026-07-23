"""Response schema for the document metadata stub (Task 12 extends this)."""

from datetime import datetime

from pydantic import BaseModel


class DocumentMeta(BaseModel):
    id: int
    patient_id: int
    filename: str
    document_type: str
    created_at: datetime
