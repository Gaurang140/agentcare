# Kubernetes manifests

This directory contains the application base, GCP application overlay and
separate GCP migration overlay for AgentCare. Use the
[GCP deployment runbook](../../docs/deployment-gcp.md) for provisioning,
secrets, database setup, DNS, rollback and cost controls. Use
[CI/CD](../../docs/ci-cd.md) for automatic releases.

## Layout

```text
base/
  service-account.yaml
  backend.yaml
  frontend.yaml
  configmap.yaml
  secret.example.yaml
  kustomization.yaml
overlays/gcp/
  ingress.yaml
  backendconfig.yaml
  frontendconfig.yaml
  podmonitoring.yaml
  configmap-runtime.yaml
  configmap-storage.yaml
  configmap-model-armor.yaml
  serviceaccount-workload-identity.yaml
  kustomization.yaml
overlays/gcp-migration/
  migration-job.yaml
  kustomization.yaml
```

`secret.example.yaml` is documentation only and is not rendered by either
overlay.

## Prerequisites

Before applying:

- `kubectl` must target the intended GKE cluster
- `agentcare-secrets` must exist in the namespace
- the Cloud SQL database and user must exist
- renderer environment values must name GCS, Model Armor, public origin, model profile and the commit SHA
- Terraform must have created the backend Workload Identity binding
- Terraform must have reserved the `agentcare-ingress` global address
- Terraform must have created the Model Armor regional endpoint and private DNS when provider screening is enabled

## Render

Do not edit source sentinels or run `kustomize edit`. The renderer validates
one environment and copies the source to a temporary directory:

```bash
export GCP_PROJECT_ID="YOUR_PROJECT_ID"
export GCP_REGION="europe-west3"
export IMAGE_TAG="$(git rev-parse HEAD)"
export DOCUMENTS_BUCKET="YOUR_BUCKET"
export MODEL_ARMOR_TEMPLATE="projects/YOUR_PROJECT_ID/locations/europe-west3/templates/agentcare-guard"
export PUBLIC_URL="https://YOUR_DOMAIN"
export LLM_PROFILE="vertex"
export LANGFUSE_PUBLIC_KEY=""
export LANGFUSE_BASE_URL="https://cloud.langfuse.com"
export LANGFUSE_SAMPLE_RATE="0"
export RENDERED_K8S="/tmp/agentcare-k8s-$IMAGE_TAG"
.venv/bin/python scripts/render_gcp_manifests.py \
  --output "$RENDERED_K8S"
kubectl kustomize "$RENDERED_K8S/overlays/gcp-migration"
kubectl kustomize "$RENDERED_K8S/overlays/gcp"
kubectl apply --dry-run=server -k "$RENDERED_K8S/overlays/gcp"
```

The output path must not already exist. Rendering never proves that cloud
resources or credentials exist.

## Apply in order

Migration Job specs are immutable, so delete a prior Job before applying its
overlay:

```bash
kubectl delete job backend-migrate --ignore-not-found
kubectl apply --dry-run=server -k "$RENDERED_K8S/overlays/gcp-migration"
kubectl apply -k "$RENDERED_K8S/overlays/gcp-migration"
kubectl wait --for=condition=complete job/backend-migrate --timeout=600s
kubectl logs job/backend-migrate
kubectl apply -k "$RENDERED_K8S/overlays/gcp"
```

Do not apply the application overlay until the Job succeeds. The application
overlay contains no Job. It includes backend and frontend workloads, GCE
Ingress, a `FrontendConfig` that redirects HTTP to HTTPS and the long-timeout
`BackendConfig` used by SSE. A `PodMonitoring` resource collects the existing
backend `/metrics` endpoint through Google Managed Service for Prometheus. The
backend container sets `SKIP_STARTUP_MIGRATIONS=true`.

The backend uses `Recreate`, so a planned upgrade deletes the old Pod before
creating its replacement. This causes brief downtime and avoids rolling
overlap, but it is not a distributed singleton guarantee for failures or
manual Pod operations. Keep one replica until scheduled work moves to an
external worker or gains a distributed lock.

## Verify

```bash
kubectl rollout status deployment/backend --timeout=600s
kubectl rollout status deployment/frontend --timeout=600s
kubectl get pods,services,ingress
curl -sSI --max-time 10 http://YOUR_DOMAIN/ | head -n 1
```

The public HTTP check must report a 308 redirect after the managed certificate
and GKE load balancer finish reconciling.

Continue with health, workflow and Model Armor checks in the
[deployment runbook](../../docs/deployment-gcp.md#10-verify-real-end-to-end-behavior).

## Teardown

```bash
kubectl delete -k "$RENDERED_K8S/overlays/gcp"
kubectl delete job backend-migrate --ignore-not-found
```

The out-of-band `agentcare-secrets` Secret is not owned by either
kustomization. Delete it separately only when the environment is being
removed. The backend Kubernetes service account is owned by the application
overlay.
