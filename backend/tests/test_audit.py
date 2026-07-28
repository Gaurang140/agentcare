"""TDD for the audit trail: write_audit itself, and that every mutating tool
in the tools layer calls it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import AppointmentSlot, AuditEvent, User
from app.services.workflow_service import create_run
from app.tools.appointment_tools import book_appointment, cancel_appointment, reschedule_appointment
from app.tools.audit_tools import write_audit
from app.tools.escalation_tools import create_escalation, resolve_escalation
from app.tools.followup_tools import create_reminder


@pytest.fixture(autouse=True)
def _local_upload_dir(tmp_path, monkeypatch):
    from app import config as config_module

    monkeypatch.setattr(config_module.settings, "upload_dir", str(tmp_path))


def _free_slot(db) -> AppointmentSlot:
    slot = (
        db.query(AppointmentSlot)
        .filter_by(status="free")
        .order_by(AppointmentSlot.start_time.desc())
        .first()
    )
    assert slot is not None
    return slot


def test_write_audit_inserts_a_row(db, seeded):
    write_audit(db, actor_id=1, action="test.action", entity_type="test", entity_id=1, metadata={"k": "v"})
    db.commit()

    row = db.query(AuditEvent).filter_by(action="test.action").first()
    assert row is not None
    assert row.actor_id == 1
    assert row.entity_type == "test"
    assert row.metadata_json == {"k": "v"}


def test_write_audit_allows_null_actor_and_entity(db, seeded):
    write_audit(db, actor_id=None, action="system.tick", entity_type="system", entity_id=None)
    db.commit()

    row = db.query(AuditEvent).filter_by(action="system.tick").first()
    assert row is not None
    assert row.actor_id is None
    assert row.entity_id is None


def test_workflow_started_audit_does_not_copy_raw_request(db, seeded):
    patient = db.query(User).filter_by(role="patient").first()
    assert patient is not None
    raw_request = "Book an appointment for synthetic patient 12345"

    workflow = create_run(db, patient, raw_request)

    row = (
        db.query(AuditEvent)
        .filter_by(action="workflow.started", entity_id=workflow.id)
        .one()
    )
    assert raw_request not in str(row.metadata_json)
    assert row.metadata_json == {"request_length": len(raw_request)}


def test_book_appointment_writes_audit(db, seeded):
    slot = _free_slot(db)
    booking = book_appointment(db, patient_id=1, slot_id=slot.id, reason="checkup")

    row = db.query(AuditEvent).filter_by(action="appointment.booked", entity_id=booking["id"]).first()
    assert row is not None
    assert row.metadata_json["slot_id"] == slot.id


def test_reschedule_appointment_writes_audit(db, seeded):
    old_slot = _free_slot(db)
    booking = book_appointment(db, patient_id=1, slot_id=old_slot.id, reason="checkup")
    new_slot = (
        db.query(AppointmentSlot)
        .filter(AppointmentSlot.status == "free", AppointmentSlot.id != old_slot.id)
        .first()
    )
    reschedule_appointment(db, appointment_id=booking["id"], new_slot_id=new_slot.id)

    row = (
        db.query(AuditEvent)
        .filter_by(action="appointment.rescheduled", entity_id=booking["id"])
        .first()
    )
    assert row is not None


def test_cancel_appointment_writes_audit(db, seeded):
    slot = _free_slot(db)
    booking = book_appointment(db, patient_id=1, slot_id=slot.id, reason="checkup")
    cancel_appointment(db, appointment_id=booking["id"])

    row = db.query(AuditEvent).filter_by(action="appointment.cancelled", entity_id=booking["id"]).first()
    assert row is not None


def test_store_document_writes_audit(db, seeded):
    from app.tools.document_tools import store_document

    result = store_document(db, 1, "ecg.pdf", b"content", "ecg_report")

    row = db.query(AuditEvent).filter_by(action="document.stored", entity_id=result["id"]).first()
    assert row is not None


def test_create_reminder_writes_audit(db, seeded):
    reminder = create_reminder(
        db,
        patient_id=1,
        appointment_id=None,
        reminder_type="appointment",
        scheduled_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1),
    )

    row = db.query(AuditEvent).filter_by(action="reminder.created", entity_id=reminder["id"]).first()
    assert row is not None


def test_escalation_lifecycle_writes_audit(db, seeded):
    escalation = create_escalation(db, workflow_run_id=None, reason="uncertain intent", severity="uncertainty")
    resolve_escalation(db, escalation_id=escalation["id"], reviewer_id=3, approve=True, note="looks fine")

    created = db.query(AuditEvent).filter_by(action="escalation.created", entity_id=escalation["id"]).first()
    resolved = db.query(AuditEvent).filter_by(action="escalation.resolved", entity_id=escalation["id"]).first()
    assert created is not None
    assert resolved is not None
    assert resolved.actor_id == 3
