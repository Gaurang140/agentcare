"""Behavioral tests for the appointment schedule-range migration."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.config import settings

_PREVIOUS_REVISION = "8524b9522086"
_BACKEND_DIR = Path(__file__).resolve().parents[1]


def _alembic_config(database_url: str) -> Config:
    config = Config(str(_BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_overlap_migration_refuses_dirty_existing_data_without_modifying_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = f"sqlite:///{tmp_path / 'dirty-appointments.db'}"
    monkeypatch.setattr(settings, "database_url", database_url)
    config = _alembic_config(database_url)
    command.upgrade(config, _PREVIOUS_REVISION)

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO departments (id, name)
                VALUES (1, 'Migration Medicine')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO users (id, email, password_hash, role, full_name)
                VALUES (1, 'migration-patient@example.com', 'h', 'patient', 'Migration Patient')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO doctors (id, department_id, name, active)
                VALUES
                  (1, 1, 'Dr. Migration One', 1),
                  (2, 1, 'Dr. Migration Two', 1)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO appointment_slots
                    (id, doctor_id, start_time, end_time, status)
                VALUES
                    (1, 1, '2026-08-07 09:00:00', '2026-08-07 10:00:00', 'booked'),
                    (2, 2, '2026-08-07 09:30:00', '2026-08-07 10:30:00', 'booked')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO appointments
                    (id, patient_id, doctor_id, slot_id, status)
                VALUES
                    (1, 1, 1, 1, 'confirmed'),
                    (2, 1, 2, 2, 'pending')
                """
            )
        )

    with pytest.raises(RuntimeError, match=r"patient overlaps=1"):
        command.upgrade(config, "head")

    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id, patient_id, slot_id, status FROM appointments ORDER BY id")
        ).all()
        column_names = {
            column["name"] for column in inspect(connection).get_columns("appointments")
        }

    assert rows == [
        (1, 1, 1, "confirmed"),
        (2, 1, 2, "pending"),
    ]
    assert "scheduled_start" not in column_names
    assert "scheduled_end" not in column_names


def test_range_migration_refuses_non_positive_slot_before_ddl_and_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = f"sqlite:///{tmp_path / 'invalid-slot-range.db'}"
    monkeypatch.setattr(settings, "database_url", database_url)
    config = _alembic_config(database_url)
    command.upgrade(config, _PREVIOUS_REVISION)

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO departments (id, name)
                VALUES (1, 'Migration Medicine')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO doctors (id, department_id, name, active)
                VALUES (1, 1, 'Dr. Invalid Range', 1)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO appointment_slots
                    (id, doctor_id, start_time, end_time, status)
                VALUES
                    (1, 1, '2026-08-08 09:00:00', '2026-08-08 09:00:00', 'free')
                """
            )
        )

    with pytest.raises(RuntimeError, match=r"invalid slot ranges=1"):
        command.upgrade(config, "head")

    with engine.connect() as connection:
        column_names = {
            column["name"] for column in inspect(connection).get_columns("appointments")
        }
        revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()

    assert revision == _PREVIOUS_REVISION
    assert "scheduled_start" not in column_names
    assert "scheduled_end" not in column_names

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE appointment_slots
                SET end_time = '2026-08-08 09:30:00'
                WHERE id = 1
                """
            )
        )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        column_names = {
            column["name"] for column in inspect(connection).get_columns("appointments")
        }
        revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()

    assert revision == "b6a7d8e9f012"
    assert {"scheduled_start", "scheduled_end"} <= column_names
