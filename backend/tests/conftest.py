"""Shared fixtures: a session-scoped tmp sqlite file db wired into the app,
plus client factories for a plain patient, a staff member, and a second
patient's document (used by the ownership RBAC test).

Also provides `db` and `seeded`: a fresh, function-scoped in-memory sqlite
session for the tools-layer tests, isolated from the HTTP-level fixtures
above and from each other (same reasoning as tests/test_seed.py's own
throwaway engine - tools tests want predictable ids, e.g. patient_id=1 for
the first seeded patient, undisturbed by other test files' rows).
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.agents.llm import set_llm_client_for_tests
from app.auth.security import hash_password
from app.db.base import Base
from app.db.seed import seed
from app.db.session import get_db
from app.main import app
from app.models import PatientDocument, User

_TEST_PASSWORD = "s3cret-pw-123"  # noqa: S105 - fixture-only test credential


@pytest.fixture(scope="session")
def engine():
    """One sqlite file for the whole test session, torn down at the end."""
    tmp_dir = tempfile.mkdtemp(prefix="agentcare-test-")
    db_path = Path(tmp_dir) / "test.db"
    test_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(test_engine)
    yield test_engine
    test_engine.dispose()


@pytest.fixture(scope="session")
def session_factory(engine):
    return sessionmaker(bind=engine)


@pytest.fixture(scope="session", autouse=True)
def _override_get_db(session_factory):
    """Point the app's get_db dependency at the tmp test db for every test."""

    def _get_db_override() -> Generator[Session, None, None]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db_override
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture()
def db_session(session_factory) -> Generator[Session, None, None]:
    """A raw session for fixtures that need to seed rows directly."""
    db = session_factory()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as test_client:
        yield test_client


def _register(test_client: TestClient, *, email: str, name: str) -> None:
    # Ignore the response: across tests sharing the session-scoped db, a
    # second registration with the same email 409s on purpose, and the
    # ensuing login still succeeds against the already-created account.
    test_client.post(
        "/api/auth/register",
        json={
            "name": name,
            "email": email,
            "password": _TEST_PASSWORD,
            "dob": "1990-01-01",
            "phone": "+49 170 0000000",
            "preferred_language": "en",
            "emergency_contact": "Jane Doe",
        },
    )


@pytest.fixture()
def patient_client(client: TestClient) -> TestClient:
    """A logged-in patient (self-registered through the real endpoint)."""
    _register(client, email="patient@example.com", name="Pat Patient")
    login_resp = client.post(
        "/api/auth/login",
        json={"email": "patient@example.com", "password": _TEST_PASSWORD},
    )
    assert login_resp.status_code == 200, login_resp.text
    return client


@pytest.fixture()
def staff_client(client: TestClient, db_session: Session) -> TestClient:
    """A logged-in staff user, created directly in the db (no self-signup)."""
    existing = db_session.query(User).filter_by(email="staff@example.com").first()
    if existing is None:
        staff = User(
            email="staff@example.com",
            password_hash=hash_password(_TEST_PASSWORD),
            role="staff",
            full_name="Sam Staff",
        )
        db_session.add(staff)
        db_session.commit()

    login_resp = client.post(
        "/api/auth/login",
        json={"email": "staff@example.com", "password": _TEST_PASSWORD},
    )
    assert login_resp.status_code == 200, login_resp.text
    return client


@pytest.fixture()
def other_patient_doc(db_session: Session) -> PatientDocument:
    """A second patient plus one document, owned by that second patient."""
    other = db_session.query(User).filter_by(email="other-patient@example.com").first()
    if other is None:
        other = User(
            email="other-patient@example.com",
            password_hash=hash_password(_TEST_PASSWORD),
            role="patient",
            full_name="Other Patient",
        )
        db_session.add(other)
        db_session.flush()

    doc = PatientDocument(
        patient_id=other.id,
        filename="insurance-card.pdf",
        document_type="insurance",
        checksum="0" * 64,
        storage_ref=f"local://other-patient/insurance-card-{other.id}.pdf",
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)
    return doc


@pytest.fixture()
def db() -> Generator[Session, None, None]:
    """A throwaway in-memory sqlite session, fresh per test (tools layer)."""
    test_engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(test_engine)
    session = Session(test_engine)
    try:
        yield session
    finally:
        session.close()
        test_engine.dispose()


@pytest.fixture()
def seeded(db: Session) -> dict[str, int]:
    """Populate `db` with the standard synthetic demo data (app.db.seed.seed).

    In a fresh db this makes patient_id=1 the first seeded patient (Max
    Mustermann), patient_id=2 the second (Erika Musterfrau), and Cardiology
    the first department (id=1), matching the brief's test pseudocode.
    """
    return seed(db)


# --- Fake LLM client, shared by every agents/*.run() test -------------------
# Same shape as tests/test_llm.py's FakeClient (a scripted openai-shaped
# chat.completions.create), reused here so the six agent-node test files
# don't each redefine it. Lives in conftest rather than test_llm.py because
# it's infrastructure the agent tests depend on, not part of what test_llm.py
# itself is testing.


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    """Replays a scripted list of responses/exceptions, one per call."""

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._script:
            raise AssertionError("fake LLM client called more times than scripted")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeChat:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.completions = completions


class FakeLLMClient:
    """An openai.OpenAI-shaped client that replays a scripted response list."""

    def __init__(self, script: list) -> None:
        self.chat = _FakeChat(_FakeCompletions(script))


@pytest.fixture()
def fake_llm() -> Generator:
    """Factory fixture: fake_llm([{...}, ValueError(...), {...}]) builds a
    FakeLLMClient, injects it via set_llm_client_for_tests, and returns it so
    the test can assert on `.chat.completions.calls`. Cleared after the test
    regardless of outcome.
    """

    def _make(script_items: list) -> FakeLLMClient:
        script = [
            item if isinstance(item, Exception) else _FakeResponse(json.dumps(item))
            for item in script_items
        ]
        client = FakeLLMClient(script)
        set_llm_client_for_tests(client)
        return client

    yield _make
    set_llm_client_for_tests(None)
