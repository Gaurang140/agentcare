"""Minimal documents router: ownership-checked metadata lookup only.

Task 12 extends this with upload, listing, and content retrieval.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth.dependencies import ensure_owner_or_staff, get_current_user
from app.db.session import get_db
from app.exceptions import NotFoundError
from app.models import PatientDocument, User
from app.schemas.document import DocumentMeta

router = APIRouter(prefix="/documents", tags=["documents"])


@router.get("/{document_id}", response_model=DocumentMeta)
def get_document(
    document_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> DocumentMeta:
    doc = db.get(PatientDocument, document_id)
    if doc is None:
        raise NotFoundError("Document not found")

    ensure_owner_or_staff(current_user, doc.patient_id, db)

    return DocumentMeta(
        id=doc.id,
        patient_id=doc.patient_id,
        filename=doc.filename,
        document_type=doc.document_type,
        created_at=doc.created_at,
    )
