"""PostgreSQL-only integration coverage for concurrent appointment booking."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.exceptions import ConflictError
from app.models import Appointment, AppointmentSlot, Department, Doctor, User
from app.tools.appointment_tools import book_appointment


_POSTGRES_TEST_URL = os.getenv("POSTGRES_TEST_URL")
_BACKEND_DIR = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not _POSTGRES_TEST_URL,
    reason="POSTGRES_TEST_URL is required for PostgreSQL integration tests",
)


def _alembic_config(database_url: str) -> Config:
    config = Config(str(_BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_concurrent_overlapping_patient_bookings_return_one_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing the named patient exclusion constraint lets both commits win."""
    assert _POSTGRES_TEST_URL is not None
    assert make_url(_POSTGRES_TEST_URL).get_backend_name() == "postgresql"

    monkeypatch.setattr(settings, "database_url", _POSTGRES_TEST_URL)
    command.upgrade(_alembic_config(_POSTGRES_TEST_URL), "head")

    engine = create_engine(_POSTGRES_TEST_URL)
    session_factory = sessionmaker(bind=engine)
    unique = uuid4().hex

    with Session(engine) as seed_session:
        department = Department(name=f"PostgreSQL concurrency {unique}")
        patient = User(
            email=f"postgres-concurrency-{unique}@example.test",
            password_hash="test-only",
            role="patient",
            full_name="PostgreSQL Test Patient",
        )
        doctors = [
            Doctor(department=department, name=f"Dr. PostgreSQL {index} {unique}")
            for index in range(2)
        ]
        start = datetime(2040, 1, 2, 9, 0)
        slots = [
            AppointmentSlot(
                doctor=doctor,
                start_time=start + timedelta(minutes=offset),
                end_time=start + timedelta(minutes=offset + 60),
                status="free",
            )
            for doctor, offset in zip(doctors, (0, 30), strict=True)
        ]
        seed_session.add_all([patient, *slots])
        seed_session.commit()
        patient_id = patient.id
        slot_ids = [slot.id for slot in slots]

    both_ready_to_insert = Barrier(2, timeout=10)

    def attempt_booking(slot_id: int) -> str:
        with session_factory() as booking_session:
            def synchronize_flushes(
                _session: Session,
                _flush_context: object,
                _instances: object,
            ) -> None:
                if any(
                    isinstance(row, Appointment) for row in booking_session.new
                ):
                    both_ready_to_insert.wait()

            event.listen(
                booking_session,
                "before_flush",
                synchronize_flushes,
                once=True,
            )
            try:
                book_appointment(
                    booking_session,
                    patient_id=patient_id,
                    slot_id=slot_id,
                    reason="PostgreSQL concurrency test",
                )
            except ConflictError:
                return "conflict"
            return "committed"

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(attempt_booking, slot_ids))
    finally:
        engine.dispose()

    assert sorted(outcomes) == ["committed", "conflict"]
