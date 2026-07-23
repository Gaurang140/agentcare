"""Registration, login, logout, and the current-user endpoint."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.auth.security import create_token, hash_password, verify_password
from app.config import settings
from app.db.session import get_db
from app.exceptions import ConflictError, PermissionDeniedError
from app.models import PatientProfile, User
from app.schemas.auth import LoginRequest, RegisterRequest, UserSummary

router = APIRouter(prefix="/auth", tags=["auth"])

_COOKIE_NAME = "access_token"


def _set_auth_cookie(response: Response, user: User) -> None:
    token = create_token(user.id, user.role)
    response.set_cookie(
        key=_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.environment != "dev",
    )


def _to_summary(user: User) -> UserSummary:
    return UserSummary(id=user.id, name=user.full_name, email=user.email, role=user.role)


@router.post("/register", response_model=UserSummary, status_code=201)
def register(
    payload: RegisterRequest,
    db: Annotated[Session, Depends(get_db)],
) -> UserSummary:
    existing = db.query(User).filter(User.email == payload.email).first()
    if existing is not None:
        raise ConflictError("An account with this email already exists")

    user = User(
        email=payload.email,
        password_hash=hash_password(payload.password),
        role="patient",
        full_name=payload.name,
    )
    db.add(user)
    try:
        db.flush()  # assigns user.id inside the same transaction

        profile = PatientProfile(
            user_id=user.id,
            date_of_birth=payload.dob,
            phone=payload.phone,
            preferred_language=payload.preferred_language,
            emergency_contact=payload.emergency_contact,
        )
        db.add(profile)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError("An account with this email already exists") from exc

    db.refresh(user)
    return _to_summary(user)


@router.post("/login", response_model=UserSummary)
def login(
    payload: LoginRequest,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> UserSummary:
    user = db.query(User).filter(User.email == payload.email).first()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise PermissionDeniedError("Invalid email or password")

    _set_auth_cookie(response, user)
    return _to_summary(user)


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(_COOKIE_NAME)
    return {"status": "ok"}


@router.get("/me", response_model=UserSummary)
def me(current_user: Annotated[User, Depends(get_current_user)]) -> UserSummary:
    return _to_summary(current_user)
