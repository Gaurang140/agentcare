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
from app.models import AGENT_NAMES, AgentRule, Department, RequiredDocument


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
        assert first["agent_rules"] == 12  # 6 agents x 2 starter rules each

        for name in AGENT_NAMES:
            rows = db.query(AgentRule).filter_by(agent_name=name, source="seed", active=True).all()
            assert len(rows) == 2

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


def test_agent_rules_seeded_even_on_a_db_already_past_the_department_gate() -> None:
    """A db seeded before this feature shipped already has Department rows,
    so seed()'s main idempotency check short-circuits - _seed_agent_rules
    must still run and insert the default rules on that next call."""
    db = _fresh_session()
    try:
        db.add(Department(name="Pre-existing Department"))
        db.commit()
        assert db.query(AgentRule).count() == 0

        counts = seed(db)

        assert counts["agent_rules"] == 12
        assert db.query(AgentRule).count() == 12

        # A second call stays idempotent.
        seed(db)
        assert db.query(AgentRule).count() == 12
    finally:
        db.close()
