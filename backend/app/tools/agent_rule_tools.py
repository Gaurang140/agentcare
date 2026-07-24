"""Staff CRUD over `app.models.AgentRule`: list (optionally scoped to one
agent), create a staff-authored rule, and toggle a rule active/inactive.
Mirrors `department_tools.py`'s pattern for the rest of the staff catalog
admin - every mutation writes an AuditEvent.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.exceptions import NotFoundError, ValidationError
from app.models import AGENT_NAMES, AgentRule
from app.tools.audit_tools import write_audit


def _to_dict(rule: AgentRule) -> dict:
    return {
        "id": rule.id,
        "agent_name": rule.agent_name,
        "rule_text": rule.rule_text,
        "source": rule.source,
        "active": rule.active,
        "created_at": rule.created_at,
    }


def list_rules(db: Session, agent_name: str | None = None) -> list[dict]:
    """Every rule, oldest first, optionally scoped to one agent."""
    query = db.query(AgentRule)
    if agent_name is not None:
        query = query.filter(AgentRule.agent_name == agent_name)
    rules = query.order_by(AgentRule.id).all()
    return [_to_dict(rule) for rule in rules]


def create_rule(db: Session, actor_id: int, agent_name: str, rule_text: str) -> dict:
    """Add a staff-authored rule. Raises ValidationError for an
    unrecognized agent name rather than silently seeding a rule no node
    will ever read - source is always "staff" here; "seed" rules only ever
    come from app.db.seed."""
    if agent_name not in AGENT_NAMES:
        raise ValidationError(f"Unknown agent {agent_name!r}, expected one of {AGENT_NAMES}")

    rule = AgentRule(agent_name=agent_name, rule_text=rule_text, source="staff")
    db.add(rule)
    db.flush()
    write_audit(db, actor_id, "agent_rule.created", "agent_rule", rule.id, {"agent_name": agent_name})
    db.commit()
    return _to_dict(rule)


def set_rule_active(db: Session, actor_id: int, rule_id: int, active: bool) -> dict:
    """Toggle a rule's active flag - never deletes, so its history (and any
    audit trail referencing it) stays intact."""
    rule = db.get(AgentRule, rule_id)
    if rule is None:
        raise NotFoundError(f"Agent rule {rule_id} not found")

    rule.active = active
    db.flush()
    write_audit(db, actor_id, "agent_rule.updated", "agent_rule", rule.id, {"active": active})
    db.commit()
    return _to_dict(rule)
