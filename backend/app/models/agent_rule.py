"""Procedural agent memory: one operating rule scoped to one agent, read at
call time and appended to that agent's base system prompt (see
`app/agents/memory.py`). Seeded rules ship with the app (`source="seed"`,
`app/db/seed.py`); staff add more through the staff routes (`source="staff"`,
`app/tools/agent_rule_tools.py`). A rule is deactivated, never deleted, so
its history stays in the table.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# The six agents that read procedural rules (app/agents/prompts.py owns the
# matching base prompt for each of these names). Not a DB constraint:
# agent_name stays a plain indexed string so a rule for a future agent
# never needs a migration - this tuple is the validation boundary instead,
# enforced by app/tools/agent_rule_tools.py::create_rule.
AGENT_NAMES: tuple[str, ...] = (
    "coordinator",
    "routing",
    "appointment",
    "document",
    "followup",
    "safety",
)


class AgentRule(Base):
    """One procedural rule for one named agent."""

    __tablename__ = "agent_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_name: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    rule_text: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(
        Enum("seed", "staff", name="agent_rule_source"), nullable=False
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
