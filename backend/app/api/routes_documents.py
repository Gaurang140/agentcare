"""Document metadata: listing (with per-department requirement badges) and
single-document lookup. Upload happens through POST /api/requests
(multipart, alongside the request text) - not here; this router is
read-only.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth.dependencies import ensure_owner_or_staff, get_current_user
from app.db.session import get_db
from app.exceptions import NotFoundError
from app.models import PatientDocument, User
from app.schemas.document import DocumentListResponse, DocumentMeta
from app.tools.document_tools import check_required_documents

router = APIRouter(prefix="/documents", tags=["documents"])


def _to_meta(doc: PatientDocument) -> DocumentMeta:
    return DocumentMeta(
        id=doc.id,
        patient_id=doc.patient_id,
        filename=doc.filename,
        document_type=doc.document_type,
        created_at=doc.created_at,
    )


@router.get("", response_model=DocumentListResponse)
def list_documents(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    patient_id: int | None = None,
    department_id: int | None = None,
) -> DocumentListResponse:
    target_id = current_user.id
    if patient_id is not None:
        ensure_owner_or_staff(current_user, patient_id)
        target_id = patient_id

    docs = (
        db.query(PatientDocument)
        .filter_by(patient_id=target_id)
        .order_by(PatientDocument.created_at.desc())
        .all()
    )
    requirements = (
        check_required_documents(db, target_id, department_id) if department_id is not None else None
    )
    return DocumentListResponse(documents=[_to_meta(doc) for doc in docs], requirements=requirements)


@router.get("/{document_id}", response_model=DocumentMeta)
def get_document(
    document_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> DocumentMeta:
    doc = db.get(PatientDocument, document_id)
    if doc is None:
        raise NotFoundError("Document not found")

    ensure_owner_or_staff(current_user, doc.patient_id)

    return _to_meta(doc)
