# Architecture decisions

This file records current choices and their trade-offs. It is not a change
diary. Runtime detail lives in [architecture.md](architecture.md), controls
live in [security.md](security.md) and operator commands live only in
[deployment-gcp.md](deployment-gcp.md).

## Explicit LangGraph state machine

**Decision:** Use a typed LangGraph `StateGraph` with one coordinator, five
specialist roles and a deterministic escalation node.

**Why:** Administrative work needs visible transition rules, persisted pause
and resume plus checkpoint recovery. A free-form agent conversation would hide
those decisions.

**Trade-off:** Adding a workflow step requires a state field, node, transition
guard and audit behavior. That explicit work is intentional.

The graph remains small and uses shared state. Subgraphs and conversational
memory would add boundaries the current workflow does not need.

## Deterministic gates around model work

**Decision:** Enforce emergency, medical-scope and known injection rules before
model work. Enforce transition ordering and patient-facing medical boundaries
after model output in code.

**Why:** A prompt cannot guarantee an emergency handoff, prevent medical advice
or stop an invalid database transition. Code can.

**Trade-off:** Pattern rules require maintenance and can never cover every
expression. Optional Model Armor or classifier screening adds a second
opinion, while the deterministic layers remain first and last.

Provider screening fails open to the deterministic result. A positive provider
verdict blocks, but a network failure does not stop all administration.

## Six model-assisted roles plus human control

**Decision:** Keep coordinator, routing, appointment, document, follow-up and
safety/finalization as the six model-assisted roles. Keep escalation
deterministic.

**Why:** The roles match distinct prompts, tools and audit events. Human
approval is a control decision, not another autonomous role.

**Trade-off:** The coordinator makes more model calls than a fixed pipeline.
The graph gains the ability to revisit a specialist or stop when the request
does not justify the next step.

## Synchronous transaction core

**Decision:** Keep SQLAlchemy tool work and LangGraph invocation synchronous.
Use FastAPI asynchronous features only at their real boundaries.

**Why:** One ordinary SQLAlchemy session makes transaction ownership,
conditional slot claims and atomic audit writes clear. LangGraph uses
`durability="sync"` so the completed side effect is checkpointed before the
next super-step.

**Trade-off:** A true async conversion cannot be achieved by changing route
syntax. It would require async nodes, an async database layer, awaited model
work and `.ainvoke` or `.astream`.

Lifespan, HTTP middleware and SSE delivery remain natively asynchronous.
FastAPI `BackgroundTasks` moves graph work outside the HTTP response, but the
work itself is synchronous.

## LangChain factory with YAML and environment precedence

**Decision:** Build models only through LangChain `init_chat_model`. Define
named defaults in `backend/llm.yaml`, with environment variables overriding
the selected profile field by field.

**Why:** Groq and local OpenAI-compatible endpoints plus Google GenAI on Vertex
fit one provider-neutral factory. Configuration changes do not require model
construction elsewhere in the codebase.

**Trade-off:** Each provider still needs its matching LangChain integration.
Provider-specific parameters remain in the profile and require focused tests.

Current profiles are:

- `groq`, the default through `langchain-openai`
- `local`, an OpenAI-compatible local endpoint
- `vertex`, Gemini through `google_genai` with `vertexai: true`

Vertex construction is unit-tested. Live authentication, quota and response
behavior have not been verified.

## `chat_json` as the model policy boundary

**Decision:** Route every structured agent result through
`app/agents/llm.py::chat_json`.

**Why:** LangChain `with_structured_output` should own provider formatting and
ordinary schema parsing. The application still needs one place for transport
retry, strict-schema compatibility, a corrective prompt, fallback selection
and application errors.

**Trade-off:** The boundary contains provider compatibility policy as well as
validation policy. Keeping it singular prevents those rules from drifting
between agents.

The optional injection classifier is the only other model call. It returns a
plain label and does not pretend to be structured agent output.

## Model-bound PII protection

**Decision:** Keep the original administrative record in SQL. Redact supported
PII only on the copy prepared for each model call, using deterministic
patterns plus Presidio.

**Why:** The patient record and audit context need the original request, while
the model provider usually does not need inline identifiers.

**Trade-off:** Stored data remains sensitive. Small NER models and pattern
families cannot guarantee complete PII detection.

Redaction is a call-site boundary, not a global pre-graph transformation.

## Separate checkpoint and domain responsibilities

**Decision:** Treat LangGraph checkpoints and application SQL rows as separate
state systems, even when Postgres hosts both.

**Why:** Checkpoints own execution position, interrupts and resume identity.
Domain SQL owns patients, bookings, documents, reminders, escalations and
audit. Combining their responsibilities would make recovery overwrite
business truth.

**Trade-off:** Operators must migrate and monitor both sets of tables.

Local SQLite uses separate files for domain rows and checkpoints. Postgres
uses `PostgresSaver` in the same server/database with its own tables.

## Direct SQL tools and append-only audit

**Decision:** Give specialist roles narrow SQL tools. Write an `AuditEvent`
inside every owning mutation transaction.

**Why:** Appointment claims, cancellations, reminders and staff approvals need
database concurrency semantics. A second generic tool service would obscure
the transaction that owns each change.

**Trade-off:** Tools are coupled to the relational domain model. That is
preferred to eventually consistent writes for this application.

`write_audit` flushes but does not commit. The mutation and its event therefore
commit or roll back together. Replay-sensitive operations check existing work
because checkpoint resume can restart a node.

## SQL for procedural rules

**Decision:** Store staff-controlled agent rules in an audited SQL table and
load active rows for each call.

**Why:** These rules are short, exact and administrative. They need RBAC,
history and deterministic retrieval rather than semantic memory.

**Trade-off:** The system does not provide fuzzy cross-thread memory. Add a
separate design only if a concrete use case needs it.

## Backend RBAC as the sole authorization truth

**Decision:** Enforce role and ownership in FastAPI dependencies. Treat
frontend redirects as user experience only.

**Why:** Browser code can be bypassed. Backend checks cover every staff and
patient-data route regardless of client.

**Trade-off:** Each new route must declare its dependency explicitly and be
covered by authorization tests.

## GCP-only deployment adapters

**Decision:** Keep GCP as the only cloud deployment target:

- GKE Autopilot for backend and frontend workloads
- Cloud SQL for Postgres
- GCS for documents
- Artifact Registry for images
- Workload Identity for the backend runtime identity
- Model Armor for optional provider screening

Infrastructure source lives in `infra/terraform` and Kubernetes source lives
in `infra/k8s`.

**Why:** One cloud path can be documented and reviewed end to end. Parallel
cloud procedures would create unverified configuration branches.

**Trade-off:** GKE and Cloud SQL create real cost while running. The backend
must remain one replica while APScheduler jobs have no distributed lock.

The backend KSA/GSA relationship is declarative. Kubernetes migrations are a
separate Job-only stage that must succeed before the application overlay.
Deployment remains manual. The configuration is committed but not evidence
of live deployment. Operator work still includes billing, API enablement,
database and secret creation, DNS, TLS, migration and smoke tests.

## Process-local background execution

**Decision:** Use FastAPI `BackgroundTasks` for graph dispatch and APScheduler
for reminders and stalled-run handling. Do not add a broker for the current
single-process application.

**Why:** It keeps the local system small and makes current transaction behavior
easy to follow.

**Trade-off:** A process loss can interrupt an active run. Checkpoints preserve
graph progress, while the stall sweep sends abandoned work to staff. Multiple
backend replicas would duplicate scheduled jobs.

Move dispatch and periodic jobs to a durable external worker before scaling
the backend horizontally.
