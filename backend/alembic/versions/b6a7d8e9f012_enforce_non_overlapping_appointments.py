"""enforce non-overlapping appointment ranges

Revision ID: b6a7d8e9f012
Revises: 8524b9522086
Create Date: 2026-07-28

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b6a7d8e9f012"
down_revision: Union[str, Sequence[str], None] = "8524b9522086"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ACTIVE_STATUSES = "('pending', 'confirmed')"
_PATIENT_EXCLUSION = "ex_appointments_patient_schedule"
_DOCTOR_EXCLUSION = "ex_appointment_slots_doctor_schedule"


def _scalar_count(statement: sa.TextClause) -> int:
    return int(op.get_bind().execute(statement).scalar_one())


def _doctor_overlap_count() -> int:
    return _scalar_count(
        sa.text(
            """
            SELECT COUNT(*)
            FROM appointment_slots AS left_slot
            JOIN appointment_slots AS right_slot
              ON left_slot.id < right_slot.id
             AND left_slot.doctor_id = right_slot.doctor_id
             AND left_slot.start_time < right_slot.end_time
             AND left_slot.end_time > right_slot.start_time
            """
        )
    )


def _invalid_slot_range_count() -> int:
    return _scalar_count(
        sa.text(
            """
            SELECT COUNT(*)
            FROM appointment_slots
            WHERE end_time <= start_time
            """
        )
    )


def _raise_for_range_conflicts(
    patient_overlaps: int,
    doctor_overlaps: int,
    invalid_slot_ranges: int,
) -> None:
    if patient_overlaps or doctor_overlaps or invalid_slot_ranges:
        raise RuntimeError(
            "Cannot enforce appointment schedule ranges: "
            f"patient overlaps={patient_overlaps}, "
            f"doctor slot overlaps={doctor_overlaps}, "
            f"invalid slot ranges={invalid_slot_ranges}"
        )


def _preflight_source_ranges() -> None:
    """Protect non-transactional SQLite DDL from leaving a partial revision."""
    patient_overlaps = _scalar_count(
        sa.text(
            f"""
            SELECT COUNT(*)
            FROM appointments AS left_appointment
            JOIN appointments AS right_appointment
              ON left_appointment.id < right_appointment.id
             AND left_appointment.patient_id = right_appointment.patient_id
             AND left_appointment.status IN {_ACTIVE_STATUSES}
             AND right_appointment.status IN {_ACTIVE_STATUSES}
            JOIN appointment_slots AS left_slot
              ON left_slot.id = left_appointment.slot_id
            JOIN appointment_slots AS right_slot
              ON right_slot.id = right_appointment.slot_id
             AND left_slot.start_time < right_slot.end_time
             AND left_slot.end_time > right_slot.start_time
            """
        )
    )
    _raise_for_range_conflicts(
        patient_overlaps,
        _doctor_overlap_count(),
        _invalid_slot_range_count(),
    )


def _raise_on_existing_overlaps() -> None:
    patient_overlaps = _scalar_count(
        sa.text(
            f"""
            SELECT COUNT(*)
            FROM appointments AS left_appointment
            JOIN appointments AS right_appointment
              ON left_appointment.id < right_appointment.id
             AND left_appointment.patient_id = right_appointment.patient_id
             AND left_appointment.status IN {_ACTIVE_STATUSES}
             AND right_appointment.status IN {_ACTIVE_STATUSES}
             AND left_appointment.scheduled_start < right_appointment.scheduled_end
             AND left_appointment.scheduled_end > right_appointment.scheduled_start
            """
        )
    )
    _raise_for_range_conflicts(
        patient_overlaps,
        _doctor_overlap_count(),
        _invalid_slot_range_count(),
    )


def _create_sqlite_triggers() -> None:
    op.execute(
        f"""
        CREATE TRIGGER trg_appointments_patient_schedule_insert
        BEFORE INSERT ON appointments
        WHEN NEW.status IN {_ACTIVE_STATUSES}
          AND EXISTS (
            SELECT 1
            FROM appointments AS existing
            WHERE existing.patient_id = NEW.patient_id
              AND existing.status IN {_ACTIVE_STATUSES}
              AND existing.scheduled_start < NEW.scheduled_end
              AND existing.scheduled_end > NEW.scheduled_start
          )
        BEGIN
          SELECT RAISE(ABORT, '{_PATIENT_EXCLUSION}');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_appointments_patient_schedule_update
        BEFORE UPDATE ON appointments
        WHEN NEW.status IN {_ACTIVE_STATUSES}
          AND EXISTS (
            SELECT 1
            FROM appointments AS existing
            WHERE existing.patient_id = NEW.patient_id
              AND existing.id != OLD.id
              AND existing.status IN {_ACTIVE_STATUSES}
              AND existing.scheduled_start < NEW.scheduled_end
              AND existing.scheduled_end > NEW.scheduled_start
          )
        BEGIN
          SELECT RAISE(ABORT, '{_PATIENT_EXCLUSION}');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_appointment_slots_doctor_schedule_insert
        BEFORE INSERT ON appointment_slots
        WHEN EXISTS (
          SELECT 1
          FROM appointment_slots AS existing
          WHERE existing.doctor_id = NEW.doctor_id
            AND existing.start_time < NEW.end_time
            AND existing.end_time > NEW.start_time
        )
        BEGIN
          SELECT RAISE(ABORT, '{_DOCTOR_EXCLUSION}');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_appointment_slots_doctor_schedule_update
        BEFORE UPDATE ON appointment_slots
        WHEN EXISTS (
          SELECT 1
          FROM appointment_slots AS existing
          WHERE existing.doctor_id = NEW.doctor_id
            AND existing.id != OLD.id
            AND existing.start_time < NEW.end_time
            AND existing.end_time > NEW.start_time
        )
        BEGIN
          SELECT RAISE(ABORT, '{_DOCTOR_EXCLUSION}');
        END
        """
    )


def upgrade() -> None:
    """Add schedule snapshots and database-level overlap enforcement."""
    # SQLite does not roll ALTER TABLE back reliably. This equivalent
    # source-range preflight preserves retryability there; the required
    # post-backfill snapshot check below remains authoritative.
    _preflight_source_ranges()

    op.add_column(
        "appointments",
        sa.Column("scheduled_start", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "appointments",
        sa.Column("scheduled_end", sa.DateTime(), nullable=True),
    )
    op.execute(
        """
        UPDATE appointments
        SET scheduled_start = (
                SELECT appointment_slots.start_time
                FROM appointment_slots
                WHERE appointment_slots.id = appointments.slot_id
            ),
            scheduled_end = (
                SELECT appointment_slots.end_time
                FROM appointment_slots
                WHERE appointment_slots.id = appointments.slot_id
            )
        """
    )

    _raise_on_existing_overlaps()

    with op.batch_alter_table("appointments") as batch_op:
        batch_op.alter_column(
            "scheduled_start",
            existing_type=sa.DateTime(),
            nullable=False,
        )
        batch_op.alter_column(
            "scheduled_end",
            existing_type=sa.DateTime(),
            nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_appointments_scheduled_range",
            "scheduled_end > scheduled_start",
        )
    with op.batch_alter_table("appointment_slots") as batch_op:
        batch_op.create_check_constraint(
            "ck_appointment_slots_range",
            "end_time > start_time",
        )

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
        op.execute(
            f"""
            ALTER TABLE appointments
            ADD CONSTRAINT {_PATIENT_EXCLUSION}
            EXCLUDE USING gist (
              patient_id WITH =,
              tsrange(scheduled_start, scheduled_end, '[)') WITH &&
            )
            WHERE (status IN {_ACTIVE_STATUSES})
            """
        )
        op.execute(
            f"""
            ALTER TABLE appointment_slots
            ADD CONSTRAINT {_DOCTOR_EXCLUSION}
            EXCLUDE USING gist (
              doctor_id WITH =,
              tsrange(start_time, end_time, '[)') WITH &&
            )
            """
        )
    else:
        _create_sqlite_triggers()


def downgrade() -> None:
    """Remove only schedule-range objects introduced by this revision."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.drop_constraint(_PATIENT_EXCLUSION, "appointments")
        op.drop_constraint(_DOCTOR_EXCLUSION, "appointment_slots")
    else:
        op.execute("DROP TRIGGER IF EXISTS trg_appointments_patient_schedule_insert")
        op.execute("DROP TRIGGER IF EXISTS trg_appointments_patient_schedule_update")
        op.execute(
            "DROP TRIGGER IF EXISTS trg_appointment_slots_doctor_schedule_insert"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS trg_appointment_slots_doctor_schedule_update"
        )

    with op.batch_alter_table("appointment_slots") as batch_op:
        batch_op.drop_constraint("ck_appointment_slots_range", type_="check")
    with op.batch_alter_table("appointments") as batch_op:
        batch_op.drop_constraint("ck_appointments_scheduled_range", type_="check")
        batch_op.drop_column("scheduled_end")
        batch_op.drop_column("scheduled_start")
