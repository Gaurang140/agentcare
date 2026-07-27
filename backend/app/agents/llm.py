"""The two LLM entry points for the whole codebase, on the langchain
chat-model layer.

`invoke_structured` is where every agent node gets its structured,
Pydantic-validated output. `classify_injection` is the one other model call:
a single plain completion for the optional classifier layer of the
prompt-injection guard (`safety/injection_guard.py`), since a prompt-guard
model returns a bare label, not schema-shaped JSON. Nothing else builds a
chat model.

Models come from `langchain.chat_models.init_chat_model`, configured by the
profiles in `backend/llm.yaml` (see `agents/model_config.py`; env vars win
over the file). The verified integrations are OpenAI-compatible endpoints
through `langchain-openai` and Gemini on Vertex AI through
`langchain-google-genai`. Adding another LangChain provider also requires
compatibility tests for its structured-output and transport-error behavior.

Call sequence for one `invoke_structured(...)`:

1. Resolve the active profile (or the test override injected by
   `set_llm_client_for_tests`) and build the chat model.
2. Ask LangChain for strict structured output (`response_format:
   json_schema`, `strict: true`) - Groq's gpt-oss models support this.
3. If the endpoint rejects that request format (`openai.BadRequestError`,
   e.g. a local LM Studio server), retry the same model with
   `{"type": "json_object"}` and the schema spelled out in the prompt
   instead.
4. Accept LangChain's validated Pydantic instance (or validate a provider
   dict). On a validation failure, re-prompt once with the error appended;
   if that still fails, raise `LLMOutputError`.
5. Transport failures and 5xx/429s are retried with exponential backoff
   (1-8s) via langchain's `with_retry`, within a single logical request.
   SDK retry loops are disabled with zero OpenAI retries or one total Google
   attempt, so the two policies never multiply. Each attempt is bounded by
   the profile's `timeout`.
6. If the primary's retries are exhausted and a fallback is configured
   (`LLM_FALLBACK_BASE_URL`, or `fallback_profile` in llm.yaml), repeat
   the whole thing once against the fallback in `json_object` mode.
"""

from __future__ import annotations

import json
from typing import TypeVar

import httpx
import openai
from google.genai.errors import ClientError as GoogleClientError
from google.genai.errors import ServerError as GoogleServerError
from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable, RunnableLambda
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError

from app.agents.model_config import ModelProfile, load_llm_profiles
from app.config import settings
from app.logging_setup import get_logger

logger = get_logger(__name__)

SchemaT = TypeVar("SchemaT", bound=BaseModel)

# Direct errors worth retrying with backoff: dropped connections, timeouts,
# 5xx and 429 rate limits. Google client errors are wrapped by its LangChain
# integration, so wrapped Google 429s are classified separately below.
_DIRECT_RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.InternalServerError,
    openai.RateLimitError,
    GoogleServerError,
    httpx.TransportError,
)

_override = None
_override_fallback = None


class LLMOutputError(Exception):
    """Raised when the model never returns output that validates against
    the requested schema, even after the one allowed re-prompt."""


class LLMConfigError(Exception):
    """Raised when a profile names a provider whose langchain integration
    package is not installed."""


class _RetryableProviderError(Exception):
    """Private marker used only to drive LangChain's type-based retry API."""

    def __init__(self, original: Exception):
        super().__init__(str(original))
        self.original = original


def set_llm_client_for_tests(client, fallback=None) -> None:
    """Inject fake openai-shaped client(s) so tests never touch the network.

    The fakes are wrapped in a langchain ChatOpenAI, so they must answer the
    real SDK surface (`chat.completions.with_raw_response.create/.parse`
    returning real ChatCompletion objects) - see tests/conftest.py.

    Pass `None` (the default) to clear the override and go back to building
    real models from the configured profile. `fallback`, if given, is used
    in place of a real fallback whenever the primary's retries exhaust.
    """
    global _override, _override_fallback
    _override = client
    _override_fallback = fallback


def _wrap_test_client(fake, model_name: str) -> BaseChatModel:
    """A ChatOpenAI running entirely against an injected fake client."""
    return ChatOpenAI(
        model=model_name,
        api_key="test-override",
        client=fake.chat.completions,
        root_client=fake,
        max_retries=0,
    )


def _build_chat_model(profile: ModelProfile, *, model_name: str | None = None) -> BaseChatModel:
    """Build the chat model for a profile through init_chat_model.

    SDK-internal retries stay off: OpenAI-compatible clients use zero retries,
    while Google's SDK uses one total attempt because zero restores its
    default retry policy. The application retry policy lives in `_with_retry`,
    so the two policies never multiply. The "missing-key" placeholder keeps a
    keyless boot possible - openai 2.x refuses to construct a client with no
    credentials, and the endpoint answers 401 at call time either way."""
    name = model_name or profile.model
    kwargs: dict = dict(profile.params)
    if profile.provider == "openai":
        kwargs["base_url"] = profile.base_url
        kwargs["api_key"] = settings.llm_api_key or "missing-key"
        kwargs["timeout"] = profile.timeout
        kwargs["max_retries"] = 0
    elif profile.provider == "google_genai":
        kwargs["timeout"] = profile.timeout
        kwargs["max_retries"] = 1
    try:
        return init_chat_model(name, model_provider=profile.provider, **kwargs)
    except (ImportError, ValueError) as exc:
        package = f"langchain-{profile.provider.replace('_', '-')}"
        raise LLMConfigError(
            f"model provider '{profile.provider}' is not available; if it is a "
            f"real langchain provider, run `pip install {package}` "
            f"(see backend/llm.yaml)"
        ) from exc


def _resolve_primary(profiles) -> BaseChatModel:
    if _override is not None:
        return _wrap_test_client(_override, profiles.primary.model or settings.llm_model)
    return _build_chat_model(profiles.primary)


def _resolve_fallback(profiles) -> BaseChatModel | None:
    # When a test override is active, only ever use the fallback the test
    # explicitly injected - never fall through to a real settings-built
    # model, so tests stay network-free even if .env sets a real fallback.
    if _override is not None:
        if _override_fallback is None:
            return None
        return _wrap_test_client(_override_fallback, settings.llm_fallback_model)
    if profiles.fallback is None:
        return None
    api_key = settings.llm_fallback_api_key if settings.llm_fallback_base_url else None
    fallback = profiles.fallback
    if api_key is not None:
        model = ChatOpenAI(
            model=fallback.model,
            base_url=fallback.base_url,
            api_key=api_key,
            timeout=fallback.timeout,
            max_retries=0,
        )
        return model
    return _build_chat_model(fallback)


def _schema_in_prompt(system: str, schema_model: type[BaseModel]) -> str:
    """json_object mode carries the schema in the system prompt instead of
    the request format."""
    schema_json = json.dumps(schema_model.model_json_schema())
    return (
        f"{system}\n\n"
        "Respond with a single JSON object only, no prose, no markdown "
        f"fences, matching this JSON schema exactly:\n{schema_json}"
    )


def _is_retryable_provider_error(exc: Exception) -> bool:
    if isinstance(exc, _DIRECT_RETRYABLE_EXCEPTIONS):
        return True

    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(current, GoogleClientError) and current.code == 429:
            return True
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return False


def _is_unsupported_structured_format_error(exc: openai.BadRequestError) -> bool:
    error = exc.body.get("error", exc.body) if isinstance(exc.body, dict) else {}
    code = str(error.get("code", "")).lower() if isinstance(error, dict) else ""
    param = str(error.get("param", "")).lower() if isinstance(error, dict) else ""
    message = (
        str(error.get("message", exc)).lower()
        if isinstance(error, dict)
        else str(exc).lower()
    )
    normalized = " ".join(
        message.translate(str.maketrans("", "", "'\"`")).split()
    )
    rejects_schema_detail = (
        code in {"invalid_json_schema", "invalid_schema"}
        or "invalid schema" in normalized
        or "schema keyword" in normalized
        or (
            "keyword" in normalized
            and ("json_schema" in normalized or "json schema" in normalized)
        )
    )
    if rejects_schema_detail:
        return False

    format_names = ("response_format", "response format", "json_schema", "json schema")
    capability_rejected = any(
        phrase in normalized
        for name in format_names
        for phrase in (
            f"{name} is not supported",
            f"{name} not supported",
            f"{name} is unsupported",
            f"{name} unsupported",
            f"does not support {name}",
            f"does not support the {name}",
            f"doesnt support {name}",
            f"doesnt support the {name}",
        )
    )
    if capability_rejected:
        return True

    if code not in {"invalid_value", "unsupported_value"} or param != "response_format":
        return False
    invalid_value_at = normalized.find("invalid value")
    supported_values_at = normalized.find("supported values")
    if invalid_value_at < 0 or supported_values_at <= invalid_value_at:
        return False
    rejected_value = normalized[invalid_value_at:supported_values_at]
    allowed_values = normalized[supported_values_at + len("supported values") :]
    return "json_schema" in rejected_value and "json_schema" not in allowed_values


def _with_retry(runnable: Runnable, max_retries: int) -> Runnable:
    """Use at most `max_retries` total LangChain attempts with 1-8s backoff."""

    def invoke_with_retry_marker(runnable_input):
        try:
            return runnable.invoke(runnable_input)
        except Exception as exc:
            if _is_retryable_provider_error(exc):
                raise _RetryableProviderError(exc) from exc
            raise

    return RunnableLambda(invoke_with_retry_marker).with_retry(
        retry_if_exception_type=(_RetryableProviderError,),
        stop_after_attempt=max_retries,
        wait_exponential_jitter=True,
        exponential_jitter_params={"initial": 1, "max": 8},
    )


def _invoke_structured(
    model: BaseChatModel,
    messages: list[BaseMessage],
    schema_model: type[SchemaT],
    max_retries: int,
    *,
    json_object_mode: bool,
) -> dict:
    if json_object_mode:
        structured = model.with_structured_output(
            schema_model,
            method="json_mode",
            include_raw=True,
        )
    else:
        structured = model.with_structured_output(
            schema_model,
            method="json_schema",
            strict=True,
            include_raw=True,
        )
    try:
        return _with_retry(structured, max_retries).invoke(messages)
    except _RetryableProviderError as exc:
        raise exc.original


def _validated_result(
    result: dict,
    schema_model: type[SchemaT],
) -> tuple[SchemaT | None, AIMessage | None, Exception | None]:
    """Normalize include_raw output across providers.

    LangChain normally returns the requested Pydantic instance. Some provider
    integrations return a dict instead, so the application boundary still
    validates that dict before accepting it.
    """
    raw = result.get("raw")
    raw_message = raw if isinstance(raw, AIMessage) else None
    parsing_error = result.get("parsing_error")
    if parsing_error is not None:
        return None, raw_message, parsing_error
    parsed = result.get("parsed")
    if isinstance(parsed, schema_model):
        return parsed, raw_message, None
    try:
        return schema_model.model_validate(parsed), raw_message, None
    except ValidationError as exc:
        return None, raw_message, exc


def _invoke_structured_once(
    model: BaseChatModel,
    system: str,
    user: str,
    schema_model: type[SchemaT],
    max_retries: int,
    *,
    force_json_object: bool = False,
) -> SchemaT:
    json_object_mode = force_json_object

    if json_object_mode:
        messages: list[BaseMessage] = [
            SystemMessage(_schema_in_prompt(system, schema_model)),
            HumanMessage(user),
        ]
    else:
        messages = [SystemMessage(system), HumanMessage(user)]

    try:
        result = _invoke_structured(
            model,
            messages,
            schema_model,
            max_retries,
            json_object_mode=json_object_mode,
        )
    except openai.BadRequestError as exc:
        if json_object_mode or not _is_unsupported_structured_format_error(exc):
            raise
        logger.info("llm_json_schema_unsupported_falling_back")
        json_object_mode = True
        messages = [
            SystemMessage(_schema_in_prompt(system, schema_model)),
            HumanMessage(user),
        ]
        try:
            result = _invoke_structured(
                model,
                messages,
                schema_model,
                max_retries,
                json_object_mode=True,
            )
        except ValidationError as exc:
            parsed, raw_message, validation_error = None, None, exc
        else:
            parsed, raw_message, validation_error = _validated_result(result, schema_model)
    except ValidationError as exc:
        parsed, raw_message, validation_error = None, None, exc
    else:
        parsed, raw_message, validation_error = _validated_result(result, schema_model)

    if validation_error is None:
        assert parsed is not None
        return parsed

    logger.warning("llm_validation_failed_reprompting", error=str(validation_error))
    retry_messages = [*messages]
    if raw_message is not None:
        retry_messages.append(raw_message)
    retry_messages.append(
        HumanMessage(
            "That response failed schema validation with this error: "
            f"{validation_error}\nReturn corrected JSON only, matching the schema."
        )
    )
    try:
        retry_result = _invoke_structured(
            model,
            retry_messages,
            schema_model,
            max_retries,
            json_object_mode=json_object_mode,
        )
    except ValidationError as exc2:
        raise LLMOutputError(
            f"{schema_model.__name__} validation failed after retry: {exc2}"
        ) from exc2
    parsed, _, validation_error = _validated_result(retry_result, schema_model)
    if validation_error is not None:
        raise LLMOutputError(
            f"{schema_model.__name__} validation failed after retry: {validation_error}"
        ) from validation_error
    assert parsed is not None
    return parsed


def invoke_structured(
    system: str,
    user: str,
    schema_model: type[SchemaT],
    max_retries: int | None = None,
) -> SchemaT:
    """Return a validated `schema_model` instance from the configured model.

    This is the codebase's structured-model policy boundary. See the module
    docstring for the retry and fallback sequence. `max_retries` is the
    maximum total LangChain attempts and defaults to the active profile's
    value from llm.yaml.
    """
    profiles = load_llm_profiles(settings)
    retries = max_retries if max_retries is not None else profiles.primary.max_retries
    primary = _resolve_primary(profiles)
    try:
        return _invoke_structured_once(primary, system, user, schema_model, retries)
    except Exception as exc:
        if not _is_retryable_provider_error(exc):
            raise
        fallback = _resolve_fallback(profiles)
        if fallback is None:
            raise
        logger.warning("llm_primary_exhausted_using_fallback", error=str(exc))
        return _invoke_structured_once(
            fallback,
            system,
            user,
            schema_model,
            max_retries=1,
            force_json_object=True,
        )


def classify_injection(text: str) -> str:
    """Send `text` to the configured prompt-guard model and return its raw
    completion content unparsed (a label, e.g. "benign" or "malicious" -
    interpreting that label is `safety/injection_guard.py`'s job, not this
    function's).

    Uses the guard's own endpoint profile (llm.yaml `injection_guard.profile`,
    defaulting to the primary when that is OpenAI-compatible; the test
    override wins over both), a single plain message, no structured-output
    request and no retry loop. Raises on any transport, API or configuration
    error; the caller is responsible for catching that and falling back,
    since a classifier outage must never block a request on its own.
    """
    profiles = load_llm_profiles(settings)
    if _override is not None:
        model = _wrap_test_client(_override, profiles.injection_guard_model)
    elif profiles.guard is None:
        raise LLMConfigError(
            "no OpenAI-compatible endpoint profile for the injection guard; "
            "set injection_guard.profile in backend/llm.yaml"
        )
    else:
        model = _build_chat_model(profiles.guard, model_name=profiles.injection_guard_model)
    reply = model.invoke([HumanMessage(text)])
    return reply.text
