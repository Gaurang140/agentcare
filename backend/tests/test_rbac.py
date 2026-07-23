"""RBAC enforcement tests, verbatim from the Task 4 brief."""


def test_staff_route_denied_for_patient(patient_client):
    r = patient_client.get("/api/staff/escalations")
    assert r.status_code == 403


def test_patient_cannot_read_other_patients_documents(patient_client, other_patient_doc):
    r = patient_client.get(f"/api/documents/{other_patient_doc.id}")
    assert r.status_code == 403


def test_me_requires_auth():
    from app.main import app
    from fastapi.testclient import TestClient

    assert TestClient(app).get("/api/auth/me").status_code == 403
