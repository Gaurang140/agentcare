# AgentCare security

AgentCare handles hospital administration only. It supports registration,
routing, appointment work, document coordination, reminders and follow-up.
Diagnosis, prescribing, dosage and treatment decisions remain with clinicians.

This document owns trust boundaries, enforced controls and known limitations.
It does not repeat setup or deployment commands.

## Security model

AgentCare assumes all patient text, uploads, filenames and staff-authored agent
rules can be hostile. It also assumes model output can be wrong or unsafe.

Controls surround model work:

1. deterministic request-scope decisions
2. deterministic injection screening
3. optional provider screening
4. per-call PII protection at the model boundary
5. structured-output validation
6. deterministic output sanitizing
7. persisted audit and human escalation

No prompt is treated as an authorization or safety control.

## Trust boundaries

| Boundary | Untrusted side | Enforced control |
|---|---|---|
| Browser to API | Cookies, route parameters and form data | Backend authentication, RBAC and ownership checks |
| Upload to storage | Name, extension, size and bytes | Group validation, filename sanitizing, size/type allowlist and checksum |
| Request to graph | Patient text | Emergency, medical-scope and injection gates |
| Application to model | Patient-derived prompt content | Injection clearance, PII redaction and structured schemas |
| Model to application | Generated fields and prose | Pydantic validation, transition guards and output sanitizer |
| Agent to domain data | Tool arguments and repeated execution | Transactions, conditional updates, idempotency checks and audit |
| Staff to paused graph | Approval and note | Staff RBAC, persisted decision claim and same-thread resume |
| Runtime to GCP | Backend pod identity and VPC traffic | Workload Identity, scoped IAM, regional PSC endpoint and private DNS |

The last row is configured but not live-verified. The deployment runbook lists
the operator checks required before trusting the binding.

## Deterministic healthcare boundary

`backend/app/safety/guardrails.py::screen_request` runs inside
`workflow_service.create_run` before graph or model work.

Emergency phrases return localized emergency guidance and create an emergency
escalation. Medical-advice requests return a refusal and an offer of
administrative help. These decisions also win when a request mixes valid
administration with diagnosis or prescription language.

The screen reads normalized and confusable-folded text in English and German.
It is deterministic, so a model cannot override the result. Emergency and
medical-refusal paths make zero model calls.

`sanitize_agent_output` owns the last healthcare check. It normalizes the
candidate response and replaces any sentence that states a diagnosis,
recommends treatment or gives a dosage. The result is fixed referral text.
This runs after model review and after any Model Armor response verdict.

## Prompt injection controls

Request text is screened before graph execution. Document text and normalized
filename readings are screened before the document role includes them in a
prompt.

### Deterministic layer

The always-on layer checks English and German instruction-overrides,
model-role markers, long base64-like runs and related control patterns. It
also checks confusable-folded readings.

A deterministic match is fail-closed for model work. The request is blocked,
a safety escalation is recorded and the suspicious text is not sent to a
model. A blocked document remains classified as `other` and the workflow
continues without trusting its content.

### Optional provider slot

Exactly one provider can occupy the optional second layer:

- Model Armor when `MODEL_ARMOR_TEMPLATE` is configured
- the compatible injection classifier otherwise, when its model and endpoint
  are available

Model Armor takes precedence if both are configured. The provider receives a
PII-redacted copy. A group of document readings is joined for one provider
call rather than one call per reading.

The GCP deployment creates a regional Private Service Connect endpoint for
`modelarmor.REGION.rep.googleapis.com` and an apex private DNS record in the
cluster VPC. Google requires that path for regional Model Armor calls from a
VPC. The endpoint, DNS zone and template are disabled together when
`enable_model_armor=false`.

Model Armor uses one bounded attempt with SDK retries disabled. A positive
verdict blocks. An unavailable client, timeout, incomplete no-opinion result
or unreadable response fails open to the already-completed deterministic
layer. It logs the provider failure without blocking ordinary administration.

The response-side Model Armor call uses the same bounded behavior. A positive
verdict replaces the draft with referral text and writes
`safety.model_armor_blocked`. A provider failure passes the draft to the
deterministic output sanitizer, which still owns the final decision.

This fail-open behavior prevents an optional network service from stopping all
patient administration. It does not bypass either deterministic layer.

## PII at the model boundary

The domain database keeps the original request and extracted document text.
That original is needed for the patient record and audit context. It remains
sensitive stored data.

Redaction applies to the copy prepared for a model call, not to the stored row
and not to a global graph input. Nodes that embed patient-submitted content
call `redact_for_llm` at their own boundary.

The redactor has two passes:

- deterministic patterns for email, phone, IBAN, German health insurance
  number and date-of-birth shapes
- Presidio with the selected English or German spaCy model for supported
  person and location spans

Typed replacement tokens preserve the kind of information without the value.
Overlapping spans are removed so a deterministic token is not redacted again.
Language selection uses text cues and the stored patient preference.

If Presidio initialization or analysis fails, the system logs one warning and
keeps the deterministic pattern result. It does not send the raw pre-redaction
copy as a fallback.

When redaction occurs, the node writes `safety.pii_redacted` with category
counts only. Raw values are not placed in that audit context.

Model-bound finalization content is composed from selected persisted facts
rather than embedding the patient's raw request. The patient-facing stored
response is not replaced by redaction tokens.

## Structured model output

All agent schemas pass through `app/agents/llm.py::invoke_structured`.

LangChain `with_structured_output` handles provider-specific request formatting
and ordinary Pydantic parsing. AgentCare adds:

- bounded transport retries
- strict-schema to JSON-mode compatibility
- one corrective prompt after validation failure
- one optional fallback-model attempt
- application-specific configuration and output errors

An agent does not regex-extract JSON from prose. A persistent model failure
becomes an agent error, and graph guards send that run to deterministic human
escalation instead of inventing domain output.

## Authentication and authorization

Passwords use `pwdlib.PasswordHash.recommended()`, currently Argon2id.
Sessions are HS256 JWTs in an httpOnly cookie. Token decoding pins HS256
instead of accepting an algorithm from the token header.

Cookies use `SameSite=Lax`. The `secure` flag is enabled outside development.
The browser does not store the token in `localStorage`.

Backend dependencies are the sole authorization truth:

- `get_current_user` validates the session
- `require_role("staff")` protects staff routes
- `ensure_owner_or_staff` protects patient-owned records
- `require_internal_or_staff` protects the reminder trigger

Frontend route checks only improve navigation. A visible or hidden frontend
control never grants access.

Staff agent-rule changes are RBAC-protected and audited. Rules are deactivated
rather than silently deleted, preserving who changed model procedure.

## Upload controls

The request route validates all supplied files before storing any of them.
One invalid file therefore prevents a partial multi-file upload.

Current restrictions are:

- extensions limited to PDF, PNG, JPG, JPEG and TXT
- maximum size of 10 MB per file
- directory components removed from filenames
- remaining names normalized to a safe character set
- content deduplicated by SHA-256 checksum per patient

The storage adapter writes locally by default and can write to GCS when
configured. The GCS runtime role is bucket-scoped
`roles/storage.objectCreator`; UUID object names make create-only access
sufficient. Database rows keep metadata and storage references.

Extension and size checks do not prove a file is benign. See known
limitations.

## Transactions, replay and audit

Domain mutations use SQL transactions. `write_audit` flushes in the caller's
transaction rather than committing on its own, so the action and event commit
or roll back together.

Appointment slot claims use a conditional update against `status = 'free'`.
Concurrent callers cannot both claim one slot. Booking, rescheduling,
cancellation, reminder batches, staff decisions and workflow resumes include
replay or idempotency checks because LangGraph can restart a node.

`AuditEvent` rows record actor, action, entity identity, timestamp and bounded
context. Agent exits, tool mutations, registration and staff approvals are
covered. The staff audit view and patient SSE timeline read from this table.

An escalation stores severity, status, reviewer, note and resolution time.
The decision is persisted before the graph resumes. Resume uses the original
`thread_id`; a new thread cannot impersonate continuation of the paused run.

## Secrets and logs

Application configuration comes from environment variables. `.env` is
gitignored. Example configuration contains no credential value.

The logging processor recursively redacts values whose keys contain
`password`, `token`, `authorization`, `api_key` or `secret`. Audit contexts are
designed to store identifiers and category counts rather than request secrets
or redacted PII values.

The GCP design separates configuration from credentials. Pods read one
Kubernetes Secret. Terraform declares the backend KSA-to-GSA binding and the
GCP overlay declares the KSA annotation. Cloud SQL rejects unencrypted
connections, and the documented direct DSN requests TLS. That DSN does not
verify server identity. Repository configuration alone does not prove the
runtime identity, private DNS or TLS path in a live cluster.

## Known limitations

- This project uses synthetic data and has not been assessed or certified for
  production healthcare regulation.
- The PII redactor covers named patterns and small English/German NER models.
  It cannot guarantee detection of every identifier or free-text disclosure.
- Original patient text remains in the domain database. Database access,
  backup encryption, retention and deletion policy require an operator.
- Model Armor, Vertex authentication and the complete GCP runtime have not
  been live-verified.
- Model Armor fails open to deterministic controls. This preserves
  availability but removes provider screening during an outage.
- Upload validation does not include malware scanning, PDF active-content
  inspection or content-type sniffing.
- The append-only audit contract is enforced by application behavior, not a
  write-once storage system or database trigger.
- HS256 session signing depends on protecting and rotating one shared secret.
- Graph dispatch uses process-local background work. A process loss can leave
  a run pending until checkpoint resume or the stall sweep hands it to staff.
- The backend is intentionally single-replica while APScheduler jobs remain
  in-process without a distributed lock.

These constraints must be resolved before real patient data or public
production traffic is considered.
