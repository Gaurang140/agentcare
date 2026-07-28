"""Provision or rotate a staff account from operator-supplied environment values."""

from __future__ import annotations

import os

from sqlalchemy.orm import Session

from app.auth.security import hash_password
from app.db.session import SessionLocal
from app.models import User

_MIN_PASSWORD_LENGTH = 16


def provision_staff(
    db: Session,
    *,
    email: str,
    password: str,
    full_name: str = "AgentCare reviewer",
) -> User:
    normalized_email = email.strip().lower()
    if "@" not in normalized_email:
        raise ValueError("AGENTCARE_STAFF_EMAIL must be a valid email address")
    if len(password) < _MIN_PASSWORD_LENGTH or password == "demo1234":
        raise ValueError(
            f"AGENTCARE_STAFF_PASSWORD must be at least {_MIN_PASSWORD_LENGTH} characters"
        )

    user = db.query(User).filter_by(email=normalized_email).one_or_none()
    if user is None:
        user = User(
            email=normalized_email,
            password_hash=hash_password(password),
            role="staff",
            full_name=full_name.strip() or "AgentCare reviewer",
        )
        db.add(user)
    else:
        if user.role != "staff":
            raise ValueError("The requested email already belongs to a patient")
        user.password_hash = hash_password(password)
        user.full_name = full_name.strip() or user.full_name
    db.commit()
    db.refresh(user)
    return user


def main() -> None:
    email = os.environ.get("AGENTCARE_STAFF_EMAIL", "")
    password = os.environ.get("AGENTCARE_STAFF_PASSWORD", "")
    full_name = os.environ.get("AGENTCARE_STAFF_NAME", "AgentCare reviewer")

    db = SessionLocal()
    try:
        user = provision_staff(
            db,
            email=email,
            password=password,
            full_name=full_name,
        )
        print(f"staff account provisioned: {user.email}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
