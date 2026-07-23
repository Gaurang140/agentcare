"""Single LLM entry point for the whole codebase.

`chat_json` is the only place `client.chat.completions.create(...)` is
called. Every agent node asks for a pydantic model back and gets one,
validated; nothing else should import `openai` directly.

Call sequence for one `chat_json(...)`:

1. Build (or reuse the test override of) the primary client from settings.
2. Ask for strict structured output (`response_format: json_schema`,
   `strict: true`, schema from `schema_model.model_json_schema()` with
   `additionalProperties: False`) - Groq's gpt-oss models support this.
3. If the endpoint rejects that request format (`openai.BadRequestError`,
   e.g. a local LM Studio server), retry the same client with
   `{"type": "json_object"}` and the schema spelled out in the prompt
   instead.
4. Validate the reply with `schema_model.model_validate_json`. On a
   validation failure, re-prompt once with the validation error appended
   and validate again; if that still fails, raise `LLMOutputError`.
5. Transport failures and 5xx/429s are retried with tenacity exponential
   backoff (1-8s) within a single logical request.
6. If the primary client's retries are exhausted and a fallback endpoint is
   configured (`llm_fallback_base_url`), repeat the whole thing once against
   the fallback in `json_object` mode.
"""

from __future__ import annotations

import json
from typing import TypeVar

import openai
from openai import OpenAI
from pydantic import BaseModel, ValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import settings
from app.logging_setup import get_logger

logger = get_logger(__name__)

SchemaT = TypeVar("SchemaT", bound=BaseModel)

# Errors worth retrying with backoff: dropped connections, timeouts, 5xx,
# and 429 rate limits. A 400 (BadRequestError) is a different signal - it
# means the request itself is unsupported, not that it should be retried -
# so it is handled separately in _chat_json_once.
_TRANSPORT_EXCEPTIONS: tuple[type[Exception], ...] = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.InternalServerError,
    openai.RateLimitError,
)

_override: OpenAI | None = None
_override_fallback: OpenAI | None = None


class LLMOutputError(Exception):
    """Raised when the model never returns output that validates against
    the requested schema, even after the one allowed re-prompt."""


def set_llm_client_for_tests(client: OpenAI | None, fallback: OpenAI | None = None) -> None:
    """Inject fake client(s) so tests never touch the network.

    Pass `None` (the default) to clear the override and go back to building
    real clients from settings. `fallback`, if given, is used in place of a
    real fallback-endpoint client whenever the primary's retries exhaust.
    """
    global _override, _override_fallback
    _override = client
    _override_fallback = fallback


def get_client() -> OpenAI:
    """Build the primary OpenAI-compatible client from settings."""
    return OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key)


def _fallback_client() -> OpenAI | None:
    if not settings.llm_fallback_base_url:
        return None
    return OpenAI(base_url=settings.llm_fallback_base_url, api_key=settings.llm_fallback_api_key)


def _resolve_primary() -> OpenAI:
    return _override if _override is not None else get_client()


def _resolve_fallback() -> OpenAI | None:
    # When a test override is active, only ever use the fallback the test
    # explicitly injected - never fall through to a real settings-built
    # client, so tests stay network-free even if .env sets a real fallback.
    if _override is not None:
        return _override_fallback
    return _fallback_client()


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


def _build_messages(
    system: str, user: str, schema_model: type[BaseModel], *, json_object_mode: bool
) -> list[dict]:
    if json_object_mode:
        schema_json = json.dumps(schema_model.model_json_schema())
        system = (
            f"{system}\n\n"
            "Respond with a single JSON object only, no prose, no markdown "
            f"fences, matching this JSON schema exactly:\n{schema_json}"
        )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _request(
    client: OpenAI,
    model: str,
    messages: list[dict],
    response_format: dict,
    max_retries: int,
):
    """One logical LLM call, retried with exponential backoff (1-8s) on
    transport/5xx/429 errors. A BadRequestError (unsupported request) is
    not retryable and propagates on the first attempt."""

    @retry(
        retry=retry_if_exception_type(_TRANSPORT_EXCEPTIONS),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        stop=stop_after_attempt(max_retries),
        reraise=True,
    )
    def _do_call():
        return client.chat.completions.create(
            model=model,
            messages=messages,
            response_format=response_format,
        )

    return _do_call()


def _extract_content(response) -> str:
    return response.choices[0].message.content


def _chat_json_once(
    client: OpenAI,
    model: str,
    system: str,
    user: str,
    schema_model: type[SchemaT],
    max_retries: int,
    *,
    force_json_object: bool = False,
) -> SchemaT:
    json_object_mode = force_json_object
    messages = _build_messages(system, user, schema_model, json_object_mode=json_object_mode)

    if json_object_mode:
        response = _request(client, model, messages, {"type": "json_object"}, max_retries)
    else:
        try:
            response = _request(
                client, model, messages, _strict_schema(schema_model), max_retries
            )
        except openai.BadRequestError:
            logger.info("llm_json_schema_unsupported_falling_back", model=model)
            json_object_mode = True
            messages = _build_messages(system, user, schema_model, json_object_mode=True)
            response = _request(client, model, messages, {"type": "json_object"}, max_retries)

    content = _extract_content(response)
    try:
        return schema_model.model_validate_json(content)
    except ValidationError as exc:
        logger.warning("llm_validation_failed_reprompting", model=model, error=str(exc))
        retry_messages = [
            *messages,
            {"role": "assistant", "content": content},
            {
                "role": "user",
                "content": (
                    "That response failed schema validation with this error: "
                    f"{exc}\nReturn corrected JSON only, matching the schema."
                ),
            },
        ]
        response_format = (
            {"type": "json_object"} if json_object_mode else _strict_schema(schema_model)
        )
        response = _request(client, model, retry_messages, response_format, max_retries)
        content = _extract_content(response)
        try:
            return schema_model.model_validate_json(content)
        except ValidationError as exc2:
            raise LLMOutputError(
                f"{schema_model.__name__} validation failed after retry: {exc2}"
            ) from exc2


def chat_json(
    system: str,
    user: str,
    schema_model: type[SchemaT],
    max_retries: int = 3,
) -> SchemaT:
    """Ask the LLM for JSON matching `schema_model` and return a validated
    instance. The only LLM entry point in the codebase - see module
    docstring for the full retry/fallback sequence."""
    primary = _resolve_primary()
    try:
        return _chat_json_once(primary, settings.llm_model, system, user, schema_model, max_retries)
    except _TRANSPORT_EXCEPTIONS as exc:
        fallback = _resolve_fallback()
        if fallback is None:
            raise
        logger.warning("llm_primary_exhausted_using_fallback", error=str(exc))
        return _chat_json_once(
            fallback,
            settings.llm_fallback_model,
            system,
            user,
            schema_model,
            max_retries=1,
            force_json_object=True,
        )
