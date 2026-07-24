# AgentCare runbook - every command, explained

This is the complete command reference for AgentCare, from a cold clone on a laptop to a
GKE Autopilot deployment on GCP. Every command below is copied from this repository's own
configuration (`AGENTS.md`, `docker-compose.yml`, `infra/`, `docs/deployment-gcp.md`), and
each one is followed by what it actually does and why the flags are there. Read it top to
bottom the first time to learn the system, then use Part 5 as the card you keep open while
you work.

---

## Part 1 - Local development

Python 3.12 and Node 22. Backend commands run from `backend/`, the seed runs from the
repository root. No Docker needed for this part.

### Create the virtualenv

```bash
python3 -m venv .venv
```

**What this does:**

Creates an isolated Python environment at `.venv/` in the repository root. Every backend
command in this runbook calls the interpreter inside it by path (`.venv/bin/python`) rather
than activating it, so there is no hidden shell state and the same command works from a
script, a Makefile or a fresh terminal.

---

### Install backend dependencies

```bash
.venv/bin/pip install -r requirements.txt
```

**What this does:**

* Installs every pinned dependency from the single root `requirements.txt` (FastAPI,
  LangGraph, SQLAlchemy 2.0, pwdlib, PyJWT, the OpenAI-compatible client and the test
  tooling).
* `requirements.txt` lives at the root, not under `backend/`, because CI dependency
  scanners read it there, and because the backend Dockerfile copies it before the app tree
  so a code change does not invalidate the pip layer.
* Pinned versions are deliberate: the stack has moving parts (LangGraph checkpointers, the
  Groq model lineup, Next.js `proxy.ts`) where an unpinned upgrade breaks things quietly.

---

### Create the database schema

```bash
cd backend && ../.venv/bin/alembic upgrade head
```

**What this does:**

* Runs every Alembic migration in order against `DATABASE_URL`. The default is SQLite
  (`sqlite:///./agentcare.db`), which needs no server and no setup.
* The `cd backend` is not cosmetic. `alembic.ini` and the `alembic/` directory live at the
  `backend/` root, and the default SQLite path is relative to the working directory, so
  running from anywhere else creates a second, empty database file in the wrong place.
* `upgrade head` means "apply everything up to the newest revision". It is safe to re-run:
  Alembic records the current revision in the database and skips what is already applied.

---

### Seed the synthetic demo data

```bash
.venv/bin/python scripts/seed_demo.py
```

**What this does:**

* Run this one from the repository root, not from `backend/`. The script puts `backend/` on
  `sys.path` and then `chdir`s into it itself, so the seed, Alembic and uvicorn all agree on
  the same `backend/agentcare.db` file.
* Creates departments, doctors, appointment slots, document requirements and three demo
  users, then prints a count per table.
* It is idempotent. Running it twice does not duplicate rows, so it is safe in a container
  entrypoint and safe to re-run after a schema change.
* All seed data is obviously synthetic on purpose. There is no real patient information in
  this repository.

---

### Start the API

```bash
cd backend && ../.venv/bin/python -m uvicorn app.main:app --reload
```

**What this does:**

* `app.main:app` points uvicorn at the `app` FastAPI instance inside `backend/app/main.py`.
* `--reload` restarts the server on every file save. Development only: it runs a file
  watcher and a second process, neither of which belongs in production.
* On startup the lifespan builds and compiles the LangGraph workflow once and opens the
  checkpointer for the process lifetime, then starts the in-process reminder scheduler.
* The API is then at `http://localhost:8000`, interactive docs at
  `http://localhost:8000/docs`, health at `http://localhost:8000/api/health` and Prometheus
  metrics at `http://localhost:8000/metrics`.

---

### Start the frontend

```bash
cd frontend
npm install
npm run dev
```

**What this does:**

* `npm install` resolves the Next.js 16 / React 19 / Tailwind v4 tree from
  `frontend/package-lock.json`.
* `npm run dev` runs `next dev` on port 3000.
* `frontend/next.config.ts` rewrites `/api/:path*` to `BACKEND_URL` (default
  `http://localhost:8000`). That rewrite is why the browser only ever talks to port 3000:
  the session cookie rides along same-origin and never crosses an origin boundary.

---

### Log in

Open `http://localhost:3000`.

| Role | Email | Password |
|---|---|---|
| Patient | `patient@agentcare-demo.com` | `demo1234` |
| Staff | `staff@agentcare-demo.com` | `demo1234` |

A third seeded account, `erika@agentcare-demo.com` (same password), is a patient with a
German language preference, useful for showing the German side of the safety screens.

These credentials come from `backend/app/db/seed.py` and exist only in seeded demo data.
They are not secrets and there is nothing real behind them.

---

### Run the test suite

```bash
cd backend && ../.venv/bin/python -m pytest -q
```

**What this does:**

* Runs the full suite from `backend/`, where `pytest` finds its configuration and the
  `app` package.
* `-q` prints one line per outcome instead of one line per test.
* No test touches the network and no test needs an API key: LLM-dependent tests inject a
  fake client through `app.agents.llm.set_llm_client_for_tests(...)`.
* The suite must stay green before every commit. Run the focused test while iterating, run
  the whole thing before you commit.

---

### Lint

```bash
.venv/bin/ruff check backend
```

**What this does:**

Runs ruff over the backend package. It must stay clean, not "mostly clean". Ruff is fast
enough that there is no reason to defer it to CI.

---

### Byte-compile check

```bash
.venv/bin/python -m compileall backend -q
```

**What this does:**

Parses and compiles every Python file under `backend/` without importing or executing any
of it. It catches a syntax error in a module that no test happens to import, which is
exactly the failure that otherwise surfaces first in a container at startup. `-q` prints
only failures.

---

## Part 2 - Full stack with Docker Compose

One command brings up the whole system, including the pieces the no-Docker path leaves out:
Postgres instead of SQLite, plus Prometheus and Grafana.

### Bring the stack up

```bash
docker compose up --build
```

**What this does:**

* `--build` rebuilds both application images before starting, so your working-tree changes
  are actually in the containers. Without it, Compose reuses whatever it built last time.
* Five services come up, in dependency order:
  * **db**: `postgres:16-alpine`, user/password/database all `agentcare`, data on the
    `db_data` named volume. It has a `pg_isready` healthcheck, and the backend waits on
    `service_healthy` rather than merely `service_started`, so migrations never race an
    unready database.
  * **backend**: built from `backend/Dockerfile` with the **repository root** as build
    context (the Dockerfile copies the root `requirements.txt` before the app tree).
    Published on port 8000. Its entrypoint runs `alembic upgrade head`, then the idempotent
    seed, then `exec`s uvicorn, so the database is populated by the time the container is
    healthy.
  * **frontend**: built from `./frontend` with `BACKEND_URL=http://backend:8000` baked in
    as a build arg, because `next.config.ts` reads it at module load time. Published on
    port 3000.
  * **prometheus**: `prom/prometheus:v3.13.1`, config bind-mounted read-only from
    `monitoring/prometheus.yml`, scraping `backend:8000/metrics` every 15 seconds.
  * **grafana**: `grafana/grafana:13.1.1` with provisioning and dashboards bind-mounted
    read-only from `monitoring/`, anonymous viewer access on and the admin password set to
    `admin` for the demo.

The URLs once it is up:

| Service | URL | Note |
|---|---|---|
| App | `http://localhost:3000` | log in with the demo accounts from Part 1 |
| API docs | `http://localhost:8000/docs` | FastAPI interactive OpenAPI page |
| Health | `http://localhost:8000/api/health` | also the k8s liveness and readiness probe path |
| Prometheus | `http://localhost:9090` | scrape target `agentcare-backend` |
| Grafana | `http://localhost:3001` | login `admin` / `admin`, AgentCare dashboard pre-provisioned |

Grafana is on host port 3001 mapped to container port 3000, because the frontend already
owns 3000 on the host.

---

### Watch the logs of one service

```bash
docker compose logs -f backend
```

**What this does:**

Follows just the backend's stdout instead of the interleaved output of five containers.
Swap `backend` for `db`, `frontend`, `prometheus` or `grafana`.

---

### Stop the stack, keep the data

```bash
docker compose down
```

**What this does:**

Stops and removes the containers and the default network. The `db_data` volume survives, so
the next `up` starts with the same Postgres contents.

---

### Reset everything, including the database

```bash
docker compose down -v
```

**What this does:**

* `-v` also removes the named volumes, which means `db_data` is deleted and Postgres comes
  back empty.
* This is the correct fix for a broken or half-migrated local database: the next
  `docker compose up --build` migrates and seeds a fresh one from scratch.
* It destroys local demo data only. Nothing here touches a remote database.

---

## Part 3 - Security, localhost to the public web

This part is narration, not commands. It explains what protects the application, and what
has to change when the same code stops serving `localhost` and starts serving the internet.
Everything described here exists in the repository today (`docs/security.md` is the longer
version).

### What guards the front door

**Passwords** are hashed with `pwdlib.PasswordHash.recommended()`, which is Argon2id
(`backend/app/auth/security.py`). pwdlib replaced passlib, which is unmaintained. The
database stores a hash and a salt, never a password.

**Sessions** are HS256 JWTs carrying the user id and role, issued at login and delivered as
a cookie: `httponly=True`, `samesite="lax"` and `secure` on whenever `ENVIRONMENT` is not
`dev`. Because the cookie is httpOnly, no JavaScript on the page can read the token, and
there is nothing in `localStorage` for a cross-site script to steal. `decode_token`
hardcodes `algorithms=["HS256"]` and never reads the algorithm out of the token header,
which is what blocks the classic algorithm-confusion attack (an attacker re-signing a token
as `none` or as HMAC over the public key).

**Authorization is backend-only truth** (`backend/app/auth/dependencies.py`).
`get_current_user` resolves the caller from the cookie and raises the same permission error
for every failure mode (missing cookie, expired token, mis-signed token, unknown subject) so
the API does not leak which one it was. `require_role("staff")` gates every staff route, and
`ensure_owner_or_staff(user, patient_id, db)` guards every patient-data query: staff pass,
a patient passes only for their own id. The frontend `proxy.ts` bounces a cookie-less
browser away from `/portal/*` and `/staff/*`, but it can only see that a cookie exists. It
cannot read the contents and proves nothing about role or validity. That redirect is user
experience. It is never the security boundary.

### The three-layer safety pipeline

AgentCare is administrative software: registration, routing, booking, documents, reminders
and follow-up. It never diagnoses, prescribes or doses. That boundary is enforced in code,
and two of the three layers are deterministic, which means no model can be talked out of
their decision.

1. **Deterministic pre-screen** (`backend/app/safety/guardrails.py::screen_request`) runs in
   `workflow_service.create_run`, before any graph node and before any LLM call. It matches
   the inbound request against English and German keyword lists with whole-word regex
   boundaries. An emergency phrase returns `escalate_emergency` with 112 guidance and opens
   an `emergency` escalation. A medical-advice ask returns `refuse_medical`. Both outrank an
   administrative request, so "book me an appointment and diagnose my cough" still refuses,
   and in both cases no model is called at all.

2. **Injection guard, then PII redaction, before every LLM call.**
   `backend/app/safety/injection_guard.py::screen_injection` runs on the patient's request
   text and on a document's extracted text before either reaches a prompt. Layer 1 is always
   on: EN/German regex for known injection phrasing, a 120+ character base64-looking run and
   role markers such as `assistant:` or `<|im_start|>`. Layer 2 is optional: a small
   classifier on Groq, used only when both `LLM_API_KEY` and `INJECTION_GUARD_MODEL` are
   set, and a classifier failure logs and falls back to layer 1 rather than blocking. Text
   that clears the guard then passes through `backend/app/safety/pii.py::redact_for_llm`,
   which replaces email addresses, phone numbers, IBANs, German health-insurance numbers and
   date-of-birth-shaped dates with typed `[REDACTED_...]` tokens. The redaction applies only
   to the copy heading for the model. The database keeps what the patient actually wrote,
   and the patient is never shown a redacted version of their own words. Each redacting node
   writes one `safety.pii_redacted` audit row carrying category counts only, never values.

3. **Output sanitizer** (`guardrails.py::sanitize_agent_output`) gets the last word. It
   splits the candidate response into sentences and replaces any whole sentence matching a
   forbidden pattern (a diagnosis, an explicit dosage, a treatment recommendation) with a
   fixed referral to the care team. Whole sentences, not matched substrings, so nothing
   medically specific survives around the edge of a regex. A model that reports `safe: true`
   over a poisoned sentence does not get to publish it.

Underneath all of it, every tool mutation, every agent node exit and every mutating route
writes an `AuditEvent` row. `write_audit` flushes but never commits, so the audit row lands
inside the same transaction as the change it documents and cannot drift away from it.

### Secrets

Configuration is environment-only, through pydantic-settings. `.env` is gitignored and
`.env.example` documents every key with no real values. Never committed, in any branch:
`.env`, `*.db`, `uploads/`, real patient data, credentials of any kind. The structlog setup
(`backend/app/logging_setup.py`) runs a redaction processor that recursively replaces the
value of any key containing `password`, `token`, `authorization`, `api_key` or `secret` with
`[redacted]`, so a secret cannot reach a log line even by accident.

### What changes when you leave localhost

Five things, and skipping any of them is a real hole:

1. **HTTPS.** On localhost the cookie is sent over plain HTTP and `secure` is off because
   `ENVIRONMENT=dev`. On GCP, `ENVIRONMENT` is `prod` in
   `infra/k8s/base/configmap.yaml`, so `secure` turns on and the cookie stops being sent
   over anything but TLS. Terminating TLS is the Ingress's job:
   `infra/k8s/overlays/gcp/ingress.yaml` provisions a GCE load balancer and a
   `ManagedCertificate`. That certificate needs a real domain with matching DNS, otherwise
   it sits in "Provisioning" forever without saying why.

2. **Origins.** `FRONTEND_ORIGIN` is `http://localhost:3000` locally and a
   placeholder in the ConfigMap that a real deploy must replace with the actual frontend
   origin. FastAPI's CORS middleware uses it as an explicit single-origin allowlist, with
   `allow_credentials=True`, which forbids wildcard origins, methods or headers. In normal
   use the browser never triggers CORS at all, because Next.js rewrites `/api/*` to the
   backend and every call is same-origin. The correct CORS origin is the seatbelt for the
   cases where it does.

3. **Single replica, on purpose.** `infra/k8s/base/backend.yaml` sets `replicas: 1` because
   the reminder scheduler runs in-process with APScheduler and has no distributed lock. Two
   replicas means two schedulers, which means duplicate reminders. Scaling out is a change
   to make after the jobs move to a separate worker, not a number to bump.

4. **Values that must not ship at their defaults.** Three of them:
   * `JWT_SECRET` - `.env.example` and `docker-compose.yml` both carry
     `change_me_generate_a_long_random_string`. Anyone who knows that string can mint a valid
     staff session for your deployment. Generate a long random value and put it in Secret
     Manager and in the Kubernetes Secret.
   * `INTERNAL_TASK_TOKEN` - empty by default, which makes
     `POST /api/internal/reminders/run-due` require a staff login cookie. If you set it so a
     cron caller can use the `X-Internal-Token` header instead, it must be a real random
     value, because it is a bearer credential for a route that mutates data.
   * The database password - `agentcare/agentcare` in `docker-compose.yml` is a local
     convenience. The Cloud SQL user is created by hand with a password you choose (Part 4),
     and it belongs in the Kubernetes Secret, nowhere else.

5. **Two hand-maintained copies of the same secrets.** Secret Manager holds the values, and
   the Kubernetes `Secret` the backend reads through `envFrom` holds them again. Nothing
   syncs one to the other. A rotation applied in one place and not the other leaves the
   cluster serving the old value. Rotate in both, in the same sitting.

---

## Part 4 - GCP deployment, end to end

> **Honest status note.** Nothing in this repository has applied any of what follows: there
> are no GCP credentials in this environment and none were created to write it. Every
> command below is taken from the committed Terraform, the kustomize manifests and
> `.github/workflows/deploy.yml`, and the manual gaps are called out where they exist, but
> the first real deploy still needs a GCP project with billing enabled. Treat this as a
> validated plan, not as a transcript of a run that happened.

The target shape: Artifact Registry for images, a GCS bucket for patient documents, IAM
including Workload Identity Federation for GitHub Actions, a GKE Autopilot cluster and a
Cloud SQL for PostgreSQL 17 instance. Terraform (OpenTofu-compatible HCL) is in
`infra/terraform/`, the Kubernetes side is a kustomize base plus a `gcp` overlay in
`infra/k8s/`.

### Authenticate the gcloud CLI

```bash
gcloud auth login
```

**What this does:**

Authenticates the `gcloud` command itself with your Google account, through a browser. No
other gcloud command works until this succeeds. This credential is for the CLI only.

---

### Authenticate the libraries

```bash
gcloud auth application-default login
```

**What this does:**

Writes a separate credential file that client libraries look for, called Application Default
Credentials. Terraform's Google provider reads ADC, not the gcloud CLI credential, which is
why both logins are needed. Skipping this one produces a "could not find default
credentials" failure at `tofu plan`, after you have already convinced yourself you are
logged in.

---

### Select the project

```bash
gcloud config set project YOUR_PROJECT_ID
```

**What this does:**

Sets the project every later gcloud command runs against, so you stop passing `--project` on
each one. Think of it as `cd`, for GCP projects.

---

### Enable the APIs this configuration needs

```bash
gcloud services enable \
  artifactregistry.googleapis.com \
  storage.googleapis.com \
  container.googleapis.com \
  sqladmin.googleapis.com \
  servicenetworking.googleapis.com \
  secretmanager.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  sts.googleapis.com
```

**What this does:**

* Turns on each Google API the Terraform resources call. The Terraform in this repository
  deliberately does not manage API enablement, so this runs once per project, before the
  first `tofu apply`.
* Enabling an already-enabled API does nothing, so this is safe to re-run.
* Why each one: `artifactregistry` for the image repository, `storage` for the documents
  bucket, `container` for GKE, `sqladmin` for Cloud SQL, `servicenetworking` for the Cloud
  SQL private-IP peering, `secretmanager` for the secrets and `iam` plus `iamcredentials`
  plus `sts` for the service accounts and Workload Identity Federation.
* Getting this wrong fails late and confusingly: creating a Cloud SQL instance errors out if
  `sqladmin` is off, even though the failure looks like a quota or permission problem.

---

### Install OpenTofu

```bash
which tofu || brew install opentofu
```

**What this does:**

Checks whether `tofu` is already on PATH and installs it with Homebrew if not. The HCL in
`infra/terraform/` is OpenTofu-compatible and plain Terraform runs it just as well:
substitute `terraform` for `tofu` in every command in this part if that is what you have.
The two read the same files, take the same flags and produce the same state format at these
versions.

---

### Initialize Terraform

```bash
cd infra/terraform
tofu init
```

**What this does:**

* Downloads the Google provider pinned in `versions.tf` and wires up the modules under
  `modules/`.
* Uses the local state backend by default (`backend.tf`), so no state bucket has to exist
  before your first run. `backend.tf` carries a commented `gcs` block: switch to it once you
  have created a state bucket by hand, then run `tofu init -migrate-state`.

---

### Plan the infrastructure

```bash
tofu plan \
  -var="project_id=YOUR_PROJECT_ID" \
  -var="github_repository=Gaurang140/agentcare"
```

**What this does:**

* Shows exactly what would be created, changed or destroyed, without touching anything.
  Read it. This is the last cheap moment to catch a wrong project id.
* `project_id` has no default, on purpose: there is no sensible fallback and a typo here
  creates resources in a project you did not mean.
* `github_repository` is the `owner/repo` string allowed to assume the deploy service
  account through Workload Identity Federation. It is what stops any other repository on
  GitHub from impersonating your deployer.
* Defaults you inherit unless you override them: `region` and `gcs_location` are
  `europe-west3` (EU data residency), and `enable_cloud_sql` is `true`.

---

### Apply

```bash
tofu apply \
  -var="project_id=YOUR_PROJECT_ID" \
  -var="github_repository=Gaurang140/agentcare"
```

**What this does:**

* Creates the Artifact Registry repository, the GCS documents bucket, the three service
  accounts with their IAM bindings, the Workload Identity Federation pool and provider, the
  GKE Autopilot cluster and (because `enable_cloud_sql` defaults to `true`) a Cloud SQL for
  PostgreSQL 17 instance named `agentcare-postgres` with private-IP networking.
* Cloud SQL is the primary database path while the GCP trial credit covers it. Pass
  `-var="enable_cloud_sql=false"` and point `DATABASE_URL` at Neon free-tier Postgres in the
  Kubernetes Secret once the credit lapses. That swap is one connection string and no code
  change.
* GKE Autopilot takes several minutes to create. That is normal.
* `tofu output` afterwards prints the Artifact Registry URL, the bucket name, the three
  service account emails, the cluster name and location, the Cloud SQL connection name and
  the `workload_identity_provider` string GitHub Actions needs.

---

### Create the secrets by hand

```bash
echo -n "your_groq_key" | gcloud secrets create llm-api-key --data-file=- --replication-policy=automatic
echo -n "a_long_random_string" | gcloud secrets create jwt-secret --data-file=- --replication-policy=automatic
```

**What this does:**

* Terraform never creates a secret value here, only the permission to read one
  (`roles/secretmanager.secretAccessor` on the backend service account in the `iam` module).
  Secret values are created by hand, once, after apply. That is the point: a secret value in
  a Terraform variable ends up in the state file.
* `echo -n` prints the value with no trailing newline. This is not a style detail. Without
  `-n`, the newline becomes part of the secret, and two processes reading the "same" secret
  can then disagree about what it is.
* `--data-file=-` reads the value from stdin, which is the piped `echo`, so the secret never
  appears as a command-line argument that a process listing or a shell history file can pick
  up.
* `--replication-policy=automatic` lets Google choose where to store the replicas.

To rotate a value later, add a version instead of deleting and recreating the secret:

```bash
echo -n "rotated_value" | gcloud secrets versions add llm-api-key --data-file=-
```

Readers pinned to `:latest` pick the new version up, and the old version stays available to
roll back to.

---

### Create the database and the database user by hand

```bash
gcloud sql databases create agentcare --instance=agentcare-postgres
gcloud sql users create agentcare --instance=agentcare-postgres --password="A_LONG_RANDOM_PASSWORD"
```

**What this does:**

* The Terraform module creates the Cloud SQL instance and its private-IP networking only.
  It creates no database and no user, so the `DATABASE_URL` the backend needs does not exist
  until you run both of these.
* The instance is the server. The database is the named storage inside it where tables live.
  The user is the identity the application connects as, which is not the `postgres` admin.
* The instance has no public IP. Anything connecting to it either runs inside the VPC (the
  GKE pods do) or goes through the Cloud SQL Auth Proxy using the `cloud_sql_connection_name`
  output.
* Assemble the resulting connection string as
  `postgresql+psycopg://agentcare:PASSWORD@HOST:5432/agentcare` and put it in the Kubernetes
  Secret in the next step.

---

### Point Docker at Artifact Registry

```bash
REPO=$(tofu -chdir=infra/terraform output -raw artifact_registry_repository_url)
gcloud auth configure-docker "${REPO%%/*}"
```

**What this does:**

* `tofu -chdir=infra/terraform output -raw` reads one output value with no quotes or
  formatting around it, so it drops straight into a shell variable. `$REPO` ends up looking
  like `europe-west3-docker.pkg.dev/YOUR_PROJECT/agentcare`.
* `${REPO%%/*}` strips everything from the first `/` onward, leaving just the registry
  host (`europe-west3-docker.pkg.dev`). `gcloud auth configure-docker` wants the host, not
  the full image path.
* This writes a credential helper entry into your Docker config so `docker push` to that
  host authenticates as you. Once per machine per host. Skip it and the push fails with
  "unauthorized", which reads like a permissions problem in GCP rather than a missing local
  helper.

---

### Build and push both images

```bash
docker buildx build --platform linux/amd64 -t "$REPO/backend:latest" -f backend/Dockerfile --push .
docker buildx build --platform linux/amd64 -t "$REPO/frontend:latest" --push ./frontend
```

**What this does:**

* Run both from the repository root.
* `--platform linux/amd64` forces the target architecture. GKE Autopilot nodes are amd64,
  and an image built on an Apple Silicon laptop without this flag is arm64 and crash-loops
  on the cluster with "exec format error".
* The backend build context is `.`, the repository root, not `backend/`. The Dockerfile
  copies the root `requirements.txt` before the app tree, so it needs the root in context,
  which is why the Dockerfile is named explicitly with `-f backend/Dockerfile`.
* The frontend is the simple case: context `./frontend`, Dockerfile found there by default.
* `--push` builds and pushes in one step. Without it the image stays in the local builder
  cache and the cluster cannot pull it.
* This matches what `.github/workflows/deploy.yml` builds, so a manual deploy and a CI
  deploy produce the same images.

---

### Create the Kubernetes Secret out-of-band

```bash
gcloud container clusters get-credentials \
  "$(tofu -chdir=infra/terraform output -raw gke_cluster_name)" \
  --region "$(tofu -chdir=infra/terraform output -raw gke_cluster_location)"
```

**What this does:**

Fetches cluster credentials and writes a context into your `~/.kube/config`, so `kubectl`
now talks to this cluster. Autopilot clusters are regional, hence `--region` and not
`--zone`.

```bash
kubectl create secret generic agentcare-secrets \
  --from-literal=LLM_API_KEY=your_groq_key \
  --from-literal=LLM_FALLBACK_API_KEY= \
  --from-literal=JWT_SECRET=a_long_random_string \
  --from-literal=DATABASE_URL='postgresql+psycopg://user:pass@host:5432/agentcare' \
  --from-literal=INTERNAL_TASK_TOKEN= \
  --from-literal=LANGFUSE_SECRET_KEY=
```

**What this does:**

* Creates the `agentcare-secrets` Secret that both the backend Deployment and the migration
  Job read through `envFrom`. Run it once per cluster or namespace, before the first
  `kubectl apply -k`.
* The keys and this exact command come from `infra/k8s/base/secret.example.yaml`. That file
  is a template and is deliberately **not** listed in `base/kustomization.yaml`, so
  `kubectl apply -k` never overwrites your real Secret with its `REPLACE_ME` placeholders.
* `JWT_SECRET` and `DATABASE_URL` are the two that must carry real values. The empty ones
  are optional features (the fallback LLM endpoint, the internal task token, Langfuse
  tracing) that stay off until you fill them.
* The non-secret half of the configuration lives in `infra/k8s/base/configmap.yaml`. Note
  that its `FRONTEND_ORIGIN` is a placeholder that a real deploy has to replace once the
  domain or load balancer IP is known.

To rotate one value later without retyping the rest:

```bash
kubectl create secret generic agentcare-secrets --from-literal=JWT_SECRET=new_value \
  --dry-run=client -o yaml | kubectl apply -f -
```

`--dry-run=client -o yaml` renders the manifest locally instead of sending it, and the
piped `kubectl apply` merges it. Delete-and-recreate would leave the app without a Secret in
between, which restarts pods into a crash loop.

---

### Wire Workload Identity for the pods (manual, not yet in the manifests)

**This step is not wired.** Terraform creates the two runtime service accounts and the
cluster is created with `workload_identity_config`, but nothing under `infra/k8s/` creates a
Kubernetes service account, carries the `iam.gke.io/gcp-service-account` annotation or sets
`serviceAccountName` on a pod. Until someone does this by hand, the pods run under the
namespace default service account, so the backend does not reach GCS or Secret Manager as
`agentcare-backend`. The app still runs: it reads its configuration from the ConfigMap and
Secret, and local file storage works. What does not work is anything that needs the
backend's own GCP identity.

```bash
BACKEND_SA=$(tofu -chdir=infra/terraform output -raw backend_service_account_email)
kubectl create serviceaccount agentcare-backend
kubectl annotate serviceaccount agentcare-backend "iam.gke.io/gcp-service-account=$BACKEND_SA"
gcloud iam service-accounts add-iam-policy-binding "$BACKEND_SA" \
  --role roles/iam.workloadIdentityUser \
  --member "serviceAccount:YOUR_PROJECT_ID.svc.id.goog[default/agentcare-backend]"
```

**What this does:**

* Creates a Kubernetes service account and annotates it with the Google service account it
  should act as.
* The `add-iam-policy-binding` is the other half of the handshake: it tells the Google
  service account which Kubernetes identity is allowed to impersonate it. The member string
  is the workload identity pool format, `PROJECT.svc.id.goog[NAMESPACE/KSA_NAME]`. Both
  halves are required. One without the other silently does nothing.

Then add `serviceAccountName: agentcare-backend` to the pod spec in
`infra/k8s/base/backend.yaml`.

The Workload Identity Federation that GitHub Actions uses is a different mechanism and it
**is** wired: see `.github/workflows/deploy.yml` and the `workload_identity_provider`
Terraform output.

---

### Point the overlay at the images you just pushed

```bash
cd infra/k8s/overlays/gcp
kustomize edit set image \
  "agentcare-backend=$REPO/backend:latest" \
  "agentcare-frontend=$REPO/frontend:latest"
```

**What this does:**

* Rewrites the two image entries in `overlays/gcp/kustomization.yaml` from their
  `REGION-docker.pkg.dev/PROJECT/agentcare/...:TAG` placeholders to your real registry path
  and tag.
* `agentcare-backend` and `agentcare-frontend` are the image names used in the base
  manifests, which is what the transform matches on. The base carries the name without a
  real tag on purpose so this rewrite is the only place a tag is set.
* CI does exactly this with the git SHA as the tag instead of `latest`, right before its
  apply. Prefer an immutable tag over `latest` for anything you plan to roll back.

---

### Clear the previous migration Job

```bash
kubectl delete job backend-migrate --ignore-not-found
```

**What this does:**

* Job specs are immutable once created, so a leftover `backend-migrate` from the previous
  deploy makes the next `kubectl apply -k` fail outright, whether that Job succeeded or is
  still running.
* `--ignore-not-found` makes it a no-op on the first deploy, so the same command works every
  time.
* The Job has `ttlSecondsAfterFinished: 600` and cleans itself up ten minutes after
  completing, but that does not help a still-running one, which is why the explicit delete
  is here. CI runs the same command before every apply.

---

### Apply the overlay

```bash
kubectl apply -k infra/k8s/overlays/gcp
```

**What this does:**

* `-k` builds the kustomization and applies the result. The `gcp` overlay pulls in the base
  (ConfigMap, backend Deployment plus Service, frontend Deployment plus Service and the
  migration Job), adds `ingress.yaml` and `backendconfig.yaml`, then patches the backend
  Service with a `cloud.google.com/backend-config` annotation so the GCE Ingress applies its
  3600 second timeout to the SSE traffic instead of cutting the live timeline off at the
  default.
* Render it first without touching the cluster if you want to see exactly what goes up:
  `kubectl kustomize infra/k8s/overlays/gcp`.
* The migration Job runs the backend image's own entrypoint (`alembic upgrade head`, then
  the idempotent seed) rather than reimplementing either step, so the cluster database gets
  the same treatment the local one gets.

---

### Watch the rollout

```bash
kubectl rollout status deployment/backend --timeout=300s
kubectl rollout status deployment/frontend --timeout=300s
```

**What this does:**

Blocks until each Deployment reports its new pods as available, or gives up after five
minutes. A first Autopilot deploy is slower than later ones, because Autopilot provisions
nodes to fit the pods' requests.

```bash
kubectl logs job/backend-migrate
```

**What this does:**

Prints the migration Job's output, which is where an Alembic failure or a wrong
`DATABASE_URL` shows up first. If the backend pods are unhealthy, read this before anything
else: a backend that cannot reach its database looks identical from the outside to a dozen
other problems.

---

### Find the address and verify

```bash
kubectl get ingress agentcare -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
```

**What this does:**

Prints just the external IP of the GCE load balancer, with no table headers around it. It is
empty for the first few minutes while the load balancer provisions, and stays empty forever
if you skipped `ingress.yaml`.

```bash
curl -sf --max-time 10 "http://INGRESS_IP/api/health"
```

**What this does:**

Hits the same `/api/health` endpoint the liveness and readiness probes use. `-s` silences
the progress meter, `-f` makes curl exit non-zero on an HTTP error instead of printing the
error body as if it were success and `--max-time 10` stops it hanging on an Ingress that is
not serving yet.

For a demo without paying roughly 18 USD/month for the load balancer, skip `ingress.yaml`
and port-forward instead:

```bash
kubectl port-forward svc/frontend 3000:3000
```

**What this does:**

Tunnels local port 3000 to the frontend Service through the Kubernetes API. The app is then
at `http://localhost:3000` with no public IP, no load balancer and no cost. The tunnel lives
as long as the command runs.

---

### Teardown

```bash
kubectl delete -k infra/k8s/overlays/gcp
```

**What this does:**

Removes everything the overlay created, including the Ingress and therefore the load
balancer, which is the expensive part. The `agentcare-secrets` Secret survives, because it
was never part of the kustomization.

```bash
cd infra/terraform
tofu destroy \
  -var="project_id=YOUR_PROJECT_ID" \
  -var="github_repository=Gaurang140/agentcare"
```

**What this does:**

* Destroys everything Terraform created: the cluster, Cloud SQL, the bucket, Artifact
  Registry and the IAM bindings. Run it as soon as a demo is over. GKE Autopilot pods and a
  Cloud SQL instance bill continuously, and the trial credit is finite.
* The documents bucket leaves `force_destroy` at its default `false`, so `tofu destroy`
  fails on a non-empty bucket rather than quietly deleting whatever a patient uploaded
  during the demo. That failure is the feature. If you really want it gone, empty it
  deliberately first:

```bash
gsutil -m rm -r gs://BUCKET_NAME/**
```

`-m` deletes in parallel, `-r` recurses and `/**` matches every object under the prefix. It
is not reversible.

---

## Part 5 - Quick reference card

```
LOCAL BACKEND (from repo root unless noted)
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
  cd backend && ../.venv/bin/alembic upgrade head
  .venv/bin/python scripts/seed_demo.py
  cd backend && ../.venv/bin/python -m uvicorn app.main:app --reload

LOCAL FRONTEND
  cd frontend && npm install && npm run dev
  # app http://localhost:3000  |  api docs http://localhost:8000/docs

CHECKS BEFORE EVERY COMMIT
  cd backend && ../.venv/bin/python -m pytest -q
  .venv/bin/ruff check backend
  .venv/bin/python -m compileall backend -q

DOCKER COMPOSE
  docker compose up --build
  docker compose logs -f backend
  docker compose down          # keep the db volume
  docker compose down -v       # wipe the db volume and start clean
  # app :3000  api :8000  prometheus :9090  grafana :3001 (admin/admin)

GCP - ONE-TIME PROJECT SETUP
  gcloud auth login
  gcloud auth application-default login
  gcloud config set project YOUR_PROJECT_ID
  gcloud services enable artifactregistry.googleapis.com storage.googleapis.com container.googleapis.com sqladmin.googleapis.com servicenetworking.googleapis.com secretmanager.googleapis.com iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com
  which tofu || brew install opentofu

GCP - INFRASTRUCTURE
  cd infra/terraform
  tofu init
  tofu plan  -var="project_id=YOUR_PROJECT_ID" -var="github_repository=Gaurang140/agentcare"
  tofu apply -var="project_id=YOUR_PROJECT_ID" -var="github_repository=Gaurang140/agentcare"
  tofu output

GCP - MANUAL STEPS AFTER APPLY
  echo -n "your_groq_key" | gcloud secrets create llm-api-key --data-file=- --replication-policy=automatic
  echo -n "a_long_random_string" | gcloud secrets create jwt-secret --data-file=- --replication-policy=automatic
  gcloud sql databases create agentcare --instance=agentcare-postgres
  gcloud sql users create agentcare --instance=agentcare-postgres --password="A_LONG_RANDOM_PASSWORD"

IMAGES
  REPO=$(tofu -chdir=infra/terraform output -raw artifact_registry_repository_url)
  gcloud auth configure-docker "${REPO%%/*}"
  docker buildx build --platform linux/amd64 -t "$REPO/backend:latest" -f backend/Dockerfile --push .
  docker buildx build --platform linux/amd64 -t "$REPO/frontend:latest" --push ./frontend

CLUSTER
  gcloud container clusters get-credentials "$(tofu -chdir=infra/terraform output -raw gke_cluster_name)" --region "$(tofu -chdir=infra/terraform output -raw gke_cluster_location)"
  kubectl create secret generic agentcare-secrets --from-literal=LLM_API_KEY=... --from-literal=LLM_FALLBACK_API_KEY= --from-literal=JWT_SECRET=... --from-literal=DATABASE_URL='postgresql+psycopg://user:pass@host:5432/agentcare' --from-literal=INTERNAL_TASK_TOKEN= --from-literal=LANGFUSE_SECRET_KEY=
  cd infra/k8s/overlays/gcp && kustomize edit set image "agentcare-backend=$REPO/backend:latest" "agentcare-frontend=$REPO/frontend:latest"
  kubectl delete job backend-migrate --ignore-not-found
  kubectl apply -k infra/k8s/overlays/gcp
  kubectl rollout status deployment/backend --timeout=300s
  kubectl rollout status deployment/frontend --timeout=300s

VERIFY
  kubectl logs job/backend-migrate
  kubectl get ingress agentcare -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
  curl -sf --max-time 10 "http://INGRESS_IP/api/health"
  kubectl port-forward svc/frontend 3000:3000     # zero-cost demo path, no ingress

TEARDOWN
  kubectl delete -k infra/k8s/overlays/gcp
  cd infra/terraform && tofu destroy -var="project_id=YOUR_PROJECT_ID" -var="github_repository=Gaurang140/agentcare"
  gsutil -m rm -r gs://BUCKET_NAME/**              # only if you want the documents gone
```
