"""Staff-only routes: the request/escalation queues, the audit trail,
minimal catalog admin (departments/doctors/slots), and the procedural
agent-rules CRUD. Everything here requires require_role("staff") except the
internal reminders trigger, which accepts either an internal token or a
staff cookie (see `app.auth.dependencies.require_internal_or_staff`).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth.dependencies import require_internal_or_staff, require_role
from app.db.session import get_db
from app.exceptions import NotFoundError
from app.models import AuditEvent, Escalation, User, WorkflowRun
from app.schemas.appointment import DepartmentOut, SlotOut
from app.schemas.staff import (
    AgentRuleCreate,
    AgentRuleOut,
    AgentRuleUpdate,
    AuditEventOut,
    DepartmentCreate,
    DoctorCreate,
    DoctorOut,
    DoctorUpdate,
    EscalationOut,
    ReminderRunResponse,
    ResolveEscalationRequest,
    SlotGenerateRequest,
    WorkflowRunSummary,
)
from app.tools.agent_rule_tools import create_rule, list_rules, set_rule_active
from app.tools.appointment_tools import generate_slots_for_doctor
from app.tools.department_tools import (
    create_department,
    create_doctor,
    list_doctors,
    set_doctor_active,
)
from app.tools.escalation_tools import resolve_escalation
from app.tools.followup_tools import send_due_reminders

router = APIRouter(prefix="/staff", tags=["staff"])
internal_router = APIRouter(prefix="/internal", tags=["internal"])


def _to_summary(run: WorkflowRun) -> WorkflowRunSummary:
    return WorkflowRunSummary(
        id=run.id,
        patient_id=run.patient_id,
        status=run.status,
        current_step=run.current_step,
        request_text=run.request_text,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _to_escalation_out(escalation: Escalation) -> EscalationOut:
    return EscalationOut(
        id=escalation.id,
        workflow_run_id=escalation.workflow_run_id,
        reason=escalation.reason,
        severity=escalation.severity,
        status=escalation.status,
        reviewed_by=escalation.reviewed_by,
        resolution_note=escalation.resolution_note,
        created_at=escalation.created_at,
    )


@router.get("/requests", response_model=list[WorkflowRunSummary])
def list_requests(
    _staff: Annotated[User, Depends(require_role("staff"))],
    db: Annotated[Session, Depends(get_db)],
    status: str | None = None,
) -> list[WorkflowRunSummary]:
    query = db.query(WorkflowRun)
    if status is not None:
        query = query.filter(WorkflowRun.status == status)
    runs = query.order_by(WorkflowRun.created_at.desc()).all()
    return [_to_summary(run) for run in runs]


@router.get("/escalations", response_model=list[EscalationOut])
def list_escalations(
    _staff: Annotated[User, Depends(require_role("staff"))],
    db: Annotated[Session, Depends(get_db)],
    status: str = "open",
) -> list[EscalationOut]:
    query = db.query(Escalation)
    if status:
        query = query.filter(Escalation.status == status)
    escalations = query.order_by(Escalation.created_at.desc()).all()
    return [_to_escalation_out(e) for e in escalations]


@router.post("/escalations/{escalation_id}/resolve", response_model=EscalationOut)
def resolve_escalation_route(
    escalation_id: int,
    payload: ResolveEscalationRequest,
    staff: Annotated[User, Depends(require_role("staff"))],
    db: Annotated[Session, Depends(get_db)],
) -> EscalationOut:
    # resolve_escalation writes its own audit event with staff.id as the
    # real actor - no separate write_audit call needed here.
    resolve_escalation(db, escalation_id, staff.id, payload.approve, payload.note)
    escalation = db.get(Escalation, escalation_id)
    if escalation is None:
        raise NotFoundError(f"Escalation {escalation_id} not found")
    return _to_escalation_out(escalation)


@router.get("/audit", response_model=list[AuditEventOut])
def list_audit(
    _staff: Annotated[User, Depends(require_role("staff"))],
    db: Annotated[Session, Depends(get_db)],
    entity_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[AuditEventOut]:
    query = db.query(AuditEvent)
    if entity_type is not None:
        query = query.filter(AuditEvent.entity_type == entity_type)
    rows = query.order_by(AuditEvent.id.desc()).offset(offset).limit(limit).all()
    return [
        AuditEventOut(
            id=r.id,
            actor_id=r.actor_id,
            action=r.action,
            entity_type=r.entity_type,
            entity_id=r.entity_id,
            metadata=r.metadata_json,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.post("/departments", response_model=DepartmentOut, status_code=201)
def create_department_route(
    payload: DepartmentCreate,
    staff: Annotated[User, Depends(require_role("staff"))],
    db: Annotated[Session, Depends(get_db)],
) -> DepartmentOut:
    result = create_department(db, staff.id, payload.name, payload.description)
    return DepartmentOut(**result)


@router.get("/doctors", response_model=list[DoctorOut])
def list_doctors_route(
    _staff: Annotated[User, Depends(require_role("staff"))],
    db: Annotated[Session, Depends(get_db)],
    department_id: int | None = None,
) -> list[DoctorOut]:
    # No route listed doctors before this (only create/toggle existed) -
    # the catalog page's doctor list and the slot-generation form's doctor
    # picker both need real data to select from, not a client-only cache
    # of doctors created in the current session. Added alongside Task 15.
    return [DoctorOut(**row) for row in list_doctors(db, department_id)]


@router.post("/doctors", response_model=DoctorOut, status_code=201)
def create_doctor_route(
    payload: DoctorCreate,
    staff: Annotated[User, Depends(require_role("staff"))],
    db: Annotated[Session, Depends(get_db)],
) -> DoctorOut:
    result = create_doctor(db, staff.id, payload.department_id, payload.name)
    return DoctorOut(**result)


@router.patch("/doctors/{doctor_id}", response_model=DoctorOut)
def update_doctor_route(
    doctor_id: int,
    payload: DoctorUpdate,
    staff: Annotated[User, Depends(require_role("staff"))],
    db: Annotated[Session, Depends(get_db)],
) -> DoctorOut:
    result = set_doctor_active(db, staff.id, doctor_id, payload.active)
    return DoctorOut(**result)


@router.post("/slots/generate", response_model=list[SlotOut])
def generate_slots_route(
    payload: SlotGenerateRequest,
    _staff: Annotated[User, Depends(require_role("staff"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[SlotOut]:
    rows = generate_slots_for_doctor(db, payload.doctor_id, payload.date_from, payload.date_to)
    return [SlotOut(**row) for row in rows]


@router.get("/agent-rules", response_model=list[AgentRuleOut])
def list_agent_rules_route(
    _staff: Annotated[User, Depends(require_role("staff"))],
    db: Annotated[Session, Depends(get_db)],
    agent_name: str | None = None,
) -> list[AgentRuleOut]:
    return [AgentRuleOut(**row) for row in list_rules(db, agent_name)]


@router.post("/agent-rules", response_model=AgentRuleOut, status_code=201)
def create_agent_rule_route(
    payload: AgentRuleCreate,
    staff: Annotated[User, Depends(require_role("staff"))],
    db: Annotated[Session, Depends(get_db)],
) -> AgentRuleOut:
    result = create_rule(db, staff.id, payload.agent_name, payload.rule_text)
    return AgentRuleOut(**result)


@router.patch("/agent-rules/{rule_id}", response_model=AgentRuleOut)
def update_agent_rule_route(
    rule_id: int,
    payload: AgentRuleUpdate,
    staff: Annotated[User, Depends(require_role("staff"))],
    db: Annotated[Session, Depends(get_db)],
) -> AgentRuleOut:
    result = set_rule_active(db, staff.id, rule_id, payload.active)
    return AgentRuleOut(**result)


@internal_router.post("/reminders/run-due", response_model=ReminderRunResponse)
def run_due_reminders(
    _authorized: Annotated[None, Depends(require_internal_or_staff)],
    db: Annotated[Session, Depends(get_db)],
) -> ReminderRunResponse:
    return ReminderRunResponse(**send_due_reminders(db))
