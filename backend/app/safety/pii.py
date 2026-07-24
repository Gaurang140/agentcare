"""PII redaction at the LLM boundary.

Boundary rule: the database always keeps the original, unredacted text a
patient submitted (`WorkflowRun.request_text`, `PatientDocument.extracted_text`).
Redaction in this module applies ONLY to a copy of that text on its way into
an LLM prompt - never to what is stored, never to what is shown back to the
patient. `agents/safety.py` composes the patient-facing `final_response` from
freshly queried database rows, not from patient-submitted text, so it never
calls into this module (see that node's own docstring).

`redact_for_llm` is the one function every call site should import. It runs
two passes over the text, in this order:

Pass 1, regex (`PIIRedactor.redact`): five categories of PII found with
battle-tested regex families (each documented at its definition below), every
match replaced by a fixed `[REDACTED_<CATEGORY>]` token, so the shape of the
text survives for the LLM (still a sentence about an appointment) while the
sensitive value does not. Deterministic, no model, no state.

Pass 2, Presidio (`presidio-analyzer` + `presidio-anonymizer` over spaCy
`en_core_web_sm` / `de_core_news_sm`): named-entity recognition over what pass
1 left, for the categories no regex can describe - person names and locations -
plus a second net under the email, phone and IBAN families. Its findings are
replaced with the same token style (`[REDACTED_NAME]`, `[REDACTED_LOCATION]`),
so the counts dict and the audit row keep their shape: category counts only,
never a value.

Score threshold: a Presidio result below `_PRESIDIO_SCORE_THRESHOLD` (0.5) is
ignored. The number is chosen against what the engines actually return here -
spaCy NER entities arrive at 0.85, a validated email or IBAN at 1.0, and a
bare phone-number shape with no context word at 0.4. 0.5 keeps the first two
groups and drops the phone-shape guesses, which pass 1 has already had its own
stricter go at.

Language: pass 2 analyzes with the language the caller names
(`PatientProfile.preferred_language`, "en" or "de"). With no language it
analyzes English, and adds a German pass only when the text carries German
cues (`_GERMAN_HINT_RE`). That gate is deliberate: `de_core_news_sm` reads
English admin text badly enough to tag "Book", "Insurance card" and "sinus
rhythm" as person names, so an unconditional German pass would redact half of
an ordinary English request.

Failure containment: the engines are built lazily on first use and held as a
module-level singleton (construction loads two spaCy models, about a second).
A build failure is logged once per process as `pii_presidio_unavailable` and
never retried; an analysis failure is logged as `pii_presidio_failed`. Both
return the pass-1 result unchanged, so a Presidio problem degrades redaction
back to the regex families instead of blocking a patient request.

Runtime needs no network: the spaCy models are installed packages, pinned by
wheel URL in the root requirements.txt, and the one library call underneath
Presidio that could reach out (tldextract, behind its email validation) is
capped in `_build_engines` before it is ever imported.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from typing import Any, NamedTuple

from app.logging_setup import get_logger

logger = get_logger(__name__)

# --- Category patterns -------------------------------------------------------
# Applied in a fixed order (see `_PIPELINE` below) chosen so an earlier,
# more specific category consumes its match before a later, broader one gets
# a chance to misread part of it - e.g. email runs first so a digit-heavy
# local part is never mistaken for a phone number, and IBAN/health-insurance
# run before phone so a two-letter-plus-digits or letter-plus-digits id is
# never partially swallowed by the phone patterns.

# Standard email shape: local part (letters, digits, ., _, %, +, -), an
# "@", a domain of labels separated by dots and a final label of at least
# two letters (the TLD). Deliberately not RFC 5322-complete (no quoted
# local parts, no comments) - real-world emails in patient text are plain.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# German IBAN, compact form (no spaces): "DE" + 2 check digits + 8-digit
# bank code + 10-digit account number = "DE" + 20 digits. Checked first as
# the common case for a German clinic's patients.
_IBAN_DE_RE = re.compile(r"\bDE\d{20}\b")
# Generic IBAN, any country, compact form: 2-letter country code + 2 check
# digits + 11-30 further alphanumerics (IBAN total length varies 15-34
# characters by country; this already matches DE IBANs too, so the DE
# pattern above is redundant but kept for its own explicit comment and
# because it is the case this clinic's data will hit most often).
_IBAN_GENERIC_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
# IBANs in free text are often grouped in 4s ("DE89 3704 0044 0532 0130
# 00"). Not handled here - a documented limitation, not a silent gap (see
# docs/security.md's PII boundary subsection).

# German statutory health insurance number ("Krankenversichertennummer"):
# exactly one letter followed by 9 digits, no separator. `\b` on both ends
# so it never matches as a trailing fragment of a longer alphanumeric id.
_HEALTH_INSURANCE_RE = re.compile(r"\b[A-Za-z]\d{9}\b")

# Phone numbers: three shapes, checked in order, none of them using "."
# as a separator - "." is reserved for the dd.mm.yyyy date patterns below,
# so a German date and a hyphen/slash/space-separated phone number can
# never be mistaken for each other.
# 1) International, "+" prefix: country code then 1-4 further digit groups
#    separated by space/hyphen/slash, e.g. "+49 176 12345678", "+1-555-123-4567".
_PHONE_INTL_RE = re.compile(r"(?<!\w)\+\d{1,3}(?:[ \-/]?\d{2,8}){1,4}(?!\w)")
# 2) German national, trunk-prefix "0": optional parens around the leading
#    "0<area code>", then the subscriber number, e.g. "0176 12345678",
#    "030-12345678", "(030) 12345678", "089/12345678".
_PHONE_DE_NATIONAL_RE = re.compile(r"(?<!\w)\(?0\d{1,5}\)?[ \-/]?\d{3,8}(?:[ \-/]?\d{2,4})?(?!\w)")
# 3) Generic fallback (non-German, no "+", no leading trunk "0"): the
#    common 3-3-4 grouping, e.g. "555-123-4567".
_PHONE_GENERIC_RE = re.compile(r"(?<!\w)\d{3}[ \-/]\d{3}[ \-/]\d{4}(?!\w)")

# Date-of-birth-like dates. Two deliberately different rules, kept
# separate on purpose:
# 1) Context-triggered: a date directly preceded by an English or German
#    birth-context word ("born", "geboren", "geb"/"geb.", "date of
#    birth", "dob") redacts regardless of year, since the context word
#    itself already says what the date means. The whole match (context
#    word plus date) is replaced, matching every other category's
#    whole-match replacement.
# 2) Standalone: a bare dd.mm.yyyy or yyyy-mm-dd date with no context word
#    only redacts when its year falls in 1900-2015. This is the
#    conservative half of the rule: a hospital appointment date is always
#    near "now" (2026 onward in this system), so restricting the
#    standalone check to a plausible birth-year window means an ordinary
#    "book me for 15.08.2026" sentence is never touched, while an
#    unlabelled birth date typed by a patient ("15.03.1990") still is.
#    The one accepted false-positive: an inbound sentence that names an
#    old *appointment* date inside 1900-2015 (e.g. rescheduling something
#    from years ago) would also be redacted - traded off deliberately for
#    fewer missed birth dates, and it never touches what is stored or what
#    the patient is shown, only the copy sent to the LLM.
_DOB_CONTEXT_RE = re.compile(
    r"(?i)\b(?:born|geboren|geb\.?|date of birth|dob)(?=[\s:\-])\s*[:\-]?\s*"
    r"(?:\d{1,2}\.\d{1,2}\.\d{4}|\d{4}-\d{2}-\d{2})"
)
_DOB_STANDALONE_RE = re.compile(
    r"\b\d{1,2}\.\d{1,2}\.(?:19\d{2}|20(?:0\d|1[0-5]))\b"
    r"|\b(?:19\d{2}|20(?:0\d|1[0-5]))-\d{2}-\d{2}\b"
)

_TOKENS: dict[str, str] = {
    "email": "[REDACTED_EMAIL]",
    "iban": "[REDACTED_IBAN]",
    "health_insurance": "[REDACTED_HEALTH_INSURANCE]",
    "phone": "[REDACTED_PHONE]",
    "date_of_birth": "[REDACTED_DOB]",
    # Pass 2 only (Presidio): no regex family describes these.
    "name": "[REDACTED_NAME]",
    "location": "[REDACTED_LOCATION]",
}

# Category -> patterns, applied in this exact order (see comments above for
# why this order matters). Every pattern in a category replaces its whole
# match with that category's single token.
_PIPELINE: list[tuple[str, list[re.Pattern[str]]]] = [
    ("email", [_EMAIL_RE]),
    ("iban", [_IBAN_DE_RE, _IBAN_GENERIC_RE]),
    ("health_insurance", [_HEALTH_INSURANCE_RE]),
    ("phone", [_PHONE_INTL_RE, _PHONE_DE_NATIONAL_RE, _PHONE_GENERIC_RE]),
    ("date_of_birth", [_DOB_CONTEXT_RE, _DOB_STANDALONE_RE]),
]


class PIIRedactor:
    """Stateless PII redactor: compiles nothing per call, reads no config.

    Instantiate once (the module-level `_redactor` below) and reuse it -
    `redact` has no side effects and no dependency on the database, a
    request, or any other piece of app state.
    """

    def redact(self, text: str) -> tuple[str, dict[str, int]]:
        """Replace every recognized PII span in `text` with its category
        token. Returns the redacted text and a `{category: count}` dict
        containing only categories that actually matched (no zero entries),
        so a caller can test `if counts:` directly."""
        working = text
        counts: dict[str, int] = {}
        for category, patterns in _PIPELINE:
            matched = 0
            for pattern in patterns:
                working, n = pattern.subn(_TOKENS[category], working)
                matched += n
            if matched:
                counts[category] = matched
        return working, counts


_redactor = PIIRedactor()


# --- Pass 2: Presidio --------------------------------------------------------

# Presidio entity type -> the category (and so the token) it is reported as.
# PERSON and LOCATION are what pass 2 exists for. The other three are the
# second net under the regex families: a value pass 1's pattern missed still
# comes out as the same token pass 1 would have used, never a new one.
_PRESIDIO_CATEGORIES: dict[str, str] = {
    "PERSON": "name",
    "LOCATION": "location",
    "EMAIL_ADDRESS": "email",
    "PHONE_NUMBER": "phone",
    "IBAN_CODE": "iban",
}

# See the module docstring for how this number was chosen.
_PRESIDIO_SCORE_THRESHOLD = 0.5

# Small spaCy models only (the ceiling this project sets for a 16 GB machine).
# Both are installed as packages, pinned by wheel URL in the root
# requirements.txt, so nothing is downloaded at runtime.
_SPACY_MODELS: dict[str, str] = {"en": "en_core_web_sm", "de": "de_core_news_sm"}

# German cues: function words and umlauts that an English request does not
# carry. One hit is enough to add the German pass (see the module docstring for
# why the German model is not run unconditionally). Over-triggering is the safe
# direction: it only adds a second opinion on the same text.
_GERMAN_HINT_RE = re.compile(
    r"(?i)(?<!\w)(ich|mein|meine|meinen|meiner|mir|mich|bitte|termin|arzt|"
    r"aerztin|krankenhaus|und|ist|sind|nicht|kein|keine|einen|eine|der|die|das|"
    r"wohne|wohnhaft|geboren|habe|hat|fuer|von|zum|zur|sehr|geehrte)(?!\w)"
    r"|[äöüßÄÖÜ]"
)

# A token pass 1 already wrote. Pass 2 never touches one: without this an
# `[REDACTED_EMAIL]` could be read as a name and rewritten a second time.
_TOKEN_RE = re.compile(r"\[REDACTED_[A-Z_]+\]")


class _Engines(NamedTuple):
    """The Presidio objects, built together and cached together."""

    analyzer: Any
    anonymizer: Any
    operators: dict[str, Any]


_engines: _Engines | None = None
_engines_unavailable = False


def _build_operators() -> dict[str, Any]:
    """Presidio operator per entity type: replace the span with this module's
    token for that category."""
    from presidio_anonymizer.entities import OperatorConfig

    return {
        entity: OperatorConfig("replace", {"new_value": _TOKENS[category]})
        for entity, category in _PRESIDIO_CATEGORIES.items()
    }


def _build_engines() -> _Engines:
    """Load both spaCy models and wire them into an analyzer. About a second of
    work and tens of MB of memory, so it happens once, on first use."""
    # presidio's email recognizer validates a match through tldextract, which
    # by default tries to refresh the public suffix list over the network and
    # falls back to its bundled snapshot. Cap that attempt before tldextract is
    # imported: this module's contract is that no patient request waits on a
    # network call.
    os.environ.setdefault("TLDEXTRACT_CACHE_TIMEOUT", "2")

    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider
    from presidio_anonymizer import AnonymizerEngine

    nlp_engine = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [
                {"lang_code": lang, "model_name": model}
                for lang, model in _SPACY_MODELS.items()
            ],
        }
    ).create_engine()
    analyzer = AnalyzerEngine(
        nlp_engine=nlp_engine, supported_languages=list(_SPACY_MODELS)
    )
    return _Engines(
        analyzer=analyzer, anonymizer=AnonymizerEngine(), operators=_build_operators()
    )


def _get_engines() -> _Engines | None:
    """The cached engines, or None when they cannot be built. A build failure
    is remembered, so a broken install logs one warning for the process instead
    of one per patient request."""
    global _engines, _engines_unavailable
    if _engines is not None:
        return _engines
    if _engines_unavailable:
        return None
    try:
        _engines = _build_engines()
    except Exception as exc:  # noqa: BLE001 - any failure degrades to pass 1
        _engines_unavailable = True
        logger.warning("pii_presidio_unavailable", error=str(exc))
        return None
    return _engines


def reset_engines_for_tests() -> None:
    """Drop the cached engines (and any remembered build failure). Tests that
    swap `_build_engines` call this on the way in and on the way out."""
    global _engines, _engines_unavailable
    _engines = None
    _engines_unavailable = False


def _languages_for(text: str, language: str | None) -> tuple[str, ...]:
    """Which analyzer languages to run. A caller's language wins; with none,
    English always runs and German joins it on German-looking text."""
    named = (language or "").strip().lower()[:2]
    if named in _SPACY_MODELS:
        return (named,)
    if _GERMAN_HINT_RE.search(text):
        return ("en", "de")
    return ("en",)


def _presidio_pass(text: str, language: str | None) -> tuple[str, dict[str, int]]:
    """Run the analyzer over pass 1's output and replace what it finds.

    Returns the text unchanged and no counts whenever the engines are missing,
    the analysis fails, or nothing clears the score threshold.
    """
    engines = _get_engines()
    if engines is None:
        return text, {}

    existing = [match.span() for match in _TOKEN_RE.finditer(text)]
    try:
        results = []
        for lang in _languages_for(text, language):
            results.extend(
                engines.analyzer.analyze(
                    text=text, language=lang, entities=list(_PRESIDIO_CATEGORIES)
                )
            )
        keep = [
            result
            for result in results
            if result.score >= _PRESIDIO_SCORE_THRESHOLD
            and not any(result.start < end and start < result.end for start, end in existing)
        ]
        if not keep:
            return text, {}
        # The anonymizer resolves overlaps between the two language passes, so
        # a name both models found is replaced (and counted) exactly once.
        anonymized = engines.anonymizer.anonymize(
            text=text, analyzer_results=keep, operators=engines.operators
        )
        counts = Counter(
            _PRESIDIO_CATEGORIES[item.entity_type] for item in anonymized.items
        )
    except Exception as exc:  # noqa: BLE001 - any failure degrades to pass 1
        logger.warning("pii_presidio_failed", error=str(exc))
        return text, {}
    return anonymized.text, dict(counts)


def redact_for_llm(text: str, language: str | None = None) -> tuple[str, dict[str, int]]:
    """Redact `text` before it is embedded in a prompt bound for the LLM
    provider. The one call every agent node uses - see the module docstring
    for the boundary this function draws, the two passes it runs and which
    nodes call it (`agents/routing.py`, `agents/coordinator.py`,
    `agents/appointment.py`, `agents/document.py`).

    `language` is the patient's `preferred_language` ("en" or "de") when the
    caller has it. Anything else, including None, lets the function decide
    from the text.
    """
    redacted, counts = _redactor.redact(text)
    if not redacted.strip():
        return redacted, counts

    redacted, ner_counts = _presidio_pass(redacted, language)
    for category, found in ner_counts.items():
        counts[category] = counts.get(category, 0) + found
    return redacted, counts
