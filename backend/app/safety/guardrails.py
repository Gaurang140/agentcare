"""Deterministic, pre-LLM safety screening for the patient agent.

Pure functions only: no LLM calls, no DB access. Cheap enough to run on
every inbound request before it reaches the agent (`screen_request`) and on
every agent-generated response before it reaches the patient
(`sanitize_agent_output`). AgentCare handles administration only, so both
directions are guarded: incoming requests that ask for a diagnosis or
signal a medical emergency, and outgoing text that reads like one even if
the agent generated it by accident.

AgentCare serves a German clinic, so every keyword list carries English and
German phrasing.

Matching strategy: every keyword/phrase below is matched as a whole word or
whole phrase via `\\b` regex boundaries, case-insensitive, never a plain
substring and never a stemmed prefix. That is a deliberate choice: "diagnose"
and "diagnosis" must trigger a refusal, but administrative language that
merely starts with similar letters, e.g. "the diagnostics department is on
the second floor", must not. We don't need special-case logic to keep that
safe: "diagnostics" is not a substring of "diagnose" or "diagnosis" at all
(they diverge right after "diagno-": "...ose"/"...osis" vs "...stics"), so
the literal word-boundary match leaves it alone by construction. The `\\b`
wrapping exists mainly to guard future keyword additions against the same
kind of accidental prefix collision.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

Action = Literal["allow", "escalate_emergency", "refuse_medical"]


@dataclass
class ScreenResult:
    action: Action
    reason: str
    matched: list[str]


# --- Messages shown to the patient ------------------------------------------
# Administrative signposting only, no diagnosis, prescription, or dosage
# wording, per the repo-wide safety boundary.

EMERGENCY_GUIDANCE = (
    "This looks urgent. Please call 112 (Germany) or your local emergency "
    "number now. Hospital staff have been notified."
)

MEDICAL_REFUSAL = (
    "I'm not able to give medical advice. Please speak with a clinician "
    "about that. I can help you book an appointment instead."
)

SANITIZED_SENTENCE = "For medical questions, please speak with your care team."


# --- Keyword lists (EN + DE) -------------------------------------------------
# The brief's exact lists, plus a small number of sensible synonyms (each
# addition commented so it's clear what's brief-mandated vs. extended).

EMERGENCY_KEYWORDS = [
    "chest pain",
    "brustschmerz",
    "brustschmerzen",  # extension: DE plural, the natural patient phrasing
    "heart attack",
    "herzinfarkt",
    "stroke",
    "schlaganfall",
    "can't breathe",
    "cannot breathe",  # extension: spelled-out variant of "can't breathe"
    "atemnot",
    "unconscious",
    "bewusstlos",
    "suicide",
    "suizid",
    "selbstmord",
    "severe bleeding",
    "starke blutung",
    "starke blutungen",  # extension: DE plural form
    "overdose",
    "überdosis",
    "cardiac arrest",  # extension: distinct clinical event from "heart attack"
    "herzstillstand",  # extension: DE for "cardiac arrest"
]

MEDICAL_ADVICE_KEYWORDS = [
    "diagnose",
    "diagnosis",
    "was habe ich",
    "what do i have",
    "which medicine",
    "welches medikament",
    "prescribe",
    "verschreiben",
    "dosage",
    "dosierung",
    "is it cancer",
    "ist es krebs",
    "what medication should i take",  # extension: EN synonym of "which medicine"
    "was soll ich einnehmen",  # extension: DE synonym ("what should I take")
]

# Regexes (not plain keywords): each one describes a shape of sentence that
# states a diagnosis, a dosage, or a treatment recommendation outright. A
# leading `\b` keeps them from matching mid-word; the rest is the brief's
# patterns verbatim.
OUTPUT_FORBIDDEN_PATTERNS = [
    r"\byou (probably |likely )?have [a-z]",  # states a diagnosis as fact ("you have X")
    r"\bdiagnosis is\b",  # "the diagnosis is ..."
    r"\btake \d+ ?mg\b",  # explicit dosage instruction, e.g. "take 5mg" / "take 500 mg"
    r"\bi recommend (taking|stopping)\b",  # prescriptive treatment recommendation
    # The same four shapes in German. The clinic is German, so a model can
    # state a diagnosis or a dosage in German just as readily, and until these
    # existed the output sanitizer only guarded one of the two languages the
    # rest of this module handles. The umlauts are spelled into the character
    # class because a German noun can open with one ("Ödem") and `[a-z]` does
    # not cover them.
    r"\bsie haben (wahrscheinlich |vermutlich )?[a-zäöü]",  # states a diagnosis as fact ("Sie haben X")
    r"\bdiagnose (ist|lautet)\b",  # "die Diagnose ist ..." / "Ihre Diagnose lautet ..."
    r"\bnehmen sie \d+ ?mg\b",  # explicit dosage instruction, e.g. "Nehmen Sie 5 mg"
    # prescriptive treatment recommendation
    r"\bich empfehle (die einnahme|das absetzen|einzunehmen|abzusetzen)\b",
]


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Compile a keyword/phrase into a case-insensitive, word-boundary regex.

    Multi-word phrases join their words on `\\s+` so irregular whitespace in
    the input still matches; `\\b` on both ends of the whole phrase stops it
    from matching inside a longer word.
    """
    words = phrase.split()
    body = r"\s+".join(re.escape(word) for word in words)
    return re.compile(rf"\b{body}\b", re.IGNORECASE)


_EMERGENCY_PATTERNS = [(kw, _phrase_pattern(kw)) for kw in EMERGENCY_KEYWORDS]
_MEDICAL_ADVICE_PATTERNS = [
    (kw, _phrase_pattern(kw)) for kw in MEDICAL_ADVICE_KEYWORDS
]
_OUTPUT_FORBIDDEN_COMPILED = [
    re.compile(p, re.IGNORECASE) for p in OUTPUT_FORBIDDEN_PATTERNS
]

# Split on whitespace that follows a sentence-ending punctuation mark, so
# "You have X. Booked." -> ["You have X.", "Booked."] while keeping each
# sentence's own trailing punctuation attached (needed so a replaced
# sentence can be swapped out whole, not surgically edited).
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# Zero-width space, the two joiners and the byte-order mark. They render as
# nothing at all, so a forbidden phrase can be broken across one of them
# ("ha<zwsp>ve arrhythmia") and still read normally to the patient while
# matching none of the patterns above.
_ZERO_WIDTH_RE = re.compile("[\u200b-\u200d\ufeff]")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_output(text: str) -> str:
    """Fold an agent response into one shape before the forbidden patterns
    read it: Unicode NFKC, zero-width characters removed, runs of whitespace
    collapsed to a single space.

    NFKC is what makes a compatibility spelling of a word the same string as
    the plain one, and it also folds the exotic space characters (non-breaking
    space and friends) onto an ordinary space, which the collapse then joins
    with any other run of whitespace.

    Output path only. `screen_request` keeps its own matching semantics, which
    its own tests define; this is a rewrite of what the agent produced, not of
    what the patient wrote.
    """
    folded = unicodedata.normalize("NFKC", text)
    return _WHITESPACE_RE.sub(" ", _ZERO_WIDTH_RE.sub("", folded)).strip()


def _find_matches(text: str, patterns: list[tuple[str, re.Pattern[str]]]) -> list[str]:
    return [keyword for keyword, pattern in patterns if pattern.search(text)]


def screen_request(text: str) -> ScreenResult:
    """Screen an incoming patient request before it reaches the agent.

    Emergency language always outranks a plain medical-advice ask, and
    either outranks an admin-only request (which passes through as
    "allow"). This means a message mixing an admin ask with a medical one
    ("book me an appointment and diagnose my cough") still refuses: the
    medical ask wins.
    """
    emergency_matches = _find_matches(text, _EMERGENCY_PATTERNS)
    if emergency_matches:
        return ScreenResult(
            action="escalate_emergency",
            reason=EMERGENCY_GUIDANCE,
            matched=emergency_matches,
        )

    medical_matches = _find_matches(text, _MEDICAL_ADVICE_PATTERNS)
    if medical_matches:
        return ScreenResult(
            action="refuse_medical",
            reason=MEDICAL_REFUSAL,
            matched=medical_matches,
        )

    return ScreenResult(
        action="allow", reason="no safety concerns detected", matched=[]
    )


def sanitize_agent_output(text: str) -> tuple[str, bool]:
    """Rewrite any sentence in an agent response that reads like medical advice.

    Normalizes the text first (`_normalize_output`), then splits it on
    sentence-ending punctuation, checks each sentence against
    OUTPUT_FORBIDDEN_PATTERNS, and replaces whole offending sentences (never a
    partial substring) with a fixed, safe sentence, so nothing medically
    specific can leak through around the edges of a regex match. Sentences
    with no match are returned untouched, in their normalized form: the
    normalization is what the patterns read, so it is also what ships, and an
    invisible character that survived into a clean sentence is gone either
    way.
    """
    sentences = _SENTENCE_SPLIT_RE.split(_normalize_output(text))
    flagged = False
    rewritten: list[str] = []
    for sentence in sentences:
        if any(pattern.search(sentence) for pattern in _OUTPUT_FORBIDDEN_COMPILED):
            rewritten.append(SANITIZED_SENTENCE)
            flagged = True
        else:
            rewritten.append(sentence)
    return " ".join(rewritten), flagged
