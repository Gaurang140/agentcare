"""Google Model Armor, the GCP-hosted provider for the guard slots this
codebase already has. It owns the SDK and nothing else: no policy decision
lives here, only "what did the service say".

It is not a third safety layer and not a second pipeline. Two slots call it:

- `safety/injection_guard.py` layer 2, the optional classifier slot. Model
  Armor replaces the Groq prompt-guard call there when
  `MODEL_ARMOR_TEMPLATE` is set; layer 1's deterministic patterns still run
  first and still decide on their own.
- `agents/safety.py`, immediately before the deterministic output sanitizer,
  which keeps the last word so a cloud outage can never let a diagnosis
  through.

What this provider is and is not responsible for:

| Concern | Owner |
|---|---|
| Known injection phrasing, healthcare boundary, emergency and medical refusal | deterministic code, no network and no key needed |
| Prompt injection and jailbreak detection by model | Model Armor, layer 2 |
| Malicious URI in patient text | Model Armor (AgentCare screens no URLs otherwise) |
| PII detection and redaction | Presidio, in-process (ADR-13). The template's SDP settings stay off |
| Final healthcare-boundary sanitizing of the answer | deterministic `sanitize_agent_output`, last |

Both screens return `None` for "no opinion": the adapter is disabled, the
package is missing, the client could not be built or the call failed. That is
deliberately distinct from a clean verdict, so a caller never reads an outage
as a pass. Nothing here ever raises into a request path, and neither the log
lines nor the verdict carry the screened text - categories only.

The SDK is imported lazily inside the builder, exactly like
`services/storage.py::GCSStorage` does with google-cloud-storage, so a
machine without `google-cloud-modelarmor` installed still runs the whole app.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.logging_setup import get_logger

logger = get_logger(__name__)

# Seconds. This call sits inside a request a patient is waiting on, and it is
# the optional second opinion: layer 1 has already decided in microseconds and
# the deterministic output sanitizer still runs whatever happens here. So the
# budget is what a patient does not notice next to the LLM turn that follows,
# not what a screening service might want at its slowest. Past it the call is
# abandoned and the verdict is "no opinion".
#
# Every call also passes retry=None. The generated client ships a default
# Retry that re-sends on ServiceUnavailable for up to 60 seconds, and it wraps
# the deadline rather than sitting inside it, so the timeout alone bounds one
# attempt and not the call: a 503 measured at 59.8 seconds over 20 attempts.
# One attempt with no retry is the whole point of a second opinion that is
# allowed to have no opinion.
CALL_TIMEOUT_SECONDS = 2.0

# `filter_match_state` and every per-filter `match_state` are the same enum
# (`modelarmor_v1.FilterMatchState`, values MATCH_FOUND / NO_MATCH_FOUND /
# FILTER_MATCH_STATE_UNSPECIFIED). Compared by name so an enum, a plain int
# alias or a string all read the same way.
_MATCH_FOUND = "MATCH_FOUND"

# The per-filter payload fields on `modelarmor_v1.FilterResult`, verified with
# dir() against google-cloud-modelarmor 0.7.1. `filter_results` is a map from
# a filter name ("pi_and_jailbreak", "malicious_uris") to one of these, with
# exactly one field set; an unset one reads back as an empty message, not as
# None. `sdp_filter_result` is the one that carries no `match_state` of its
# own (it wraps inspect/deidentify results that each carry theirs), and this
# template keeps SDP off anyway, so it never appears. A filter this list
# cannot read only costs the category name: `flagged` is the top-level state.
_FILTER_RESULT_FIELDS = (
    "rai_filter_result",
    "pi_and_jailbreak_filter_result",
    "malicious_uri_filter_result",
    "csam_filter_filter_result",
    "virus_scan_filter_result",
)


@dataclass(frozen=True)
class ModelArmorVerdict:
    """What the service said. `categories` names the filters that matched,
    never the text that matched them."""

    flagged: bool
    categories: tuple[str, ...]


_client: Any | None = None
_client_unavailable = False
_sdk: Any | None = None
_sdk_unavailable = False


def set_model_armor_client_for_tests(client: Any | None) -> None:
    """Inject a fake client so tests never touch the network.

    Mirrors `app.agents.llm.set_llm_client_for_tests`. Pass `None` to clear
    the override and go back to building a real client from settings, which
    also clears the "unavailable" latch below.
    """
    global _client, _client_unavailable
    _client = client
    _client_unavailable = False


# The value the gcp overlay ships for MODEL_ARMOR_TEMPLATE until someone
# substitutes the real template name. Treating it as "not configured" turns a
# forgotten substitution into a quiet no-op rather than a doomed API call, a
# warning and wasted latency on every single request.
_PLACEHOLDER = "REPLACE_ME"


def is_enabled() -> bool:
    """True only with a real template configured. Empty is the default
    everywhere off GCP, and it keeps the no-key demo path free of any network
    call. The overlay's own placeholder counts as unconfigured."""
    template = settings.model_armor_template.strip()
    return bool(template) and template != _PLACEHOLDER


def _import_sdk() -> Any:
    from google.cloud import modelarmor_v1

    return modelarmor_v1


def _build_client() -> Any:
    """Regional endpoint, REST transport. Model Armor has no global endpoint:
    a client built without `api_endpoint` talks to the wrong place."""
    from google.api_core.client_options import ClientOptions

    sdk = _import_sdk()
    endpoint = f"modelarmor.{settings.model_armor_location}.rep.googleapis.com"
    return sdk.ModelArmorClient(
        transport="rest", client_options=ClientOptions(api_endpoint=endpoint)
    )


def _sdk_types() -> Any | None:
    """The `modelarmor_v1` module, imported once and held for the process.

    None when the package is not installed, which is the ordinary case off
    GCP and must not be an error: the request types are built from this
    module, so a missing package has to fail here rather than at a call.
    """
    global _sdk, _sdk_unavailable
    if _sdk is not None:
        return _sdk
    if _sdk_unavailable:
        return None
    try:
        _sdk = _import_sdk()
    except Exception as exc:  # noqa: BLE001 - a missing SDK is "no opinion", not an error
        _sdk_unavailable = True
        logger.warning("model_armor_unavailable", error=str(exc))
        return None
    return _sdk


def _get_client() -> Any | None:
    """The client, built on first use and held for the process.

    A construction failure (no credentials, a bad endpoint) is latched: it
    would fail identically on the next request, and logging it per request
    turns one configuration problem into a log flood.
    """
    global _client, _client_unavailable
    if _client is not None:
        return _client
    if _client_unavailable:
        return None
    try:
        _client = _build_client()
    except Exception as exc:  # noqa: BLE001 - never raise into a request path
        _client_unavailable = True
        logger.warning("model_armor_unavailable", error=str(exc))
        return None
    return _client


def _is_match(state: Any) -> bool:
    return getattr(state, "name", str(state)) == _MATCH_FOUND


def _filter_matched(filter_result: Any) -> bool:
    for field in _FILTER_RESULT_FIELDS:
        payload = getattr(filter_result, field, None)
        if payload and _is_match(getattr(payload, "match_state", None)):
            return True
    return False


def _verdict(response: Any) -> ModelArmorVerdict | None:
    """Read a sanitize response honestly. `invocation_result` reports whether
    the filters actually ran, irrespective of match state (SUCCESS: all ran;
    PARTIAL: some skipped or failed; FAILURE: all skipped or failed). A
    positive match always stands - a filter that matched demonstrably ran.
    A "no match" is a clean verdict only under SUCCESS; from a FAILURE,
    PARTIAL or unset invocation it is no opinion (None), because reading a
    degraded execution as clean silently overstates protection."""
    result = response.sanitization_result
    categories = tuple(
        sorted(name for name, item in result.filter_results.items() if _filter_matched(item))
    )
    if _is_match(result.filter_match_state):
        return ModelArmorVerdict(flagged=True, categories=categories)
    invocation = getattr(result, "invocation_result", None)
    if getattr(invocation, "name", str(invocation)) != "SUCCESS":
        logger.warning(
            "model_armor_incomplete_execution_no_opinion",
            invocation_result=getattr(invocation, "name", str(invocation)),
        )
        return None
    return ModelArmorVerdict(flagged=False, categories=categories)


def _screen(text: str, *, operation: str) -> ModelArmorVerdict | None:
    if not is_enabled():
        return None
    sdk = _sdk_types()
    if sdk is None:
        return None
    client = _get_client()
    if client is None:
        return None

    try:
        if operation == "sanitize_user_prompt":
            request = sdk.SanitizeUserPromptRequest(
                name=settings.model_armor_template,
                user_prompt_data=sdk.DataItem(text=text),
            )
            response = client.sanitize_user_prompt(
                request=request, timeout=CALL_TIMEOUT_SECONDS, retry=None
            )
        else:
            request = sdk.SanitizeModelResponseRequest(
                name=settings.model_armor_template,
                model_response_data=sdk.DataItem(text=text),
            )
            response = client.sanitize_model_response(
                request=request, timeout=CALL_TIMEOUT_SECONDS, retry=None
            )
        # Parsing is inside the try on purpose: a payload shape this adapter
        # cannot read is the same kind of problem as a transport error, and
        # both have to end as "no opinion" rather than a 500 for the patient.
        return _verdict(response)
    except Exception as exc:  # noqa: BLE001 - never raise into a request path
        logger.warning("model_armor_failed", operation=operation, error=str(exc))
        return None


def screen_prompt(text: str) -> ModelArmorVerdict | None:
    """Screen text on its way into a prompt. None means no opinion."""
    return _screen(text, operation="sanitize_user_prompt")


def screen_response(text: str) -> ModelArmorVerdict | None:
    """Screen a draft answer on its way to the patient. None means no
    opinion; the deterministic sanitizer runs either way."""
    return _screen(text, operation="sanitize_model_response")
