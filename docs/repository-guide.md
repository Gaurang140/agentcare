# Repository guide

This guide answers three questions for a new engineer:

1. What does each tracked folder own?
2. Which local files are generated and intentionally absent from Git?
3. Which production resources live outside the repository?

The [architecture guide](architecture.md) explains runtime control flow. This
guide explains source ownership.

## Top-level map

| Path | Owner and purpose |
|---|---|
| `.github/workflows/` | CI, automatic application deployment and organizer checks |
| `backend/` | Python API, LangGraph workflow, SQL domain logic, safety and tests |
| `frontend/` | Next.js patient portal and staff console |
| `infra/bootstrap/` | One-time Terraform for remote state and GitHub OIDC |
| `infra/terraform/` | GCP application infrastructure managed by Terraform |
| `infra/k8s/` | Kustomize base, GCP application overlay and migration overlay |
| `monitoring/` | Local Prometheus scrape config and local Grafana dashboard |
| `evals/` | Reproducible safety and workflow evaluation dataset and scorer |
| `scripts/` | Synthetic seed and deterministic deployment renderer |
| `docs/` | Public architecture, security, operations and demo documentation |
| `requirements.txt` | Pinned Python runtime, test and evaluation dependencies |
| `docker-compose.yml` | Complete local stack for development and demonstration |
| `.env.example` | Configuration names with no secret values |
| `AGENTS.md` | One contributor rule file for coding agents |
| `LICENSE` | MIT license for the submitted source |

## Backend

```text
backend/
  alembic/                 schema migration runner and revisions
  app/
    agents/                LangGraph state, graph and six model-assisted roles
    api/                   FastAPI routes and HTTP ownership checks
    auth/                  password hashing, cookie JWT and backend RBAC
    db/                    SQLAlchemy engine, sessions and synthetic seed
    models/                persistent domain entities
    observability/         privacy-safe optional Langfuse integration
    safety/                scope, injection, PII and output controls
    schemas/               request and response validation models
    services/              workflow execution and document storage adapters
    tools/                 transactional database operations used by agents
    config.py              environment-backed application settings
    logging_setup.py       structured logging with sensitive-key redaction
    main.py                FastAPI lifecycle, middleware, routes and metrics
    scheduler.py           in-process reminder and stalled-workflow jobs
  tests/                   unit, integration, security and deployment contracts
  llm.yaml                 named Groq, local and Vertex model profiles
```

`backend/app/agents/llm.py` is the only chat-model construction and invocation
boundary. `backend/llm.yaml` selects providers and model parameters without
duplicating clients in each agent.

`backend/app/tools/` is not a mock layer. Tools read and mutate the same SQL
domain tables used by the API. Mutations write `AuditEvent` in the caller's
transaction.

`backend/app/observability/` is separate from the business audit. Langfuse
failure cannot change workflow outcome.

## Frontend

```text
frontend/
  app/                     App Router pages and route layouts
  components/              reusable workflow, status and navigation views
  hooks/                   client-side API state hooks
  lib/                     typed API client, domain types and UI helpers
  proxy.ts                 navigation guard for Next.js 16
  next.config.ts           standalone build and in-cluster API rewrite
  Dockerfile               multi-stage production image
```

Frontend route checks are user experience only. FastAPI dependencies remain
the authorization boundary.

## Infrastructure

```text
infra/
  bootstrap/
    main.tf                state bucket, GitHub OIDC and deployer identity
    variables.tf           project and immutable GitHub identity inputs
    outputs.tf             GitHub production environment values
    versions.tf            Terraform and Google provider constraints
  terraform/
    modules/               Artifact Registry, GCS, GKE, IAM, SQL, Model Armor
    backend.tf             partial remote GCS state configuration
    main.tf                resources, modules and the reserved Ingress address
    variables.tf           environment-level infrastructure choices
    outputs.tf             values consumed during application setup
  k8s/
    base/                  reusable backend, frontend, Services and config
    overlays/gcp/          GCP identity, storage, ingress, TLS and metrics
    overlays/gcp-migration/database migration and seed Job
```

The bootstrap and application stacks have different lifecycles. Bootstrap
creates the state bucket and keyless deployment identity. The main stack owns
the resources that can be created or destroyed for an AgentCare environment.

The committed Kubernetes files contain named sentinels. The deployment runner
copies the complete `infra/k8s` tree to a temporary directory and replaces
those sentinels through `scripts/render_gcp_manifests.py`. It never edits the
tracked source manifests.

## Monitoring and evaluation

`monitoring/` is needed for local operational metrics:

- Prometheus scrapes `backend:8000/metrics`
- Grafana displays request rate, latency and HTTP failures

GCP does not deploy these two containers. `PodMonitoring` sends the same
metrics to Google Managed Service for Prometheus. See
[observability](observability.md).

`evals/` is also needed. It is evidence that safety behavior and routing can be
measured instead of claimed. Phase 1 records application output against the
golden dataset. Phase 2 scores the saved evidence. Keeping it does not
duplicate backend tests:

- tests check individual contracts and regressions
- evals measure end-to-end behavior and produce judge-facing evidence

## Tracked documentation

| Document | Source of truth for |
|---|---|
| `README.md` | project entry point, local start and evidence summary |
| `docs/architecture.md` | graph, agents, tools, state and runtime boundaries |
| `docs/security.md` | enforced controls and known limitations |
| `docs/ci-cd.md` | GitHub release flow and automatic deployment |
| `docs/deployment-gcp.md` | first deployment, Terraform lifecycle and teardown |
| `docs/observability.md` | audit, logs, metrics, Grafana and Langfuse |
| `docs/decisions.md` | engineering decisions and rejected alternatives |
| `docs/demo-script.md` | two-minute demonstration sequence |
| `evals/README.md` | evaluation dataset, execution and interpretation |

## Generated or local files not committed

These files can exist in a working directory and still be absent from
`git status` because `.gitignore` excludes them:

| Local path or pattern | Why it stays out of Git |
|---|---|
| `.env`, `.env.*` | API keys, signing secret and local configuration |
| `.venv/`, `node_modules/` | installed dependencies |
| `.next/` | generated frontend build |
| `*.db`, `*.sqlite3` | local domain and checkpoint databases |
| `uploads/` | local patient document bytes |
| `.terraform/` | downloaded providers and backend metadata |
| `*.tfstate*` | infrastructure state can contain sensitive values |
| `.worktrees/` | local isolated Git worktrees created during development |
| `gha-creds-*.json` | short-lived GitHub OIDC credential file |
| `gha-kubeconfig-*` | short-lived deployment kubeconfig |
| `__pycache__/`, `.pytest_cache/`, `.ruff_cache/` | generated caches |

The repository currently has local ignored SQLite and Terraform state files.
They are operator data, not missing source. Never copy them into a new public
repository. Migrate the Terraform state to GCS before discarding this working
directory.

## Publish a clean repository

Do not copy this working directory into another folder. Git pushes only
tracked files, so ignored databases, state, uploads, caches and `.env` stay
local:

```bash
git status --short
git status --ignored --short
git remote add origin https://github.com/OWNER/REPOSITORY.git
git push -u origin main
```

Before pushing, `git status --short` should show no uncommitted change. Review
ignored files locally, but do not force-add them. Keeping the existing Git
history also gives judges useful authorship and implementation evidence.

## Production resources outside Git

The following are references in source, not values stored in source:

- Google Cloud project and billing account
- GKE cluster and runtime Kubernetes Secret
- Cloud SQL database, user and password
- Artifact Registry images
- GCS document and Terraform state buckets
- Model Armor template and private endpoint
- GitHub `production` environment variables
- GitHub Actions secrets, including `SUBMISSION_TOKEN`
- Langfuse project and secret key
- Google user login and Application Default Credentials
- public DNS ownership and certificate state

The exact setup order is in [GCP deployment](deployment-gcp.md). Daily
code-release behavior is in [CI/CD](ci-cd.md).
