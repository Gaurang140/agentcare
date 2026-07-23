"""TDD for app.agents.llm.chat_json: the only LLM entry point in the app.

All tests inject a fake OpenAI-shaped client via set_llm_client_for_tests so
nothing here touches the network. tenacity's sleep is patched to a no-op so
the exponential-backoff paths (which really do call time.sleep(1..8)) run
instantly.
"""

from __future__ import annotations

import json

import httpx
import openai
import pytest
from pydantic import BaseModel

from app.agents.llm import LLMOutputError, chat_json, set_llm_client_for_tests


class _Verdict(BaseModel):
    ok: bool
    reason: str


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]


def _ok(payload: dict) -> _FakeResponse:
    return _FakeResponse(json.dumps(payload))


class _FakeCompletions:
    """Replays a scripted list of responses/exceptions, one per call."""

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._script:
            raise AssertionError("fake client called more times than scripted")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeChat:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.completions = completions


class FakeClient:
    def __init__(self, script: list) -> None:
        self.chat = _FakeChat(_FakeCompletions(script))


def _bad_request_error(message: str = "response_format not supported") -> openai.BadRequestError:
    request = httpx.Request("POST", "https://fake.test/v1/chat/completions")
    response = httpx.Response(400, request=request, json={"error": {"message": message}})
    return openai.BadRequestError(message, response=response, body={"error": {"message": message}})


def _connection_error() -> openai.APIConnectionError:
    request = httpx.Request("POST", "https://fake.test/v1/chat/completions")
    return openai.APIConnectionError(request=request)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """The retry paths under test really do call tenacity's backoff sleep;
    keep the suite fast without changing production wait times."""
    monkeypatch.setattr("time.sleep", lambda _seconds: None)


@pytest.fixture(autouse=True)
def _reset_override():
    set_llm_client_for_tests(None)
    yield
    set_llm_client_for_tests(None)


def test_chat_json_strict_schema_happy_path():
    client = FakeClient([_ok({"ok": True, "reason": "fine"})])
    set_llm_client_for_tests(client)

    result = chat_json("system prompt", "user prompt", _Verdict)

    assert result == _Verdict(ok=True, reason="fine")
    assert len(client.chat.completions.calls) == 1
    call = client.chat.completions.calls[0]
    assert call["response_format"]["type"] == "json_schema"
    assert call["response_format"]["json_schema"]["name"] == "_Verdict"
    assert call["response_format"]["json_schema"]["strict"] is True
    assert call["response_format"]["json_schema"]["schema"]["additionalProperties"] is False


def test_chat_json_falls_back_to_json_object_on_bad_request():
    client = FakeClient(
        [
            _bad_request_error(),
            _ok({"ok": False, "reason": "fallback worked"}),
        ]
    )
    set_llm_client_for_tests(client)

    result = chat_json("system prompt", "user prompt", _Verdict)

    assert result == _Verdict(ok=False, reason="fallback worked")
    assert len(client.chat.completions.calls) == 2
    first_call, second_call = client.chat.completions.calls
    assert first_call["response_format"]["type"] == "json_schema"
    assert second_call["response_format"] == {"type": "json_object"}
    # schema instructions must be embedded in the prompt for json_object mode
    system_content = second_call["messages"][0]["content"]
    assert "schema" in system_content.lower()
    assert "_Verdict" in system_content or "ok" in system_content


def test_chat_json_reprompts_once_on_validation_error_then_succeeds():
    client = FakeClient(
        [
            _ok({"ok": "not-a-boolean"}),  # invalid: bad type, missing "reason"
            _ok({"ok": True, "reason": "corrected"}),
        ]
    )
    set_llm_client_for_tests(client)

    result = chat_json("system prompt", "user prompt", _Verdict)

    assert result == _Verdict(ok=True, reason="corrected")
    assert len(client.chat.completions.calls) == 2
    retry_messages = client.chat.completions.calls[1]["messages"]
    assert any("valid" in m["content"].lower() for m in retry_messages)


def test_chat_json_raises_llm_output_error_when_validation_never_succeeds():
    client = FakeClient(
        [
            _ok({"ok": "nope"}),
            _ok({"ok": "still-nope"}),
        ]
    )
    set_llm_client_for_tests(client)

    with pytest.raises(LLMOutputError):
        chat_json("system prompt", "user prompt", _Verdict)

    assert len(client.chat.completions.calls) == 2


def test_chat_json_uses_fallback_endpoint_when_primary_exhausted():
    primary = FakeClient([_connection_error(), _connection_error(), _connection_error()])
    fallback = FakeClient([_ok({"ok": True, "reason": "via fallback"})])
    set_llm_client_for_tests(primary, fallback)

    result = chat_json("system prompt", "user prompt", _Verdict, max_retries=3)

    assert result == _Verdict(ok=True, reason="via fallback")
    assert len(primary.chat.completions.calls) == 3
    assert len(fallback.chat.completions.calls) == 1
    assert fallback.chat.completions.calls[0]["response_format"] == {"type": "json_object"}


def test_chat_json_raises_when_primary_exhausted_and_no_fallback_configured():
    primary = FakeClient([_connection_error(), _connection_error(), _connection_error()])
    set_llm_client_for_tests(primary)

    with pytest.raises(openai.APIConnectionError):
        chat_json("system prompt", "user prompt", _Verdict, max_retries=3)

    assert len(primary.chat.completions.calls) == 3
