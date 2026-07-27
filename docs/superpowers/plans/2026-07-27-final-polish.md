# AgentCare Final Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `subagent-driven-development` to execute this plan task by task, with an
> independent review after each task.

**Goal:** Deliver a concise, truthful, production-shaped hackathon repository
that removes confirmed dead code and stale documentation while preserving
AgentCare's verified safety, persistence, interrupt/resume, and transaction
behavior.

**Architecture:** Keep the existing synchronous SQLAlchemy and LangGraph
transaction core. Consolidate repeated agent-boundary behavior behind a focused
`app.agents.support` module, keep model selection in the existing LangChain
factory plus `llm.yaml`, and make the README and detailed docs describe the
runtime as implemented. Use native async only where the code already performs
native asynchronous work: lifespan, request middleware, and SSE delivery.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2, Alembic, LangChain 1.3,
LangGraph 1.2, Pydantic 2, pytest, Ruff, Next.js 16, React 19, TypeScript,
Tailwind CSS, GCP Model Armor/GCS/GKE/Cloud SQL, OpenTofu, and Kustomize.

## Global Constraints

- GCP is the only cloud deployment target.
- Do not convert synchronous SQLAlchemy, LangGraph, or scheduler code into
  `async def` unless every operation in that path has a native asynchronous
  implementation and the behavior is covered end to end.
- Preserve emergency and medical-scope gates, injection screening, model-bound
  PII redaction, RBAC, replay idempotency, checkpoint recovery, and
  `interrupt()`/same-thread resume behavior.
- Do not add an agent framework, guardrail framework, queue, cache, state
  library, or a second Groq integration.
- Keep `langchain-openai` for the verified Groq OpenAI-compatible path. Add
  `langchain-google-genai==4.2.7` only for the committed Vertex AI profile.
- Delete confirmed junk permanently. Do not create `archive`, `legacy`,
  `attic`, or similar repository folders.
- Preserve comments that explain safety, concurrency, transaction, or recovery
  invariants. Remove task-history narration and comments contradicted by the
  installed dependencies.
- Do not claim a live GCP deployment, a live Vertex AI call, or measured model
  quality that has not actually been verified.
- All shell commands in this repository begin with `rtk`.
- Use `apply_patch` for edits and deletions. Each task ends in a focused commit
  and a clean working tree.

---

## Task 1: Remove backend dead code and close the registration audit gap

**Files:**

- Modify: `requirements.txt`
- Modify: `backend/app/api/routes_auth.py`
- Modify: `backend/app/auth/dependencies.py`
- Modify: `backend/app/api/routes_documents.py`
- Modify: `backend/app/api/routes_events.py`
- Modify: `backend/app/api/routes_patient.py`
- Modify: `backend/app/api/routes_workflows.py`
- Modify: `backend/app/services/workflow_service.py`
- Modify: `backend/app/exceptions.py`
- Modify: `backend/app/agents/followup.py`
- Modify: `backend/app/agents/safety.py`
- Modify: `backend/app/tools/appointment_tools.py`
- Modify: `backend/app/tools/followup_tools.py`
- Modify: `backend/tests/test_auth.py`
- Modify: `backend/tests/test_audit.py`
- Modify: `backend/tests/test_booking.py`
- Delete: `backend/app/tools/patient_tools.py`

### Step 1: Write the failing registration audit test

At the top of `backend/tests/test_auth.py`, import `AuditEvent`:

```python
from app.models import AuditEvent
```

Add this behavior test:

```python
def test_registration_writes_audit_event(client, db_session):
    response = client.post(
        "/api/auth/register",
        json={
            "name": "Audit Patient",
            "email": "registration-audit@example.com",
            "password": "s3cret-pw-123",
            "dob": "1991-04-12",
            "phone": None,
            "preferred_language": "en",
            "emergency_contact": None,
        },
    )

    assert response.status_code == 201, response.text
    row = db_session.query(AuditEvent).filter_by(action="user.registered").one()
    assert row.actor_id == response.json()["id"]
    assert row.entity_type == "user"
    assert row.entity_id == response.json()["id"]
    assert row.metadata_json == {"role": "patient"}
```

### Step 2: Run the focused test and record RED

From `backend/`:

```bash
rtk "../../../.venv/bin/python" -m pytest tests/test_auth.py::test_registration_writes_audit_event -q
```

Expected: fail because registration currently creates no `user.registered`
row. Record the command and relevant failure in the implementation report.

### Step 3: Write the audit event in the existing transaction

In `backend/app/api/routes_auth.py`, import `write_audit`. After adding the
`PatientProfile` and before `db.commit()`, write:

```python
write_audit(
    db,
    user.id,
    "user.registered",
    "user",
    user.id,
    {"role": user.role},
)
```

Do not add a second commit. The user, profile, and audit row must succeed or
roll back together under the existing `IntegrityError` handler.

### Step 4: Run the focused test and record GREEN

From `backend/`:

```bash
rtk "../../../.venv/bin/python" -m pytest tests/test_auth.py::test_registration_writes_audit_event -q
```

Expected: one passing test with no new warning.

### Step 5: Remove backend code proven to have no runtime consumer

Perform repository-wide reference checks before each deletion, then:

- delete `backend/app/tools/patient_tools.py`;
- delete its self-contained test in `backend/tests/test_booking.py`;
- delete `create_followup_task` from `backend/app/tools/followup_tools.py`, its
  import and self-contained test from `backend/tests/test_audit.py`, and any
  imports made unused by that removal;
- remove `SafetyBlockedError`;
- remove stale comments in `agents/followup.py`, `agents/safety.py`, and
  `appointment_tools.py` that name the deleted helpers;
- remove `pytest-asyncio==1.4.0` from `requirements.txt`.

Do not remove reminder batching, deterministic safety exceptions, or any test
that covers a runtime path.

### Step 6: Remove unused arguments without changing behavior

Change:

```python
def ensure_owner_or_staff(user: User, patient_id: int, db: Session) -> None:
```

to:

```python
def ensure_owner_or_staff(user: User, patient_id: int) -> None:
```

Remove the now-unused SQLAlchemy `Session` import from
`backend/app/auth/dependencies.py` only if no other function needs it. Update
all seven route call sites.

Remove `document_ids` from `workflow_service.create_run` and its three callers.
Keep `document_ids` on the subsequent `execute_workflow` call; document IDs
still belong in graph execution even though they are not needed to construct
the database row.

### Step 7: Verify the task

From `backend/`:

```bash
rtk "../../../.venv/bin/python" -m pytest tests/test_auth.py tests/test_audit.py tests/test_booking.py tests/test_routes_workflows.py tests/test_injection_guard.py tests/test_sse_events.py -q
rtk "../../../.venv/bin/ruff" check app tests
rtk "../../../.venv/bin/python" -m pytest -q
```

Expected: all tests pass; the full-suite count is lower only by the two
self-contained tests deleted with their unused helpers.

### Step 8: Commit

```bash
rtk git add requirements.txt backend
rtk git commit -m "clean backend boundaries and audit registration"
```

---

## Task 2: Consolidate repeated backend boundaries

**Files:**

- Create: `backend/app/agents/support.py`
- Create: `backend/tests/test_agent_support.py`
- Modify: `backend/app/agents/appointment.py`
- Modify: `backend/app/agents/coordinator.py`
- Modify: `backend/app/agents/document.py`
- Modify: `backend/app/agents/followup.py`
- Modify: `backend/app/agents/routing.py`
- Modify: `backend/app/agents/safety.py`
- Modify: `backend/app/schemas/staff.py`
- Modify: `backend/app/api/routes_workflows.py`
- Modify: `backend/app/api/routes_staff.py`
- Modify: `backend/app/tools/followup_tools.py`
- Modify existing focused tests only when an assertion must reflect an
  unchanged shared implementation.

### Step 1: Add failing support-boundary tests

Create `backend/tests/test_agent_support.py`:

```python
from app.agents.support import record_agent_exit, redact_request_for_agent
from app.models import AuditEvent


def test_redact_request_for_agent_redacts_and_audits_counts(db, seeded):
    state = {
        "workflow_id": 41,
        "patient_id": 1,
        "request_text": "email jane.doe@example.com about my appointment",
    }

    redacted = redact_request_for_agent(db, state, "routing")

    assert redacted == "email [REDACTED_EMAIL] about my appointment"
    row = db.query(AuditEvent).filter_by(
        action="safety.pii_redacted",
        entity_type="workflow_run",
        entity_id=41,
    ).one()
    assert row.metadata_json == {"node": "routing", "counts": {"email": 1}}


def test_record_agent_exit_commits_named_audit_event(db, seeded):
    record_agent_exit(db, "coordinator", 42, {"next_step": "finalize"})

    row = db.query(AuditEvent).filter_by(
        action="agent.coordinator.completed",
        entity_type="workflow_run",
        entity_id=42,
    ).one()
    assert row.metadata_json == {"next_step": "finalize"}
```

### Step 2: Run the focused tests and record RED

From `backend/`:

```bash
rtk "../../../.venv/bin/python" -m pytest tests/test_agent_support.py -q
```

Expected: collection fails because `app.agents.support` does not yet exist.

### Step 3: Implement the focused agent support module

Create `backend/app/agents/support.py` with only these responsibilities:

```python
from sqlalchemy.orm import Session

from app.agents.responses import patient_language
from app.agents.state import AgentState
from app.safety.pii import redact_for_llm, resolve_language
from app.tools.audit_tools import write_audit


def record_agent_exit(
    db: Session,
    agent_name: str,
    workflow_id: int | None,
    summary: dict,
) -> None:
    write_audit(
        db,
        None,
        f"agent.{agent_name}.completed",
        "workflow_run",
        workflow_id,
        summary,
    )
    db.commit()


def redact_request_for_agent(
    db: Session,
    state: AgentState,
    agent_name: str,
) -> str:
    request_text = state.get("request_text", "")
    redacted, counts = redact_for_llm(
        request_text,
        language=resolve_language(
            request_text,
            patient_language(db, state.get("patient_id")),
        ),
    )
    if counts:
        write_audit(
            db,
            None,
            "safety.pii_redacted",
            "workflow_run",
            state.get("workflow_id"),
            {"node": agent_name, "counts": counts},
        )
    return redacted
```

Do not create a generic `utils.py`. Keep this module limited to shared agent
boundaries.

### Step 4: Run support tests and record GREEN

From `backend/`:

```bash
rtk "../../../.venv/bin/python" -m pytest tests/test_agent_support.py -q
```

Expected: both tests pass.

### Step 5: Replace repeated agent wrappers

In all six model-assisted agents, import and call `record_agent_exit` with the
agent's stable name. Delete all six private `_exit_audit` definitions and
remove imports made unused.

In coordinator and routing, replace their private `_redact_request_text`
functions with:

```python
request_text = redact_request_for_agent(db, state, "coordinator")
```

and:

```python
request_text = redact_request_for_agent(db, state, "routing")
```

respectively. Preserve all existing try/rollback/error-handling boundaries and
the meaning of every audit payload.

### Step 6: Replace duplicate workflow summary serializers

In `backend/app/schemas/staff.py`, import `ConfigDict` and configure:

```python
class WorkflowRunSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    # existing fields remain unchanged
```

Delete the duplicate `_to_summary` functions in the workflow and staff route
modules. At their call sites use:

```python
WorkflowRunSummary.model_validate(run)
```

Keep response schemas and JSON output unchanged.

### Step 7: Consolidate reminder construction without moving commits

In `backend/app/tools/followup_tools.py`, add a private, non-committing
`_add_reminder` that constructs and flushes one `Reminder`, writes its
`reminder.created` audit event, and returns the ORM row. Use it from
`create_reminder` and `create_reminders_batch`.

`create_reminder` must still commit exactly once after one row.
`create_reminders_batch` must still commit once after the entire batch and
roll back the complete batch on any error. Do not move transaction ownership
into `_add_reminder`.

### Step 8: Verify the task

From `backend/`:

```bash
rtk "../../../.venv/bin/python" -m pytest tests/test_agent_support.py tests/test_pii.py tests/test_audit.py tests/test_followup.py tests/test_routes_workflows.py tests/test_staff.py -q
rtk "../../../.venv/bin/ruff" check app tests
rtk "../../../.venv/bin/python" -m pytest -q
```

Expected: all tests pass with no behavior changes.

### Step 9: Commit

```bash
rtk git add backend
rtk git commit -m "consolidate agent and workflow boundaries"
```

---

## Task 3: Delegate structured output to LangChain and activate Vertex AI

**Files:**

- Modify: `requirements.txt`
- Modify: `backend/llm.yaml`
- Modify: `backend/app/agents/llm.py`
- Modify: `backend/app/agents/model_config.py`
- Modify: `backend/tests/conftest.py`
- Modify: `backend/tests/test_graph_e2e.py`
- Modify: `backend/tests/test_model_config.py`
- Modify: `backend/tests/test_llm.py`

### Step 1: Add a failing recursively strict schema test

The current `_strict_schema()` only sets `additionalProperties: false` on the
top-level object. Groq strict mode requires it on every object, including
`FollowupOutput.$defs.ReminderSpec`.

Add a no-network integration test in `backend/tests/test_llm.py` that:

1. creates a real `openai.OpenAI` client using `httpx.Client` with
   `httpx.MockTransport`;
2. captures the JSON request body and returns a valid chat-completion response
   whose message content contains:

```json
{
  "reminders": [
    {"type": "appointment", "days_before_appointment": 1}
  ],
  "followup_days_after": 14
}
```

3. injects that SDK client through `set_llm_client_for_tests`;
4. calls `chat_json("system", "user", FollowupOutput)`;
5. asserts the result is the expected `FollowupOutput`;
6. asserts:

```python
schema = captured["response_format"]["json_schema"]["schema"]
assert schema["additionalProperties"] is False
assert schema["$defs"]["ReminderSpec"]["additionalProperties"] is False
```

Use the real SDK client for this boundary because the scripted fake sees
arguments before the SDK converts a Pydantic class into its wire schema. The
mock transport must not contact any network.

### Step 2: Run the nested-schema test and record RED

From `backend/`:

```bash
rtk "../../../.venv/bin/python" -m pytest tests/test_llm.py::test_chat_json_sends_recursively_strict_schema -q
```

Expected: fail because the existing manual schema leaves nested
`ReminderSpec` open.

### Step 3: Make the scripted fake support SDK Pydantic parsing

`backend/tests/conftest.py::_FakeWithRawResponse.parse` currently returns an
ordinary `ChatCompletion`; the real OpenAI SDK returns a parsed completion when
`response_format` is a Pydantic class.

Extend only the fake's `parse()` path:

- consume the scripted completion once;
- when `kwargs["response_format"]` is a Pydantic model class, validate the
  response message content with that class;
- expose the validated instance as the completion message's `parsed` value in
  the same shape the SDK provides;
- let `pydantic.ValidationError` propagate for invalid native-schema output;
- leave `create()` and JSON-object behavior unchanged.

Use public OpenAI response model classes or a direct test-only message
attribute supported by the pinned model. Do not import OpenAI private parsing
helpers. Preserve one recorded fake call per model request.

### Step 4: Replace manual schema plumbing with `with_structured_output`

Keep the public `chat_json(system, user, schema_model, max_retries=None)` API.
It is AgentCare's policy boundary, not a generic JSON utility.

Inside `backend/app/agents/llm.py`:

- delete `_strict_schema`;
- stop calling `model.bind(response_format=...)` directly;
- build the native runnable with:

```python
model.with_structured_output(
    schema_model,
    method="json_schema",
    strict=True,
    include_raw=True,
)
```

- build the compatibility runnable with:

```python
model.with_structured_output(
    schema_model,
    method="json_mode",
    include_raw=True,
)
```

- wrap the complete structured runnable with `_with_retry`, still restricted
  to the existing transport exception tuple;
- on `openai.BadRequestError`, add `_schema_in_prompt(...)` and retry through
  JSON mode;
- if `include_raw` returns `parsing_error`, or native SDK parsing raises
  `pydantic.ValidationError`, issue the existing single corrective re-prompt;
- append the raw `AIMessage` before the correction when it is available;
- after a second validation failure, raise `LLMOutputError`;
- keep primary-to-fallback switching and force the fallback model through
  prompt-guided JSON mode once;
- accept either a schema-model instance or a provider-returned dict, validating
  a dict with `schema_model.model_validate`.

Do not remove `chat_json`, `_schema_in_prompt`, `_with_retry`, profile
resolution, test overrides, or `LLMOutputError`. Do not introduce LangChain's
agent factory; the explicit LangGraph nodes remain the orchestrator.

### Step 5: Update and extend structured-output regression tests

Preserve every existing behavior test. Adjust native-path fake assertions to
expect the Pydantic class; the `httpx.MockTransport` test owns exact wire-schema
assertions.

Add a validation-repair test after JSON-schema rejection:

```python
client = FakeClient(
    [
        _bad_request_error(),
        _ok({"ok": "not-a-boolean"}),
        _ok({"ok": True, "reason": "corrected in json mode"}),
    ]
)
```

Assert the corrected `_Verdict` is returned, the first call is native, calls
two and three use `{"type": "json_object"}`, and the third call contains a
correction message.

Keep green the existing tests for native structured output, 400-to-JSON-mode
fallback, validation repair, two-invalid-response failure, transport retry
count, fallback-model JSON mode, and network-free test overrides.

If an end-to-end test serializes the fake call's entire kwargs dictionary to
inspect prompt content, narrow it to `call["messages"]`. A Pydantic
`response_format` class is provider control data, not prompt content and is not
JSON-serializable; all existing guidance/PII assertions must remain.

From `backend/`:

```bash
rtk "../../../.venv/bin/python" -m pytest tests/test_llm.py -q
```

Expected: all LLM tests pass. Record the nested-schema RED and complete GREEN.

### Step 6: Add a failing committed Vertex profile test

In `backend/tests/test_model_config.py`, add:

```python
def test_committed_yaml_provides_vertex_profile(monkeypatch):
    settings = _settings(monkeypatch, LLM_PROFILE="vertex")

    profiles = load_llm_profiles(settings)

    assert profiles.primary.provider == "google_genai"
    assert profiles.primary.model == "gemini-2.5-flash"
    assert profiles.primary.params == {"vertexai": True}
```

Run it before changing YAML:

```bash
rtk "../../../.venv/bin/python" -m pytest tests/test_model_config.py::test_committed_yaml_provides_vertex_profile -q
```

Expected: fail because `vertex` is still only a commented example.

### Step 7: Add the model-factory boundary test

In `backend/tests/test_llm.py`, monkeypatch module-level `init_chat_model`, call
`_build_chat_model` with:

```python
ModelProfile(
    provider="google_genai",
    model="gemini-2.5-flash",
    params={"vertexai": True},
)
```

Assert the fake factory receives:

```python
("gemini-2.5-flash",)
{"model_provider": "google_genai", "vertexai": True}
```

The factory returns a sentinel and the test asserts the sentinel is returned.
This test must not contact Google.

### Step 8: Add only the required provider integration

In `requirements.txt` add:

```text
langchain-google-genai==4.2.7
```

Do not add `langchain-groq`, `langchain-google-vertexai`, or another model
router.

In `backend/llm.yaml`, replace the commented Gemini example with:

```yaml
  # Gemini through Vertex AI. This profile is configured but has not been
  # verified against a live GCP project.
  vertex:
    provider: google_genai
    model: gemini-2.5-flash
    vertexai: true
```

Keep `groq` as `default_profile`. Authentication is Application Default
Credentials with `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` supplied by
the deployment environment.

### Step 9: Correct provider guidance and fixtures

Update stale examples to name `langchain-google-genai`, not the deprecated
Vertex-specific package. Update test-only provider spelling from
`google_vertexai` to `google_genai` where it represents current Gemini on
Vertex. Preserve the generic missing-package error.

Do not add provider-specific conditionals to `_build_chat_model`; `params`
already carries `vertexai` through `init_chat_model`.

### Step 10: Install and verify

From the repository root:

```bash
rtk "../../.venv/bin/python" -m pip install "langchain-google-genai==4.2.7"
```

From `backend/`:

```bash
rtk "../../../.venv/bin/python" -m pytest tests/test_model_config.py tests/test_llm.py -q
rtk "../../../.venv/bin/ruff" check app tests
rtk "../../../.venv/bin/python" -m compileall -q app
rtk "../../../.venv/bin/python" -m pytest -q
rtk "../../../.venv/bin/python" -m pip check
```

Expected: all checks pass. This verifies Groq-compatible wire formatting over a
local mock plus Vertex configuration/construction; it is not a live Groq or
Vertex model test.

### Step 11: Commit

```bash
rtk git add requirements.txt backend
rtk git commit -m "delegate structured output and add Vertex profile"
```

---

## Task 4: Remove frontend dead code, assets, and the ineffective theme layer

**Files:**

- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Modify: `frontend/components/ui/sonner.tsx`
- Modify: `frontend/hooks/use-current-user.ts`
- Modify: `frontend/lib/api.ts`
- Modify: `frontend/lib/types.ts`
- Delete: `frontend/public/file.svg`
- Delete: `frontend/public/globe.svg`
- Delete: `frontend/public/next.svg`
- Delete: `frontend/public/vercel.svg`
- Delete: `frontend/public/window.svg`

### Step 1: Reconfirm dead exports and assets

From the repository root, use repository-wide searches to prove that each
target has no consumer beyond its declaration:

```bash
rtk rg -n "resumeWorkflow|staffRunDueReminders|ResumeResponse|ReminderRunResponse|DoctorUpdate|AgentRuleUpdate" frontend
rtk rg -n "file\\.svg|globe\\.svg|next\\.svg|vercel\\.svg|window\\.svg" frontend
rtk rg -n "useTheme|ThemeProvider|next-themes" frontend
```

Do not delete an item if the search reveals a real consumer.

### Step 2: Remove unused API and type surface

Delete `resumeWorkflow` and `staffRunDueReminders` from `frontend/lib/api.ts`.
Delete `ResumeResponse`, `ReminderRunResponse`, `DoctorUpdate`, and
`AgentRuleUpdate` from `frontend/lib/types.ts`. Remove imports made unused.

In `frontend/hooks/use-current-user.ts`, remove the unused `ApiError` import,
`error` state, `nonce` state, `refresh` callback, and their interface fields.
Keep the existing request cancellation, loading state, and `user` behavior:

```typescript
interface UseCurrentUserResult {
  user: UserSummary | null;
  loading: boolean;
}
```

The catch path must still set `user` to `null`. Do not introduce a context,
query cache, or auth redesign.

### Step 3: Remove the ineffective theme dependency

Rewrite `frontend/components/ui/sonner.tsx` so it no longer imports or calls
`useTheme`. Supply the current effective default directly:

```tsx
<Sonner
  theme="system"
  className="toaster group"
  // existing properties
  {...props}
/>
```

Keep `{...props}` last so callers can still override `theme`.

From `frontend/` run:

```bash
rtk npm uninstall next-themes
```

This must update both `package.json` and `package-lock.json`.

### Step 4: Delete unused starter assets

Delete the five confirmed unused SVG files. Keep `favicon.ico` and any asset
with a real reference.

### Step 5: Verify the task

From `frontend/`:

```bash
rtk npm run lint
rtk npx tsc --noEmit
rtk npm run build
```

From the repository root:

```bash
rtk rg -n "resumeWorkflow|staffRunDueReminders|ResumeResponse|ReminderRunResponse|DoctorUpdate|AgentRuleUpdate|next-themes|useTheme" frontend
```

Expected: lint, type-check, and production build pass; the final search returns
no live code matches.

### Step 6: Commit

```bash
rtk git add frontend
rtk git commit -m "remove unused frontend surface"
```

---

## Task 5: Replace stale and duplicate documentation with one truthful set

**Files:**

- Rewrite: `.env.example`
- Rewrite: `README.md`
- Rewrite: `docs/architecture.md`
- Rewrite: `docs/security.md`
- Rewrite: `docs/deployment-gcp.md`
- Rewrite: `docs/decisions.md`
- Rewrite: `docs/demo-script.md`
- Review and update only if needed: `evals/README.md`
- Rewrite: `infra/k8s/README.md`
- Delete: `docs/runbook.md`
- Delete: `docs/index.md`
- Delete: `docs/architecture.mmd`
- Delete: `frontend/README.md`
- Delete: `backend/alembic/README`

### Step 1: Build the canonical documentation map

The final set has one owner for each concern:

- `README.md`: product, architecture overview, quickstart, demos,
  configuration, verification, and documentation links;
- `docs/architecture.md`: components, LangGraph topology, request and
  interrupt/resume sequences, state, data, and deployment views;
- `docs/security.md`: trust boundaries and safety controls;
- `docs/deployment-gcp.md`: complete GCP deployment, verification,
  troubleshooting, and teardown;
- `docs/decisions.md`: current architectural decisions and trade-offs;
- `docs/demo-script.md`: executable two-minute judge demo;
- `evals/README.md`: evaluation harness;
- `infra/k8s/README.md`: manifest-local reference only.

Merge unique commands from files slated for deletion before deleting them.
Do not create an archive folder.

### Step 2: Rewrite the README around the actual request path

Target 250-360 lines and no more than about 2,000 words. Use this order:

1. title, one-sentence value proposition, and an honest status note;
2. compact feature/evidence table;
3. one Mermaid diagram showing:
   - FastAPI and deterministic emergency/medical gates;
   - injection screen before model-bound work;
   - coordinator and five specialist roles;
   - SQL tools and append-only audit;
   - escalation `interrupt()` and same-thread resume;
   - SQLite/Postgres checkpointer;
   - PII redaction at each model boundary, not as a global pre-graph gate;
4. stack and concise repository map;
5. prerequisites with check commands;
6. local setup in exact execution order;
7. patient request and staff approval demos;
8. tests and evaluation;
9. model configuration summary including Groq default and configured-but-not
   live-tested Vertex profile;
10. documentation links and license.

Describe six model-assisted roles. Describe escalation as a deterministic
human-control node, not a seventh agent. State clearly that GCP deployment and
live Vertex smoke tests remain operator actions unless they have actually been
performed.

### Step 3: Rewrite detailed architecture and security docs

`docs/architecture.md` must include:

- a runtime component view;
- the real LangGraph topology and transition guards;
- a patient request sequence;
- a staff interrupt/resume sequence using the same `thread_id`;
- checkpoint versus domain-state responsibilities;
- the main persisted entities and audit flow;
- GCP deployment boundaries;
- the synchronous-core decision and the specific places that are natively
  asynchronous.

`docs/security.md` must include:

- trust boundaries;
- deterministic emergency and medical-advice refusal;
- injection layers and Model Armor fail-open/fail-closed behavior as
  implemented;
- model-bound PII redaction and stored-data boundary;
- backend RBAC;
- upload restrictions;
- audit events and persisted approvals;
- known limitations stated without minimizing them.

Do not duplicate setup or deployment commands in either file.

### Step 4: Make GCP deployment the single operator runbook

`docs/deployment-gcp.md` must be the only end-to-end cloud procedure. Include:

- exact local tools and authentication checks;
- project, region, billing, and API prerequisites;
- OpenTofu plan/apply;
- image build/push;
- secret and config setup without committing values;
- database migration;
- Kustomize apply;
- health, log, workflow, Model Armor, and rollback checks;
- teardown order and cost warning;
- manual gaps, including public DNS/TLS and any environment value the existing
  infrastructure does not generate.

Use only existing GCP infrastructure. Do not suggest an alternative cloud.

### Step 5: Make decisions and demo concise and executable

`docs/decisions.md` records current choices, not a diary. Cover:

- LangGraph explicit state machine;
- deterministic code gates around model work;
- sync SQLAlchemy/LangGraph transaction core;
- LangChain model factory plus YAML/env precedence;
- separate checkpointer and domain persistence;
- direct SQL tools and append-only audit;
- GCP-only deployment adapters.

`docs/demo-script.md` must fit a two-minute walkthrough and use actions the UI
actually supports. Demonstrate duplicate-upload/idempotency evidence through a
new request plus the staff audit view, not a nonexistent portal upload action.

Update `.env.example` so `LLM_PROFILE` documents `groq | local | vertex`,
explains that the Vertex profile uses Application Default Credentials, and
names `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` without adding any
credential value or secret. Remove the stale commented-example wording.

Keep `infra/k8s/README.md` to manifest-local prerequisites, render/apply,
verification, and teardown links. Keep `evals/README.md` focused on the eval
harness.

### Step 6: Delete absorbed documents permanently

After unique material is merged, delete:

- `docs/runbook.md`;
- `docs/index.md`;
- `docs/architecture.mmd`;
- `frontend/README.md`;
- `backend/alembic/README`.

### Step 7: Verify documentation truth and links

From the repository root:

```bash
rtk wc -l README.md
rtk wc -w README.md
rtk rg -n "seven agents|7 agents|regex-only|not installed|Task [0-9]+|per the brief|ChatVertexAI|google_vertexai|langchain-google-vertexai|portal upload" README.md docs evals infra backend/app frontend
rtk rg -n "docs/(runbook|index)\\.md|architecture\\.mmd|frontend/README\\.md|alembic/README" README.md docs evals infra backend frontend
rtk rg -n "TODO|TBD|PLACEHOLDER|coming soon" README.md docs evals infra
rtk git diff --check
```

Inspect every remaining match; remove stale prose while retaining legitimate
test fixtures or historical identifiers only when their meaning is current.
Manually follow every local Markdown link in the final documentation set.

### Step 8: Commit

```bash
rtk git add .env.example README.md docs evals infra frontend/README.md backend/alembic/README
rtk git commit -m "replace stale docs with canonical project guide"
```

---

## Task 5A: Repair the reviewed deployment path and remove speculative cloud surface

This fix wave is required by the independent Task 5 review and the current-GCP
audit. It makes the canonical deployment guide executable instead of
rewording around broken configuration.

**Files:**

- Add: `backend/tests/test_deployment_config.py`
- Modify: `backend/tests/test_documents.py`
- Modify: `backend/app/tools/document_tools.py`
- Modify: `backend/docker-entrypoint.sh`
- Modify: `docker-compose.yml`
- Add: `infra/k8s/base/service-account.yaml`
- Add: `infra/k8s/overlays/gcp/serviceaccount-workload-identity.yaml`
- Add: `infra/k8s/overlays/gcp-migration/kustomization.yaml`
- Add: `infra/k8s/overlays/gcp-migration/migration-job.yaml`
- Modify: `infra/k8s/base/backend.yaml`
- Modify: `infra/k8s/base/configmap.yaml`
- Modify: `infra/k8s/base/kustomization.yaml`
- Delete: `infra/k8s/base/migration-job.yaml`
- Modify: `infra/k8s/overlays/gcp/kustomization.yaml`
- Modify: `infra/terraform/main.tf`
- Modify: `infra/terraform/variables.tf`
- Modify: `infra/terraform/outputs.tf`
- Modify: `infra/terraform/modules/iam/main.tf`
- Modify: `infra/terraform/modules/iam/variables.tf`
- Modify: `infra/terraform/modules/iam/outputs.tf`
- Modify: `infra/terraform/modules/cloud-sql/main.tf`
- Modify: `infra/terraform/modules/cloud-sql/variables.tf`
- Modify: `infra/terraform/modules/cloud-sql/outputs.tf`
- Modify comments only where stale in GCS, Model Armor, GKE and GCP overlay
  files found by the review
- Delete: `.github/workflows/deploy.yml`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/security.md`
- Modify: `docs/decisions.md`
- Modify: `docs/deployment-gcp.md`
- Modify: `docs/demo-script.md`
- Modify: `evals/results/summary-nokey-baseline.md`
- Modify: `infra/k8s/README.md`

### Step 1: Pin the configuration regressions with failing tests

Add tests proving:

- Compose reads the documented environment without hardcoding a known JWT
  secret or injecting `LLM_MODEL`/`LLM_BASE_URL` over a selected YAML profile;
- the Kubernetes base selects `LLM_PROFILE=groq` without field-level model or
  endpoint overrides;
- the backend Deployment uses the declared `agentcare-backend` service
  account and skips startup migrations;
- the migration Job is absent from the application base and owned by the
  separate GCP migration overlay;
- duplicate-document audit metadata does not expose the checksum.

Run the focused tests and record the expected RED failures before editing
implementation/configuration.

### Step 2: Restore real model-profile selection

Use the repo-root `.env.example` plus an optional `.env` as Compose
`env_file` inputs. Keep the Compose Postgres URL and frontend origin as
explicit overrides. Require a non-empty operator-provided `JWT_SECRET`.
Remove synthesized Groq `LLM_MODEL` and `LLM_BASE_URL` values so
`backend/llm.yaml` remains the default source and explicit env fields remain
real overrides.

In Kubernetes, set only `LLM_PROFILE: "groq"` in the base ConfigMap. Remove
`LLM_MODEL` and `LLM_BASE_URL`. The GCP guide must explain that local
OpenAI-compatible servers need a container-reachable endpoint and that local
Vertex use requires ADC not automatically mounted by Compose.

### Step 3: Make migration ownership singular and ordered

Add a guarded `SKIP_STARTUP_MIGRATIONS` switch to the Docker entrypoint.
Compose keeps the existing migrate/seed startup default. The Kubernetes
backend Deployment sets the switch to true.

Remove the migration Job from the application base Kustomization. Put that
Job entirely in `infra/k8s/overlays/gcp-migration`, with its own immutable
backend image transform. Keeping the manifest self-contained in the overlay
also avoids Kustomize load-restriction coupling to the application base. It
remains the only Kubernetes migration/seed owner. The GCP runbook must
delete/apply/wait/log the Job before applying the application overlay. Do not
deploy the application before the Job succeeds.

### Step 4: Make runtime workload identity declarative

Add one `agentcare-backend` Kubernetes service account to the base, use it via
`serviceAccountName`, and add the GCP service-account annotation through an
overlay patch. Add the corresponding
`roles/iam.workloadIdentityUser` binding in Terraform. The GCP operator
replaces the project sentinel before rendering; remove every manual
create/annotate/bind/patch command.

### Step 5: Remove unused cloud and CD functionality

The unverified deploy workflow applies unresolved values and is not the
canonical deployment path. Delete `.github/workflows/deploy.yml` and the
Terraform GitHub WIF pool/provider, deploy service account, mutable
repository-name authorization, deploy IAM grants and outputs. Manual
deployment remains the only truthful GCP procedure until a credentialed CD
pipeline is designed and live-tested.

Also delete:

- the unused frontend Google service account and output;
- project-wide Secret Manager accessor IAM and duplicate Secret Manager
  instructions/API enablement, because pods read one Kubernetes Secret;
- `roles/cloudsql.client` and the proxy-only connection-name output, because
  the implemented database path is direct private-IP PostgreSQL.

Do not add an alternative cloud or speculative replacement.

### Step 6: Complete Vertex support with least privilege

Add `enable_vertex_ai` (default false) to root Terraform and the IAM module.
When true, grant the backend GSA `roles/aiplatform.user`. Add the Vertex AI
API to the documented conditional enablement path. The guide must connect
`enable_vertex_ai=true`, `LLM_PROFILE=vertex`, project/location environment
values and the declarative KSA/GSA binding. Groq remains the default.

### Step 7: Repair reviewed documentation details

- Keep the duplicate audit event collapsed in the demo and remove checksum
  from its audit metadata.
- Split the committed no-key baseline reproduction into two runnable
  terminals.
- Add compact Compose log and destructive local-volume reset commands with an
  explicit data-loss warning.
- Remove CI/WIF, Secret Manager, Cloud SQL Proxy, Neon, retired `ADR-*` and
  duplicate-migration claims from current docs and infrastructure comments.
- Guard recursive GCS teardown by re-reading the Terraform output, checking
  the expected bucket name and requiring typed confirmation.
- Keep live GCP, Vertex and Model Armor status unverified.

### Step 8: Verify and commit

Run:

```bash
rtk "../../../.venv/bin/python" -m pytest -q tests/test_deployment_config.py tests/test_documents.py tests/test_model_config.py tests/test_llm.py
rtk "../../../.venv/bin/ruff" check app tests ../evals
rtk sh -n docker-entrypoint.sh
rtk env JWT_SECRET=test-only-not-for-deploy docker compose config --quiet
rtk kubectl kustomize infra/k8s/overlays/gcp
rtk kubectl kustomize infra/k8s/overlays/gcp-migration
rtk tofu -chdir=infra/terraform fmt -check -recursive
rtk tofu -chdir=infra/terraform validate
rtk rg -n "Secret Manager|secretmanager|cloudsql\\.client|Cloud SQL Auth Proxy|Workload Identity Federation|github_repository|Neon|ADR-[0-9]+" README.md docs infra .github
rtk git diff --check
```

Commands are run from the directory their paths assume. Inspect all search
matches rather than hiding them. If OpenTofu is unavailable, use an installed
compatible Terraform binary for local `fmt` and `validate`, and disclose the
exact binary and version without claiming an OpenTofu run. Commit as:

```bash
rtk git add -A
rtk git commit -m "make the GCP deployment path executable"
```

---

## Task 6: Repository-wide consistency pass

**Files:**

- Modify only files containing a confirmed stale comment, broken reference, or
  formatting issue found by this task.
- Do not add features or reorganize untouched code.

### Step 1: Scan removed symbols and stale implementation-history prose

From the repository root:

```bash
rtk rg -n "patient_tools|create_followup_task|SafetyBlockedError|document_ids.*create_run|ensure_owner_or_staff\\([^\\n]*db|_exit_audit|next-themes|resumeWorkflow|staffRunDueReminders" .
rtk rg -n "Task [0-9]+|per the brief|not installed|not pinned|placeholder behavior|google_vertexai|langchain-google-vertexai" backend frontend README.md docs evals infra
```

Classify every match. Remove only obsolete comments, imports, docs, or
declarations. Do not edit test inputs where the matched text is intentionally
under test.

### Step 2: Scan repository hygiene

From the repository root:

```bash
rtk git ls-files | rtk rg "(\\.env$|\\.env\\.|\\.db$|\\.sqlite|\\.pem$|\\.key$|node_modules|__pycache__|\\.next/)"
rtk git diff --check
rtk git status --short
```

Expected: no tracked secret, database, key, cache, dependency, or generated
build artifact. The only `.env`-shaped tracked file may be a documented
example that contains no secret.

### Step 3: Run the complete verification matrix

From `backend/`:

```bash
rtk "../../../.venv/bin/python" -m pytest -q
rtk "../../../.venv/bin/ruff" check app tests ../evals
rtk "../../../.venv/bin/python" -m compileall -q app ../evals
rtk "../../../.venv/bin/python" -m pip check
rtk "../../../.venv/bin/alembic" heads
```

Verify the Alembic output contains one head. Upgrade a fresh temporary SQLite
database using the existing test-safe configuration and remove only that
explicit temporary file afterward.

From `frontend/`:

```bash
rtk npm run lint
rtk npx tsc --noEmit
rtk npm run build
```

From the repository root:

```bash
rtk kubectl kustomize infra/k8s/overlays/gcp
rtk kubectl kustomize infra/k8s/overlays/gcp-migration
rtk tofu -chdir=infra/terraform fmt -check -recursive
rtk tofu -chdir=infra/terraform validate
```

If `tofu` is unavailable, report that exact environmental limitation. An
installed compatible Terraform binary may be used for local `fmt` and
`validate` when its exact version is recorded; do not claim OpenTofu itself
ran. Do not deploy or mutate GCP from this task.

### Step 4: Record exact evidence

The task report records:

- backend passed/failed test count and runtime;
- Ruff, compile, and `pip check` outputs;
- frontend lint, type-check, and build results;
- Alembic head and fresh-upgrade result;
- both Kustomize overlay results and the exact OpenTofu/Terraform-compatible
  validation result;
- hygiene scan results;
- any warning, skipped check, or environmental limitation.

### Step 5: Commit only if the consistency scan changed files

```bash
rtk git add -A
rtk git commit -m "finish repository consistency pass"
```

If the scan changes nothing, do not create an empty commit.

---

## Task 7: Make the LLM transport policy provider-neutral

This focused fix is required by the post-Task-6 LLM boundary audit. LangChain
already owns structured-output schema generation and parsing; this task fixes
only the application policy around the provider call.

**Files:**

- Modify: `backend/app/agents/llm.py`
- Modify: `backend/tests/test_llm.py`

### Step 1: Pin the provider gaps with failing tests

Add tests proving:

- the `google_genai` factory receives the profile timeout and exactly one SDK
  attempt, because the pinned Google integration interprets `1` as no retry
  and `0` as its default retry policy;
- Google 5xx, network, timeout and wrapped 429 errors use the existing
  LangChain retry policy;
- Google 400/401/403/404 errors do not retry or activate endpoint fallback;
- exhausted retryable Google errors activate the configured endpoint fallback
  and re-raise the original error when no fallback exists;
- only an unsupported `response_format`/`json_schema` error downgrades the
  OpenAI-compatible request to `json_object`; invalid schemas and unrelated
  `BadRequestError` responses propagate after one call.

Run the focused tests and record the expected RED failures before editing
production code.

### Step 2: Normalize only retryable provider failures

Keep LangChain's public `Runnable.with_retry` as the one retry mechanism.
Wrap the structured runnable with a small `RunnableLambda` adapter because
`with_retry` accepts exception types rather than a predicate. Convert only
these failures to a private retry marker:

- the existing OpenAI connection, timeout, 5xx and rate-limit exceptions;
- Google `ServerError`;
- `httpx.TransportError`;
- a Google `ClientError` with status 429 found in the cause/context chain.

After exhaustion, re-raise the original provider exception. Apply the same
finite predicate before selecting the configured endpoint fallback. Never
retry or fall back on other Google client errors.

### Step 3: Bound the Google SDK and narrow compatibility downgrade

For `provider: google_genai`, pass the profile timeout and one SDK attempt to
`init_chat_model`; OpenAI-compatible models continue using zero SDK retries.
The outer LangChain runnable remains the only backoff loop.

Narrow the `BadRequestError` compatibility path to explicit unsupported
structured-format signals. Keep automatic `json_object` compatibility for
servers that reject `json_schema`, but do not weaken an arbitrary invalid
request. Update comments to describe total attempts accurately. Do not add a
library, a second retry stack, a YAML field, or an async wrapper.

### Step 4: Verify, review and commit

From `backend/` run:

```bash
rtk "../../../.venv/bin/python" -m pytest -q tests/test_llm.py tests/test_model_config.py
rtk "../../../.venv/bin/ruff" check app/agents/llm.py tests/test_llm.py
rtk "../../../.venv/bin/python" -m compileall -q app/agents/llm.py
```

Then run the full backend suite before review. Commit as:

```bash
rtk git add backend/app/agents/llm.py backend/tests/test_llm.py
rtk git commit -m "make LLM transport policy provider neutral"
```

---

## Final Acceptance

After all task reviews are approved:

1. copy the design and plan into the ignored SDD evidence directory so the
   final review can still read them;
2. permanently delete the tracked Superpowers design/plan artifacts from the
   final repository tree and commit that deletion;
3. generate one whole-branch review package from the pre-task base through the
   final cleanup commit;
4. run an independent final review for correctness, architecture truth,
   documentation usability, security regression risk, and repository hygiene;
5. fix every Critical or Important finding with focused verification and a
   scoped re-review;
6. rerun the complete verification matrix on the exact reviewed head;
7. follow `finishing-a-development-branch` and integrate the approved branch
   into `main` without discarding unrelated user work.

Completion requires a clean `main` worktree and evidence from the exact final
commit. A passing unit suite alone is not sufficient.
