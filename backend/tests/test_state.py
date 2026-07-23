"""AgentState's multi-writer keys must use Annotated reducers, or LangGraph
raises "can receive only one value per step" the moment two nodes in the
same superstep both write to the same key. This exercises that directly by
compiling a tiny graph with two nodes fanning out from START and both
writing to completed_steps/safety_flags/reminders in parallel.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.agents.state import AgentState


def _node_a(state: AgentState) -> dict:
    return {
        "completed_steps": ["node_a"],
        "safety_flags": ["flag_a"],
        "reminders": [{"from": "node_a"}],
    }


def _node_b(state: AgentState) -> dict:
    return {
        "completed_steps": ["node_b"],
        "safety_flags": ["flag_b"],
        "reminders": [{"from": "node_b"}],
    }


def test_multi_writer_keys_merge_via_reducer_instead_of_conflicting():
    builder = StateGraph(AgentState)
    builder.add_node("node_a", _node_a)
    builder.add_node("node_b", _node_b)
    builder.add_edge(START, "node_a")
    builder.add_edge(START, "node_b")
    builder.add_edge("node_a", END)
    builder.add_edge("node_b", END)
    graph = builder.compile()

    result = graph.invoke({"workflow_id": 1})

    assert sorted(result["completed_steps"]) == ["node_a", "node_b"]
    assert sorted(result["safety_flags"]) == ["flag_a", "flag_b"]
    assert sorted(r["from"] for r in result["reminders"]) == ["node_a", "node_b"]


def test_single_writer_keys_stay_plain_typeddict_fields():
    """department_id etc. are plain (last-write-wins) fields, not reduced -
    confirms we didn't over-apply Annotated to single-writer keys."""

    def _set_department(state: AgentState) -> dict:
        return {"department_id": 3, "department_name": "Cardiology"}

    builder = StateGraph(AgentState)
    builder.add_node("set_department", _set_department)
    builder.add_edge(START, "set_department")
    builder.add_edge("set_department", END)
    graph = builder.compile()

    result = graph.invoke({"workflow_id": 1})

    assert result["department_id"] == 3
    assert result["department_name"] == "Cardiology"
