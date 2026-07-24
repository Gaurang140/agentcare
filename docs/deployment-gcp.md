# Deploying to GCP

This is the honest step-by-step for the designed GCP path: Artifact Registry, a GCS
documents bucket, IAM (including Workload Identity Federation for GitHub Actions), a GKE
Autopilot cluster, and an optional Cloud SQL instance. The Terraform (OpenTofu-compatible)
source is `infra/terraform/`; see `docs/decisions.md` ADR-03 through ADR-05 and ADR-12 for
why each piece was chosen. Nothing in this repo has applied any of it: there are no GCP
credentials in this environment and none were created to write it.

The Kubernetes manifests this section hands off to (Deployments, Services, Ingress) are a
kustomize overlay under `infra/k8s/`, a separate piece of work from this Terraform layer.

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

Add `-var="enable_cloud_sql=true"` only for the enterprise-path demo; the default path
below runs on Neon. `tofu output` after apply prints the Artifact Registry URL, the bucket
name, the three service account emails, and the `workload_identity_provider` string GitHub
Actions needs.

## Build and push images

GKE Autopilot nodes are `linux/amd64`; building on an Apple Silicon laptop needs an
explicit target platform or the image will not start on the cluster.

```bash
REPO=$(tofu -chdir=infra/terraform output -raw artifact_registry_repository_url)
gcloud auth configure-docker "${REPO%%/*}"

docker buildx build --platform linux/amd64 -t "$REPO/backend:latest" --push ./backend
docker buildx build --platform linux/amd64 -t "$REPO/frontend:latest" --push ./frontend
```

## Handoff to the k8s overlay

The kustomize overlay under `infra/k8s/` consumes four things from this layer's outputs:
the image URLs above, `gke_cluster_name` / `gke_cluster_location` for
`gcloud container clusters get-credentials`, the `backend_service_account_email` and
`frontend_service_account_email` for the Kubernetes service account
`iam.gke.io/gcp-service-account` annotation that completes Workload Identity binding, and
`documents_bucket_name` for the backend's `GCS_BUCKET` environment variable. That overlay
also owns the Ingress and, if `var.domain` is set, the ManagedCertificate resource; neither
is provisioned by this Terraform layer.

```bash
gcloud container clusters get-credentials \
  "$(tofu -chdir=infra/terraform output -raw gke_cluster_name)" \
  --region "$(tofu -chdir=infra/terraform output -raw gke_cluster_location)"
kubectl apply -k infra/k8s/overlays/prod
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

| Item | Free tier | What it costs beyond free |
|---|---|---|
| Artifact Registry | 0.5 GB storage free | ~$0.10/GB-month after; the keep-last-10 cleanup policy bounds growth |
| GCS documents bucket | 5 GB-months, US regions only | ~$0.02/GB-month (Standard, `US`); a few cents/month at demo volume even outside the free tier |
| GKE Autopilot control plane | One cluster/billing account covered by the Autopilot free-tier credit | $0.10/hour if a second cluster exists or the credit lapses |
| GKE Autopilot pods | None | Billed per vCPU/memory/storage request per pod, roughly $15-30/month for this app's two small Deployments run continuously |
| Cloud SQL (`enable_cloud_sql=true`) | None | `db-f1-micro` ~$8/month plus ~$0.17-0.22/GB-month SSD; off by default (see below) |
| Secret Manager | 6 active versions, 10k accesses/month | $0.06/version/month beyond that |
| Workload Identity Federation | Free, no limit | $0 |

**Default demo path uses Neon, not Cloud SQL.** `enable_cloud_sql` defaults to `false`
because Neon's free-tier Postgres (0.5 GB storage, 100 compute-hours/month, scale-to-zero)
covers a portfolio demo at $0 with no idle-cost risk, while Cloud SQL has no always-free
tier at all (docs/decisions.md ADR-03). Flip the flag only to demonstrate the enterprise
path once, then `tofu destroy` that piece rather than leaving it running.
