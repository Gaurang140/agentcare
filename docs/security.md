# AgentCare security

## Healthcare boundary

AgentCare handles hospital administration only: registration, department routing, appointment
booking, document coordination, reminders and follow-up. It never diagnoses, never prescribes and
never doses. That boundary is enforced in code, not left to prompt wording, and it is the first
thing every safety control below is built to protect.

## The three safety layers

Safety is defense in depth. Two of the three layers are deterministic and cannot be talked out of
their decision by a model.

1. **Deterministic pre-screen** (`backend/app/safety/guardrails.py::screen_request`). Runs in
   `workflow_service.create_run` before any graph node or LLM call. It matches the inbound request
   against English and German keyword lists using whole-word regex boundaries. An emergency phrase
   (chest pain, Herzinfarkt, suicide and so on) returns `escalate_emergency` with 112 guidance and
   opens an `emergency` escalation. A medical-advice ask (diagnose, Dosierung, "is it cancer") returns
   `refuse_medical`. Both outrank an administrative request, so "book me an appointment and diagnose
   my cough" still refuses. An emergency or refusal is terminal here, with no LLM call at all.

2. **Safety agent review** (`backend/app/agents/safety.py`). The `safety_finalize` node composes the
   patient-facing answer from freshly re-queried database rows (never the possibly-stale in-memory
   state), then asks the LLM reviewer for `SafetyOutput{safe, violations, rewritten}`. If the LLM call
   fails, finalize does not block: it falls back to the deterministic layer below and still ships a
   safe answer.

3. **Output sanitizer** (`backend/app/safety/guardrails.py::sanitize_agent_output`). The last word.
   It splits the candidate response into sentences and replaces any whole sentence that matches a
   forbidden pattern (states a diagnosis, an explicit dosage like "take 5mg" or a treatment
   recommendation) with a fixed referral to the care team. Because it swaps whole sentences, nothing
   medically specific leaks around the edge of a regex match. A model that claims `safe: true` over a
   poisoned sentence does not get to publish it.

## RBAC

Access control is backend-only truth (`backend/app/auth/dependencies.py`).

- `get_current_user` resolves the caller from the httpOnly `access_token` cookie and raises
  `PermissionDeniedError` on any auth failure: a missing cookie, a bad token (unparseable, expired or
  mis-signed) or a subject that no longer maps to a user. The app keeps a single 403 permission class
  on purpose rather than leaking a 401-versus-403 distinction.
- `require_role("staff")` gates every staff route.
- `ensure_owner_or_staff(user, patient_id, db)` guards every patient-data query: staff pass, a patient
  passes only for their own `patient_id`. It takes `db` even though it does not use it today, so a
  later join-based ownership check keeps the same signature.
- `require_internal_or_staff` lets the cron-style reminder endpoint accept an `X-Internal-Token`
  header (when `INTERNAL_TASK_TOKEN` is set) instead of a staff cookie, falling back to the staff
  check by default.

The frontend `proxy.ts` redirects a cookie-less browser away from `/portal/*` and `/staff/*`, but it
only checks that the cookie exists, cannot read its contents and proves nothing about role or
validity. It is UX, never security. No authorization logic lives there that the backend does not also
enforce.

## Authentication

`backend/app/auth/security.py`.

- Passwords are hashed with `pwdlib.PasswordHash.recommended()`, which is Argon2id, replacing the
  unmaintained passlib.
- Sessions are HS256 JWTs (PyJWT) carrying the user id and role. `decode_token` hardcodes
  `algorithms=["HS256"]` and never reads the algorithm from the token header, which blocks
  algorithm-confusion attacks.
- The token is delivered as an httpOnly cookie set at login: `httponly=True`, `samesite="lax"`, and
  `secure` on outside development. The browser never handles the token in JavaScript, and the API
  client sends it same-origin with `credentials: "include"`, so there is no token in `localStorage`.

## Upload validation

`backend/app/api/routes_workflows.py` and `backend/app/services/storage.py`.

- Every file is validated before any file is stored, so one rejected file in a multi-file upload never
  leaves a partial set behind. The allowlist is `.pdf`, `.png`, `.jpg`, `.jpeg` and `.txt`, and the
  size cap is 10 MB.
- Filenames are sanitized before they touch disk or an object key: `Path(...).name` drops directory
  components from either separator style, then a character whitelist collapses anything else, so no
  `/`, `\` or `..` traversal can survive.
- Content is deduplicated by SHA-256 checksum per patient, so re-uploading the same bytes does not
  write them again.

## Secrets and PII

- Configuration is environment-only through pydantic-settings. `.env` is gitignored, `.env.example`
  documents every key with no real values, and `.env`, `*.db` and `uploads/` are never committed.
- The structlog setup (`backend/app/logging_setup.py`) runs a `redact_processor` that recursively
  replaces the value of any key containing `password`, `token`, `authorization`, `api_key` or
  `secret` with `[redacted]`, so secrets never reach a log line.
- On GCP, secrets move to Secret Manager and the CI service account is keyless through Workload
  Identity Federation (see `docs/decisions.md`, ADR-12).
- PII posture: seed data is obviously synthetic, the app carries no real patient data and the
  administrative-only boundary means the system never records or emits clinical judgments about a
  person. The redacting logger and the single-identity `patient_id = users.id` design keep the
  personal surface small and auditable.

## Audit trail

`backend/app/tools/audit_tools.py` and `backend/app/models/audit.py`. Every tool mutation, agent node
exit and mutating route writes one `AuditEvent` row (actor, action, entity type, entity id, JSON
context, timestamp). `write_audit` flushes but never commits, so the audit row lands in the same
transaction as the change it documents and cannot drift from it. The staff audit view
(`GET /api/staff/audit`) and the live SSE timeline both read from this table, so an agent's actions
are reconstructable after the fact and watchable while they happen.

## Escalation and human approval

`backend/app/tools/escalation_tools.py` and `backend/app/api/routes_staff.py`. When the system is
uncertain or unsafe, it hands the case to a human rather than guessing. `create_escalation` opens a
row with a severity of `emergency`, `safety`, `uncertainty` or `agent_failure` and status `open`.
Escalations are raised by the pre-screen (emergency), by the routing agent below its 0.7 confidence
threshold, by any specialist that fails and by the last-resort wrapper if the graph itself crashes.
Staff review the queue at `GET /api/staff/escalations` and record a decision through
`POST /api/staff/escalations/{id}/resolve`, which sets the status to `approved` or `rejected`, stamps
the reviewing staff id and note, then writes its own audit event. A stalled workflow that no process
finished is swept into an `agent_failure` escalation after 30 minutes, so nothing sits silently stuck.
