"""Request/response schemas for staff-only routes: requests, escalations,
audit, the minimal catalog admin (departments/doctors/slots) CRUD, and the
procedural agent-rules CRUD.
"""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class WorkflowRunSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    patient_id: int
    status: str
    current_step: str | None
    request_text: str
    created_at: datetime
    updated_at: datetime


class EscalationOut(BaseModel):
    id: int
    workflow_run_id: int | None
    reason: str
    severity: str
    status: str
    reviewed_by: int | None = None
    resolution_note: str | None = None
    created_at: datetime


class ResolveEscalationRequest(BaseModel):
    approve: bool
    note: str = ""


class AuditEventOut(BaseModel):
    id: int
    actor_id: int | None
    action: str
    entity_type: str
    entity_id: int | None
    metadata: dict | None = None
    created_at: datetime


class DepartmentCreate(BaseModel):
    name: str
    description: str | None = None


class DoctorCreate(BaseModel):
    department_id: int
    name: str


class DoctorUpdate(BaseModel):
    active: bool


class DoctorOut(BaseModel):
    id: int
    department_id: int
    name: str
    active: bool


class SlotGenerateRequest(BaseModel):
    doctor_id: int
    date_from: date
    date_to: date


class ReminderRunResponse(BaseModel):
    sent_count: int
    reminder_ids: list[int]


class AgentRuleOut(BaseModel):
    id: int
    agent_name: str
    rule_text: str
    source: str  # "seed" | "staff"
    active: bool
    created_at: datetime


class AgentRuleCreate(BaseModel):
    agent_name: str
    rule_text: str


class AgentRuleUpdate(BaseModel):
    active: bool
