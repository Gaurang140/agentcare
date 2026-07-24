"""TDD for app.agents.memory: rule retrieval, formatting, and injection into
a real agent node's system prompt at call time.
"""

from __future__ import annotations

from app.agents import routing
from app.agents.memory import build_system_prompt, format_rules, get_rules
from app.models import AgentRule


def _add_rule(db, agent_name: str, rule_text: str, *, active: bool = True, source: str = "seed") -> AgentRule:
    rule = AgentRule(agent_name=agent_name, rule_text=rule_text, source=source, active=active)
    db.add(rule)
    db.commit()
    return rule


def test_get_rules_returns_active_rules_ordered_by_id(db):
    _add_rule(db, "routing", "rule one")
    _add_rule(db, "routing", "rule two", source="staff")
    _add_rule(db, "appointment", "a different agent's rule")

    assert get_rules(db, "routing") == ["rule one", "rule two"]


def test_get_rules_excludes_inactive_rules(db):
    _add_rule(db, "routing", "active rule")
    _add_rule(db, "routing", "disabled rule", active=False)

    assert get_rules(db, "routing") == ["active rule"]


def test_get_rules_empty_for_agent_with_no_rules(db):
    assert get_rules(db, "routing") == []


def test_format_rules_empty_string_when_no_rules():
    assert format_rules([]) == ""


def test_format_rules_renders_bullets_under_header():
    text = format_rules(["first rule", "second rule"])
    assert text == "Additional operating rules:\n- first rule\n- second rule"


def test_build_system_prompt_returns_base_prompt_unchanged_when_no_rules(db):
    assert build_system_prompt(db, "routing", "BASE PROMPT") == "BASE PROMPT"


def test_build_system_prompt_appends_formatted_rules(db):
    _add_rule(db, "routing", "prefer reschedule over book")

    system = build_system_prompt(db, "routing", "BASE PROMPT")

    assert system == "BASE PROMPT\n\nAdditional operating rules:\n- prefer reschedule over book"


def test_routing_agent_injects_active_rules_into_system_message_and_excludes_inactive(
    db, seeded, fake_llm
):
    """Integration: a real node (routing) sends rule text in its system
    message, and an inactive rule never reaches the LLM at all."""
    _add_rule(db, "routing", "custom staff rule for routing", source="staff")
    _add_rule(db, "routing", "disabled routing rule", active=False)

    client = fake_llm(
        [{"intent": "book", "department": "Cardiology", "confidence": 0.95, "reason": "wants cardiology"}]
    )

    routing.run(
        {"workflow_id": 1, "patient_id": 1, "request_text": "book me a cardiology appointment"}, db
    )

    system_message = client.chat.completions.calls[0]["messages"][0]["content"]
    assert "custom staff rule for routing" in system_message
    assert "disabled routing rule" not in system_message
