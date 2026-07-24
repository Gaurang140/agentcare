"""Procedural agent memory: staff-editable operating rules, read fresh on
every call and appended to an agent's base system prompt (the constants in
`app/agents/prompts.py`, which stays the single home of each base prompt).

Rules are written through `app/tools/agent_rule_tools.py` (the staff
routes); nothing in this module ever writes to the `agent_rules` table -
`get_rules` and `build_system_prompt` are read-only from the agent's point
of view, by design.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import AgentRule

_RULES_HEADER = "Additional operating rules:"


def get_rules(db: Session, agent_name: str) -> list[str]:
    """Active rule texts for `agent_name` (seed and staff sources alike),
    ordered by id - seeded rules first, then staff-added ones in the order
    they were created."""
    rows = (
        db.query(AgentRule)
        .filter(AgentRule.agent_name == agent_name, AgentRule.active.is_(True))
        .order_by(AgentRule.id)
        .all()
    )
    return [row.rule_text for row in rows]


def format_rules(rules: list[str]) -> str:
    """Render `rules` as one bulleted block under a fixed header, or "" when
    `rules` is empty. Callers append the result straight onto a base system
    prompt string, so an empty result must add nothing - no stray header,
    no stray blank line."""
    if not rules:
        return ""
    bullets = "\n".join(f"- {rule}" for rule in rules)
    return f"{_RULES_HEADER}\n{bullets}"


def build_system_prompt(db: Session, agent_name: str, base_prompt: str) -> str:
    """One agent's final system message: `base_prompt` (from prompts.py)
    plus its active procedural rules, fetched fresh on every call so a
    staff edit takes effect on the very next request - no caching, no
    restart required. Returns `base_prompt` unchanged when there are no
    active rules for `agent_name`.
    """
    rules_block = format_rules(get_rules(db, agent_name))
    if not rules_block:
        return base_prompt
    return f"{base_prompt}\n\n{rules_block}"
