"""The single state dict threaded through every agent node's LangGraph.

Keys more than one node can write to in the same superstep use an
`Annotated[list[...], operator.add]` reducer, so LangGraph merges concurrent
writes by concatenation instead of raising "can receive only one value per
step" (its default behavior for a plain, non-reduced key). All other keys
are single-writer and stay plain.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict


class AgentState(TypedDict, total=False):
    workflow_id: int
    user_id: int
    patient_id: int
    request_text: str
    uploaded_document_ids: list[int]
    intent: str  # book|reschedule|cancel|attach_documents|status|other
    department_id: int | None
    department_name: str | None
    routing_confidence: float
    appointment: dict | None
    documents_result: dict | None
    reminders: Annotated[list[dict], operator.add]
    safety_flags: Annotated[list[str], operator.add]
    escalation_id: int | None
    plan: list[str]  # coordinator's remaining steps
    completed_steps: Annotated[list[str], operator.add]
    final_response: str | None
    error: str | None
