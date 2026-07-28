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
from app.tools.appointment_tools import (
    book_appointment,
    cancel_appointment,
    get_available_slots,
)

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


def test_patient_slot_endpoint_filters_callers_existing_times(patient_client, db_session):
    dept_id = _cardiology_id(db_session)
    user = db_session.query(User).filter_by(email="patient@example.com").first()
    today = date.today()
    available = get_available_slots(
        db_session,
        dept_id,
        today,
        today + timedelta(days=_SLOT_WINDOW_DAYS),
        limit=100,
        patient_id=user.id,
    )
    slots_by_start: dict[str, list[dict]] = {}
    for slot in available:
        slots_by_start.setdefault(slot["start_time"], []).append(slot)
    same_time_slots = next(slots for slots in slots_by_start.values() if len(slots) >= 2)
    booked_slot, conflicting_slot = same_time_slots[:2]
    booking = book_appointment(db_session, user.id, booked_slot["slot_id"], "checkup")
    try:
        resp = patient_client.get(f"/api/departments/{dept_id}/slots?limit=100")
    finally:
        cancel_appointment(db_session, booking["id"])

    assert resp.status_code == 200
    returned_ids = {slot["slot_id"] for slot in resp.json()}
    assert conflicting_slot["slot_id"] not in returned_ids


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


# --- Patient profile: view and update ----------------------------------------
# Profile creation happens at registration; these routes cover later updates.
# The audit row names which fields changed but never carries the values (phone
# numbers and contacts are PII; the audit trail stores categories, not data).


def test_profile_requires_auth():
    assert TestClient(app).get("/api/profile").status_code == 403


def test_get_profile_returns_current_patient_fields(patient_client):
    resp = patient_client.get("/api/profile")
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "patient@example.com"
    assert body["phone"] == "+49 170 0000000"
    assert body["preferred_language"] == "en"
    assert body["emergency_contact"] == "Jane Doe"


def test_patch_profile_updates_fields_and_persists(patient_client, db_session):
    resp = patient_client.patch(
        "/api/profile",
        json={"phone": "+49 171 9999999", "preferred_language": "de"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["phone"] == "+49 171 9999999"
    assert resp.json()["preferred_language"] == "de"

    from app.models import PatientProfile

    user = db_session.query(User).filter_by(email="patient@example.com").first()
    profile = db_session.query(PatientProfile).filter_by(user_id=user.id).first()
    db_session.refresh(profile)
    assert profile.phone == "+49 171 9999999"
    assert profile.preferred_language == "de"


def test_patch_profile_partial_update_leaves_other_fields(patient_client):
    before = patient_client.get("/api/profile").json()
    resp = patient_client.patch("/api/profile", json={"phone": "+49 172 1111111"})
    assert resp.status_code == 200
    after = resp.json()
    assert after["phone"] == "+49 172 1111111"
    assert after["preferred_language"] == before["preferred_language"]
    assert after["emergency_contact"] == before["emergency_contact"]


def test_patch_profile_rejects_unsupported_language(patient_client):
    resp = patient_client.patch("/api/profile", json={"preferred_language": "fr"})
    assert resp.status_code == 422


def test_patch_profile_audit_names_fields_but_not_values(patient_client, db_session):
    resp = patient_client.patch("/api/profile", json={"phone": "+49 173 2222222"})
    assert resp.status_code == 200

    from app.models import AuditEvent

    row = (
        db_session.query(AuditEvent)
        .filter_by(action="patient.profile_updated")
        .order_by(AuditEvent.id.desc())
        .first()
    )
    assert row is not None
    assert row.metadata_json == {"updated_fields": ["phone"]}
    assert "+49 173 2222222" not in str(row.metadata_json)


def test_staff_account_has_no_patient_profile(staff_client):
    resp = staff_client.get("/api/profile")
    assert resp.status_code == 404
