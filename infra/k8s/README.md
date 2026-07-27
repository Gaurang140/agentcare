# Kubernetes manifests

This directory contains the application base, GCP application overlay and
separate GCP migration overlay for AgentCare. Use the
[GCP deployment runbook](../../docs/deployment-gcp.md) for provisioning,
secrets, images, database setup, DNS, rollback and cost controls.

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
- `kustomize` must be installed for image rewrites
- `agentcare-secrets` must exist in the namespace
- the Cloud SQL database and user must exist
- GCS, Model Armor template/region, frontend origin, project and image sentinels must be replaced
- Terraform must have created the backend Workload Identity binding
- Terraform must have created the Model Armor regional endpoint and private DNS when provider screening is enabled

## Render

```bash
kubectl kustomize infra/k8s/base
kubectl kustomize infra/k8s/overlays/gcp-migration
kubectl kustomize infra/k8s/overlays/gcp
kubectl apply --dry-run=server -k infra/k8s/overlays/gcp
```

Rendering does not prove that cloud resources or credentials exist.

## Apply in order

Migration Job specs are immutable, so delete a prior Job before applying its
overlay:

```bash
kubectl delete job backend-migrate --ignore-not-found
kubectl apply --dry-run=server -k infra/k8s/overlays/gcp-migration
kubectl apply -k infra/k8s/overlays/gcp-migration
kubectl wait --for=condition=complete job/backend-migrate --timeout=300s
kubectl logs job/backend-migrate
kubectl apply -k infra/k8s/overlays/gcp
```

Do not apply the application overlay until the Job succeeds. The application
overlay contains no Job. It includes backend and frontend workloads, GCE
Ingress, a `FrontendConfig` that redirects HTTP to HTTPS and the long-timeout
`BackendConfig` used by SSE. The backend container sets
`SKIP_STARTUP_MIGRATIONS=true`.

## Verify

```bash
kubectl rollout status deployment/backend --timeout=300s
kubectl rollout status deployment/frontend --timeout=300s
kubectl get pods,services,ingress
curl -sSI --max-time 10 http://YOUR_DOMAIN/ | head -n 1
```

The public HTTP check must report a 308 redirect after the managed certificate
and GKE load balancer finish reconciling.

Continue with health, workflow and Model Armor checks in the
[deployment runbook](../../docs/deployment-gcp.md#15-verify-health-logs-and-workflow-behavior).

## Teardown

```bash
kubectl delete -k infra/k8s/overlays/gcp
kubectl delete job backend-migrate --ignore-not-found
```

The out-of-band `agentcare-secrets` Secret is not owned by either
kustomization. Delete it separately only when the environment is being
removed. The backend Kubernetes service account is owned by the application
overlay.
