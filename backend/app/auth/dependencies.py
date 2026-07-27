"""FastAPI auth dependencies: current-user resolution and role/ownership gates.

Patient-data routes use ensure_owner_or_staff to enforce ownership or staff
access before returning protected records.
"""

import hmac
from typing import Annotated

from fastapi import Depends, Request
from jwt.exceptions import InvalidTokenError
from sqlalchemy.orm import Session

from app.auth.security import decode_token
from app.config import settings
from app.db.session import get_db
from app.exceptions import PermissionDeniedError
from app.models import User


def get_current_user(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> User:
    """Resolve the caller from the httpOnly access_token cookie.

    Missing cookie, an unparseable/expired/mis-signed token, or a token
    whose subject no longer maps to a user: all raise PermissionDeniedError
    (403), never a distinct 401. The app deliberately keeps a single
    permission error class.
    """
    token = request.cookies.get("access_token")
    if not token:
        raise PermissionDeniedError("Not authenticated")

    try:
        payload = decode_token(token)
    except InvalidTokenError as exc:
        raise PermissionDeniedError("Not authenticated") from exc

    user_id = payload.get("sub")
    user = db.get(User, int(user_id)) if user_id is not None else None
    if user is None:
        raise PermissionDeniedError("Not authenticated")
    return user


def require_role(*roles: str):
    """Build a dependency that only lets the given roles through."""

    def _dependency(
        user: Annotated[User, Depends(get_current_user)],
    ) -> User:
        if user.role not in roles:
            raise PermissionDeniedError("Insufficient permissions")
        return user

    return _dependency


def ensure_owner_or_staff(user: User, patient_id: int) -> None:
    """Raise PermissionDeniedError unless user is staff or owns patient_id.
    """
    if user.role == "staff":
        return
    if user.id != patient_id:
        raise PermissionDeniedError("Not allowed to access this resource")


def require_internal_or_staff(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> None:
    """Dependency for cron-style internal endpoints (POST
    /api/internal/reminders/run-due): an `X-Internal-Token` header equal to
    settings.internal_task_token when that setting is non-empty (the
    no-browser-session path for an actual cron caller), else fall back to
    require_role("staff")'s cookie check - the default, since the setting
    defaults to empty.
    """
    token = settings.internal_task_token
    if token:
        supplied = request.headers.get("X-Internal-Token", "")
        # compare_digest: constant-time comparison, no timing oracle on the
        # shared token.
        if not hmac.compare_digest(supplied, token):
            raise PermissionDeniedError("Invalid internal token")
        return

    user = get_current_user(request, db)
    if user.role != "staff":
        raise PermissionDeniedError("Insufficient permissions")
