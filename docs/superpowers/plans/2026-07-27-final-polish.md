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
def test_registration_writes_audit_event(client, db):
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
    row = db.query(AuditEvent).filter_by(action="user.registered").one()
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
        "request_text": "Email jane.doe@example.com about my appointment",
    }

    redacted = redact_request_for_agent(db, state, "routing")

    assert redacted == "Email [REDACTED_EMAIL] about my appointment"
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

## Task 3: Activate the current Vertex AI model path through LangChain

**Files:**

- Modify: `requirements.txt`
- Modify: `backend/llm.yaml`
- Modify: `backend/app/agents/llm.py`
- Modify: `backend/app/agents/model_config.py`
- Modify: `backend/tests/test_model_config.py`
- Modify: `backend/tests/test_llm.py`

### Step 1: Add a failing committed-profile test

In `backend/tests/test_model_config.py`, add:

```python
def test_committed_yaml_provides_vertex_profile(monkeypatch):
    settings = _settings(monkeypatch, LLM_PROFILE="vertex")

    profiles = load_llm_profiles(settings)

    assert profiles.primary.provider == "google_genai"
    assert profiles.primary.model == "gemini-2.5-flash"
    assert profiles.primary.params == {"vertexai": True}
```

### Step 2: Add a failing model-factory boundary test

In `backend/tests/test_llm.py`, add a test that monkeypatches the module-level
`init_chat_model`, calls `_build_chat_model` with:

```python
ModelProfile(
    provider="google_genai",
    model="gemini-2.5-flash",
    params={"vertexai": True},
)
```

and asserts the factory receives:

```python
("gemini-2.5-flash",)
{"model_provider": "google_genai", "vertexai": True}
```

The fake factory returns a sentinel object and the test asserts that sentinel
is returned. This is a local boundary test; it must not contact Google.

### Step 3: Run the focused tests and record RED

From `backend/`:

```bash
rtk "../../../.venv/bin/python" -m pytest tests/test_model_config.py::test_committed_yaml_provides_vertex_profile tests/test_llm.py -q
```

Expected: the committed-profile test fails because `vertex` is still only a
commented example. If the factory-boundary test already passes, record that
honestly; the required RED is the missing active profile.

### Step 4: Add only the required provider integration

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

Keep `groq` as `default_profile`. Authentication remains Application Default
Credentials with `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` supplied by
the deployment environment.

### Step 5: Correct provider guidance and test fixtures

Update stale `llm.py` examples to name `langchain-google-genai`, not the
deprecated Vertex-specific package. Update any test-only profile spelling from
`google_vertexai` to `google_genai` when the test is meant to demonstrate the
current Google provider. Preserve the generic missing-package error behavior.

Do not add provider-specific conditionals to `_build_chat_model`; `params`
already passes `vertexai` through `init_chat_model`.

### Step 6: Install and verify the pinned integration

From the repository root:

```bash
rtk "../../.venv/bin/python" -m pip install "langchain-google-genai==4.2.7"
```

From `backend/`:

```bash
rtk "../../../.venv/bin/python" -m pytest tests/test_model_config.py tests/test_llm.py -q
rtk "../../../.venv/bin/ruff" check app tests
rtk "../../../.venv/bin/python" -m pytest -q
rtk "../../../.venv/bin/python" -m pip check
```

Expected: all tests pass and dependency resolution is clean. This verifies
configuration and construction only, not a live Vertex response.

### Step 7: Commit

```bash
rtk git add requirements.txt backend
rtk git commit -m "add configurable Vertex AI model profile"
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
rtk git add README.md docs evals infra frontend/README.md backend/alembic/README
rtk git commit -m "replace stale docs with canonical project guide"
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
rtk tofu -chdir=infra/tofu fmt -check -recursive
rtk tofu -chdir=infra/tofu validate
```

If `tofu` is unavailable, report that exact environmental limitation; do not
claim validation passed. Do not deploy or mutate GCP from this task.

### Step 4: Record exact evidence

The task report records:

- backend passed/failed test count and runtime;
- Ruff, compile, and `pip check` outputs;
- frontend lint, type-check, and build results;
- Alembic head and fresh-upgrade result;
- Kustomize and OpenTofu results;
- hygiene scan results;
- any warning, skipped check, or environmental limitation.

### Step 5: Commit only if the consistency scan changed files

```bash
rtk git add -A
rtk git commit -m "finish repository consistency pass"
```

If the scan changes nothing, do not create an empty commit.

---

## Final Acceptance

After all six task reviews are approved:

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
