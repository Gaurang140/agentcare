"""Request/response schemas for the workflow routes."""

from datetime import datetime

from pydantic import BaseModel

from app.schemas.document import DocumentMeta


class CreateRequestResponse(BaseModel):
    """POST /requests replies immediately, before the graph has run."""

    workflow_id: int
    status: str


class WorkflowRunDetail(BaseModel):
    """GET /workflows/{id}: the run plus everything it produced."""

    id: int
    status: str
    current_step: str | None
    request_text: str
    state: dict | None
    created_at: datetime
    updated_at: datetime
    appointment: dict | None = None
    documents: list[DocumentMeta] = []
    escalation: dict | None = None


class ResumeResponse(BaseModel):
    id: int
    status: str
