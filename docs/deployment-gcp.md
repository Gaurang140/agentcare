# Deploying to GCP

This is the honest step-by-step for the designed GCP path: Artifact Registry, a GCS
documents bucket, IAM (including Workload Identity Federation for GitHub Actions), a GKE
Autopilot cluster and a Cloud SQL for PostgreSQL 17 instance (on by default, the primary
database path while the owner's GCP trial credit covers it; Neon free-tier Postgres is the
documented post-credit swap). The Terraform (OpenTofu-compatible) source is
`infra/terraform/`; see `docs/decisions.md` ADR-03 through ADR-05 and ADR-12 for why each
piece was chosen. Nothing in this repo has applied any of it: there are no GCP credentials
in this environment and none were created to write it.

The Kubernetes manifests this section hands off to (Deployments, Services, Ingress) are a
kustomize base plus a `gcp` overlay under `infra/k8s/` (see `infra/k8s/README.md`), and the
apply step itself runs from `.github/workflows/deploy.yml` on a manual `workflow_dispatch` -
both committed, but that first real deploy still needs a GCP project with billing enabled.

## Prerequisites

1. A GCP project with billing enabled. `gcloud config set project PROJECT_ID`.
2. `gcloud auth login` and `gcloud auth application-default login` (Terraform's Google
   provider reads Application Default Credentials).
3. Enable the APIs this configuration's resources need. Terraform does not manage API
   enablement here, so run this once per project before `tofu apply`:

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

4. OpenTofu or Terraform on the machine that runs `tofu apply`:

   ```bash
   which tofu || brew install opentofu
   ```

## Out-of-band secrets

Terraform never creates a secret value, only the access to read one (`iam` module,
`roles/secretmanager.secretAccessor` on the backend service account). Create each secret
by hand once, after `tofu apply` has run:

```bash
echo -n "your_groq_key" | gcloud secrets create llm-api-key --data-file=- --replication-policy=automatic
echo -n "a_long_random_string" | gcloud secrets create jwt-secret --data-file=- --replication-policy=automatic
```

The `-n` on `echo` is not decoration. Without it, `echo` appends a trailing newline, and
that newline becomes part of the secret value, which then fails to match anything that
compares it byte for byte (a JWT secret with a stray `\n` still signs tokens, but two
different processes reading the "same" secret can disagree on whether it decoded cleanly).
To update a secret later, add a new version rather than deleting and recreating it:

```bash
echo -n "rotated_value" | gcloud secrets versions add llm-api-key --data-file=-
```

Secret Manager and the Kubernetes `Secret` the backend reads through `envFrom`
(`infra/k8s/base/secret.example.yaml`) are two hand-maintained copies of the same values,
with nothing syncing one to the other, so a rotation has to be applied in both places or
the cluster keeps serving the old value.

## Terraform (OpenTofu) apply order

```bash
cd infra/terraform
tofu init                                    # local backend by default, see backend.tf
tofu plan \
  -var="project_id=YOUR_PROJECT_ID" \
  -var="github_repository=Gaurang140/agentcare"
tofu apply \
  -var="project_id=YOUR_PROJECT_ID" \
  -var="github_repository=Gaurang140/agentcare"
```

Cloud SQL for PostgreSQL 17 provisions by default (`enable_cloud_sql` defaults `true`), the
primary database path while the GCP trial credit covers it. Pass
`-var="enable_cloud_sql=false"` and set an external Neon `DATABASE_URL` in the Kubernetes
Secret instead once the credit lapses (`docs/decisions.md` ADR-03). `tofu output` after
apply prints the Artifact Registry URL, the bucket name, the three service account emails,
and the `workload_identity_provider` string GitHub Actions needs.

The module creates the instance and its private-IP networking only. It creates no database
and no user, so the `DATABASE_URL` the backend needs does not exist until you add both by
hand:

```bash
gcloud sql databases create agentcare --instance=agentcare-postgres
gcloud sql users create agentcare --instance=agentcare-postgres --password="A_LONG_RANDOM_PASSWORD"
```

The instance has no public IP, so anything connecting to it runs inside the VPC or goes
through the Cloud SQL Auth Proxy with `cloud_sql_connection_name`. Put the resulting
`DATABASE_URL` in the Kubernetes Secret next to the other values.

## Build and push images

GKE Autopilot nodes are `linux/amd64`; building on an Apple Silicon laptop needs an
explicit target platform or the image will not start on the cluster. Run both commands from
the repository root. The backend build context is the repository root rather than
`backend/`, because the Dockerfile copies the root `requirements.txt` before the app tree,
which is why it names the Dockerfile with `-f`. This matches what
`.github/workflows/deploy.yml` builds.

```bash
REPO=$(tofu -chdir=infra/terraform output -raw artifact_registry_repository_url)
gcloud auth configure-docker "${REPO%%/*}"

docker buildx build --platform linux/amd64 -t "$REPO/backend:latest" -f backend/Dockerfile --push .
docker buildx build --platform linux/amd64 -t "$REPO/frontend:latest" --push ./frontend
```

## Handoff to the k8s overlay

The kustomize overlay under `infra/k8s/` consumes three things from this layer's outputs:
the image URLs above, `gke_cluster_name` / `gke_cluster_location` for
`gcloud container clusters get-credentials`, and `documents_bucket_name` for the backend's
`GCS_BUCKET` environment variable. That overlay also owns the Ingress and, if `var.domain`
is set, the ManagedCertificate resource; neither is provisioned by this Terraform layer.

**Workload Identity for the pods is a manual step, not yet wired in the manifests.** The
Terraform layer creates the two runtime service accounts
(`backend_service_account_email`, `frontend_service_account_email`) and the cluster is
created with `workload_identity_config`, but nothing under `infra/k8s/` creates a
Kubernetes service account, carries the `iam.gke.io/gcp-service-account` annotation or sets
`serviceAccountName` on a pod. Until someone does this by hand the pods run under the
namespace default service account, so the backend does not yet reach GCS or Secret Manager
as `agentcare-backend`. Wiring it takes three commands and a one-line manifest change:

```bash
BACKEND_SA=$(tofu -chdir=infra/terraform output -raw backend_service_account_email)
kubectl create serviceaccount agentcare-backend
kubectl annotate serviceaccount agentcare-backend "iam.gke.io/gcp-service-account=$BACKEND_SA"
gcloud iam service-accounts add-iam-policy-binding "$BACKEND_SA" \
  --role roles/iam.workloadIdentityUser \
  --member "serviceAccount:YOUR_PROJECT_ID.svc.id.goog[default/agentcare-backend]"
```

then add `serviceAccountName: agentcare-backend` to the pod spec in
`infra/k8s/base/backend.yaml`. The Workload Identity Federation that GitHub Actions uses is
a different mechanism and is wired: see `.github/workflows/deploy.yml` and the
`workload_identity_provider` output.

```bash
gcloud container clusters get-credentials \
  "$(tofu -chdir=infra/terraform output -raw gke_cluster_name)" \
  --region "$(tofu -chdir=infra/terraform output -raw gke_cluster_location)"
kubectl apply -k infra/k8s/overlays/gcp
```

## Teardown

```bash
cd infra/terraform
tofu destroy \
  -var="project_id=YOUR_PROJECT_ID" \
  -var="github_repository=Gaurang140/agentcare"
```

The documents bucket has `force_destroy` left at its default `false`, so `tofu destroy`
fails on a non-empty bucket instead of silently deleting whatever a patient uploaded during
the demo. Empty it on purpose first if you actually want it gone:
`gsutil -m rm -r gs://BUCKET_NAME/**`.

## Costs

| Item | Free tier | Covered by the trial credit | What it costs beyond both |
|---|---|---|---|
| Artifact Registry | 0.5 GB storage free | yes | ~$0.10/GB-month after; the keep-last-10 cleanup policy bounds growth |
| GCS documents bucket (`europe-west3`, default) | none - Always Free 5 GB-months applies to US regions only | yes | ~$0.02-0.03/GB-month (Standard, EU); a few cents/month at demo volume |
| GKE Autopilot control plane | One cluster/billing account covered by the Autopilot free-tier credit | yes | $0.10/hour if a second cluster exists or the credit lapses |
| GKE Autopilot pods | None | yes | Billed per vCPU/memory/storage request per pod, roughly $15-30/month for this app's two small Deployments run continuously |
| Cloud SQL (`enable_cloud_sql=true`, default) | None | yes | `db-f1-micro` ~$8/month plus ~$0.17-0.22/GB-month SSD once the credit lapses |
| Secret Manager | 6 active versions, 10k accesses/month | yes | $0.06/version/month beyond that |
| Workload Identity Federation | Free, no limit | n/a ($0 regardless) | $0 |

**Default deployment path uses Cloud SQL, not Neon.** `enable_cloud_sql` now defaults to
`true`: the whole GCP path (GKE Autopilot, Cloud SQL, the `europe-west3` GCS bucket) is
scoped to run inside the owner's one-time ~€250, 3-month GCP trial credit (roughly through
late October 2026), which comfortably covers a `db-f1-micro` instance plus the rest of the
table above. Before the credit lapses, either set `enable_cloud_sql=false` and point
`DATABASE_URL` at Neon's free-tier Postgres (0.5 GB storage, 100 compute-hours/month,
scale-to-zero, one connection-string change and no code change, see docs/decisions.md
ADR-03), or budget for the ~$8/month `db-f1-micro` floor going forward.
