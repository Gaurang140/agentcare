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
import os
import tempfile
from collections.abc import Generator
from pathlib import Path

# Must be set before `from app.main import app` below: app.main's lifespan
# calls app.scheduler.start_scheduler(), which no-ops under TESTING so
# pytest never spins up a real BackgroundScheduler (every `client` fixture
# enters/exits the lifespan once per test).
os.environ.setdefault("TESTING", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.agents.llm import set_llm_client_for_tests
from app.auth.security import hash_password
from app.config import settings
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
    """Point the app's get_db dependency at the tmp test db for every test -
    and also app.db.session.SessionLocal itself, since Task 12's background
    task and scheduler jobs open their own session directly through that
    module attribute (looked up at call time, not bound at import time)
    rather than through the get_db dependency. Without this, that code path
    would silently write to the real dev db file instead of the test db.
    """
    import app.db.session as db_session_module

    def _get_db_override() -> Generator[Session, None, None]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db_override
    original_session_local = db_session_module.SessionLocal
    db_session_module.SessionLocal = session_factory

    yield

    db_session_module.SessionLocal = original_session_local
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


def _ensure_staff_user(db_session: Session) -> None:
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


@pytest.fixture()
def staff_client(client: TestClient, db_session: Session) -> TestClient:
    """A logged-in staff user, created directly in the db (no self-signup)."""
    _ensure_staff_user(db_session)

    login_resp = client.post(
        "/api/auth/login",
        json={"email": "staff@example.com", "password": _TEST_PASSWORD},
    )
    assert login_resp.status_code == 200, login_resp.text
    return client


@pytest.fixture()
def independent_staff_client(db_session: Session) -> Generator[TestClient, None, None]:
    """A second, independent logged-in-as-staff TestClient, for tests that
    need a patient session and a staff session open at the same time.

    `patient_client` and `staff_client` both wrap the same function-scoped
    `client` fixture, so requesting both in one test resolves to the exact
    same TestClient object (fixture caching within one test call) - whoever
    logs in last silently wins the shared cookie jar, and the other
    "session" is actually that same login. This fixture opens its own
    TestClient instead, so both roles are genuinely independent.
    """
    _ensure_staff_user(db_session)

    with TestClient(app) as staff_tc:
        login_resp = staff_tc.post(
            "/api/auth/login",
            json={"email": "staff@example.com", "password": _TEST_PASSWORD},
        )
        assert login_resp.status_code == 200, login_resp.text
        yield staff_tc


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


# --- Fake Model Armor client, shared by the adapter and the guard tests ------
# Same reasoning as FakeLLMClient above: the adapter test, the injection-guard
# test and the safety-agent test all need one, so it lives here once. The
# responses are built from the real `modelarmor_v1` message types, so the
# adapter's parsing is exercised against the shapes the SDK actually returns.


def _armor_sanitization_result(matched: dict[str, bool]):
    """A SanitizationResult where each named filter reports MATCH_FOUND or
    NO_MATCH_FOUND. AgentCare's template turns on `pi_and_jailbreak` and
    `malicious_uris`; SDP stays off because Presidio owns PII locally."""
    from google.cloud import modelarmor_v1

    states = {
        True: modelarmor_v1.FilterMatchState.MATCH_FOUND,
        False: modelarmor_v1.FilterMatchState.NO_MATCH_FOUND,
    }
    filter_results = {}
    for name, hit in matched.items():
        if name == "malicious_uris":
            filter_results[name] = modelarmor_v1.FilterResult(
                malicious_uri_filter_result=modelarmor_v1.MaliciousUriFilterResult(
                    match_state=states[hit]
                )
            )
        else:
            filter_results[name] = modelarmor_v1.FilterResult(
                pi_and_jailbreak_filter_result=modelarmor_v1.PiAndJailbreakFilterResult(
                    match_state=states[hit],
                    confidence_level=modelarmor_v1.DetectionConfidenceLevel.HIGH,
                )
            )

    return modelarmor_v1.SanitizationResult(
        filter_match_state=states[any(matched.values())],
        filter_results=filter_results,
    )


def _armor_outcome(outcome, *, is_prompt: bool):
    """A scripted outcome: a {filter: matched} dict becomes the matching
    response message, an Exception is left alone to be raised, and anything
    else is returned as given (so a test can hand back a payload the adapter
    cannot read)."""
    from google.cloud import modelarmor_v1

    if not isinstance(outcome, dict):
        return outcome
    result = _armor_sanitization_result(outcome)
    if is_prompt:
        return modelarmor_v1.SanitizeUserPromptResponse(sanitization_result=result)
    return modelarmor_v1.SanitizeModelResponseResponse(sanitization_result=result)


class FakeArmorClient:
    """A ModelArmorClient-shaped fake: records every call, replays one
    scripted outcome per method, raises a scripted Exception."""

    def __init__(self, prompt_outcome=None, response_outcome=None) -> None:
        self._prompt_outcome = prompt_outcome
        self._response_outcome = response_outcome
        self.prompt_calls: list[dict] = []
        self.response_calls: list[dict] = []

    @property
    def texts_screened(self) -> list[str]:
        """The prompt text of every screen call, in order."""
        return [call["request"].user_prompt_data.text for call in self.prompt_calls]

    def _replay(self, outcome, operation: str):
        if outcome is None:
            raise AssertionError(f"fake model armor client called without a scripted {operation}")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def sanitize_user_prompt(self, request=None, timeout=None):
        self.prompt_calls.append({"request": request, "timeout": timeout})
        return self._replay(self._prompt_outcome, "sanitize_user_prompt")

    def sanitize_model_response(self, request=None, timeout=None):
        self.response_calls.append({"request": request, "timeout": timeout})
        return self._replay(self._response_outcome, "sanitize_model_response")


@pytest.fixture()
def fake_model_armor() -> Generator:
    """Factory fixture: `fake_model_armor(prompt={"pi_and_jailbreak": True})`
    builds a FakeArmorClient, injects it via set_model_armor_client_for_tests
    and returns it so the test can assert on the calls it recorded. Cleared
    after the test regardless of outcome.

    `prompt` and `response` each take a {filter_name: matched} dict, an
    Exception to raise, or any other object to return as-is. Left at None,
    that method asserts if it is called at all.
    """
    from app.safety.model_armor import set_model_armor_client_for_tests

    def _make(prompt=None, response=None) -> FakeArmorClient:
        client = FakeArmorClient(
            _armor_outcome(prompt, is_prompt=True),
            _armor_outcome(response, is_prompt=False),
        )
        set_model_armor_client_for_tests(client)
        return client

    yield _make
    set_model_armor_client_for_tests(None)


@pytest.fixture()
def model_armor_on(monkeypatch):
    """Enable the Model Armor layer for one test."""
    monkeypatch.setattr(
        settings, "model_armor_template", "projects/demo/locations/europe-west3/templates/agentcare"
    )


@pytest.fixture()
def redaction_language(monkeypatch):
    """Factory: swap one agent module's `redact_for_llm` for a stub that
    records the `language` it was called with, and hand back the list those
    calls append to.

    What these tests assert is the argument the node passes, not what the
    redactor then does with it, so the stub returns the text unchanged and no
    counts - and the Presidio engines stay out of it entirely.
    """

    def _capture(module) -> list[str | None]:
        seen: list[str | None] = []

        def _fake(text: str, language: str | None = None) -> tuple[str, dict[str, int]]:
            seen.append(language)
            return text, {}

        monkeypatch.setattr(module, "redact_for_llm", _fake)
        return seen

    return _capture
