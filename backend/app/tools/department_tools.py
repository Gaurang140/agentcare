"""Read-only lookups over the department catalog."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Department


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
