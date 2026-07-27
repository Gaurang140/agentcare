"""The two LLM entry points for the whole codebase, on the langchain
chat-model layer.

`chat_json` is where every agent node gets its structured, pydantic-validated
output. `classify_injection` is the one other model call: a single plain
completion for the optional classifier layer of the prompt-injection guard
(`safety/injection_guard.py`), since a prompt-guard model returns a bare
label, not schema-shaped JSON. Nothing else builds a chat model.

Models come from `langchain.chat_models.init_chat_model`, configured by the
profiles in `backend/llm.yaml` (see `agents/model_config.py`; env vars win
over the file). The default profile is Groq's OpenAI-compatible endpoint via
langchain-openai; any other provider works by installing its langchain
package (e.g. `pip install langchain-google-vertexai` for Vertex AI) and
naming it in a profile.

Call sequence for one `chat_json(...)`:

1. Resolve the active profile (or the test override injected by
   `set_llm_client_for_tests`) and build the chat model.
2. Ask for strict structured output (`response_format: json_schema`,
   `strict: true`, schema from `schema_model.model_json_schema()` with
   `additionalProperties: False`) - Groq's gpt-oss models support this.
3. If the endpoint rejects that request format (`openai.BadRequestError`,
   e.g. a local LM Studio server), retry the same model with
   `{"type": "json_object"}` and the schema spelled out in the prompt
   instead.
4. Validate the reply with `schema_model.model_validate_json`. On a
   validation failure, re-prompt once with the validation error appended
   and validate again; if that still fails, raise `LLMOutputError`.
5. Transport failures and 5xx/429s are retried with exponential backoff
   (1-8s) via langchain's `with_retry`, within a single logical request.
   The SDK's own internal retries are disabled (`max_retries=0`) so the
   two policies never multiply, and each attempt is bounded by the
   profile's `timeout`.
6. If the primary's retries are exhausted and a fallback is configured
   (`LLM_FALLBACK_BASE_URL`, or `fallback_profile` in llm.yaml), repeat
   the whole thing once against the fallback in `json_object` mode.
"""

from __future__ import annotations

import json
from typing import TypeVar

import openai
from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError

from app.agents.model_config import ModelProfile, load_llm_profiles
from app.config import settings
from app.logging_setup import get_logger

logger = get_logger(__name__)

SchemaT = TypeVar("SchemaT", bound=BaseModel)

# Errors worth retrying with backoff: dropped connections, timeouts, 5xx,
# and 429 rate limits. langchain-openai raises the openai SDK's exception
# types unchanged. A 400 (BadRequestError) is a different signal - it means
# the request itself is unsupported, not that it should be retried - so it
# is handled separately in _chat_json_once.
_TRANSPORT_EXCEPTIONS: tuple[type[Exception], ...] = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.InternalServerError,
    openai.RateLimitError,
)

_override = None
_override_fallback = None


class LLMOutputError(Exception):
    """Raised when the model never returns output that validates against
    the requested schema, even after the one allowed re-prompt."""


class LLMConfigError(Exception):
    """Raised when a profile names a provider whose langchain integration
    package is not installed."""


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

    SDK-internal retries stay off (`max_retries=0`): the retry policy lives
    in `_with_retry`, and stacking the two would multiply attempts. The
    "missing-key" placeholder keeps a keyless boot possible - openai 2.x
    refuses to construct a client with no credentials, and the endpoint
    answers 401 at call time either way."""
    name = model_name or profile.model
    kwargs: dict = dict(profile.params)
    if profile.provider == "openai":
        kwargs["base_url"] = profile.base_url
        kwargs["api_key"] = settings.llm_api_key or "missing-key"
        kwargs["timeout"] = profile.timeout
        kwargs["max_retries"] = 0
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


def _strict_schema(schema_model: type[BaseModel]) -> dict:
    schema = schema_model.model_json_schema()
    schema["additionalProperties"] = False
    return {
        "type": "json_schema",
        "json_schema": {
            "name": schema_model.__name__,
            "strict": True,
            "schema": schema,
        },
    }


def _schema_in_prompt(system: str, schema_model: type[BaseModel]) -> str:
    """json_object mode carries the schema in the system prompt instead of
    the request format."""
    schema_json = json.dumps(schema_model.model_json_schema())
    return (
        f"{system}\n\n"
        "Respond with a single JSON object only, no prose, no markdown "
        f"fences, matching this JSON schema exactly:\n{schema_json}"
    )


def _with_retry(runnable: Runnable, max_retries: int) -> Runnable:
    """Exponential backoff (1-8s) on transport/5xx/429 within one logical
    request; a BadRequestError propagates on the first attempt."""
    return runnable.with_retry(
        retry_if_exception_type=_TRANSPORT_EXCEPTIONS,
        stop_after_attempt=max_retries,
        wait_exponential_jitter=True,
        exponential_jitter_params={"initial": 1, "max": 8},
    )


def _invoke(
    model: BaseChatModel,
    messages: list[BaseMessage],
    response_format: dict,
    max_retries: int,
) -> AIMessage:
    bound = model.bind(response_format=response_format)
    return _with_retry(bound, max_retries).invoke(messages)


def _chat_json_once(
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
        reply = _invoke(model, messages, {"type": "json_object"}, max_retries)
    else:
        messages = [SystemMessage(system), HumanMessage(user)]
        try:
            reply = _invoke(model, messages, _strict_schema(schema_model), max_retries)
        except openai.BadRequestError:
            logger.info("llm_json_schema_unsupported_falling_back")
            json_object_mode = True
            messages = [
                SystemMessage(_schema_in_prompt(system, schema_model)),
                HumanMessage(user),
            ]
            reply = _invoke(model, messages, {"type": "json_object"}, max_retries)

    content = reply.text
    try:
        return schema_model.model_validate_json(content)
    except ValidationError as exc:
        logger.warning("llm_validation_failed_reprompting", error=str(exc))
        retry_messages = [
            *messages,
            AIMessage(content),
            HumanMessage(
                "That response failed schema validation with this error: "
                f"{exc}\nReturn corrected JSON only, matching the schema."
            ),
        ]
        response_format = (
            {"type": "json_object"} if json_object_mode else _strict_schema(schema_model)
        )
        reply = _invoke(model, retry_messages, response_format, max_retries)
        try:
            return schema_model.model_validate_json(reply.text)
        except ValidationError as exc2:
            raise LLMOutputError(
                f"{schema_model.__name__} validation failed after retry: {exc2}"
            ) from exc2


def chat_json(
    system: str,
    user: str,
    schema_model: type[SchemaT],
    max_retries: int | None = None,
) -> SchemaT:
    """Ask the LLM for JSON matching `schema_model` and return a validated
    instance. The only structured LLM entry point in the codebase - see
    module docstring for the full retry/fallback sequence. `max_retries`
    defaults to the active profile's value from llm.yaml."""
    profiles = load_llm_profiles(settings)
    retries = max_retries if max_retries is not None else profiles.primary.max_retries
    primary = _resolve_primary(profiles)
    try:
        return _chat_json_once(primary, system, user, schema_model, retries)
    except _TRANSPORT_EXCEPTIONS as exc:
        fallback = _resolve_fallback(profiles)
        if fallback is None:
            raise
        logger.warning("llm_primary_exhausted_using_fallback", error=str(exc))
        return _chat_json_once(
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
