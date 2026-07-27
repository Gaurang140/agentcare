"""Idempotently reconcile the synthetic demo catalog and users."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.auth.security import hash_password
from app.models import (
    AgentRule,
    AppointmentSlot,
    Department,
    Doctor,
    PatientProfile,
    RequiredDocument,
    User,
)

# All values below are synthetic demo data only - no real patient or
# provider information.
_DEMO_PASSWORD = "demo1234"

# Department -> its two synthetic doctors.
_DEPARTMENTS: dict[str, list[str]] = {
    "Cardiology": ["Dr. Anna Beispiel", "Dr. Thomas Krause"],
    "Dermatology": ["Dr. Lena Vogel", "Dr. Felix Bauer"],
    "Orthopedics": ["Dr. Julia Hoffmann", "Dr. Stefan Wolf"],
    "General Medicine": ["Dr. Nina Schulz", "Dr. Markus Fischer"],
    "Radiology": ["Dr. Sophie Lange", "Dr. Peter Zimmer"],
}

# Department -> document types a patient must upload before that
# department's appointment.
_REQUIRED_DOCUMENTS: dict[str, list[str]] = {
    "Cardiology": ["ecg_report", "blood_test"],
    "Radiology": ["referral_letter"],
    "Orthopedics": ["imaging_report"],
}

_SLOT_START_HOUR = 9
_SLOT_END_HOUR = 17
_SLOT_MINUTES = 30
_WEEKDAY_COUNT = 14

# Default procedural memory (app/agents/memory.py): agent name -> its
# starter operating rules. Purely administrative, matching the boundary the
# base prompts in app/agents/prompts.py already hold to.
_AGENT_RULES: dict[str, list[str]] = {
    "routing": [
        "When a request mentions an existing appointment, prefer reschedule intent over book.",
        "Treat a request that only asks about status, with no new booking action, as intent status rather than book.",
    ],
    "appointment": [
        "Prefer the earliest slot inside the patient's stated time window; never propose slots in the past.",
        "If no slot fits the patient's stated window, pick the earliest slot overall rather than escalating immediately.",
    ],
    "document": [
        "When document type is ambiguous, classify as other rather than guessing.",
        "Never classify a document as insurance_card unless the extracted text or filename clearly indicates an insurance document.",
    ],
    "followup": [
        "Never schedule a reminder after the appointment time.",
        "Always include one reminder for each missing required document type.",
    ],
    "safety": [
        "When unsure whether wording is medical advice, escalate instead of answering.",
        "Never state a specific appointment time that was not confirmed by a real slot booking.",
    ],
    "coordinator": [
        "Escalate rather than loop: if the same step has run twice without progress, choose escalate.",
        "Run route_department before handle_appointment for every new booking request.",
    ],
}


def _next_weekdays(start: date, count: int) -> list[date]:
    """The next `count` Mon-Fri dates starting from (and including) `start`."""
    days: list[date] = []
    current = start
    while len(days) < count:
        if current.weekday() < 5:  # Monday=0 ... Sunday=6
            days.append(current)
        current += timedelta(days=1)
    return days


def _slot_times_for_day(day: date) -> list[tuple[datetime, datetime]]:
    """Every 30-minute (start, end) pair from 09:00 up to 17:00 on `day`."""
    day_start = datetime.combine(day, datetime.min.time()).replace(hour=_SLOT_START_HOUR)
    day_end = datetime.combine(day, datetime.min.time()).replace(hour=_SLOT_END_HOUR)
    step = timedelta(minutes=_SLOT_MINUTES)

    times: list[tuple[datetime, datetime]] = []
    cursor = day_start
    while cursor + step <= day_end:
        times.append((cursor, cursor + step))
        cursor += step
    return times


def _counts(db: Session) -> dict[str, int]:
    return {
        "departments": db.query(Department).count(),
        "doctors": db.query(Doctor).count(),
        "slots": db.query(AppointmentSlot).count(),
        "required_documents": db.query(RequiredDocument).count(),
        "users": db.query(User).count(),
        "agent_rules": db.query(AgentRule).count(),
    }


def _seed_agent_rules(db: Session) -> None:
    """Insert each missing default per-agent operating rule."""
    existing = {
        (row.agent_name, row.rule_text)
        for row in db.query(AgentRule.agent_name, AgentRule.rule_text).all()
    }
    for agent_name, rule_texts in _AGENT_RULES.items():
        for rule_text in rule_texts:
            if (agent_name, rule_text) not in existing:
                db.add(
                    AgentRule(
                        agent_name=agent_name,
                        rule_text=rule_text,
                        source="seed",
                    )
                )


def _seed_catalog(db: Session) -> None:
    weekdays = _next_weekdays(date.today(), _WEEKDAY_COUNT)
    slot_times = [pair for day in weekdays for pair in _slot_times_for_day(day)]

    for dept_name, doctor_names in _DEPARTMENTS.items():
        dept = db.query(Department).filter_by(name=dept_name).one_or_none()
        if dept is None:
            dept = Department(name=dept_name)
            db.add(dept)
            db.flush()

        existing_documents = {
            row[0]
            for row in db.query(RequiredDocument.document_type)
            .filter_by(department_id=dept.id)
            .all()
        }
        for document_type in _REQUIRED_DOCUMENTS.get(dept_name, []):
            if document_type not in existing_documents:
                db.add(
                    RequiredDocument(
                        department_id=dept.id,
                        document_type=document_type,
                    )
                )

        for doctor_name in doctor_names:
            doctor = (
                db.query(Doctor)
                .filter_by(department_id=dept.id, name=doctor_name)
                .first()
            )
            if doctor is None:
                doctor = Doctor(department_id=dept.id, name=doctor_name)
                db.add(doctor)
                db.flush()

            existing_starts = {
                row[0]
                for row in db.query(AppointmentSlot.start_time)
                .filter_by(doctor_id=doctor.id)
                .all()
            }
            for start_time, end_time in slot_times:
                if start_time not in existing_starts:
                    db.add(
                        AppointmentSlot(
                            doctor_id=doctor.id,
                            start_time=start_time,
                            end_time=end_time,
                            status="free",
                        )
                    )


def _seed_patient(
    db: Session,
    *,
    email: str,
    full_name: str,
    preferred_language: str,
    date_of_birth: date | None = None,
) -> None:
    user = db.query(User).filter_by(email=email).one_or_none()
    if user is None:
        user = User(
            email=email,
            password_hash=hash_password(_DEMO_PASSWORD),
            role="patient",
            full_name=full_name,
        )
        db.add(user)
        db.flush()

    if user.role == "patient" and user.patient_profile is None:
        db.add(
            PatientProfile(
                user_id=user.id,
                date_of_birth=date_of_birth,
                preferred_language=preferred_language,
            )
        )


def _seed_users(db: Session) -> None:
    _seed_patient(
        db,
        email="patient@agentcare-demo.com",
        full_name="Max Mustermann",
        preferred_language="en",
        date_of_birth=date(1990, 5, 14),
    )
    _seed_patient(
        db,
        email="erika@agentcare-demo.com",
        full_name="Erika Musterfrau",
        preferred_language="de",
    )

    if db.query(User).filter_by(email="staff@agentcare-demo.com").one_or_none() is None:
        db.add(
            User(
                email="staff@agentcare-demo.com",
                password_hash=hash_password(_DEMO_PASSWORD),
                role="staff",
                full_name="Admin Petra Muster",
            )
        )


def seed(db: Session) -> dict[str, int]:
    """Insert any missing canonical demo records and return current counts."""
    _seed_catalog(db)
    _seed_users(db)
    _seed_agent_rules(db)
    db.commit()
    return _counts(db)
