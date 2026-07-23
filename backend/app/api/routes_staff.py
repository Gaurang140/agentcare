"""Staff-only routes. Stub for Task 4; Task 12 replaces the escalations body."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.auth.dependencies import require_role
from app.models import User

router = APIRouter(prefix="/staff", tags=["staff"])


@router.get("/escalations")
def list_escalations(
    _staff: Annotated[User, Depends(require_role("staff"))],
) -> list:
    return []
