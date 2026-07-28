# Kubernetes manifests

This directory contains the operator-owned platform bundle, application base,
GCP application overlay and separate GCP migration overlay for AgentCare. Use the
[GCP deployment runbook](../../docs/deployment-gcp.md) for provisioning,
secrets, database setup, DNS, rollback and cost controls. Use
[CI/CD](../../docs/ci-cd.md) for automatic releases.

## Layout

```text
platform/
  namespace.yaml
  service-account.yaml
  deployer-rbac.yaml
  kustomization.yaml
base/
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
  kustomization.yaml
overlays/gcp-migration/
  migration-job.yaml
  kustomization.yaml
```

`secret.example.yaml` is documentation only and is not rendered by either
overlay. The platform bundle owns the `agentcare` namespace and runtime
`agentcare-backend` KSA; application overlays never create either object.

## Ownership and authorization

`make gcp-up` applies `platform/` after Terraform creates GKE and before a
release. It creates the `agentcare` namespace, annotates the runtime KSA and
binds the short-lived GitHub deployer GSA to the narrow release Role.

Google IAM and Kubernetes RBAC are separate. The deployer GSA can upload
Artifact Registry images and discover the cluster. The namespaced RoleBinding
permits only application-release resources. CI cannot read/create
`agentcare/agentcare-secrets`, alter Secrets/RBAC/namespaces/KSAs, or use
`exec`, `attach`, `portforward` or impersonation.

## Prerequisites

Before applying:

- `kubectl` must target the intended GKE cluster
- the operator must have created `agentcare/agentcare-secrets`; the local
  `make gcp-release` preflight verifies its existence, while CI has no Secret
  access
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
kubectl --namespace=agentcare apply --dry-run=server -k "$RENDERED_K8S/overlays/gcp"
```

The output path must not already exist. Rendering never proves that cloud
resources or credentials exist.

## Apply in order

Migration Job specs are immutable, so delete a prior Job before applying its
overlay:

```bash
kubectl --namespace=agentcare delete job backend-migrate --ignore-not-found
kubectl --namespace=agentcare apply --dry-run=server -k "$RENDERED_K8S/overlays/gcp-migration"
kubectl --namespace=agentcare apply -k "$RENDERED_K8S/overlays/gcp-migration"
kubectl --namespace=agentcare wait --for=condition=complete job/backend-migrate --timeout=600s
kubectl --namespace=agentcare logs job/backend-migrate
kubectl --namespace=agentcare apply -k "$RENDERED_K8S/overlays/gcp"
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
kubectl --namespace=agentcare rollout status deployment/backend --timeout=600s
kubectl --namespace=agentcare rollout status deployment/frontend --timeout=600s
kubectl --namespace=agentcare get pods,services,ingress
curl -sSI --max-time 10 http://YOUR_DOMAIN/ | head -n 1
```

The public HTTP check must report a 308 redirect after the managed certificate
and GKE load balancer finish reconciling.

Continue with health, workflow and Model Armor checks in the
[deployment runbook](../../docs/deployment-gcp.md#11-verify-the-environment).

## Teardown

```bash
kubectl --namespace=agentcare delete -k "$RENDERED_K8S/overlays/gcp"
kubectl --namespace=agentcare delete job backend-migrate --ignore-not-found
```

The out-of-band `agentcare/agentcare-secrets` Secret is operator-owned, not
owned by either application kustomization or CI. Delete it separately only
when the environment is being removed. The backend Kubernetes service account
is owned by the platform bundle.
