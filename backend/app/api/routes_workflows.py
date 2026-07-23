"""Starting a patient request, reading its timeline, and resuming it.

POST /requests stores any uploaded documents, screens + creates the
WorkflowRun row synchronously (`workflow_service.create_run`), and hands the
actual graph execution to a FastAPI BackgroundTasks callback that opens its
own db session - the request returns `{workflow_id, status}` immediately,
before the LLM has been called at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, UploadFile
from sqlalchemy.orm import Session

from app.auth.dependencies import ensure_owner_or_staff, get_current_user
from app.db import session as db_session_module
from app.db.session import get_db
from app.exceptions import NotFoundError, ValidationError
from app.logging_setup import get_logger
from app.models import Escalation, PatientDocument, User, WorkflowRun
from app.schemas.document import DocumentMeta
from app.schemas.workflow import CreateRequestResponse, ResumeResponse, WorkflowRunDetail
from app.services import workflow_service
from app.tools.audit_tools import write_audit
from app.tools.document_tools import store_document

logger = get_logger(__name__)

requests_router = APIRouter(tags=["requests"])
router = APIRouter(prefix="/workflows", tags=["workflows"])

_MAX_FILE_BYTES = 10 * 1024 * 1024
_ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".txt"}


def _validate_upload(filename: str, content: bytes) -> None:
    ext = Path(filename).suffix.lower()
    if ext not in _ALLOWED_EXTENSIONS:
        raise ValidationError(f"Unsupported file type: {filename!r}")
    if len(content) > _MAX_FILE_BYTES:
        raise ValidationError(f"File too large (max 10MB): {filename!r}")


def run_workflow_background(workflow_run_id: int, document_ids: list[int] | None = None) -> None:
    """The BackgroundTasks callback. Opens its own db session - never the
    request's, which may already be closed by the time this runs - and
    executes the graph for a WorkflowRun `create_run` already created.
    Tests call this directly (rather than relying on TestClient's
    background-task timing) for a deterministic request->workflow flow.

    Looks up `db_session_module.SessionLocal` at call time rather than
    binding it at import time, so the test suite's session-scoped db
    override (which patches that module attribute, not just the `get_db`
    FastAPI dependency) reaches this code path too."""
    db = db_session_module.SessionLocal()
    try:
        workflow_service.execute_workflow(db, workflow_run_id, document_ids)
    finally:
        db.close()


@requests_router.post("/requests", response_model=CreateRequestResponse, status_code=202)
def create_request(
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    text: Annotated[str, Form()],
    files: Annotated[list[UploadFile], File()] = [],  # noqa: B006 - FastAPI's own idiom
) -> CreateRequestResponse:
    # Validate every file before storing any of them, so one rejected file
    # in a multi-file upload never leaves a partial set of documents behind.
    uploads: list[tuple[str, bytes]] = []
    for upload in files:
        content = upload.file.read()
        filename = upload.filename or "upload"
        _validate_upload(filename, content)
        uploads.append((filename, content))

    document_ids = [
        store_document(db, current_user.id, filename, content, None)["id"]
        for filename, content in uploads
    ]

    workflow_run = workflow_service.create_run(db, current_user, text, document_ids)
    if workflow_run.status == "running":
        background_tasks.add_task(run_workflow_background, workflow_run.id, document_ids)

    return CreateRequestResponse(workflow_id=workflow_run.id, status=workflow_run.status)


def _serialize_escalation(escalation: Escalation | None) -> dict | None:
    if escalation is None:
        return None
    return {
        "id": escalation.id,
        "reason": escalation.reason,
        "severity": escalation.severity,
        "status": escalation.status,
        "reviewed_by": escalation.reviewed_by,
        "resolution_note": escalation.resolution_note,
    }


@router.get("/{workflow_id}", response_model=WorkflowRunDetail)
def get_workflow(
    workflow_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> WorkflowRunDetail:
    run = db.get(WorkflowRun, workflow_id)
    if run is None:
        raise NotFoundError(f"WorkflowRun {workflow_id} not found")
    ensure_owner_or_staff(current_user, run.patient_id, db)

    state = run.state or {}
    document_ids = state.get("uploaded_document_ids") or []
    documents = (
        [
            DocumentMeta(
                id=doc.id,
                patient_id=doc.patient_id,
                filename=doc.filename,
                document_type=doc.document_type,
                created_at=doc.created_at,
            )
            for doc in db.query(PatientDocument).filter(PatientDocument.id.in_(document_ids)).all()
        ]
        if document_ids
        else []
    )
    escalation = (
        db.query(Escalation).filter_by(workflow_run_id=run.id).order_by(Escalation.id.desc()).first()
    )

    return WorkflowRunDetail(
        id=run.id,
        status=run.status,
        current_step=run.current_step,
        request_text=run.request_text,
        state=run.state,
        created_at=run.created_at,
        updated_at=run.updated_at,
        appointment=state.get("appointment"),
        documents=documents,
        escalation=_serialize_escalation(escalation),
    )


@router.post("/{workflow_id}/resume", response_model=ResumeResponse)
def resume(
    workflow_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ResumeResponse:
    run = db.get(WorkflowRun, workflow_id)
    if run is None:
        raise NotFoundError(f"WorkflowRun {workflow_id} not found")
    ensure_owner_or_staff(current_user, run.patient_id, db)

    resumed = workflow_service.resume_workflow(db, workflow_id)
    write_audit(db, current_user.id, "workflow.resumed", "workflow_run", workflow_id, {})
    db.commit()

    return ResumeResponse(id=resumed.id, status=resumed.status)
