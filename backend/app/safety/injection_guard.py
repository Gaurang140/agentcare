"""Two-layer prompt-injection guard for any text that will be embedded in an
LLM prompt: the patient's own request text (`workflow_service.create_run`)
and a document's extracted text before it goes into the document agent's
classification prompt (`agents/document.py`). Both callers screen text that a
patient (or a document a patient uploaded) controls directly, so both are
places someone could try to smuggle instructions past the model.

Layer 1 (always on, deterministic): case-insensitive, word-boundary-aware
regex patterns over known injection phrasing in English and German, plus two
shape-based checks - a long base64-looking run, and role-injection markers
(fake chat-template tokens) appearing inside what should be plain user text.
Pure function, no network, cheap enough to run on every request.

Layer 2 (optional, classifier): when `settings.llm_api_key` and
`settings.injection_guard_model` are both set, a small preview model
(`agents/llm.py::classify_injection`) reviews the text too, catching phrasing
layer 1's fixed pattern list does not anticipate. A layer-2 failure never
blocks on its own - it is logged and the request falls through to layer 1's
already-clean verdict, per the rule that a classifier outage must never take
the system down.

The two layers see different strings on purpose. Layer 1 is a local pure
function, so it reads the raw text and keeps every character an attacker
typed. Layer 2 leaves the process for a model provider, so it is behind the
same PII boundary as every other prompt and reads
`safety/pii.py::redact_for_llm`'s copy instead. Nothing is lost by that:
redaction replaces values (an email, a phone number, a name) with fixed
tokens and leaves phrasing alone, and phrasing is the only thing an
injection classifier judges. No audit row is written here - `screen_injection`
has no database session, and the agent call sites already audit the
redactions they perform on their own way to the model.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

from app.agents.llm import classify_injection
from app.config import settings
from app.logging_setup import get_logger
from app.safety.pii import redact_for_llm

logger = get_logger(__name__)

Action = Literal["allow", "block"]
Via = Literal["deterministic", "classifier", "none"]


@dataclass
class InjectionResult:
    """`via` names which layer produced a "block"; it is always "none" when
    `action` is "allow", regardless of whether the classifier ran and
    explicitly cleared the text or never ran at all."""

    action: Action
    matched: list[str]
    via: Via


# --- Layer 1: deterministic patterns (EN + DE) ------------------------------
# Each pattern is the brief's phrasing verbatim, wrapped in `\b` word
# boundaries and matched case-insensitively so "Ignore ALL Previous
# Instructions" fires the same as the lowercase form, but a pattern never
# matches inside a longer, unrelated word.

_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "ignore previous instructions",
        re.compile(r"\bignore\s+(all\s+)?previous\s+(instructions|rules)\b", re.IGNORECASE),
    ),
    (
        "disregard system/above",
        re.compile(r"\bdisregard\s+(the\s+)?(system|above)\b", re.IGNORECASE),
    ),
    (
        "you are now a/an/the ...",
        re.compile(r"\byou\s+are\s+now\s+(a|an|the)\b", re.IGNORECASE),
    ),
    ("system prompt", re.compile(r"\bsystem\s+prompt\b", re.IGNORECASE)),
    ("developer mode", re.compile(r"\bdeveloper\s+mode\b", re.IGNORECASE)),
    ("jailbreak", re.compile(r"\bjailbreak\b", re.IGNORECASE)),
    ("dan mode", re.compile(r"\bdan\s+mode\b", re.IGNORECASE)),
    (
        "override safety/instructions",
        re.compile(r"\boverride\s+(safety|instructions)\b", re.IGNORECASE),
    ),
    (
        "reveal your prompt/instructions/rules",
        re.compile(r"\breveal\s+your\s+(prompt|instructions|rules)\b", re.IGNORECASE),
    ),
    # German: "vergiss (alle) (vorherigen) anweisungen" - "forget (all)
    # (previous) instructions".
    (
        "vergiss anweisungen",
        re.compile(r"\bvergiss\s+(alle\s+)?(vorherigen\s+)?anweisungen\b", re.IGNORECASE),
    ),
    # German: "ignoriere (die) regeln" - "ignore (the) rules".
    ("ignoriere regeln", re.compile(r"\bignoriere\s+(die\s+)?regeln\b", re.IGNORECASE)),
]

# Role-injection markers: a fake conversation turn or a raw chat-template
# token smuggled into what should be plain patient text, so the model reads
# it as a role change instead of user content. `re.escape` on the token
# markers since `|` and `<>` are regex-special.
_ROLE_MARKER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("assistant: marker", re.compile(r"\bassistant\s*:", re.IGNORECASE)),
    ("<|im_start|> marker", re.compile(re.escape("<|im_start|>"), re.IGNORECASE)),
    ("[INST] marker", re.compile(re.escape("[INST]"), re.IGNORECASE)),
]

# A run of 120+ base64-alphabet characters is far longer than any normal
# admin request would contain unbroken - names, addresses and appointment
# asks are all short words with spaces and punctuation - so it is a
# plausible way to smuggle an encoded instruction past a keyword scan. This
# is a shape check only (no decode step): any base64-alphabet run this long,
# padded to a multiple of 4, decodes without error regardless of what it
# actually contains, so attempting to "validate" it first would filter
# nothing and only add a misleading sense of precision.
_BASE64_RUN_RE = re.compile(r"[A-Za-z0-9+/]{120,}={0,2}")


def _deterministic_matches(text: str) -> list[str]:
    matched = [label for label, pattern in _INJECTION_PATTERNS if pattern.search(text)]
    matched += [label for label, pattern in _ROLE_MARKER_PATTERNS if pattern.search(text)]
    if _BASE64_RUN_RE.search(text):
        matched.append("base64-looking run")
    return matched


# --- Layer 2: optional classifier ------------------------------------------
# Groq does not publish an exact response schema for this preview model at
# the time this was written (checked 2026-07-24: console.groq.com/docs
# shows the call shape - a plain chat completion - but not the label
# format). The Prompt Guard 2 model card describes binary "benign"/
# "malicious" labels; community reports of the hosted endpoint also show a
# bare "JAILBREAK"-style label. Handled defensively: any label containing
# "malicious", "jailbreak" or "injection" (case-insensitive) is a block;
# anything else - including "benign" - is an allow.
_INJECTION_LABEL_RE = re.compile(r"malicious|jailbreak|injection", re.IGNORECASE)


def _classifier_label(raw: str) -> str:
    """Pull a label string out of the classifier's raw completion content.

    Handles a bare text label ("benign") and, defensively, a JSON object
    with a "label" key or a JSON-quoted string, in case the hosted endpoint
    wraps its answer - the exact contract is not documented (see the module
    comment above `_INJECTION_LABEL_RE`).
    """
    raw = (raw or "").strip()
    if not raw or raw[0] not in "{\"":
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if isinstance(parsed, dict):
        return str(parsed.get("label", raw))
    if isinstance(parsed, str):
        return parsed
    return raw


def _classifier_enabled() -> bool:
    return bool(settings.llm_api_key) and bool(settings.injection_guard_model)


def _classifier_flags_injection(text: str) -> bool:
    """Redact, then ask the classifier. See the module docstring for why the
    classifier reads the redacted copy and layer 1 reads the raw text."""
    redacted, _ = redact_for_llm(text)
    label = _classifier_label(classify_injection(redacted))
    return bool(_INJECTION_LABEL_RE.search(label))


def screen_injection(text: str) -> InjectionResult:
    """Screen `text` for prompt-injection attempts before it reaches a model.

    Layer 1 always runs and wins outright on a match. Layer 2 only runs when
    both `settings.llm_api_key` and `settings.injection_guard_model` are
    set; if it raises, the error is logged and the result falls back to
    layer 1's own (clean, by construction - layer 1 already returned above
    otherwise) verdict, so a classifier outage never blocks a request.
    """
    deterministic_matches = _deterministic_matches(text)
    if deterministic_matches:
        return InjectionResult(action="block", matched=deterministic_matches, via="deterministic")

    if _classifier_enabled():
        try:
            if _classifier_flags_injection(text):
                return InjectionResult(action="block", matched=["classifier"], via="classifier")
        except Exception as exc:  # noqa: BLE001 - classifier errors must never block
            logger.warning("injection_classifier_failed_falling_back", error=str(exc))

    return InjectionResult(action="allow", matched=[], via="none")
