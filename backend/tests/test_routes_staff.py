"""RBAC and happy-path coverage for the staff routes: requests queue,
escalations (replacing the Task 4 stub), the audit trail, the minimal
catalog admin, and the internal reminders trigger's dual auth path.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.config import settings
from app.db.seed import seed
from app.models import AgentRule, AuditEvent, Escalation, User


def test_staff_requests_denied_for_patient(patient_client):
    assert patient_client.get("/api/staff/requests").status_code == 403


def test_staff_requests_allowed_for_staff(staff_client):
    resp = staff_client.get("/api/staff/requests")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_staff_escalations_replaces_stub_and_lists_open_ones(staff_client, db_session):
    esc = Escalation(
        workflow_run_id=None, reason="task 12 test escalation", severity="uncertainty", status="open"
    )
    db_session.add(esc)
    db_session.commit()

    resp = staff_client.get("/api/staff/escalations")
    assert resp.status_code == 200
    ids = [row["id"] for row in resp.json()]
    assert esc.id in ids


def test_staff_escalations_denied_for_patient(patient_client):
    assert patient_client.get("/api/staff/escalations").status_code == 403


def test_resolve_escalation_persists_reviewer_note_and_audit(staff_client, db_session):
    esc = Escalation(
        workflow_run_id=None, reason="needs a human", severity="uncertainty", status="open"
    )
    db_session.add(esc)
    db_session.commit()

    resp = staff_client.post(
        f"/api/staff/escalations/{esc.id}/resolve",
        json={"approve": True, "note": "reviewed, looks fine"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved"
    assert body["resolution_note"] == "reviewed, looks fine"

    staff = db_session.query(User).filter_by(email="staff@example.com").first()
    assert body["reviewed_by"] == staff.id

    audit = (
        db_session.query(AuditEvent)
        .filter_by(action="escalation.resolved", entity_type="escalation", entity_id=esc.id)
        .first()
    )
    assert audit is not None
    assert audit.actor_id == staff.id


def test_resolve_escalation_denied_for_patient(patient_client, db_session):
    esc = Escalation(workflow_run_id=None, reason="x", severity="uncertainty", status="open")
    db_session.add(esc)
    db_session.commit()

    resp = patient_client.post(
        f"/api/staff/escalations/{esc.id}/resolve", json={"approve": True, "note": "n"}
    )
    assert resp.status_code == 403


def test_staff_audit_denied_for_patient(patient_client):
    assert patient_client.get("/api/staff/audit").status_code == 403


def test_staff_audit_lists_events_for_staff(staff_client):
    resp = staff_client.get("/api/staff/audit")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_create_department_doctor_and_generate_slots(staff_client):
    dept_resp = staff_client.post(
        "/api/staff/departments", json={"name": "Task12-Neurology", "description": "brain"}
    )
    assert dept_resp.status_code == 201, dept_resp.text
    dept_id = dept_resp.json()["id"]

    doctor_resp = staff_client.post(
        "/api/staff/doctors", json={"department_id": dept_id, "name": "Dr. Task Twelve"}
    )
    assert doctor_resp.status_code == 201, doctor_resp.text
    doctor_id = doctor_resp.json()["id"]
    assert doctor_resp.json()["active"] is True

    toggle_resp = staff_client.patch(f"/api/staff/doctors/{doctor_id}", json={"active": False})
    assert toggle_resp.status_code == 200
    assert toggle_resp.json()["active"] is False

    slots_resp = staff_client.post(
        "/api/staff/slots/generate",
        json={
            "doctor_id": doctor_id,
            "date_from": date.today().isoformat(),
            "date_to": (date.today() + timedelta(days=2)).isoformat(),
        },
    )
    assert slots_resp.status_code == 200, slots_resp.text
    assert len(slots_resp.json()) > 0
    assert slots_resp.json()[0]["doctor_id"] == doctor_id


def test_list_doctors_returns_created_doctor_and_filters_by_department(staff_client):
    dept_resp = staff_client.post(
        "/api/staff/departments", json={"name": "Task15-Oncology", "description": None}
    )
    assert dept_resp.status_code == 201, dept_resp.text
    dept_id = dept_resp.json()["id"]

    other_dept_resp = staff_client.post(
        "/api/staff/departments", json={"name": "Task15-Urology", "description": None}
    )
    other_dept_id = other_dept_resp.json()["id"]

    doctor_resp = staff_client.post(
        "/api/staff/doctors", json={"department_id": dept_id, "name": "Dr. Task Fifteen"}
    )
    assert doctor_resp.status_code == 201, doctor_resp.text
    doctor_id = doctor_resp.json()["id"]

    other_doctor_resp = staff_client.post(
        "/api/staff/doctors", json={"department_id": other_dept_id, "name": "Dr. Other Department"}
    )
    assert other_doctor_resp.status_code == 201

    unfiltered = staff_client.get("/api/staff/doctors")
    assert unfiltered.status_code == 200
    unfiltered_ids = [row["id"] for row in unfiltered.json()]
    assert doctor_id in unfiltered_ids
    assert other_doctor_resp.json()["id"] in unfiltered_ids

    filtered = staff_client.get(f"/api/staff/doctors?department_id={dept_id}")
    assert filtered.status_code == 200
    filtered_ids = [row["id"] for row in filtered.json()]
    assert filtered_ids == [doctor_id]


def test_list_doctors_denied_for_patient(patient_client):
    assert patient_client.get("/api/staff/doctors").status_code == 403


def test_create_department_conflict_on_duplicate_name(staff_client):
    payload = {"name": "Task12-Duplicate-Dept", "description": None}
    first = staff_client.post("/api/staff/departments", json=payload)
    assert first.status_code == 201

    second = staff_client.post("/api/staff/departments", json=payload)
    assert second.status_code == 409


def test_catalog_admin_routes_denied_for_patient(patient_client):
    assert (
        patient_client.post("/api/staff/departments", json={"name": "X"}).status_code == 403
    )
    assert (
        patient_client.post(
            "/api/staff/doctors", json={"department_id": 1, "name": "X"}
        ).status_code
        == 403
    )
    assert patient_client.patch("/api/staff/doctors/1", json={"active": True}).status_code == 403
    assert (
        patient_client.post(
            "/api/staff/slots/generate",
            json={"doctor_id": 1, "date_from": "2026-01-01", "date_to": "2026-01-02"},
        ).status_code
        == 403
    )


def test_create_list_and_toggle_agent_rule(staff_client, db_session):
    create_resp = staff_client.post(
        "/api/staff/agent-rules",
        json={"agent_name": "routing", "rule_text": "Task F3 test rule for routing"},
    )
    assert create_resp.status_code == 201, create_resp.text
    body = create_resp.json()
    assert body["agent_name"] == "routing"
    assert body["source"] == "staff"
    assert body["active"] is True
    rule_id = body["id"]

    list_resp = staff_client.get("/api/staff/agent-rules?agent_name=routing")
    assert list_resp.status_code == 200
    ids = [row["id"] for row in list_resp.json()]
    assert rule_id in ids
    assert all(row["agent_name"] == "routing" for row in list_resp.json())

    toggle_resp = staff_client.patch(f"/api/staff/agent-rules/{rule_id}", json={"active": False})
    assert toggle_resp.status_code == 200
    assert toggle_resp.json()["active"] is False

    audit = (
        db_session.query(AuditEvent)
        .filter_by(action="agent_rule.updated", entity_type="agent_rule", entity_id=rule_id)
        .first()
    )
    assert audit is not None

    created_audit = (
        db_session.query(AuditEvent)
        .filter_by(action="agent_rule.created", entity_type="agent_rule", entity_id=rule_id)
        .first()
    )
    assert created_audit is not None


def test_create_agent_rule_rejects_unknown_agent_name(staff_client):
    resp = staff_client.post(
        "/api/staff/agent-rules",
        json={"agent_name": "not-a-real-agent", "rule_text": "x"},
    )
    assert resp.status_code == 400


def test_agent_rules_denied_for_patient(patient_client):
    assert patient_client.get("/api/staff/agent-rules").status_code == 403
    assert (
        patient_client.post(
            "/api/staff/agent-rules", json={"agent_name": "routing", "rule_text": "x"}
        ).status_code
        == 403
    )
    assert patient_client.patch("/api/staff/agent-rules/1", json={"active": True}).status_code == 403


def test_list_agent_rules_includes_seed_rules(staff_client, db_session):
    seed(db_session)  # idempotent - guarantees the default rules exist regardless of test order
    seed_row = db_session.query(AgentRule).filter_by(source="seed").first()
    assert seed_row is not None

    resp = staff_client.get("/api/staff/agent-rules")
    assert resp.status_code == 200
    sources = {row["source"] for row in resp.json()}
    assert "seed" in sources


def test_internal_reminders_run_due_requires_staff_when_no_token(patient_client, monkeypatch):
    monkeypatch.setattr(settings, "internal_task_token", "")
    resp = patient_client.post("/api/internal/reminders/run-due")
    assert resp.status_code == 403


def test_internal_reminders_run_due_allows_staff_when_no_token(staff_client, monkeypatch):
    monkeypatch.setattr(settings, "internal_task_token", "")
    resp = staff_client.post("/api/internal/reminders/run-due")
    assert resp.status_code == 200
    assert "sent_count" in resp.json()


def test_internal_reminders_run_due_accepts_correct_token_without_login(client, monkeypatch):
    monkeypatch.setattr(settings, "internal_task_token", "shared-secret")
    resp = client.post("/api/internal/reminders/run-due", headers={"X-Internal-Token": "shared-secret"})
    assert resp.status_code == 200


def test_internal_reminders_run_due_rejects_wrong_token(client, monkeypatch):
    monkeypatch.setattr(settings, "internal_task_token", "shared-secret")
    resp = client.post("/api/internal/reminders/run-due", headers={"X-Internal-Token": "wrong"})
    assert resp.status_code == 403


def test_internal_reminders_run_due_rejects_missing_token_even_from_staff_cookie(
    staff_client, monkeypatch
):
    """Once a token is configured, the header IS the auth - a staff cookie
    alone no longer suffices (matches the brief: token when set, else
    require_role("staff") - not "either, when set")."""
    monkeypatch.setattr(settings, "internal_task_token", "shared-secret")
    resp = staff_client.post("/api/internal/reminders/run-due")
    assert resp.status_code == 403
