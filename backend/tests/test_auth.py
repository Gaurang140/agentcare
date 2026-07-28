"""Register / login / me happy path, and the RBAC-adjacent negative cases
that make sure staff can actually reach the routes patients are denied."""

from app.db.session import get_db
from app.main import app
from app.models import AuditEvent


def test_registration_writes_audit_event(client, db_session):
    response = client.post(
        "/api/auth/register",
        json={
            "name": "Audit Patient",
            "email": "registration-audit@example.com",
            "password": "s3cret-pw-123",
            "dob": "1991-04-12",
            "phone": None,
            "preferred_language": "en",
            "emergency_contact": None,
        },
    )

    assert response.status_code == 201, response.text
    row = db_session.query(AuditEvent).filter_by(action="user.registered").one()
    assert row.actor_id == response.json()["id"]
    assert row.entity_type == "user"
    assert row.entity_id == response.json()["id"]
    assert row.metadata_json == {"role": "patient"}


def test_register_login_me_happy_path(client):
    register_resp = client.post(
        "/api/auth/register",
        json={
            "name": "Happy Path",
            "email": "happy-path@example.com",
            "password": "s3cret-pw-123",
            "dob": "1985-03-02",
            "phone": "+49 170 1111111",
            "preferred_language": "de",
            "emergency_contact": "Someone Else",
        },
    )
    assert register_resp.status_code == 201, register_resp.text
    body = register_resp.json()
    assert body["email"] == "happy-path@example.com"
    assert body["role"] == "patient"
    assert body["name"] == "Happy Path"

    login_resp = client.post(
        "/api/auth/login",
        json={"email": "happy-path@example.com", "password": "s3cret-pw-123"},
    )
    assert login_resp.status_code == 200, login_resp.text
    login_body = login_resp.json()
    assert login_body["id"] == body["id"]
    assert login_body["name"] == "Happy Path"
    assert login_body["role"] == "patient"
    assert "access_token" in login_resp.cookies

    me_resp = client.get("/api/auth/me")
    assert me_resp.status_code == 200, me_resp.text
    me_body = me_resp.json()
    assert me_body["id"] == body["id"]
    assert me_body["email"] == "happy-path@example.com"
    assert me_body["role"] == "patient"

    logout_resp = client.post("/api/auth/logout")
    assert logout_resp.status_code == 200
    assert "happy-path@example.com" not in logout_resp.text

    me_after_logout = client.get("/api/auth/me")
    assert me_after_logout.status_code == 403


def test_register_duplicate_email_conflicts(client):
    payload = {
        "name": "First",
        "email": "dupe@example.com",
        "password": "s3cret-pw-123",
        "dob": "1990-01-01",
        "phone": None,
        "preferred_language": "en",
        "emergency_contact": None,
    }
    first = client.post("/api/auth/register", json=payload)
    assert first.status_code == 201, first.text

    second = client.post(
        "/api/auth/register", json={**payload, "name": "Second"}
    )
    assert second.status_code == 409


def test_login_wrong_password_rejected(client):
    client.post(
        "/api/auth/register",
        json={
            "name": "Wrong Pw",
            "email": "wrong-pw@example.com",
            "password": "correct-pw-123",
            "dob": None,
            "phone": None,
            "preferred_language": "en",
            "emergency_contact": None,
        },
    )
    resp = client.post(
        "/api/auth/login",
        json={"email": "wrong-pw@example.com", "password": "not-the-password"},
    )
    assert resp.status_code == 403


def test_staff_can_reach_staff_route(staff_client):
    r = staff_client.get("/api/staff/escalations")
    assert r.status_code == 200
    assert r.json() == []


def test_staff_can_read_any_patients_document(staff_client, other_patient_doc):
    r = staff_client.get(f"/api/documents/{other_patient_doc.id}")
    assert r.status_code == 200
    assert r.json()["id"] == other_patient_doc.id


def test_health_endpoint(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {
        "status": "ok",
        "db": True,
        "database_dialect": "sqlite",
        "database_revision": "unmanaged",
        "release": "dev",
    }


def test_live_endpoint_does_not_require_database(client):
    original_dependency = app.dependency_overrides[get_db]

    def unavailable_database():
        raise RuntimeError("database unavailable")
        yield

    app.dependency_overrides[get_db] = unavailable_database
    try:
        response = client.get("/api/live")
    finally:
        app.dependency_overrides[get_db] = original_dependency

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
