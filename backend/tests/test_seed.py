"""Idempotency and coverage checks for app.db.seed.

Uses its own throwaway in-memory sqlite db (not the conftest.py
session-scoped one used by the API test suite) so seeding twice here can't
interfere with, or be interfered by, the auth/rbac/document tests running
elsewhere in the same pytest session.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.seed import seed
from app.models import Department, RequiredDocument


def _fresh_session() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return Session(engine)


def test_seed_is_idempotent_and_meets_minimum_counts() -> None:
    db = _fresh_session()
    try:
        first = seed(db)
        second = seed(db)

        # Running it twice must not duplicate a single row.
        assert first == second
        assert db.query(Department).count() == first["departments"]

        assert first["departments"] >= 5
        assert first["doctors"] == 10
        assert first["slots"] >= 1000
        assert first["users"] == 3

        required_by_dept = {
            dept.name: sorted(
                rd.document_type
                for rd in db.query(RequiredDocument).filter_by(department_id=dept.id)
            )
            for dept in db.query(Department).all()
        }
        assert required_by_dept["Cardiology"] == ["blood_test", "ecg_report"]
        assert required_by_dept["Radiology"] == ["referral_letter"]
        assert required_by_dept["Orthopedics"] == ["imaging_report"]
    finally:
        db.close()
