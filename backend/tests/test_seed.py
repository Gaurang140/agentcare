"""Idempotency and coverage checks for app.db.seed.

Uses its own throwaway in-memory sqlite db (not the conftest.py
session-scoped one used by the API test suite) so seeding twice here can't
interfere with, or be interfered by, the auth/rbac/document tests running
elsewhere in the same pytest session.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.provision_staff import provision_staff
from app.db.seed import seed
from app.auth.security import verify_password
from app.models import AGENT_NAMES, AgentRule, Department, Doctor, RequiredDocument, User


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
        assert first["users"] == 2
        assert db.query(User).filter_by(role="staff").count() == 0
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


def test_staff_is_provisioned_privately_and_can_be_rotated() -> None:
    db = _fresh_session()
    try:
        user = provision_staff(
            db,
            email="reviewer@example.test",
            password="a-strong-private-password",
        )
        assert user.role == "staff"
        assert verify_password("a-strong-private-password", user.password_hash)

        rotated = provision_staff(
            db,
            email="reviewer@example.test",
            password="a-different-private-password",
            full_name="Private reviewer",
        )
        assert rotated.id == user.id
        assert rotated.full_name == "Private reviewer"
        assert verify_password("a-different-private-password", rotated.password_hash)
        assert not verify_password("a-strong-private-password", rotated.password_hash)
    finally:
        db.close()


@pytest.mark.parametrize("password", ["demo1234", "too-short"])
def test_staff_provisioning_rejects_public_or_weak_password(password) -> None:
    db = _fresh_session()
    try:
        with pytest.raises(ValueError, match="at least"):
            provision_staff(
                db,
                email="reviewer@example.test",
                password=password,
            )
    finally:
        db.close()


def test_partial_database_is_repaired_without_removing_custom_catalog_rows() -> None:
    db = _fresh_session()
    try:
        db.add(Department(name="Pre-existing Department"))
        db.commit()
        assert db.query(AgentRule).count() == 0

        counts = seed(db)

        assert counts["departments"] == 6
        assert counts["doctors"] == 10
        assert counts["slots"] >= 1000
        assert counts["required_documents"] == 4
        assert counts["agent_rules"] == 12
        assert db.query(Department).filter_by(name="Pre-existing Department").one()
        assert db.query(Department).filter_by(name="Cardiology").one()
        assert db.query(AgentRule).count() == 12

        assert seed(db) == counts
    finally:
        db.close()


def test_custom_rule_does_not_prevent_missing_defaults_from_being_seeded() -> None:
    db = _fresh_session()
    try:
        db.add(
            AgentRule(
                agent_name="routing",
                rule_text="A staff-authored rule that is not a default.",
                source="staff",
            )
        )
        db.commit()

        seed(db)

        defaults = db.query(AgentRule).filter_by(source="seed").all()
        assert len(defaults) == 12
        assert db.query(AgentRule).count() == 13

        seed(db)
        assert db.query(AgentRule).count() == 13
    finally:
        db.close()


def test_seed_tolerates_duplicate_canonical_doctors() -> None:
    db = _fresh_session()
    try:
        cardiology = Department(name="Cardiology")
        db.add(cardiology)
        db.flush()
        db.add_all(
            [
                Doctor(department_id=cardiology.id, name="Dr. Anna Beispiel"),
                Doctor(department_id=cardiology.id, name="Dr. Anna Beispiel"),
            ]
        )
        db.commit()

        counts = seed(db)

        assert (
            db.query(Doctor)
            .filter_by(
                department_id=cardiology.id,
                name="Dr. Anna Beispiel",
            )
            .count()
            == 2
        )
        assert (
            db.query(Doctor)
            .filter_by(
                department_id=cardiology.id,
                name="Dr. Thomas Krause",
            )
            .count()
            == 1
        )
        assert seed(db) == counts
    finally:
        db.close()
