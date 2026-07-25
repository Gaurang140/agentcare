"""Document agent: classifies any upload still typed "other", then reports
which of the department's required document types are on file.

Owns app.tools.document_tools. Classification reads the already-extracted
text persisted on the row at upload time (document_tools.store_document
already ran extract_text and truncated it to 1500 chars) - there is no raw
file content to re-extract from at this point in the workflow, only the DB
row, which is the real-DB-work the tool layer already did.

Extracted text comes from a file a patient uploaded, so it is screened by
the prompt-injection guard (Task F1) before it goes into the classification
prompt. A poisoned document is left typed "other" and its own audit event is
written, but it never kills the run - the rest of the uploaded documents
still get classified and the workflow continues.

Whatever survives the injection guard is then redacted (Task F2,
`safety/pii.py::redact_for_llm`) before it reaches the classification
prompt, since extracted text is still patient-submitted content on its way
to the LLM provider. Counts are summed across every document processed in
one `run()` call and reported in a single "safety.pii_redacted" audit row,
not one row per document.

The filename goes through the same two gates as the body, and for the same
reason: it is a string the uploading client chose and it lands in the
classification prompt. It is normalized first (invisible characters out,
whitespace collapsed, capped), then screened, then redacted along with the
body in one pass over the assembled prompt (see `_redacted_prompt` for why
the two are not redacted separately, and `_document_language` for the one
decision the merge is not allowed to make). A filename that the guard blocks
leaves the document typed "other" and writes the same
"safety.injection_blocked_document" row the body path writes, with a
`"field": "filename"` marker so a reader can tell the two apart. Only the
copy in the prompt changes; `PatientDocument.filename` keeps the original.

The body and the two filename readings are screened as one group
(`screen_injection_group`), not one call each: the deterministic layer still
reads every one of them separately, so a block still names the poisoned part,
while the optional classifier reads them joined and a document costs one
round trip to it instead of three. That classifier copy is redacted too, so
it gets the same pinned language the classification prompt gets - the merge
is not allowed to decide the language on either path.

That pinned language comes from the patient's own profile row
(`agents/responses.py::patient_language`) whenever the clinic has one on
file, and from the document body only as the fallback. The body's cues are a
guess about the text; the profile is what the clinic recorded about the
patient, and it still holds for a body too short to carry a cue at all. The
trade-off is measured and accepted: an English report uploaded by a
German-preferring patient is then read with the German model, which
over-redacts a word or two of the English body. Over-redaction costs prompt
quality, not privacy.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agents.prompts import DOCUMENT
from app.agents.llm import chat_json
from app.agents.memory import build_system_prompt
from app.agents.responses import patient_language
from app.agents.state import AgentState
from app.logging_setup import get_logger
from app.models import PatientDocument
from app.safety.injection_guard import InjectionResult, screen_injection_group
from app.safety.pii import _languages_for as _pii_languages_for, redact_for_llm
from app.tools.audit_tools import write_audit
from app.tools.document_tools import check_required_documents

logger = get_logger(__name__)

_UNKNOWN_TYPE = "other"

# Markers for the "field" key of a blocked-document audit row. The body has no
# marker of its own on purpose (see `_blocked_field`).
_FILENAME_FIELD = "filename"
_WHOLE_DOCUMENT_FIELD = "filename_and_text"

# A classification the model is not sure about is a guess, and a wrong
# document_type quietly changes what the department counts as missing. Under
# this threshold the guess stays in the audit trail for staff and never
# reaches the row. This is the document node's own number; routing.py sets a
# different one for a different decision.
_CONFIDENCE_THRESHOLD = 0.6


class DocumentOutput(BaseModel):
    document_type: Literal[
        "ecg_report", "blood_test", "referral_letter", "imaging_report", "insurance_card", "other"
    ]
    confidence: float


def _classification_prompt(filename: str, extracted_text: str) -> str:
    return f"Filename: {filename}\nExtracted text (may be empty): {extracted_text}"


def _document_language(preferred: str | None, extracted_text: str) -> str:
    """The one language every redaction of this document runs with: the
    patient's stored preference when `safety/pii.py` has a model for it, the
    document body's own cues otherwise.

    Both places that redact read the result: the classification prompt
    (`_redacted_prompt`) and the injection guard's own copy for its classifier,
    which merges the same readings and so has the same problem.

    Neither of them may work the language out for itself, because the string
    each is handed is the filename plus the body. That would give the filename
    a vote: one German word in a name, spaced into three by `_spaced_filename`,
    is enough to send an English report to the German model. Measured on
    `befund_der_untersuchung.pdf` over an English body, "Sinus rhythm" and
    "Book the" come back as `[REDACTED_NAME]`, which is the cross-language
    failure `safety/pii.py`'s single-model rule exists to prevent, reached
    through the filename instead of through the body. The body is the text
    that says what language the document is in, so the body is what the
    fallback reads - never the merge.

    Both halves run through the same helper `redact_for_llm` would apply on its
    own, imported rather than copied, so the two can never drift apart and this
    module never restates which languages have a model.
    """
    return _pii_languages_for(extracted_text, preferred)[0]


def _redacted_prompt(
    filename: str, extracted_text: str, language: str
) -> tuple[str, dict[str, int]]:
    """Redact the assembled prompt in one pass, rather than the filename and
    the extracted text separately.

    Same function, same boundary, and it is what actually leaves the process.
    The reason to redact them together is pass 2: NER over a bare filename has
    no context to read, and it guesses. Measured on this stack,
    `insurance.txt` on its own comes back as a location and the document loses
    the strongest classification signal it has, while inside the prompt it
    stays a filename. A patient name in the filename is still caught either
    way.

    The one thing the merge must not decide is the language, so `language` is
    passed in already settled (see `_document_language`).
    """
    return redact_for_llm(
        _classification_prompt(filename, extracted_text), language=language
    )


# --- Filename handling ------------------------------------------------------
# A filename is whatever the uploading client put in the multipart part. It
# can carry control characters, zero-width characters and bidi overrides
# (which hide what the name actually reads as), any amount of whitespace and
# any length, so it is normalized before anything reads it.
# Control characters (tab, newline and the rest of C0, plus DEL) become a
# space: they separate what is around them, so deleting them would glue two
# words into one the pattern layer no longer recognizes.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
# Zero-width and bidi characters are deleted instead. They carry no width at
# all, so they are the ones used to break a word up ("ig<zwsp>nore") or to
# reverse how a name reads.
_ZERO_WIDTH_RE = re.compile(
    "[\u200b-\u200f"  # zero-width space/joiners and directional marks
    "\u202a-\u202e\u2066-\u2069"  # bidi overrides and isolates
    "\u2060-\u2064\ufeff]"  # word joiner, invisible operators, BOM
)
_WHITESPACE_RE = re.compile(r"\s+")
# "_" and "-" are how filenames separate words.
_SEPARATOR_RE = re.compile(r"[_-]+")
# Long enough for any real document name, short enough that a filename can
# never be the bulk of the prompt.
_MAX_FILENAME_CHARS = 120


def _normalize_filename(filename: str) -> str:
    cleaned = _CONTROL_RE.sub(" ", _ZERO_WIDTH_RE.sub("", filename or ""))
    return _WHITESPACE_RE.sub(" ", cleaned).strip()[:_MAX_FILENAME_CHARS]


def _spaced_filename(normalized: str) -> str:
    """The filename read as words: `ignore_previous_instructions.txt` becomes
    `ignore previous instructions.txt`.

    Both the guard's pattern layer and the PII redactor work on word
    boundaries, and "_" and "-" are word characters, so neither of them sees
    anything at all in the packed form a real filename comes in as.
    """
    return _WHITESPACE_RE.sub(" ", _SEPARATOR_RE.sub(" ", normalized)).strip()


def _document_readings(extracted_text: str, normalized: str, spaced: str) -> list[str]:
    """Everything one document contributes to its prompt, in screening order.

    The body comes first, so a poisoned body still blocks on the body and
    keeps the unmarked audit payload it had before filenames were screened at
    all. Then the literal filename, then its separator-spaced reading, since
    neither of those covers the other: the spaced one is what makes
    `ignore_previous_instructions.txt` match a pattern, and the literal one is
    what still carries a chat-template token like `<|im_start|>`, which the
    spaced reading breaks apart. The spaced reading is dropped when it is the
    same string, which is the common case (`impfpass.pdf`).
    """
    readings = [extracted_text, normalized]
    if spaced != normalized:
        readings.append(spaced)
    return readings


def _blocked_field(reading_index: int | None) -> str | None:
    """Which part of the document a block names, from the reading the guard
    blocked on.

    Reading 0 is the body, and a body block carries no marker at all - that
    absence is what an existing audit consumer reads. The rest are the
    filename. `None` comes back from a layer 2 block, which judged every
    reading as one text and so names no single part; it gets its own marker
    rather than silently reading as a body block.
    """
    if reading_index is None:
        return _WHOLE_DOCUMENT_FIELD
    return None if reading_index == 0 else _FILENAME_FIELD


def _block_document(
    db: Session, doc: PatientDocument, injection: InjectionResult, field: str | None = None
) -> None:
    """Leave the document typed "other" and record why. `field` names the
    poisoned part; the body path passes none, so its payload is unchanged."""
    doc.document_type = _UNKNOWN_TYPE
    db.flush()
    payload = {"matched": injection.matched, "via": injection.via}
    if field is not None:
        payload["field"] = field
    write_audit(
        db, None, "safety.injection_blocked_document", "patient_document", doc.id, payload
    )


def _exit_audit(db: Session, workflow_id: int | None, summary: dict) -> None:
    write_audit(db, None, "agent.document.completed", "workflow_run", workflow_id, summary)
    db.commit()


def _merge_counts(total: dict[str, int], counts: dict[str, int]) -> None:
    for category, count in counts.items():
        total[category] = total.get(category, 0) + count


def _audit_pii_redacted(db: Session, workflow_id: int | None, counts: dict[str, int]) -> None:
    """One audit row per `run()` call, aggregating counts across every
    document processed - never one row per document."""
    if counts:
        write_audit(
            db,
            None,
            "safety.pii_redacted",
            "workflow_run",
            workflow_id,
            {"node": "document", "counts": counts},
        )


def run(state: AgentState, db: Session) -> dict:
    """Classify unknown-type uploads, then report required-document coverage."""
    workflow_id = state.get("workflow_id")
    try:
        patient_id = state["patient_id"]
        doc_ids = state.get("uploaded_document_ids") or []
        system = build_system_prompt(db, "document", DOCUMENT)
        # One indexed read of the profile row per run(), not one per document.
        preferred_language = patient_language(db, patient_id)

        classified: list[dict] = []
        pii_counts: dict[str, int] = {}
        for doc_id in doc_ids:
            doc = db.get(PatientDocument, doc_id)
            if doc is None or doc.document_type != _UNKNOWN_TYPE:
                continue

            extracted_text = doc.extracted_text or ""
            normalized_name = _normalize_filename(doc.filename)
            spaced_name = _spaced_filename(normalized_name)
            language = _document_language(preferred_language, extracted_text)
            injection, blocked_reading = screen_injection_group(
                _document_readings(extracted_text, normalized_name, spaced_name),
                language=language,
            )
            if injection.action == "block":
                _block_document(db, doc, injection, field=_blocked_field(blocked_reading))
                continue

            prompt, counts = _redacted_prompt(spaced_name, extracted_text, language)
            _merge_counts(pii_counts, counts)
            result = chat_json(system, prompt, DocumentOutput)
            if result.confidence < _CONFIDENCE_THRESHOLD:
                write_audit(
                    db,
                    None,
                    "document.low_confidence",
                    "patient_document",
                    doc.id,
                    {"confidence": result.confidence, "suggested": result.document_type},
                )
                continue

            doc.document_type = result.document_type
            db.flush()
            classified.append(
                {"id": doc.id, "document_type": result.document_type, "confidence": result.confidence}
            )
        _audit_pii_redacted(db, workflow_id, pii_counts)
        db.commit()

        department_id = state.get("department_id")
        if department_id is not None:
            documents_result = check_required_documents(db, patient_id, department_id)
        else:
            documents_result = {"required": [], "present": [], "missing": []}
        documents_result["classified"] = classified

        update = {"documents_result": documents_result, "completed_steps": ["document"]}
        _exit_audit(db, workflow_id, {"classified_count": len(classified)})
        return update
    except Exception as exc:  # noqa: BLE001 - node boundary must never crash the graph
        logger.error("document_agent_failed", workflow_id=workflow_id, error=str(exc))
        db.rollback()
        _exit_audit(db, workflow_id, {"error": str(exc)})
        return {"error": f"document agent failed: {exc}", "completed_steps": ["document"]}
