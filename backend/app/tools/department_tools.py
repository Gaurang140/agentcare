"""Lookups over the department catalog, plus the minimal admin mutations
(create department/doctor, toggle a doctor active/inactive) staff routes use.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.exceptions import ConflictError, NotFoundError
from app.models import Department, Doctor
from app.tools.audit_tools import write_audit


def list_departments(db: Session) -> list[dict]:
    """Every department, alphabetical by name."""
    departments = db.query(Department).order_by(Department.name).all()
    return [
        {"id": dept.id, "name": dept.name, "description": dept.description}
        for dept in departments
    ]


def find_department(db: Session, name_or_hint: str) -> dict | None:
    """Fuzzy match a free-text hint against department names.

    Case-insensitive substring match in either direction (hint contains the
    name, or the name contains the hint), e.g. "cardio" or "the heart dept"
    both find "Cardiology" if the hint text happens to contain the name.
    Returns None if nothing matches.
    """
    hint = name_or_hint.strip().lower()
    if not hint:
        return None

    for dept in db.query(Department).all():
        dept_name = dept.name.lower()
        if hint in dept_name or dept_name in hint:
            return {"id": dept.id, "name": dept.name, "description": dept.description}
    return None


def create_department(db: Session, actor_id: int, name: str, description: str | None) -> dict:
    """Create a department. Raises ConflictError on a duplicate name."""
    dept = Department(name=name, description=description)
    db.add(dept)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(f"A department named {name!r} already exists") from exc

    write_audit(db, actor_id, "department.created", "department", dept.id, {"name": name})
    db.commit()
    return {"id": dept.id, "name": dept.name, "description": dept.description}


def create_doctor(db: Session, actor_id: int, department_id: int, name: str) -> dict:
    """Create a doctor under an existing department."""
    dept = db.get(Department, department_id)
    if dept is None:
        raise NotFoundError(f"Department {department_id} not found")

    doctor = Doctor(department_id=department_id, name=name)
    db.add(doctor)
    db.flush()
    write_audit(
        db, actor_id, "doctor.created", "doctor", doctor.id, {"department_id": department_id, "name": name}
    )
    db.commit()
    return {"id": doctor.id, "department_id": doctor.department_id, "name": doctor.name, "active": doctor.active}


def set_doctor_active(db: Session, actor_id: int, doctor_id: int, active: bool) -> dict:
    """Toggle a doctor's active flag (inactive doctors keep their history but
    stop coming up as bookable elsewhere)."""
    doctor = db.get(Doctor, doctor_id)
    if doctor is None:
        raise NotFoundError(f"Doctor {doctor_id} not found")

    doctor.active = active
    db.flush()
    write_audit(db, actor_id, "doctor.updated", "doctor", doctor.id, {"active": active})
    db.commit()
    return {"id": doctor.id, "department_id": doctor.department_id, "name": doctor.name, "active": doctor.active}
