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
from google.genai.errors import ClientError, ServerError
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langchain_google_genai.chat_models import ChatGoogleGenerativeAIError
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


def _google_client_error(code: int) -> ClientError:
    return ClientError(
        code,
        {
            "error": {
                "code": code,
                "message": f"Google API returned {code}",
                "status": "RESOURCE_EXHAUSTED" if code == 429 else "CLIENT_ERROR",
            }
        },
    )


def _wrapped_google_client_error(code: int) -> ChatGoogleGenerativeAIError:
    provider_error = _google_client_error(code)
    wrapper = ChatGoogleGenerativeAIError(f"wrapped Google error {code}")
    wrapper.__cause__ = provider_error
    return wrapper


class _ScriptedStructuredModel:
    """Provider-neutral structured-model fake for transport policy tests."""

    def __init__(self, outcomes: list[BaseException | _Verdict]):
        self._outcomes = list(outcomes)
        self.calls = 0

    def with_structured_output(self, *_args, **_kwargs):
        def invoke(_messages):
            self.calls += 1
            outcome = self._outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return {
                "raw": AIMessage(content=outcome.model_dump_json()),
                "parsed": outcome,
                "parsing_error": None,
            }

        return RunnableLambda(invoke)


def test_retry_adapter_forwards_runnable_config():
    from app.agents.llm import _with_retry
    from langchain_core.callbacks import BaseCallbackHandler

    seen_configs = []
    callback = BaseCallbackHandler()

    def capture_config(value, config):
        seen_configs.append(config)
        return value

    result = _with_retry(RunnableLambda(capture_config), max_retries=1).invoke(
        "ok",
        config={
            "tags": ["llm-transport"],
            "configurable": {"trace_id": "test-trace"},
            "callbacks": [callback],
        },
    )

    assert result == "ok"
    assert seen_configs[0]["tags"] == ["llm-transport"]
    assert seen_configs[0]["configurable"]["trace_id"] == "test-trace"
    assert callback in seen_configs[0]["callbacks"].handlers


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


@pytest.mark.parametrize(
    "message",
    [
        "response_format is not supported by this endpoint",
        "json_schema is unsupported by this endpoint",
    ],
)
def test_chat_json_falls_back_to_json_object_when_structured_format_is_unsupported(message):
    client = FakeClient(
        [
            _bad_request_error(message),
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


@pytest.mark.parametrize(
    "message",
    [
        "Invalid schema for response_format 'Verdict': required must include reason",
        "Invalid request: temperature must be between 0 and 2",
    ],
)
def test_chat_json_does_not_downgrade_unrelated_bad_requests(message):
    error = _bad_request_error(message)
    client = FakeClient([error])
    set_llm_client_for_tests(client)

    with pytest.raises(openai.BadRequestError) as excinfo:
        chat_json("system prompt", "user prompt", _Verdict)

    assert excinfo.value is error
    assert len(client.chat.completions.calls) == 1


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


@pytest.mark.parametrize(
    "errors",
    [
        [
            ServerError(503, {"error": {"message": "unavailable"}}),
            ServerError(503, {"error": {"message": "unavailable"}}),
        ],
        [
            httpx.ConnectError("connection failed"),
            httpx.ConnectError("connection failed"),
        ],
        [
            httpx.ReadTimeout("request timed out"),
            httpx.ReadTimeout("request timed out"),
        ],
        [
            _wrapped_google_client_error(429),
            _wrapped_google_client_error(429),
        ],
    ],
    ids=["google-5xx", "http-network", "http-timeout", "wrapped-google-429"],
)
def test_chat_json_retries_provider_neutral_transient_errors(monkeypatch, errors):
    import app.agents.llm as llm_module

    primary = _ScriptedStructuredModel(
        [*errors, _Verdict(ok=True, reason="retry succeeded")]
    )
    monkeypatch.setattr(llm_module, "_resolve_primary", lambda _profiles: primary)

    result = llm_module.chat_json(
        "system prompt",
        "user prompt",
        _Verdict,
        max_retries=3,
    )

    assert result == _Verdict(ok=True, reason="retry succeeded")
    assert primary.calls == 3


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_chat_json_does_not_retry_or_fallback_for_google_client_errors(
    monkeypatch,
    code,
):
    import app.agents.llm as llm_module

    error = _wrapped_google_client_error(code)
    primary = _ScriptedStructuredModel([error])
    fallback = _ScriptedStructuredModel([_Verdict(ok=True, reason="must not run")])
    monkeypatch.setattr(llm_module, "_resolve_primary", lambda _profiles: primary)
    monkeypatch.setattr(llm_module, "_resolve_fallback", lambda _profiles: fallback)

    with pytest.raises(ChatGoogleGenerativeAIError) as excinfo:
        llm_module.chat_json(
            "system prompt",
            "user prompt",
            _Verdict,
            max_retries=3,
        )

    assert excinfo.value is error
    assert primary.calls == 1
    assert fallback.calls == 0


def test_chat_json_uses_fallback_after_google_transient_attempts_exhaust(
    monkeypatch,
):
    import app.agents.llm as llm_module

    errors = [
        ServerError(503, {"error": {"message": "unavailable"}}),
        ServerError(503, {"error": {"message": "unavailable"}}),
        ServerError(503, {"error": {"message": "unavailable"}}),
    ]
    primary = _ScriptedStructuredModel(errors)
    fallback = _ScriptedStructuredModel([_Verdict(ok=True, reason="via fallback")])
    monkeypatch.setattr(llm_module, "_resolve_primary", lambda _profiles: primary)
    monkeypatch.setattr(llm_module, "_resolve_fallback", lambda _profiles: fallback)

    result = llm_module.chat_json(
        "system prompt",
        "user prompt",
        _Verdict,
        max_retries=3,
    )

    assert result == _Verdict(ok=True, reason="via fallback")
    assert primary.calls == 3
    assert fallback.calls == 1


def test_chat_json_preserves_original_google_error_without_fallback(monkeypatch):
    import app.agents.llm as llm_module

    errors = [_wrapped_google_client_error(429) for _attempt in range(3)]
    primary = _ScriptedStructuredModel(errors)
    monkeypatch.setattr(llm_module, "_resolve_primary", lambda _profiles: primary)
    monkeypatch.setattr(llm_module, "_resolve_fallback", lambda _profiles: None)

    with pytest.raises(ChatGoogleGenerativeAIError) as excinfo:
        llm_module.chat_json(
            "system prompt",
            "user prompt",
            _Verdict,
            max_retries=3,
        )

    assert excinfo.value is errors[-1]
    assert primary.calls == 3


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
        timeout=45,
    )

    result = llm_module._build_chat_model(profile)

    assert result is sentinel
    assert captured["args"] == ("gemini-2.5-flash",)
    assert captured["kwargs"] == {
        "model_provider": "google_genai",
        "vertexai": True,
        "timeout": 45,
        "max_retries": 1,
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
