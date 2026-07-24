"""PII redaction at the LLM boundary.

Boundary rule: the database always keeps the original, unredacted text a
patient submitted (`WorkflowRun.request_text`, `PatientDocument.extracted_text`).
Redaction in this module applies ONLY to a copy of that text on its way into
an LLM prompt - never to what is stored, never to what is shown back to the
patient. `agents/safety.py` composes the patient-facing `final_response` from
freshly queried database rows, not from patient-submitted text, so it never
calls into this module (see that node's own docstring).

`PIIRedactor.redact` finds five categories of PII with battle-tested regex
families (each documented at its definition below) and replaces every match
with a fixed `[REDACTED_<CATEGORY>]` token, so the shape of the text survives
for the LLM (still a sentence about an appointment) while the sensitive value
does not. `redact_for_llm` is the one function every call site should import:
a thin wrapper around a shared, compiled `PIIRedactor` instance, so the whole
redaction rule (categories, tokens, ordering) lives in this one file.
"""

from __future__ import annotations

import re

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


def redact_for_llm(text: str) -> tuple[str, dict[str, int]]:
    """Redact `text` before it is embedded in a prompt bound for the LLM
    provider. The one call every agent node uses - see the module docstring
    for the boundary this function draws and which nodes call it
    (`agents/routing.py`, `agents/coordinator.py`, `agents/appointment.py`,
    `agents/document.py`).
    """
    return _redactor.redact(text)
