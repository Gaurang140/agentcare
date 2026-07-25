# Deploying AgentCare to GCP

**Status: validated configuration, not yet applied to a GCP project.** Every command below is
taken from the committed Terraform, the kustomize manifests, `.github/workflows/deploy.yml` or
the application code, and the whole configuration validates on its own (`tofu validate`,
`kubectl kustomize`, `actionlint`). Nothing here has run against a live project: there are no
GCP credentials in this environment, so there is no cluster, no database and no bucket yet.
Read it as a procedure to follow, not as a transcript of a run that happened.

Work through it top to bottom. The order matters, because several steps consume outputs from
earlier ones. Everything assumes macOS or Linux.

The infrastructure lives in `infra/terraform/`, the Kubernetes manifests in `infra/k8s/` and
the deploy pipeline in `.github/workflows/deploy.yml`. `docs/runbook.md` Part 4 walks the same
path command by command and explains what each flag does; this file is the procedure, that one
is the explanation. `docs/decisions.md` ADR-03 through ADR-05 and ADR-12 carry the reasoning
and the verified costs.

---

## What gets built

Terraform (`infra/terraform/`, five modules) provisions:

| Resource | Detail |
|---|---|
| Artifact Registry | Docker repository `agentcare` in `var.region`, cleanup policy keeping the last 10 versions per package |
| GCS | Bucket `PROJECT_ID-agentcare-documents` in `var.gcs_location`, uniform bucket-level access, public access prevention enforced |
| IAM runtime | Service accounts `agentcare-backend` (bucket-scoped `storage.objectAdmin`, project `secretmanager.secretAccessor`, plus `cloudsql.client` when Cloud SQL is enabled) and `agentcare-frontend` (no extra roles) |
| IAM CI | Service account `agentcare-deploy` (`artifactregistry.writer`, `container.developer`) plus a Workload Identity pool and provider pinned to one GitHub repository |
| GKE | Autopilot cluster `agentcare` in `var.region`, REGULAR release channel, Workload Identity enabled, deletion protection off |
| Cloud SQL | PostgreSQL 17 instance `agentcare-postgres`, `db-f1-micro` on the ENTERPRISE edition, private IP only, created when `enable_cloud_sql` is true (the default) |

Defaults you inherit unless you override them: `region` and `gcs_location` are `europe-west3`
for EU data residency, `enable_cloud_sql` is `true` and the Cloud SQL module peers the
project's `default` VPC network for private service access. `project_id` and
`github_repository` have no defaults on purpose.

Kubernetes (`infra/k8s/`) is a kustomize base plus a `gcp` overlay:

| Object | Detail |
|---|---|
| `agentcare-config` ConfigMap | Non-secret settings mirroring `backend/app/config.py`; `ENVIRONMENT=prod`, `FRONTEND_ORIGIN` a placeholder |
| `backend` Deployment and Service | One replica, port 8000, `/api/health` liveness and readiness probes, `envFrom` the ConfigMap and the Secret |
| `frontend` Deployment and Service | One replica, port 3000 |
| `backend-migrate` Job | Runs the backend image's own entrypoint (`alembic upgrade head` then the idempotent seed), `ttlSecondsAfterFinished: 600` |
| `agentcare` Ingress (overlay) | GCE class, `/api` to the backend Service and `/` to the frontend Service, plus a `ManagedCertificate` named `agentcare-cert` |
| `backend-backendconfig` (overlay) | `timeoutSec: 3600` so the load balancer does not cut the SSE workflow timeline at its 30 second default |
| ConfigMap storage patch (overlay) | `STORAGE_BACKEND: gcs` and `GCS_BUCKET: REPLACE_ME` |

**The backend runs at one replica on purpose.** Its APScheduler jobs (due reminders every 60
seconds, a stalled-workflow sweep every 600 seconds) run in the same process that serves
requests, with no distributed lock, so a second replica fires every reminder twice. Graph
execution is a FastAPI `BackgroundTasks` callback in that same process, so a pod that restarts
mid-run leaves the run in `running` until the stall sweep escalates it after 30 minutes.
Scaling out means moving those jobs to a worker first, not raising `replicas`.

**Three things this configuration deliberately leaves to you**, called out again at the steps
that need them and in `docs/runbook.md` Part 4:

1. The Cloud SQL **database and its user**. The module creates the instance and its networking
   only (Step 5).
2. The **`agentcare-secrets` Kubernetes Secret**. `secret.example.yaml` is a template that
   kustomize never renders, so no placeholder can overwrite a real Secret (Step 7).
3. The **Workload Identity binding for the pods**. Nothing under `infra/k8s/` creates a
   Kubernetes service account or sets `serviceAccountName` (Step 8).

---

## Prerequisites

A GCP project with billing enabled, a GitHub account and an OpenAI-compatible LLM endpoint with
a key (the default is the Groq free tier, `.env.example` documents the variables).

You need four CLIs: the Google Cloud CLI (`gcloud`), OpenTofu or Terraform, `kubectl` and the
standalone `kustomize` binary. On macOS the middle two are one command each:

```bash
which tofu || brew install opentofu
which kustomize || brew install kustomize
```

Confirm they are all there:

```bash
gcloud --version
tofu -version
kubectl version --client
kustomize version
```

`kubectl` carries kustomize's engine already, which covers rendering (`kubectl kustomize`) and
applying (`kubectl apply -k`). The standalone binary is needed only for
`kustomize edit set image` in Step 9; CI installs it at 5.8.1
(`.github/workflows/deploy.yml`).

Plain Terraform runs the same files: substitute `terraform` for `tofu` in every command below.
The HCL, the state format and the provider protocol are identical at these versions.

---

## Step 1 - Authenticate and select the project

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID
```

Two logins, not one. The first authenticates the `gcloud` command itself. The second writes
Application Default Credentials, which is what Terraform's Google provider reads. Skipping the
second one fails at `tofu plan` with "could not find default credentials", after you have
already convinced yourself you are logged in.

---

## Step 2 - Enable the APIs this configuration needs

The Terraform here does not manage API enablement, so run this once per project before the
first apply:

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

Enabling an already-enabled API is a no-op, so this is safe to re-run. Getting it wrong fails
late and confusingly: creating a Cloud SQL instance with `sqladmin` off reads like a quota or
permission problem.

---

## Step 3 - Provision the infrastructure

```bash
cd infra/terraform

tofu init

tofu plan \
  -var="project_id=YOUR_PROJECT_ID" \
  -var="github_repository=Gaurang140/agentcare"

tofu apply \
  -var="project_id=YOUR_PROJECT_ID" \
  -var="github_repository=Gaurang140/agentcare"
```

`github_repository` is the `owner/repo` string allowed to assume the deploy service account
through Workload Identity Federation. It becomes the provider's `attribute_condition`, which is
what stops any other repository on GitHub from impersonating your deployer.

State is local by default (`backend.tf`), so no state bucket has to exist first. `backend.tf`
carries a commented `gcs` block: create a bucket by hand, uncomment it, then run
`tofu init -migrate-state` if more than one person will run Terraform.

GKE Autopilot takes several minutes. When the apply finishes, read the outputs:

```bash
tofu output
```

Later steps use `artifact_registry_repository_url`, `documents_bucket_name`,
`backend_service_account_email`, `gke_cluster_name`, `gke_cluster_location` and
`workload_identity_provider`.

To run without Cloud SQL, pass `-var="enable_cloud_sql=false"` and point `DATABASE_URL` at an
external Postgres (Neon free tier is the documented post-credit swap, `docs/decisions.md`
ADR-03). That swap is one connection string and no code change.

---

## Step 4 - Create the secret values out of band

Terraform never creates a secret value here, only the permission to read one. A secret value in
a Terraform variable ends up in the state file, which is the reason for the split.

```bash
echo -n "your_llm_api_key" | gcloud secrets create llm-api-key --data-file=- --replication-policy=automatic
echo -n "a_long_random_string" | gcloud secrets create jwt-secret --data-file=- --replication-policy=automatic
```

The `-n` is not decoration. Without it `echo` appends a newline, that newline becomes part of
the secret value, and two processes reading the "same" secret then disagree about what it is.
`--data-file=-` reads from stdin, so the value never appears as a command-line argument a
process listing or a shell history file can pick up.

To rotate later, add a version instead of deleting and recreating:

```bash
echo -n "rotated_value" | gcloud secrets versions add llm-api-key --data-file=-
```

Secret Manager and the Kubernetes Secret in Step 7 are two hand-maintained copies of the same
values with nothing syncing them, so a rotation has to be applied in both places in the same
sitting. The production note at the end of this document names the add-on that removes the
second copy.

---

## Step 5 - Create the database and its user

**Manual gap 1 of 3.** The module creates the instance and its private-IP networking only, so
the `DATABASE_URL` the backend needs does not exist until you run both of these:

```bash
gcloud sql databases create agentcare --instance=agentcare-postgres
gcloud sql users create agentcare --instance=agentcare-postgres --password="A_LONG_RANDOM_PASSWORD"
```

The instance has no public IP. Anything connecting to it either runs inside the VPC, which the
GKE pods do, or goes through the Cloud SQL Auth Proxy with the `cloud_sql_connection_name`
output. Assemble the connection string as
`postgresql+psycopg://agentcare:PASSWORD@HOST:5432/agentcare` and keep it for Step 7.

---

## Step 6 - Build and push both images

```bash
REPO=$(tofu -chdir=infra/terraform output -raw artifact_registry_repository_url)
gcloud auth configure-docker "${REPO%%/*}"

docker buildx build --platform linux/amd64 -t "$REPO/backend:latest" -f backend/Dockerfile --push .
docker buildx build --platform linux/amd64 -t "$REPO/frontend:latest" --push ./frontend
```

Run both from the repository root.

`--platform linux/amd64` forces the target architecture. GKE Autopilot nodes are amd64, and an
image built on an Apple Silicon laptop without this flag crash-loops on the cluster with "exec
format error". The backend build context is the repository root rather than `backend/`, because
the Dockerfile copies the root `requirements.txt` before the app tree, which is why the
Dockerfile is named with `-f`. `${REPO%%/*}` strips everything from the first `/` on, leaving
the registry host that `gcloud auth configure-docker` wants.

This matches what `.github/workflows/deploy.yml` builds, so a manual deploy and a CI deploy
produce the same images. CI tags with the git SHA as well as `latest`; prefer the immutable tag
for anything you plan to roll back.

---

## Step 7 - Connect kubectl and create the Kubernetes Secret

```bash
gcloud container clusters get-credentials \
  "$(tofu -chdir=infra/terraform output -raw gke_cluster_name)" \
  --region "$(tofu -chdir=infra/terraform output -raw gke_cluster_location)"

kubectl get nodes
```

Autopilot clusters are regional, hence `--region` and not `--zone`.

**Manual gap 2 of 3.** The backend Deployment and the migration Job both read
`agentcare-secrets` through `envFrom`, and kustomize never creates it:
`infra/k8s/base/secret.example.yaml` is a template deliberately left out of the base's
`resources` list, so `kubectl apply -k` can never overwrite your real Secret with placeholders.
Create it once per cluster or namespace, before the first apply:

```bash
kubectl create secret generic agentcare-secrets \
  --from-literal=LLM_API_KEY=your_llm_api_key \
  --from-literal=LLM_FALLBACK_API_KEY= \
  --from-literal=JWT_SECRET=a_long_random_string \
  --from-literal=DATABASE_URL='postgresql+psycopg://agentcare:PASSWORD@HOST:5432/agentcare' \
  --from-literal=INTERNAL_TASK_TOKEN= \
  --from-literal=LANGFUSE_SECRET_KEY=
```

`JWT_SECRET` and `DATABASE_URL` are the two that must carry real values. The empty ones are
optional features (the fallback LLM endpoint, the internal task token, Langfuse tracing) that
stay off until you fill them. To rotate one value later without retyping the rest:

```bash
kubectl create secret generic agentcare-secrets --from-literal=JWT_SECRET=new_value \
  --dry-run=client -o yaml | kubectl apply -f -
```

Delete-and-recreate would leave the app without a Secret in between, which restarts pods into a
crash loop.

The non-secret half lives in `infra/k8s/base/configmap.yaml`. Two values there need editing
before a real deploy: `FRONTEND_ORIGIN` is a placeholder, and the overlay's `GCS_BUCKET` is
`REPLACE_ME` (the real name is the `documents_bucket_name` output).

---

## Step 8 - Bind Workload Identity for the pods

**Manual gap 3 of 3, and it is not wired.** Terraform creates the two runtime service accounts
and the cluster is created with `workload_identity_config`, but nothing under `infra/k8s/`
creates a Kubernetes service account, carries the `iam.gke.io/gcp-service-account` annotation
or sets `serviceAccountName` on a pod. Until someone does this by hand the pods run under the
namespace default service account, so the backend does not reach GCS or Secret Manager as
`agentcare-backend`. The app still starts: it reads its configuration from the ConfigMap and
the Secret. What does not work is anything that needs the backend's own GCP identity.

```bash
BACKEND_SA=$(tofu -chdir=infra/terraform output -raw backend_service_account_email)
kubectl create serviceaccount agentcare-backend
kubectl annotate serviceaccount agentcare-backend "iam.gke.io/gcp-service-account=$BACKEND_SA"
gcloud iam service-accounts add-iam-policy-binding "$BACKEND_SA" \
  --role roles/iam.workloadIdentityUser \
  --member "serviceAccount:YOUR_PROJECT_ID.svc.id.goog[default/agentcare-backend]"
```

Then add `serviceAccountName: agentcare-backend` to the pod spec in
`infra/k8s/base/backend.yaml`. Both halves of the handshake are required; one without the other
silently does nothing.

The Workload Identity Federation that GitHub Actions uses is a different mechanism and it **is**
wired: see `.github/workflows/deploy.yml` and the `workload_identity_provider` output.

---

## Step 9 - Point the overlay at your images and apply it

```bash
cd infra/k8s/overlays/gcp
kustomize edit set image \
  "agentcare-backend=$REPO/backend:latest" \
  "agentcare-frontend=$REPO/frontend:latest"
cd -
```

The base carries the image names without a real registry or tag on purpose, so this rewrite is
the only place either is set. CI runs the same command with the git SHA as the tag.

Clear the previous migration Job, then apply:

```bash
kubectl delete job backend-migrate --ignore-not-found
kubectl apply -k infra/k8s/overlays/gcp
```

Job specs are immutable once created, so a leftover `backend-migrate` makes the apply fail
outright whether that Job succeeded or is still running. `--ignore-not-found` makes the delete a
no-op on the first deploy, so the same two commands work every time. Render the overlay without
touching the cluster first if you want to see exactly what goes up:
`kubectl kustomize infra/k8s/overlays/gcp`.

Watch the rollout:

```bash
kubectl rollout status deployment/backend --timeout=300s
kubectl rollout status deployment/frontend --timeout=300s
kubectl logs job/backend-migrate
```

A first Autopilot deploy is slower than later ones, because Autopilot provisions nodes to fit
the pods' requests. Read the migration Job's log before anything else if the backend pods are
unhealthy: a backend that cannot reach its database looks identical from the outside to a dozen
other problems.

**Document storage on this path.** The overlay sets `STORAGE_BACKEND: gcs` and
`google-cloud-storage` is pinned in `requirements.txt`, so the image carries the adapter
(`backend/app/services/storage.py`). Two things must still line up before the first upload
works: `GCS_BUCKET` in the overlay must name the bucket Terraform created, and Step 8 must be
complete so the pod carries the bucket-scoped identity. Until both hold, uploads fail with a
403 while everything that does not touch document storage keeps working.

Then find the address:

```bash
kubectl get ingress agentcare -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
```

It is empty for the first few minutes while the load balancer provisions. For a demo without
paying roughly 18 USD/month for that load balancer, skip `ingress.yaml` and port-forward
instead: `kubectl port-forward svc/frontend 3000:3000`.

---

## Step 10 - Monitoring

**Nothing in `infra/` deploys a metrics stack into the cluster.** What the committed
configuration gives you is the scrape target and the log stream, not a dashboard.

- The backend exposes `/metrics` through `prometheus-fastapi-instrumentator`
  (`backend/app/main.py`): request rate, latency histograms and error counters.
- The backend pod template carries `prometheus.io/scrape: "true"`, `prometheus.io/port: "8000"`
  and `prometheus.io/path: "/metrics"` (`infra/k8s/base/backend.yaml`), so any collector that
  reads those annotations finds it.
- `monitoring/prometheus.yml` and the provisioned Grafana dashboard under `monitoring/` are
  wired into `docker-compose.yml` only. No manifest deploys them to the cluster.
- The GKE module sets no `monitoring_config`, so the cluster runs on whatever the Autopilot
  defaults enable. Google Managed Service for Prometheus is the documented GKE path
  (`docs/decisions.md` ADR-10) and would need a `PodMonitoring` resource, which is not
  committed here.

Read the metrics without deploying anything:

```bash
kubectl port-forward deployment/backend 8000:8000
curl -s http://localhost:8000/metrics | head -n 20
```

Read the logs. The backend logs structured JSON through structlog, with a processor that
redacts the value of any key containing `password`, `token`, `authorization`, `api_key` or
`secret`:

```bash
kubectl logs -f deployment/backend
```

The application's own record of what happened is the append-only `audit_events` table, not a
metric. Every tool mutation, agent node exit and mutating route writes a row, staff read them at
`GET /api/staff/audit`, and the live SSE timeline at `GET /api/workflows/{id}/events` streams
them while a run is happening.

---

## Step 11 - End-to-end test

Three checks against the deployed address. Use `http://INGRESS_IP` or, on the port-forward path,
`http://localhost:8000` for the API calls.

**1. Health.** This is the same endpoint the liveness and readiness probes use, and it runs a
`SELECT 1` against the database, so it fails if the Secret's `DATABASE_URL` is wrong:

```bash
curl -sf --max-time 10 "http://INGRESS_IP/api/health"
```

Expect `{"status":"ok","db":true}`.

**2. Demo login.** The migration Job runs the idempotent seed, so the synthetic demo accounts
exist as soon as it completes:

```bash
curl -s -c cookies.txt -X POST "http://INGRESS_IP/api/auth/login" \
  -H 'Content-Type: application/json' \
  -d '{"email":"patient@agentcare-demo.com","password":"demo1234"}'
```

Expect the user summary and an httpOnly `access_token` cookie in `cookies.txt`.

**3. One emergency request.** The deterministic screen decides this one before the graph starts
and calls no model at all, so it is the check that proves the deployed app end to end without
depending on the LLM key being valid:

```bash
curl -s -b cookies.txt -X POST "http://INGRESS_IP/api/requests" \
  -F 'text=I have severe chest pain and cannot breathe'
```

Expect `{"workflow_id":N,"status":"escalated"}`, an `emergency` escalation in the staff queue
and a response telling the patient to call 112. A normal booking
(`-F 'text=Book me a cardiology appointment next week'`) returns `status` `running` and needs a
working LLM endpoint to finish; watch it at `GET /api/workflows/N`.

---

## Day-to-day access

| Thing | How |
|---|---|
| Cluster context | `gcloud container clusters get-credentials "$(tofu -chdir=infra/terraform output -raw gke_cluster_name)" --region "$(tofu -chdir=infra/terraform output -raw gke_cluster_location)"` |
| Public address | `kubectl get ingress agentcare -o jsonpath='{.status.loadBalancer.ingress[0].ip}'` |
| Pod status | `kubectl get pods` |
| Backend logs | `kubectl logs -f deployment/backend` |
| Migration log | `kubectl logs job/backend-migrate` |
| Metrics | `kubectl port-forward deployment/backend 8000:8000` then `curl -s http://localhost:8000/metrics` |
| App with no load balancer | `kubectl port-forward svc/frontend 3000:3000` |
| Terraform outputs | `tofu -chdir=infra/terraform output` |
| Redeploy from CI | Actions tab, `deploy` workflow, Run workflow (needs the repo Variables `GCP_PROJECT`, `GCP_REGION`, `GCP_WIF_PROVIDER`, `GCP_DEPLOY_SA`, `GCP_GKE_CLUSTER` and `GCP_GKE_LOCATION`) |

---

## Troubleshooting

**`tofu plan` fails with "could not find default credentials"** - `gcloud auth
application-default login` was skipped in Step 1. The provider reads ADC, not the gcloud CLI
credential.

**`tofu apply` fails reading the `default` network** - the Cloud SQL module looks up a VPC named
`default` for private service access. If an org policy disabled auto-creation of the default
network, pass a network name through the module's `network_name` variable instead.

**The migration Job fails with a connection or authentication error** - the Cloud SQL database
or user from Step 5 does not exist, or `DATABASE_URL` in the Secret does not match the password
you chose. `kubectl logs job/backend-migrate` shows it directly. Terraform creates the instance
only.

**Pods in `CreateContainerConfigError`** - `agentcare-secrets` is missing or missing a key. It
is never created by `kubectl apply -k`. Check with `kubectl get secret agentcare-secrets` and go
back to Step 7.

**Pods in `ErrImagePull` or `ImagePullBackOff`** - either the overlay still carries its
`REGION-docker.pkg.dev/PROJECT/agentcare/...:TAG` placeholders, or the image was built without
`--platform linux/amd64` and cannot run on the amd64 nodes. `kubectl describe pod <name>` names
the image it tried to pull. Re-run the `kustomize edit set image` in Step 9, or rebuild with the
platform flag.

**`kubectl apply -k` fails on the Job** - a leftover `backend-migrate` from the previous deploy.
Job specs are immutable, so delete it first:
`kubectl delete job backend-migrate --ignore-not-found`.

**GCS writes fail with a 403, or Secret Manager reads fail** - the pods are running under the
namespace default service account because Step 8 was skipped. Confirm with
`kubectl get pod <name> -o jsonpath='{.spec.serviceAccountName}'`; if it says `default`, the
binding is missing.

**Document upload fails with "google-cloud-storage is not installed"** - the image was built
from an edited `requirements.txt`. The committed file pins the package; rebuild from a clean
checkout.

**The SSE workflow timeline dies after 30 seconds** - the `BackendConfig` annotation did not
land on the backend Service, so the load balancer applies its own default. Check both objects:
`kubectl get svc backend -o jsonpath='{.metadata.annotations}'` should name
`backend-backendconfig`, and `kubectl get backendconfig backend-backendconfig` should show
`timeoutSec: 3600`.

**The Ingress never gets an address, or the certificate stays in "Provisioning"** - a GCE load
balancer takes a few minutes, but a `ManagedCertificate` waits forever on a domain with no
matching DNS. `infra/k8s/overlays/gcp/ingress.yaml` ships `agentcare.example.com` as a
placeholder; replace it with a domain you control, or drop `ingress.yaml` and use the
port-forward path.

**The browser is rejected by CORS** - `FRONTEND_ORIGIN` in the ConfigMap is still
`https://FRONTEND_ORIGIN_PLACEHOLDER`. In normal use the browser never triggers CORS at all,
because Next.js rewrites `/api/*` same-origin, so this shows up only on direct API calls from a
page.

---

## Production notes, none of them applied here

Three changes this configuration would want before it carried anything real. All three are
recorded, none is built.

**Cloud SQL Auth Proxy sidecar.** Today the pods reach the instance over the VPC private IP with
credentials in a Kubernetes Secret. Google's recommended pattern on GKE is a Cloud SQL Auth
Proxy sidecar in the backend pod, connecting through the `cloud_sql_connection_name` output with
IAM instead of a password on the wire. It needs the sidecar container in
`infra/k8s/base/backend.yaml` and the `cloudsql.client` role that the `iam` module already grants
the backend service account.

**Secret Manager add-on for GKE.** Step 4 and Step 7 keep two hand-maintained copies of every
secret, and nothing syncs them. The Secret Manager add-on (Google's managed Secret Store CSI
driver) mounts the Secret Manager version straight into the pod, which removes the second copy
and the rotation drift with it.

**Cloud Tasks for durable dispatch.** Graph execution is a FastAPI `BackgroundTasks` callback in
the request-serving process, so a pod restart mid-run loses the in-flight execution and the run
sits in `running` until the stall sweep escalates it. Cloud Tasks (or Cloud Scheduler for the
periodic jobs) makes that dispatch durable and is the same change that would let the backend run
more than one replica. `docs/decisions.md` ADR-11 records why no broker is in the build.

---

## Cost

| Item | Free tier | Covered by the trial credit | What it costs beyond both |
|---|---|---|---|
| Artifact Registry | 0.5 GB storage free | yes | ~$0.10/GB-month after; the keep-last-10 cleanup policy bounds growth |
| GCS documents bucket (`europe-west3`, default) | none - Always Free 5 GB-months applies to US regions only | yes | ~$0.02-0.03/GB-month (Standard, EU); a few cents/month at demo volume |
| GKE Autopilot control plane | One cluster/billing account covered by the Autopilot free-tier credit | yes | $0.10/hour if a second cluster exists or the credit lapses |
| GKE Autopilot pods | None | yes | Billed per vCPU/memory/storage request per pod, roughly $15-30/month for this app's two small Deployments run continuously |
| Cloud SQL (`enable_cloud_sql=true`, default) | None | yes | `db-f1-micro` ~$8/month plus ~$0.17-0.22/GB-month SSD once the credit lapses |
| GCE external load balancer (the Ingress) | None | yes | ~$18/month once provisioned; skip `ingress.yaml` and port-forward to avoid it |
| Secret Manager | 6 active versions, 10k accesses/month | yes | $0.06/version/month beyond that |
| Workload Identity Federation | Free, no limit | n/a ($0 regardless) | $0 |

**The default deployment path uses Cloud SQL, not Neon.** `enable_cloud_sql` defaults to `true`:
the whole GCP path (GKE Autopilot, Cloud SQL, the `europe-west3` GCS bucket) is scoped to run
inside the owner's one-time ~€250, 3-month GCP trial credit (roughly through late October 2026),
which comfortably covers a `db-f1-micro` instance plus the rest of the table above. Before the
credit lapses, either set `enable_cloud_sql=false` and point `DATABASE_URL` at Neon's free-tier
Postgres (0.5 GB storage, 100 compute-hours/month, scale-to-zero, one connection-string change
and no code change, see `docs/decisions.md` ADR-03), or budget for the ~$8/month `db-f1-micro`
floor going forward.

---

## Teardown

Delete the Kubernetes objects first. The Ingress owns the load balancer, which is the expensive
part and the one Terraform does not track.

```bash
kubectl delete -k infra/k8s/overlays/gcp
```

The `agentcare-secrets` Secret survives, because it was never part of the kustomization.

```bash
cd infra/terraform
tofu destroy \
  -var="project_id=YOUR_PROJECT_ID" \
  -var="github_repository=Gaurang140/agentcare"
```

Run this as soon as a demo is over. GKE Autopilot pods and a Cloud SQL instance bill
continuously, and the trial credit is finite.

The documents bucket leaves `force_destroy` at its default `false`, so `tofu destroy` fails on a
non-empty bucket instead of quietly deleting whatever a patient uploaded during the demo. That
failure is the feature. Empty it deliberately first if you actually want it gone:

```bash
gsutil -m rm -r gs://BUCKET_NAME/**
```

It is not reversible.
