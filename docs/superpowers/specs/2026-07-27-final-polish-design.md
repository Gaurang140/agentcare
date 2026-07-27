# AgentCare final-polish design

## Objective

Produce a concise, truthful and maintainable hackathon repository without
changing AgentCare's proven workflow semantics. Remove files and dependencies
that have no runtime or operator value, consolidate repeated code and make the
README and architecture documentation match the implementation.

## Constraints

- Keep GCP as the only cloud deployment target.
- Keep the synchronous SQLAlchemy transaction layer, synchronous LangGraph
  execution and synchronous scheduler jobs. FastAPI already runs synchronous
  path operations in its worker pool; changing only function syntax would be a
  false async conversion.
- Keep async code where the implementation performs native asynchronous work:
  application lifespan, request middleware and SSE streaming.
- Preserve emergency refusal, medical-advice refusal, prompt-injection
  screening, PII redaction, Model Armor fallback, RBAC, checkpoint resume,
  interrupt/resume and replay-idempotency behavior.
- Do not introduce another agent framework, guardrail framework, queue, cache or
  state-management library.
- Add a dependency only when committed runtime configuration directly uses it.
- Delete confirmed junk permanently. Do not create an archive, legacy or attic
  directory inside the repository.
- Do not claim that GCP has been deployed or live-smoke-tested.

## Runtime architecture

The runtime remains an explicit LangGraph `StateGraph`:

1. FastAPI authenticates the caller and persists the request.
2. Deterministic emergency and medical-scope gates run before any model call.
3. The prompt-injection guard screens model-bound input.
4. A coordinator selects among routing, appointment, document, follow-up and
   safety nodes.
5. Specialist nodes call real SQL-backed domain tools and append audit events.
6. The escalation node pauses with LangGraph `interrupt()` and resumes the same
   thread after a persisted staff decision.
7. SQLite or Postgres checkpointers persist graph progress. SQLite or Postgres
   stores domain state. GCS and Model Armor are GCP adapters.

PII redaction is not a global pre-graph gate. Each node redacts the copy it is
about to send to a model while the original administrative record remains in
the database.

The six model-assisted roles are coordinator, routing, appointment, document,
follow-up and safety. Escalation is a deterministic human-control node, not a
seventh autonomous agent.

## Code cleanup

### Permanent deletions

Delete files only after a repository-wide reference check:

- unused Next.js starter assets:
  `frontend/public/file.svg`, `globe.svg`, `next.svg`, `vercel.svg`,
  `window.svg`;
- `backend/app/tools/patient_tools.py`, whose only consumer is its own test;
- the test that exists only for the deleted patient tool;
- the unused `SafetyBlockedError`;
- the unused `create_followup_task` helper and its self-contained test;
- `frontend/README.md` and `backend/alembic/README`, which add no information
  beyond canonical project documentation;
- `docs/runbook.md`, after its unique commands are merged into the README or GCP
  deployment guide;
- `docs/index.md`, after the README owns the documentation map;
- `docs/architecture.mmd`, after `docs/architecture.md` becomes the only
  detailed diagram source.

### Dependency cleanup

- Remove `pytest-asyncio`; the suite contains no async tests, markers or config.
- Remove `next-themes`; no `ThemeProvider` exists and its only consumer always
  falls back to the system theme.
- Keep `langchain-openai` for the verified Groq OpenAI-compatible default.
- Add `langchain-google-genai==4.2.7` because the committed GCP model profile
  must use the current unified Gemini/Vertex AI integration rather than the
  deprecated `ChatVertexAI` package.
- Do not add `langchain-groq`: the verified default uses Groq's
  OpenAI-compatible endpoint and another SDK would duplicate that path.

### Backend consolidation

Create `app/agents/support.py` with two behavior-focused helpers:

- `record_agent_exit(db, agent_name, workflow_id, summary)` writes the existing
  `agent.<name>.completed` audit event and commits once;
- `redact_request_for_agent(db, state, agent_name)` resolves language, redacts
  the request copy and writes counts-only PII audit metadata.

All six agents use the shared exit helper. Coordinator and routing use the
shared redaction helper. Safety and replay comments that explain invariants
remain; task-history essays and dependency claims that are no longer true are
removed.

Use Pydantic's ORM validation for `WorkflowRunSummary` instead of two manual
serializers. Consolidate reminder row construction behind one non-committing
private helper while preserving the single-reminder and batch transaction
boundaries.

Remove unused function arguments and unused frontend API/type exports. Do not
remove `AgentState.user_id` in this pass because staff workflow detail currently
exposes persisted state and removing the key would be an observable API change.

### Audit accuracy

Registration creates both `User` and `PatientProfile` rows and therefore writes
a `user.registered` audit event in the same transaction. Documentation says
domain mutations are audited; it does not broaden that statement to every HTTP
request.

## Model configuration

`backend/llm.yaml` remains the editable profile file and environment variables
remain the deployment override. The verified default remains Groq through
`langchain-openai`. Add an enabled `vertex` profile using provider
`google_genai`, model `gemini-2.5-flash` and `vertexai: true`.

The Vertex profile relies on Application Default Credentials plus
`GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION`. Unit tests verify that the
profile reaches `init_chat_model` with the correct provider and parameters; no
documentation may describe that as a live GCP model test.

`chat_json(...)` remains the application's single structured-model policy
boundary, but it delegates schema conversion, provider request formatting and
happy-path parsing to LangChain's `with_structured_output(...)`. AgentCare
continues to own the behavior LangChain does not supply: transport-only retry,
strict-schema to JSON-mode compatibility fallback, one corrective re-prompt,
fallback-model selection and stable application exceptions. This removes the
OpenAI-shaped manual schema code and makes the Vertex profile a real
provider-neutral path rather than configuration that only constructs a model.

## Frontend cleanup

Replace the ineffective `useTheme()` call in the Sonner wrapper with the
current effective value, `system`, while preserving caller override precedence.
Remove the unused API functions, response types and unused state from
`useCurrentUser`.

Do not add a query/cache library or redesign authentication state in this pass.
The duplicate `/me` requests are a separate behavior and test-infrastructure
project, not a safe deletion.

## Documentation structure

The final human-facing set is:

- `README.md`: product value, request-flow diagram, stack, repository map,
  prerequisites, quickstart, two high-value demos, tests/evaluation,
  configuration summary and links to detailed docs;
- `docs/architecture.md`: runtime components, graph topology, request and
  interrupt sequences, state/persistence, data model and deployment view;
- `docs/security.md`: trust boundaries, deterministic gates, prompt injection,
  PII, RBAC, uploads, audit and human approval;
- `docs/deployment-gcp.md`: the only end-to-end GCP procedure, including honest
  manual gaps, verification, troubleshooting and teardown;
- `docs/decisions.md`: short current decisions, not a historical narrative;
- `docs/demo-script.md`: executable two-minute demo;
- `evals/README.md`: evaluation harness details;
- `infra/k8s/README.md`: short manifest-local apply and teardown reference.

The README target is 250-360 lines and no more than about 2,000 words. It uses
one compact Mermaid request-flow diagram. Architecture documentation may use
additional diagrams only when they show a different view.

Remove stale statements that LangChain, GCS or Model Armor packages are absent.
Correct the README diagram so PII redaction occurs at model boundaries. Correct
the demo so duplicate-upload evidence is shown through a new request and the
staff audit view, not an impossible portal upload action.

## Error handling

Existing domain exceptions, deterministic fallbacks and agent error-to-human
handoff semantics remain unchanged. Configuration errors continue to log and
fall back to environment defaults so a malformed optional profile does not
prevent the demo from starting.

The cleanup must not replace specific errors with broad catches or create a new
global utility module.

## Testing and acceptance

Behavior changes use red-green TDD:

- registration audit test fails before the audit write and passes afterward;
- agent-support tests fail before the shared module exists and verify real
  redaction/audit side effects;
- Vertex profile test fails before the profile is active and verifies the
  `init_chat_model` boundary without network calls.
- a real OpenAI SDK client over `httpx.MockTransport` proves LangChain produces
  recursively strict nested JSON Schema for the Groq-compatible endpoint;
- existing validation repair, JSON-mode compatibility fallback, transport
  retry and fallback-model tests remain behavior contracts while the inner
  implementation moves to `with_structured_output`.

Mechanical deletions and prose changes use existing behavior suites plus static
verification.

Final acceptance requires:

- complete backend test suite;
- Ruff over backend and evals;
- Python byte compilation;
- `pip check`;
- frontend ESLint and production build;
- Alembic single-head check and an upgrade from an empty SQLite database;
- rendered GCP Kustomize overlay;
- Terraform/OpenTofu validation when the executable is available;
- repository-wide scans for removed symbols, stale claims, tracked secrets and
  generated data;
- independent task reviews and a final whole-branch review.

The result is complete only when the worktree is clean and the reviewed branch
is integrated into `main`.
