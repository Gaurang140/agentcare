"""Tests for the ORM models, session factory, and table wiring.

These exercise app/db/base.py, app/db/session.py and every module under
app/models/ against an in-memory sqlite database built straight from
Base.metadata (no alembic involved) so the tests stay fast and isolated.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models import (
    Appointment,
    AppointmentSlot,
    AuditEvent,
    Department,
    Doctor,
    Escalation,
    PatientDocument,
    PatientProfile,
    Reminder,
    RequiredDocument,
    User,
    WorkflowRun,
)


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()


def test_session_module_exports_engine_sessionlocal_and_get_db() -> None:
    from app.db.session import SessionLocal, engine, get_db

    assert engine is not None
    assert SessionLocal is not None

    gen = get_db()
    session = next(gen)
    assert isinstance(session, Session)
    # Draining the generator must close the session without raising.
    with pytest.raises(StopIteration):
        next(gen)


def test_user_and_patient_profile_round_trip(db: Session) -> None:
    user = User(
        email="patient@example.com",
        password_hash="hashed",
        role="patient",
        full_name="Max Mustermann",
    )
    db.add(user)
    db.flush()

    profile = PatientProfile(
        user_id=user.id,
        date_of_birth=date(1990, 5, 14),
        phone="+49 170 0000000",
        preferred_language="de",
        emergency_contact="Erika Musterfrau",
    )
    db.add(profile)
    db.commit()

    fetched = db.get(User, user.id)
    assert fetched is not None
    assert fetched.role == "patient"
    assert fetched.patient_profile.preferred_language == "de"
    assert fetched.patient_profile.date_of_birth == date(1990, 5, 14)
    assert fetched.created_at is not None
    assert fetched.updated_at is not None


def test_department_doctor_required_document_chain(db: Session) -> None:
    dept = Department(name="Cardiology")
    db.add(dept)
    db.flush()

    doctor = Doctor(department_id=dept.id, name="Dr. Anna Beispiel")
    required = RequiredDocument(department_id=dept.id, document_type="ecg_report")
    db.add_all([doctor, required])
    db.commit()

    assert doctor.department.name == "Cardiology"
    assert dept.doctors[0].name == "Dr. Anna Beispiel"
    assert dept.required_documents[0].document_type == "ecg_report"


def test_appointment_slot_unique_constraint_blocks_double_booking_the_same_start(
    db: Session,
) -> None:
    dept = Department(name="Radiology")
    db.add(dept)
    db.flush()
    doctor = Doctor(department_id=dept.id, name="Dr. Radiology One")
    db.add(doctor)
    db.flush()

    start = datetime(2026, 8, 3, 9, 0)
    slot_one = AppointmentSlot(
        doctor_id=doctor.id, start_time=start, end_time=start + timedelta(minutes=30)
    )
    db.add(slot_one)
    db.commit()

    dup = AppointmentSlot(
        doctor_id=doctor.id, start_time=start, end_time=start + timedelta(minutes=30)
    )
    db.add(dup)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_active_patient_ranges_cannot_overlap_but_may_be_adjacent(db: Session) -> None:
    patient = User(
        email="range-patient@example.com",
        password_hash="h",
        role="patient",
        full_name="Range Patient",
    )
    department = Department(name="Range Medicine")
    db.add_all([patient, department])
    db.flush()
    doctors = [
        Doctor(department_id=department.id, name="Dr. Range One"),
        Doctor(department_id=department.id, name="Dr. Range Two"),
        Doctor(department_id=department.id, name="Dr. Range Three"),
    ]
    db.add_all(doctors)
    db.flush()

    start = datetime(2026, 8, 4, 9, 0)
    slots = [
        AppointmentSlot(
            doctor_id=doctors[0].id,
            start_time=start,
            end_time=start + timedelta(hours=1),
        ),
        AppointmentSlot(
            doctor_id=doctors[1].id,
            start_time=start + timedelta(minutes=30),
            end_time=start + timedelta(hours=1, minutes=30),
        ),
        AppointmentSlot(
            doctor_id=doctors[2].id,
            start_time=start + timedelta(hours=1),
            end_time=start + timedelta(hours=2),
        ),
    ]
    db.add_all(slots)
    db.flush()

    first_appointment = Appointment(
        patient_id=patient.id,
        doctor_id=doctors[0].id,
        slot_id=slots[0].id,
        scheduled_start=start,
        scheduled_end=start + timedelta(hours=1),
        status="confirmed",
    )
    db.add(first_appointment)
    db.commit()

    # The update trigger must exclude the row being updated.
    first_appointment.reason = "updated without changing its range"
    db.commit()

    db.add(
        Appointment(
            patient_id=patient.id,
            doctor_id=doctors[1].id,
            slot_id=slots[1].id,
            scheduled_start=start + timedelta(minutes=30),
            scheduled_end=start + timedelta(hours=1, minutes=30),
            status="pending",
        )
    )
    with pytest.raises(IntegrityError, match="ex_appointments_patient_schedule"):
        db.commit()
    db.rollback()

    adjacent = Appointment(
        patient_id=patient.id,
        doctor_id=doctors[2].id,
        slot_id=slots[2].id,
        scheduled_start=start + timedelta(hours=1),
        scheduled_end=start + timedelta(hours=2),
        status="confirmed",
    )
    db.add(adjacent)
    db.commit()

    adjacent.scheduled_start = start + timedelta(minutes=30)
    with pytest.raises(IntegrityError, match="ex_appointments_patient_schedule"):
        db.commit()
    db.rollback()


def test_cancelled_and_completed_ranges_do_not_block_new_active_rows(
    db: Session,
) -> None:
    patient = User(
        email="historical-ranges@example.com",
        password_hash="h",
        role="patient",
        full_name="Historical Ranges",
    )
    department = Department(name="Historical Medicine")
    db.add_all([patient, department])
    db.flush()
    doctors = [
        Doctor(department_id=department.id, name="Dr. History One"),
        Doctor(department_id=department.id, name="Dr. History Two"),
        Doctor(department_id=department.id, name="Dr. History Three"),
    ]
    db.add_all(doctors)
    db.flush()

    start = datetime(2026, 8, 5, 9, 0)
    slots = [
        AppointmentSlot(
            doctor_id=doctor.id,
            start_time=start,
            end_time=start + timedelta(hours=1),
        )
        for doctor in doctors
    ]
    db.add_all(slots)
    db.flush()
    db.add_all(
        [
            Appointment(
                patient_id=patient.id,
                doctor_id=doctors[0].id,
                slot_id=slots[0].id,
                scheduled_start=start,
                scheduled_end=start + timedelta(hours=1),
                status="cancelled",
            ),
            Appointment(
                patient_id=patient.id,
                doctor_id=doctors[1].id,
                slot_id=slots[1].id,
                scheduled_start=start,
                scheduled_end=start + timedelta(hours=1),
                status="completed",
            ),
        ]
    )
    db.commit()

    db.add(
        Appointment(
            patient_id=patient.id,
            doctor_id=doctors[2].id,
            slot_id=slots[2].id,
            scheduled_start=start,
            scheduled_end=start + timedelta(hours=1),
            status="confirmed",
        )
    )
    db.commit()


def test_doctor_slot_ranges_cannot_overlap_but_may_be_adjacent(db: Session) -> None:
    department = Department(name="Slot Range Medicine")
    db.add(department)
    db.flush()
    doctor = Doctor(department_id=department.id, name="Dr. Slot Range")
    db.add(doctor)
    db.flush()

    start = datetime(2026, 8, 6, 9, 0)
    db.add(
        AppointmentSlot(
            doctor_id=doctor.id,
            start_time=start,
            end_time=start + timedelta(hours=1),
        )
    )
    db.commit()

    adjacent = AppointmentSlot(
        doctor_id=doctor.id,
        start_time=start + timedelta(hours=1),
        end_time=start + timedelta(hours=2),
    )
    db.add(adjacent)
    db.commit()

    db.add(
        AppointmentSlot(
            doctor_id=doctor.id,
            start_time=start + timedelta(minutes=30),
            end_time=start + timedelta(hours=1, minutes=30),
        )
    )
    with pytest.raises(IntegrityError, match="ex_appointment_slots_doctor_schedule"):
        db.commit()
    db.rollback()

    adjacent.start_time = start + timedelta(minutes=30)
    with pytest.raises(IntegrityError, match="ex_appointment_slots_doctor_schedule"):
        db.commit()
    db.rollback()


def test_appointment_links_patient_doctor_and_slot(db: Session) -> None:
    patient = User(
        email="p@example.com", password_hash="h", role="patient", full_name="P One"
    )
    dept = Department(name="Orthopedics")
    db.add_all([patient, dept])
    db.flush()
    doctor = Doctor(department_id=dept.id, name="Dr. Ortho")
    db.add(doctor)
    db.flush()

    start = datetime(2026, 8, 4, 9, 0)
    slot = AppointmentSlot(
        doctor_id=doctor.id,
        start_time=start,
        end_time=start + timedelta(minutes=30),
        status="booked",
    )
    db.add(slot)
    db.flush()

    appt = Appointment(
        patient_id=patient.id,
        doctor_id=doctor.id,
        slot_id=slot.id,
        scheduled_start=start,
        scheduled_end=start + timedelta(minutes=30),
        status="confirmed",
        reason="checkup",
    )
    db.add(appt)
    db.commit()

    assert appt.id is not None
    assert appt.slot.status == "booked"
    assert appt.doctor.name == "Dr. Ortho"


def test_patient_document_stores_checksum_and_storage_ref(db: Session) -> None:
    patient = User(
        email="doc@example.com",
        password_hash="h",
        role="patient",
        full_name="Doc Patient",
    )
    db.add(patient)
    db.flush()

    doc = PatientDocument(
        patient_id=patient.id,
        filename="ecg.pdf",
        document_type="ecg_report",
        checksum="a" * 64,
        storage_ref="uploads/1/uuid_ecg.pdf",
    )
    db.add(doc)
    db.commit()

    fetched = db.get(PatientDocument, doc.id)
    assert fetched is not None
    assert fetched.checksum == "a" * 64
    assert fetched.patient_id == patient.id


def test_workflow_run_reminder_escalation_and_audit_event(db: Session) -> None:
    user = User(
        email="wf@example.com",
        password_hash="h",
        role="patient",
        full_name="WF Patient",
    )
    db.add(user)
    db.flush()

    run = WorkflowRun(
        user_id=user.id,
        patient_id=user.id,
        thread_id="wf-1",
        request_text="I need a cardiology appointment next week",
        current_step="routing",
        state={"intent": "book"},
        status="running",
    )
    db.add(run)
    db.flush()

    reminder = Reminder(
        patient_id=user.id,
        appointment_id=None,
        reminder_type="appointment",
        scheduled_at=datetime(2026, 8, 5, 8, 0),
    )
    escalation = Escalation(
        workflow_run_id=run.id,
        reason="ambiguous department",
        severity="uncertainty",
        status="open",
    )
    audit = AuditEvent(
        actor_id=user.id,
        action="workflow.started",
        entity_type="workflow_run",
        entity_id=run.id,
        metadata_json={"thread_id": "wf-1"},
    )
    db.add_all([reminder, escalation, audit])
    db.commit()

    assert run.state["intent"] == "book"
    assert reminder.sent is False
    assert escalation.workflow_run.thread_id == "wf-1"
    assert audit.metadata_json["thread_id"] == "wf-1"


def test_escalation_reviewed_by_links_to_staff_user(db: Session) -> None:
    staff = User(
        email="staff@example.com",
        password_hash="h",
        role="staff",
        full_name="Admin Petra",
    )
    db.add(staff)
    db.flush()

    escalation = Escalation(
        workflow_run_id=None,
        reason="patient requested a doctor by name",
        severity="agent_failure",
        status="approved",
        reviewed_by=staff.id,
        resolution_note="manually booked",
    )
    db.add(escalation)
    db.commit()

    fetched = db.get(Escalation, escalation.id)
    assert fetched is not None
    assert fetched.reviewed_by == staff.id
