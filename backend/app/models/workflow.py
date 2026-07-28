"""Agent orchestration state: WorkflowRun (one per patient request), Reminder, Escalation."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class WorkflowRun(Base):
    """One LangGraph run for a single patient request. thread_id ties it to the checkpointer.

    status: running|waiting_approval|completed|failed|escalated
    """

    __tablename__ = "workflow_runs"
    __table_args__ = (
        Index(
            "uq_workflow_runs_patient_idempotency",
            "patient_id",
            "idempotency_key",
            unique=True,
            sqlite_where=text("idempotency_key IS NOT NULL"),
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    patient_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(255), nullable=False)
    request_text: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    current_step: Mapped[str | None] = mapped_column(String(50), nullable=True)
    state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    escalations: Mapped[list["Escalation"]] = relationship(back_populates="workflow_run")


class Reminder(Base):
    """A scheduled patient reminder or post-visit follow-up task."""

    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    appointment_id: Mapped[int | None] = mapped_column(ForeignKey("appointments.id"), nullable=True)
    reminder_type: Mapped[str] = mapped_column(String(50), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    sent: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)


class Escalation(Base):
    """A case handed to a human. severity: emergency|safety|uncertainty|agent_failure.

    status: open|approved|rejected
    """

    __tablename__ = "escalations"

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_run_id: Mapped[int | None] = mapped_column(ForeignKey("workflow_runs.id"), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False)
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    workflow_run: Mapped["WorkflowRun | None"] = relationship(back_populates="escalations")
