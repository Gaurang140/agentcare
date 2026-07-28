"""add request and booking idempotency

Revision ID: c1d2e3f4a5b6
Revises: b6a7d8e9f012
Create Date: 2026-07-28
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "b6a7d8e9f012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ACTIVE_WINDOW = (
    "booking_window_key IS NOT NULL AND status IN ('pending', 'confirmed')"
)


def upgrade() -> None:
    op.add_column(
        "workflow_runs",
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "uq_workflow_runs_patient_idempotency",
        "workflow_runs",
        ["patient_id", "idempotency_key"],
        unique=True,
        sqlite_where=sa.text("idempotency_key IS NOT NULL"),
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.add_column(
        "appointments",
        sa.Column("booking_window_key", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "uq_appointments_patient_booking_window",
        "appointments",
        ["patient_id", "booking_window_key"],
        unique=True,
        sqlite_where=sa.text(_ACTIVE_WINDOW),
        postgresql_where=sa.text(_ACTIVE_WINDOW),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_appointments_patient_booking_window",
        table_name="appointments",
    )
    op.drop_column("appointments", "booking_window_key")
    op.drop_index(
        "uq_workflow_runs_patient_idempotency",
        table_name="workflow_runs",
    )
    op.drop_column("workflow_runs", "idempotency_key")
