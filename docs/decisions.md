# Architecture decisions

Each decision below is written as a short ADR: Context, Decision, Why (with verified versions,
dates and costs) and Revisit-when. Every version and price was verified on 2026-07-23. Each ADR
is tagged with an honest status: **Implemented now** means the code is in this repo and runs;
**Designed for scale** means the decision is recorded for the GCP or container path that is not
built yet (there is no `infra/`, `Dockerfile` or `docker-compose.yml` in the repo).

Free-tier-first is the governing rule. Anything with a real monthly floor goes in a documented
scale path, not the hackathon build.

---

## ADR-01: FastAPI for the API layer

**Status:** Implemented now.

**Context.** The backend has to serve JSON routes, stream Server-Sent Events for a live workflow
timeline, run an LLM agent graph and validate request and response bodies with the same models
the rest of the code uses.

**Decision.** FastAPI 0.139.2 (released 2026-07-16, Python 3.10+ floor) on `uvicorn[standard]`
0.51.0.

**Why.** FastAPI is async-native, which SSE and the LangGraph invocation both need, and it shares
Pydantic v2 with the LLM output schemas and the settings layer, so one validation model covers the
HTTP body and the agent contract. The `lifespan` context manager (the current idiom, `on_event` is
deprecated) builds the graph and starts the scheduler once at startup and tears both down cleanly.
Global exception handlers map the `AppError` hierarchy to typed JSON envelopes. A sync framework
would force a separate async worker for the streaming and agent paths.

**Revisit when.** Never, for this project. FastAPI is the settled default for a Python agent API.

---

## ADR-02: LangGraph for orchestration, not CrewAI or AutoGen

**Status:** Implemented now.

**Context.** Six agents coordinate one patient request. The run must survive a process crash and
resume exactly where it stopped, and a hospital-administration workflow needs an auditable,
deterministic control flow rather than free-form agent chatter.

**Decision.** LangGraph 1.2.9 (released 2026-07-10, an LTS 1.0 line active until 2.0), with
`langgraph-checkpoint-sqlite` and `langgraph-checkpoint-postgres` 3.1.0.

**Why.** LangGraph gives an explicit `StateGraph` with a checkpointer, so crash-resume is
`graph.invoke(None, config)` on the same `thread_id`, re-entering from the last saved super-step.
That fault tolerance is the load-bearing feature here and is not a first-class primitive in CrewAI
or AutoGen, which lean toward role-play and conversation and hide the state machine. The typed
`AgentState` with `Annotated[list, operator.add]` reducers lets several nodes write the same key in
one step without the default "can receive only one value per step" error. The coordinator loop
(specialists always return to the coordinator, two terminal nodes) is a shape LangGraph expresses
directly. LangGraph 1.0 had no breaking change in the core graph API, only a Python 3.10 floor, so
the pin is low-risk.

**Revisit when.** A genuinely conversational, open-ended multi-agent product emerges, where role
delegation matters more than a fixed auditable state machine.

---

## ADR-03: Postgres and SQLite, with Neon for the demo

**Status:** Implemented now (SQLite default, Postgres switch in code). Deployment target designed.

**Context.** The app needs a relational core for patients, appointments and the audit trail, and
the LangGraph checkpointer needs a persistent store too. The demo has to cost about zero and stay
alive between the build and later interviews.

**Decision.** SQLAlchemy 2.0.51 over SQLite by default and Postgres (via `psycopg[binary]` 3.3.4)
when `DATABASE_URL` points at one. **Neon** free-tier Postgres for the live demo. **Cloud SQL for
PostgreSQL** as the enterprise path. AlloyDB, Spanner, Firestore and Memorystore are rejected.

**Why.**
- SQLite needs no setup for local development, and a single `DATABASE_URL` change moves both the
  app and the checkpointer onto Postgres with no code change.
- Neon free tier: 0.5 GB storage per project, 100 compute-hours per month, auto scale-to-zero after
  5 minutes idle, no credit card, PostgreSQL 14 through 18. It survives dormancy and wakes fast, so
  the portfolio piece keeps working with no attention. Supabase was the alternative but auto-pauses
  after 7 days of inactivity and needs a manual unpause, which is the wrong failure mode here.
- Cloud SQL is the boring correct managed Postgres for the enterprise story: same SQLAlchemy,
  Alembic and `PostgresSaver`, real SLA. It has no always-free tier. `db-f1-micro` is about $8/mo
  and dev-only with no SLA, and the cheapest dedicated-core `db-standard-1` is about $49/mo plus
  roughly $0.17 to $0.22 per GB-month of SSD. The Cloud SQL 30-day free-trial instance (8 vCPU,
  64 GB, 100 GB storage, $0) can prove the real managed service at $0 for a side demo.
- Rejected, each with a real non-free floor and no capability this app needs: **AlloyDB** about
  $180 to $200/mo for a 2 vCPU / 8 GB shape, no free tier; **Spanner** about $40 to $50/mo for 100
  processing units, no free tier, solves a global-write-scale problem this app does not have;
  **Firestore** has a free tier but is a document store and would fragment the relational core and
  the audit trail; **Memorystore for Redis** about $36/mo for the cheapest Basic instance, no free
  tier and nothing in the design needs sub-millisecond shared state.

**Revisit when.** Real multi-tenant OLTP write load appears (then AlloyDB earns its floor), or a
fast lane genuinely needs shared sub-millisecond state (then Memorystore).

---

## ADR-04: Google Cloud Storage for uploaded documents

**Status:** Implemented now as an adapter interface (local backend). GCS backend designed.

**Context.** Patient documents are binary blobs that do not belong in the relational database.

**Decision.** A storage adapter with one method, `save(patient_id, filename, content) -> ref`.
`LocalStorage` is the default and is fully implemented. `GCSStorage` matches the same interface for
`STORAGE_BACKEND=gcs`.

**Why.** Keeping one narrow interface means the agent and route code never knows which backend is
live. `google-cloud-storage` is intentionally not installed, so `GCSStorage` imports it lazily and
raises a clear `AppError` (not an `ImportError`) if the backend is selected without the package.
On GCP, Always-Free Cloud Storage is 5 GB-months of regional storage in US regions only
(`us-east1`, `us-west1`, `us-central1`), which covers a demo's worth of PDFs for free. Uniform
bucket-level access is Google's documented default and is the intended setting: it disables ACLs
and keeps access IAM-only. A EU-resident bucket for a data-residency story gets none of the free
tier, but the cost is a few cents either way.

**Revisit when.** Documents need lifecycle rules, customer-managed encryption keys or EU residency,
all of which are bucket configuration, not a code change.

---

## ADR-05: Terraform HCL, run with OpenTofu

**Status:** Designed for scale. No `infra/` directory exists yet.

**Context.** The GCP infrastructure (Artifact Registry, Cloud Run, the database, a GCS bucket, IAM)
should be declarative and recognizable to any reviewer.

**Decision.** Write standard Terraform HCL and run it with **OpenTofu** 1.12.x and the
`hashicorp/google` provider pinned `~> 7.41`. The `terraform` CLI works on the same files
identically.

**Why.** OpenTofu 1.12.5 (released 2026-07-21) is MPL 2.0, Linux Foundation governed and a drop-in
fork: identical `.tf` syntax, identical state format, identical provider protocol. Terraform 1.15.8
is under BUSL 1.1 (each release converts to MPL 2.0 four years later), and while its Additional Use
Grant does not restrict a portfolio project, "why not Terraform" is a fair interview question and
OpenTofu is a clean, no-downside answer after the IBM acquisition of HashiCorp in 2025. HCL is still
the most recognized IaC syntax, so a reviewer sees standard `.tf` files and switching back to the
Terraform CLI later is a one-word change, not a rewrite. The `hashicorp/google` provider ships every
one to two weeks and works against OpenTofu unmodified; pinning `~> 7.41` from a 2026 start avoids
the v6 to v7 breaking changes entirely. Pulumi, GCP Infrastructure Manager (Terraform-only, no
OpenTofu) and Crossplane were considered and add a language runtime, a managed control plane or a
Kubernetes dependency that six static modules do not need.

**Revisit when.** A client mandates the Terraform CLI (swap the binary) or the project grows into a
self-service infra platform (then reconsider Pulumi or Crossplane).

---

## ADR-06: Groq openai/gpt-oss-120b, with an LM Studio fallback

**Status:** Implemented now.

**Context.** The agents need reliable structured JSON out of an LLM, on a free tier, with a local
fallback for offline development and demo resilience.

**Decision.** Primary model `openai/gpt-oss-120b` on Groq's OpenAI-compatible endpoint
(`https://api.groq.com/openai/v1`), asked for strict `json_schema` output. Optional fallback to a
local LM Studio server (`http://localhost:1234/v1`) in `json_object` mode.

**Why.** The obvious pick, `llama-3.3-70b-versatile`, is on death row: deprecation was announced
2026-06-17 with hard EOL on 2026-08-16, so building new work on it is a dead end. `gpt-oss-120b` is
Groq's current production flagship and, with `gpt-oss-20b`, the only Groq model that supports
`response_format: json_schema` with `strict: true` (constrained decoding that guarantees
schema-valid JSON). Pricing is $0.15 per million input tokens and $0.60 per million output tokens,
131,072-token context, free developer tier with no card. `chat_json` (`app/agents/llm.py`) asks for
the strict schema first and, if the endpoint rejects that request shape (a local LM Studio server
does), retries the same call in `json_object` mode with the schema spelled out in the prompt, then
validates the reply with Pydantic. Groq strict mode requires every property in `required`, which is
why nullable fields are typed `T | None` with no Python default. Strict schema support on this exact
model has open community reports of being ignored in some cases, so the `json_object` path plus
Pydantic validation is a deliberate safety net, not dead code.

**Revisit when.** Groq deprecates `gpt-oss-120b` (check the deprecations page), or a provider with
better structured-output guarantees appears on the free tier.

---

## ADR-07: A custom LLM wrapper, not LiteLLM

**Status:** Implemented now.

**Context.** The app calls two OpenAI-compatible endpoints (Groq and a local LM Studio) and needs
retry, fallback and schema validation. LiteLLM is the well-known library for multi-provider routing.

**Decision.** Skip LiteLLM. Route every call through one function, `chat_json`
(`app/agents/llm.py`), that wraps two `OpenAI` clients with tenacity backoff, a strict-schema then
`json_object` fallback, a single validation re-prompt and a one-shot switch to the fallback
endpoint when the primary's retries are exhausted.

**Why.** LiteLLM 1.93.0 (MIT, released 2026-07-19) exists to normalize wildly different provider
SDKs into one shape. Groq and LM Studio already speak the identical OpenAI schema, so there is no
translation problem for it to solve, and the base install pulls 12 core dependencies for machinery
(cooldowns, ordered fallback groups, YAML config) that a single `try/except` covers here. Hosted
routers are worse: OpenRouter and Portkey cannot reach a `localhost:1234` LM Studio process without
a public tunnel, which breaks the free local-fallback story and adds an account and, for Portkey's
production tier, a $49/mo bill. The custom wrapper is fully legible during a live demo, which a
LiteLLM `Router` exception mid-demo is not. The research framing was a roughly 30-line wrapper; the
real `chat_json` is larger because it also owns validation and the schema fallback, but the decision
is the same: one call site, no routing dependency.

**Revisit when.** The provider roster genuinely grows (Anthropic-on-Vertex, Bedrock). Then swapping
the internals of that one call site for `litellm.Router` is a contained change.

---

## ADR-08: LangMem rejected

**Status:** Designed decision (rejected, not adopted). Preference storage implemented as plain columns.

**Context.** The app could remember patient preferences (language, preferred appointment window)
across conversations. LangMem is the LangChain package pitched for exactly that.

**Decision.** Do not add `langmem`. Store low-cardinality preference data in plain Postgres columns
through the existing SQLAlchemy models (`patient_profiles.preferred_language` already exists).

**Why.** LangMem's last PyPI release is 0.0.30 on 2025-10-27, roughly 9 months stale as of today,
and it has never left the `0.0.x` band in about 2.5 years. Its recent GitHub activity is dependency
bumps only with no feature work, and its examples still target the deprecated `create_react_agent` API.
For this app the memory candidates are exact-match structured fields, a textbook fit for a SQL
column: `WHERE preferred_language = 'de'` is deterministic and explainable, which a compliance-
flavored hospital workflow wants, whereas LangMem rewrites memory through nondeterministic LLM
judgment and spends an extra Groq call per consolidation pass for no benefit over an `UPDATE`. If
free-text cross-session memory is ever needed, LangGraph's own `PostgresStore` with `store.search()`
covers it with no new dependency, which is the pattern LangChain's current docs teach anyway. The
rejection itself, with these numbers, is the stronger engineering signal.

**Revisit when.** A real need for free-text, cross-session semantic recall appears. Then use
LangGraph's native store, not LangMem.

---

## ADR-09: Langfuse Cloud free tier, env-gated

**Status:** Implemented now as an optional, off-by-default integration.

**Context.** Per-agent-hop traces with token, cost and latency detail would help debugging, but the
build must not take on infrastructure or spend for it.

**Decision.** Wire `langfuse` 4.14.1 as an optional integration, gated on two env keys. When
`LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are both set, `workflow_service._observability`
attaches a `CallbackHandler` to the graph invocation and stamps the `workflow_id` into the trace via
`propagate_attributes`. With the keys empty (the default), the path is fully inert.

**Why.** Langfuse Cloud Hobby is 50,000 units per month free with no credit card, 30-day retention,
2 users. A LangGraph run makes roughly 10 to 20 observations, so that ceiling allows thousands of
demo runs a month, far past what a build needs. Crossing the cap does not auto-bill on Hobby, it
caps ingestion. Self-hosting v3/v4 is not "Langfuse plus Postgres" anymore: it needs ClickHouse,
Redis and an S3-compatible store on top, which is real infra with no upside for a demo, so it is
documented as the enterprise path only. The gate matters for a second reason: the callback lives in
`langfuse.langchain`, which imports `langchain`, and `langchain` is deliberately not pinned in
`requirements.txt`, so the default and CI paths never import it. The trace correlates to the audit
trail by `workflow_id`, layered alongside the audit rows rather than replacing them.

**Revisit when.** Trace volume approaches 50k units/month (upgrade to Core at $29/mo for the user
cap and 90-day retention), or data sovereignty forces the self-hosted GKE path.

---

## ADR-10: Prometheus and Grafana, local compose

**Status:** `/metrics` implemented now. The compose dashboard stack is designed.

**Context.** A live metrics dashboard is a strong, low-effort signal, but a GKE cluster for it is
not justified in a 3-day window.

**Decision.** Instrument FastAPI with `prometheus-fastapi-instrumentator` 8.0.2, which is installed
and wired in `app/main.py` as `Instrumentator().instrument(app).expose(app)`, exposing `/metrics`.
The local dashboard is a designed Docker Compose stack of Prometheus v3.13.1 (an LTS supported
through 2027-07-31) and Grafana 13.1.1. Google Managed Service for Prometheus (GMP) is the
documented GKE path.

**Why.** The instrumentator gives request rate, latency histograms and error counters out of the
box with no per-metric code. A compose stack scraping `/metrics` is local, costs nothing and gives a
clickable dashboard during a demo, a better signal per effort than anything GCP-hosted this week. On
GKE, GMP graduates the same endpoint with no Prometheus server to run, billed at $0.06 per million
samples with no storage charge and 24-month retention, which is near-free at this scale but needs a
live cluster, so it stays documented, not built. OpenTelemetry is the more unifying 2026 pattern but
needs a collector and more setup, noted as the natural next step since Cloud Trace and Cloud
Monitoring both ingest OTLP.

**Revisit when.** The app lands on a real Kubernetes cluster (adopt GMP or the
`kube-prometheus-stack` chart), or traces, metrics and logs need unifying (adopt OpenTelemetry).

---

## ADR-11: No Redis and no Celery

**Status:** Implemented now (FastAPI BackgroundTasks plus APScheduler).

**Context.** Two kinds of deferred work exist: run the agent graph off the request thread and run
periodic jobs (send due reminders, sweep stalled workflows).

**Decision.** Use FastAPI `BackgroundTasks` for off-thread graph execution and APScheduler 3.11.3
for the periodic jobs. No Redis, no Celery, no external broker.

**Why.** `POST /api/requests` hands graph execution to a `BackgroundTasks` callback that opens its
own session, so the request returns immediately. APScheduler runs two in-process jobs: reminders
every 60 seconds and a stalled-workflow sweep every 600 seconds (escalating any run stuck in
`running` past 30 minutes). Both are no-ops under `TESTING`. A single-process uvicorn app has no need
for a broker or worker fleet, and Memorystore for Redis carries an about $36/mo floor with no free
tier for state this design does not keep. The scale answer is named, not built: Cloud Tasks or Cloud
Scheduler for the periodic and queued work, Pub/Sub if fan-out is ever needed.

**Revisit when.** The app runs more than one process or instance (then APScheduler's in-process
timer double-fires, and Cloud Scheduler or Cloud Tasks becomes the right home for the jobs).

---

## ADR-12: GCP security lineup

**Status:** Application-layer security implemented now (see `docs/security.md`). The GCP-native
lineup below is designed for the deployment, which is not built yet.

**Context.** A hospital-administration app on GCP needs a credible, mostly-free security posture,
split into what a first deploy turns on and what waits for real scale.

**Decision.** Implement on first deploy: **Secret Manager**, **Workload Identity Federation** for
keyless CI, **Artifact Analysis** image scanning and **Identity-Aware Proxy** on the staff surface
only. Document as the scale path: **Binary Authorization**, **Cloud Armor**, **VPC Service Controls**
and **Security Command Center**.

**Why (first deploy, all free or near-free).**
- Secret Manager holds the Groq key, DB credentials and the JWT secret. Free tier is 6 active
  versions, 10,000 access operations and 3 rotation notifications per month, which a handful of
  secrets stays inside; beyond it, $0.06 per version per month.
- Workload Identity Federation makes GitHub Actions mint short-lived tokens instead of storing a
  long-lived JSON service-account key (the top GCP credential-leak vector). It costs $0. The
  mandatory `attribute-condition` scoping the pool to this exact repo is the one step that must not
  be skipped.
- Artifact Analysis scans images for CVEs at $0.26 per image on first push of a digest (roughly
  $3 to $8 for a whole build) and produces a real vulnerability report to show.
- IAP puts Google-identity access in front of the staff or admin surface with one flag, now that
  direct IAP-on-Cloud-Run reached GA around March 2026 (no load balancer needed), at $0. It gates on
  Google identities, so it fits the staff surface only, never the public patient endpoint.
- Baseline that is not optional: one dedicated service account per Cloud Run service with
  resource-scoped IAM bindings (never the default Compute Engine SA with project Editor), plus the
  `disableServiceAccountKeyCreation` and `automaticIamGrantsForDefaultServiceAccounts` org policy
  constraints, all $0.

**Why (scale path, real cost or org-level).**
- Binary Authorization is free on Cloud Run but its attestor, KMS key and CI signing wiring is real
  engineering for later.
- Cloud Armor needs a global external load balancer plus about $5/mo per policy, $1/mo per rule and
  $0.75 per million requests, so it is not justified for synthetic demo traffic.
- VPC Service Controls is $0 but operates at the organization level and needs Access Context Manager,
  so it is unavailable to a single-project build.
- Security Command Center Standard tier is genuinely free and can be toggled on for a findings
  dashboard; the Premium and Enterprise tiers bill per vCPU-hour and stay off.

**Revisit when.** The app carries real (non-synthetic) patient data on a public endpoint (then
Cloud Armor and Binary Authorization move up), or it gains a Cloud Identity organization (then
VPC-SC and org-wide SCC become available).
