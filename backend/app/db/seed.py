"""Idempotent synthetic seed data: departments, doctors, appointment slots,
required documents, and demo users.

`seed(db)` is safe to call from a script, from a test, or from an admin
route: it checks for any existing Department row first and, if the db has
already been seeded, returns the current counts without inserting anything
a second time.
"""

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
    """Insert the default per-agent operating rules if none exist yet.

    Idempotent independently of the department/doctor/slot check below, so
    a db already seeded before this feature shipped still gets its default
    rules the next time seed() runs.
    """
    if db.query(AgentRule).first() is not None:
        return
    for agent_name, rule_texts in _AGENT_RULES.items():
        for rule_text in rule_texts:
            db.add(AgentRule(agent_name=agent_name, rule_text=rule_text, source="seed"))
    db.commit()


def seed(db: Session) -> dict[str, int]:
    """Insert synthetic demo data if the db is empty, else no-op.

    Returns row counts for departments, doctors, slots, required_documents,
    users and agent_rules - either the counts just inserted, or (on a
    repeat call) the counts already present.
    """
    if db.query(Department).first() is not None:
        _seed_agent_rules(db)
        return _counts(db)

    weekdays = _next_weekdays(date.today(), _WEEKDAY_COUNT)
    slot_times = [pair for day in weekdays for pair in _slot_times_for_day(day)]

    for dept_name, doctor_names in _DEPARTMENTS.items():
        dept = Department(name=dept_name)
        db.add(dept)
        db.flush()  # assign dept.id for the FKs below

        for document_type in _REQUIRED_DOCUMENTS.get(dept_name, []):
            db.add(RequiredDocument(department_id=dept.id, document_type=document_type))

        for doctor_name in doctor_names:
            doctor = Doctor(department_id=dept.id, name=doctor_name)
            db.add(doctor)
            db.flush()  # assign doctor.id for the slots below

            for start_time, end_time in slot_times:
                db.add(
                    AppointmentSlot(
                        doctor_id=doctor.id,
                        start_time=start_time,
                        end_time=end_time,
                        status="free",
                    )
                )

    patient = User(
        email="patient@agentcare-demo.com",
        password_hash=hash_password(_DEMO_PASSWORD),
        role="patient",
        full_name="Max Mustermann",
    )
    db.add(patient)
    db.flush()
    db.add(
        PatientProfile(
            user_id=patient.id,
            date_of_birth=date(1990, 5, 14),
            preferred_language="de",
        )
    )

    second_patient = User(
        email="erika@agentcare-demo.com",
        password_hash=hash_password(_DEMO_PASSWORD),
        role="patient",
        full_name="Erika Musterfrau",
    )
    db.add(second_patient)
    db.flush()
    db.add(PatientProfile(user_id=second_patient.id, preferred_language="de"))

    staff = User(
        email="staff@agentcare-demo.com",
        password_hash=hash_password(_DEMO_PASSWORD),
        role="staff",
        full_name="Admin Petra Muster",
    )
    db.add(staff)

    db.commit()
    _seed_agent_rules(db)
    return _counts(db)
