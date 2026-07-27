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

Layer 2 (optional, classifier): a model reviews the text too, catching
phrasing layer 1's fixed pattern list does not anticipate. A layer-2 failure
never blocks on its own - it is logged and the request falls through to layer
1's already-clean verdict, per the rule that a classifier outage must never
take the system down.

One slot, two providers, the same shape as `services/storage.py`'s local and
GCS backends. Google Model Armor holds the slot when
`settings.model_armor_template` is set (`safety/model_armor.py`, the GCP
path); otherwise a small preview model does, when `settings.llm_api_key` and
`settings.injection_guard_model` are both set
(`agents/llm.py::classify_injection`). With both configured Model Armor wins,
because a request pays for one layer-2 round trip and not two. `via` on a
block names the provider that decided, so the audit trail says which one it
was. Neither is required: with no template and no key the deterministic layer
runs alone, which is the no-key demo path.

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

`screen_injection_group` is the same guard for a caller holding several
strings bound for one prompt: layer 1 still reads each of them, layer 2 reads
all of them in a single call. Layer 1 is a local function and layer 2 is a
network round trip, so batching keeps a document at one classifier call
instead of one per string it contributes. For layer 1 that is cost only; for
layer 2 coverage shifts, since one label over joined text is not the same as
one label per part, and a classifier has a fixed input window. Two things
follow from that and both live in `screen_injection_group`: the join puts the
short readings first so a long one cannot push them out of the window, and
the caller names the redaction language rather than letting the merged string
decide it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.agents.llm import classify_injection
from app.config import settings
from app.logging_setup import get_logger
from app.safety import model_armor
from app.safety.pii import redact_for_llm
from app.safety.text_normalize import fold_confusables

logger = get_logger(__name__)

Action = Literal["allow", "block"]
Via = Literal["deterministic", "classifier", "model_armor", "none"]


@dataclass
class InjectionResult:
    """`via` names which layer produced a "block", and for layer 2 which
    provider produced it ("classifier" for the hosted prompt-guard model,
    "model_armor" for Model Armor). It is always "none" when `action` is
    "allow", regardless of whether layer 2 ran and explicitly cleared the
    text or never ran at all."""

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
    """Every pattern against the raw text, then against its folded reading.

    Both, not one: the fold (safety/text_normalize.py) is what catches a
    zero-width character dropped inside a phrase or a full-width spelling of
    it, and folding is lossy, so the raw text stays the first reading. A label
    is reported once however many readings produced it, which keeps the list
    the same as before for text no fold changes.
    """
    readings = [text]
    folded = fold_confusables(text)
    if folded != text:
        readings.append(folded)

    matched: list[str] = []
    for reading in readings:
        for label, pattern in (*_INJECTION_PATTERNS, *_ROLE_MARKER_PATTERNS):
            if label not in matched and pattern.search(reading):
                matched.append(label)
        if "base64-looking run" not in matched and _BASE64_RUN_RE.search(reading):
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
    """True when either provider can fill the layer-2 slot."""
    if model_armor.is_enabled():
        return True
    return bool(settings.llm_api_key) and bool(settings.injection_guard_model)


def _classifier_via() -> Via:
    """Which provider holds the slot right now. Model Armor takes precedence
    when both are configured, so this is also the routing decision."""
    return "model_armor" if model_armor.is_enabled() else "classifier"


def _classifier_flags_injection(text: str, language: str | None = None) -> bool:
    """Redact, then ask whichever provider holds the slot. See the module
    docstring for why layer 2 reads the redacted copy and layer 1 reads the
    raw text. The redaction happens once, before the routing, so the
    guarantee does not depend on which provider answers.

    `language` is the caller's, forwarded straight to `redact_for_llm`. With
    None the redactor decides from the text it is handed, which is the right
    default for a single string and the wrong one for a joined group (see
    `screen_injection_group`).

    Model Armor returns None for "no opinion" (disabled, unreachable, a reply
    it could not read), which reads as no objection here and leaves layer 1's
    verdict standing.
    """
    redacted, _ = redact_for_llm(text, language=language)
    if model_armor.is_enabled():
        verdict = model_armor.screen_prompt(redacted)
        return bool(verdict and verdict.flagged)
    label = _classifier_label(classify_injection(redacted))
    return bool(_INJECTION_LABEL_RE.search(label))


def _classifier_input(readings: Sequence[str]) -> str:
    """The readings as one string for layer 2, shortest first.

    Layer 2 has a fixed input window (512 tokens for the default prompt-guard
    model) and the readings are not the same size: a document's body is capped
    at 1500 characters for a PDF and not capped at all for a `.txt`, while its
    filename readings are two short strings. In caller order the short ones sit
    behind the long one and a long enough body is all it takes for the model to
    never read them. The join order carries no meaning of its own - layer 1
    iterates the readings separately and keeps the caller's order, which is
    where the blocked index comes from - so the short readings go first.
    `sorted` is stable, so readings of the same length keep the caller's order.
    """
    return "\n".join(sorted(readings, key=len))


def screen_injection_group(
    readings: Sequence[str], language: str | None = None
) -> tuple[InjectionResult, int | None]:
    """Screen several strings that end up in the same prompt as one unit.

    A caller with more than one string to screen (`agents/document.py` has
    three for every document: the extracted text, the filename and the
    filename read as words) would otherwise pay for one classifier round trip
    per string. The two layers are priced differently, so they are batched
    differently:

    - Layer 1 runs on every reading, in order, unchanged. It is a local pure
      function, so screening three strings costs the same as screening one,
      and neither filename reading covers the other.
    - Layer 2 runs at most once, over the readings joined by newlines
      (shortest first, see `_classifier_input`). It judges phrasing, and
      phrasing does not change because it arrives in one string instead of
      three.

    `language` is the redaction language for layer 2's copy, forwarded to
    `redact_for_llm`. A caller with several readings should pass it: the
    readings are merged before they are redacted, so with None the merged
    string decides, and one German word in a filename is then enough to send
    an English body to the German spaCy model, which rewrites the phrasing
    layer 2 exists to judge as a name. The caller knows which reading is the
    text and which are labels for it; this function does not.

    Returns the verdict and the index of the reading layer 1 blocked on. The
    index is None when nothing blocked, and also when layer 2 blocked: layer 2
    read the readings as one text, so no single one of them is identifiable
    there.

    Layer 2 only runs when a provider is configured for it (see
    `_classifier_enabled`), and never for an empty group;
    if it raises, the error is logged and the result falls back to layer 1's
    own (clean, by construction - layer 1 already returned above otherwise)
    verdict, so a classifier outage never blocks a request.

    Raises TypeError for a bare `str`, which satisfies `Sequence[str]` and
    would otherwise be screened one character at a time. `screen_injection` is
    the one-string form.
    """
    if isinstance(readings, str):
        raise TypeError("screen_injection_group takes a sequence of strings; use screen_injection")

    for index, text in enumerate(readings):
        deterministic_matches = _deterministic_matches(text)
        if deterministic_matches:
            result = InjectionResult(
                action="block", matched=deterministic_matches, via="deterministic"
            )
            return result, index

    if readings and _classifier_enabled():
        try:
            if _classifier_flags_injection(_classifier_input(readings), language=language):
                via = _classifier_via()
                blocked = InjectionResult(action="block", matched=[via], via=via)
                return blocked, None
        except Exception as exc:  # noqa: BLE001 - classifier errors must never block
            logger.warning("injection_classifier_failed_falling_back", error=str(exc))

    return InjectionResult(action="allow", matched=[], via="none"), None


def screen_injection(text: str) -> InjectionResult:
    """Screen `text` for prompt-injection attempts before it reaches a model.

    Layer 1 always runs and wins outright on a match; layer 2 is optional and
    runs after it. The one-string form of `screen_injection_group`, which
    holds the layer ordering both callers share.
    """
    result, _ = screen_injection_group([text])
    return result
