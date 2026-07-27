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

from app.agents.followup import FollowupOutput
from app.agents.llm import LLMOutputError, chat_json, set_llm_client_for_tests

# The scripted openai-SDK-shaped fake lives in conftest.py, shared with the
# agent-node tests.
from conftest import FakeLLMClient as FakeClient
from conftest import _fake_completion


class _Verdict(BaseModel):
    ok: bool
    reason: str


def _ok(payload: dict):
    return _fake_completion(json.dumps(payload))


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
    assert call["response_format"] is _Verdict


def test_chat_json_sends_recursively_strict_schema():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "mock-completion",
                "object": "chat.completion",
                "created": 0,
                "model": "fake-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "reminders": [
                                        {
                                            "type": "appointment",
                                            "days_before_appointment": 1,
                                        }
                                    ],
                                    "followup_days_after": 14,
                                }
                            ),
                        },
                    }
                ],
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = openai.OpenAI(
        api_key="test-key",
        base_url="https://mock.test/v1",
        http_client=http_client,
    )
    set_llm_client_for_tests(client)

    result = chat_json("system", "user", FollowupOutput)

    assert result == FollowupOutput(
        reminders=[{"type": "appointment", "days_before_appointment": 1}],
        followup_days_after=14,
    )
    response_format = captured["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "FollowupOutput"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["ReminderSpec"]["additionalProperties"] is False


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
    assert first_call["response_format"] is _Verdict
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


def test_chat_json_repairs_validation_after_json_schema_rejection():
    client = FakeClient(
        [
            _bad_request_error(),
            _ok({"ok": "not-a-boolean"}),
            _ok({"ok": True, "reason": "corrected in json mode"}),
        ]
    )
    set_llm_client_for_tests(client)

    result = chat_json("system prompt", "user prompt", _Verdict)

    assert result == _Verdict(ok=True, reason="corrected in json mode")
    assert len(client.chat.completions.calls) == 3
    first_call, second_call, third_call = client.chat.completions.calls
    assert first_call["response_format"] is _Verdict
    assert second_call["response_format"] == {"type": "json_object"}
    assert third_call["response_format"] == {"type": "json_object"}
    assert any("valid" in message["content"].lower() for message in third_call["messages"])


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


def test_build_chat_model_configures_openai_compatible_endpoint():
    """The langchain factory must carry the profile's endpoint, timeout and
    zero SDK-internal retries (the retry policy lives in with_retry, so the
    openai SDK's own 2-attempt default must not multiply it)."""
    from app.agents.llm import _build_chat_model
    from app.agents.model_config import ModelProfile

    profile = ModelProfile(
        provider="openai",
        model="openai/gpt-oss-120b",
        base_url="https://api.groq.com/openai/v1",
        timeout=30,
        max_retries=3,
    )

    model = _build_chat_model(profile)

    assert model.model_name == "openai/gpt-oss-120b"
    assert model.openai_api_base == "https://api.groq.com/openai/v1"
    assert model.request_timeout == 30
    assert model.max_retries == 0


def test_build_chat_model_passes_vertex_params_to_langchain_factory(monkeypatch):
    import app.agents.llm as llm_module
    from app.agents.model_config import ModelProfile

    sentinel = object()
    captured: dict = {}

    def fake_init_chat_model(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return sentinel

    monkeypatch.setattr(llm_module, "init_chat_model", fake_init_chat_model)
    profile = ModelProfile(
        provider="google_genai",
        model="gemini-2.5-flash",
        params={"vertexai": True},
    )

    result = llm_module._build_chat_model(profile)

    assert result is sentinel
    assert captured["args"] == ("gemini-2.5-flash",)
    assert captured["kwargs"] == {
        "model_provider": "google_genai",
        "vertexai": True,
    }


def test_build_chat_model_missing_provider_package_raises_helpful_error():
    """A profile naming a provider whose langchain package is not installed
    must fail with the pip command in the message, not a bare ImportError."""
    from app.agents.llm import LLMConfigError, _build_chat_model
    from app.agents.model_config import ModelProfile

    profile = ModelProfile(provider="nonexistent_provider_xyz", model="some-model")

    with pytest.raises(LLMConfigError) as excinfo:
        _build_chat_model(profile)

    assert "pip install" in str(excinfo.value)
