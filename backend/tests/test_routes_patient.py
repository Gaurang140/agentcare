"""Coverage for the patient self-service routes: department/slot catalog,
appointments (list/reschedule/cancel), and reminders.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.auth.security import hash_password
from app.db.seed import seed
from app.main import app
from app.models import Appointment, AppointmentSlot, Department, Reminder, User
from app.tools.appointment_tools import book_appointment, get_available_slots

_SLOT_WINDOW_DAYS = 14


def _cardiology_id(db_session) -> int:
    seed(db_session)
    dept = db_session.query(Department).filter_by(name="Cardiology").first()
    return dept.id


def test_departments_requires_auth():
    assert TestClient(app).get("/api/departments").status_code == 403


def test_list_departments_returns_seeded_rows(patient_client, db_session):
    _cardiology_id(db_session)
    resp = patient_client.get("/api/departments")
    assert resp.status_code == 200
    assert "Cardiology" in [d["name"] for d in resp.json()]


def test_department_slots_lists_free_slots(patient_client, db_session):
    dept_id = _cardiology_id(db_session)
    resp = patient_client.get(f"/api/departments/{dept_id}/slots")
    assert resp.status_code == 200
    slots = resp.json()
    assert len(slots) > 0
    assert slots[0]["doctor"]


def test_reminders_list_scoped_to_current_patient(patient_client, db_session):
    user = db_session.query(User).filter_by(email="patient@example.com").first()
    reminder = Reminder(
        patient_id=user.id,
        reminder_type="appointment",
        scheduled_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db_session.add(reminder)
    db_session.commit()

    resp = patient_client.get("/api/reminders")
    assert resp.status_code == 200
    assert reminder.id in [r["id"] for r in resp.json()]


def test_appointment_list_reschedule_and_cancel_round_trip(patient_client, db_session):
    dept_id = _cardiology_id(db_session)
    user = db_session.query(User).filter_by(email="patient@example.com").first()

    today = date.today()
    slots = get_available_slots(db_session, dept_id, today, today + timedelta(days=_SLOT_WINDOW_DAYS), limit=5)
    slot_id, other_slot_id = slots[0]["slot_id"], slots[1]["slot_id"]

    booked = book_appointment(db_session, user.id, slot_id, "checkup")
    appt_id = booked["id"]

    list_resp = patient_client.get("/api/appointments")
    assert list_resp.status_code == 200
    assert appt_id in [a["id"] for a in list_resp.json()]

    resched_resp = patient_client.post(
        f"/api/appointments/{appt_id}/reschedule", json={"new_slot_id": other_slot_id}
    )
    assert resched_resp.status_code == 200, resched_resp.text
    assert resched_resp.json()["status"] == "confirmed"

    cancel_resp = patient_client.post(f"/api/appointments/{appt_id}/cancel")
    assert cancel_resp.status_code == 200, cancel_resp.text
    assert cancel_resp.json()["status"] == "cancelled"


def test_replayed_cancel_over_http_cannot_free_another_patients_slot(patient_client, db_session):
    """The whole corruption sequence through the routes the patients actually
    call: book, cancel, someone else takes the freed slot, then the first
    cancel arrives again (double-clicked button, retried request). The second
    cancel has to come back 409 with the new holder's booking untouched.
    """
    dept_id = _cardiology_id(db_session)
    user = db_session.query(User).filter_by(email="patient@example.com").first()

    today = date.today()
    slots = get_available_slots(
        db_session, dept_id, today, today + timedelta(days=_SLOT_WINDOW_DAYS), limit=5
    )
    slot_id = slots[0]["slot_id"]

    booked = book_appointment(db_session, user.id, slot_id, "checkup")
    assert patient_client.post(f"/api/appointments/{booked['id']}/cancel").status_code == 200

    # Anyone can take the slot now that it is free again.
    rebooked = book_appointment(db_session, user.id, slot_id, "someone else's booking")

    replay = patient_client.post(f"/api/appointments/{booked['id']}/cancel")

    assert replay.status_code == 409, replay.text
    slot = db_session.get(AppointmentSlot, slot_id)
    db_session.refresh(slot)
    assert slot.status == "booked"
    assert db_session.get(Appointment, rebooked["id"]).status == "confirmed"


def test_reschedule_and_cancel_denied_for_non_owner(patient_client, db_session):
    dept_id = _cardiology_id(db_session)
    other = db_session.query(User).filter_by(email="other-patient@example.com").first()
    if other is None:
        other = User(
            email="other-patient@example.com",
            password_hash=hash_password("s3cret-pw-123"),
            role="patient",
            full_name="Other Patient",
        )
        db_session.add(other)
        db_session.commit()

    today = date.today()
    slots = get_available_slots(db_session, dept_id, today, today + timedelta(days=_SLOT_WINDOW_DAYS), limit=10)
    other_free_slot = slots[-1]["slot_id"]
    booked = book_appointment(db_session, other.id, other_free_slot, "checkup")

    resched_resp = patient_client.post(
        f"/api/appointments/{booked['id']}/reschedule", json={"new_slot_id": other_free_slot}
    )
    assert resched_resp.status_code == 403

    cancel_resp = patient_client.post(f"/api/appointments/{booked['id']}/cancel")
    assert cancel_resp.status_code == 403
