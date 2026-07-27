# Kubernetes manifests

This directory contains the Kustomize base and GCP overlay for AgentCare.
It is a manifest-local reference. Use the
[GCP deployment runbook](../../docs/deployment-gcp.md) for provisioning,
secrets, images, database setup, DNS, rollback and cost controls.

## Layout

```text
base/
  backend.yaml
  frontend.yaml
  migration-job.yaml
  configmap.yaml
  secret.example.yaml
  kustomization.yaml
overlays/gcp/
  ingress.yaml
  backendconfig.yaml
  configmap-storage.yaml
  configmap-model-armor.yaml
  kustomization.yaml
```

`secret.example.yaml` is documentation only and is not rendered by the base.

## Prerequisites

Before applying:

- `kubectl` must target the intended GKE cluster
- `kustomize` must be installed for image rewrites
- `agentcare-secrets` must exist in the namespace
- the Cloud SQL database must exist and be migrated by the Job
- GCS, Model Armor, frontend origin and image values must be environment-specific
- backend Workload Identity must be bound as described in the deployment runbook

## Render

```bash
kubectl kustomize infra/k8s/base
kubectl kustomize infra/k8s/overlays/gcp
kubectl apply --dry-run=server -k infra/k8s/overlays/gcp
```

Rendering does not prove cloud resources or credentials exist.

## Apply

Migration Job specs are immutable, so remove a previous Job first:

```bash
kubectl delete job backend-migrate --ignore-not-found
kubectl apply -k infra/k8s/overlays/gcp
```

The overlay includes the migration Job, backend and frontend workloads, GCE
Ingress and the long-timeout BackendConfig used by SSE.

## Verify

```bash
kubectl wait --for=condition=complete job/backend-migrate --timeout=300s
kubectl logs job/backend-migrate
kubectl rollout status deployment/backend --timeout=300s
kubectl rollout status deployment/frontend --timeout=300s
kubectl get pods,services,ingress
```

Continue with health, workflow and Model Armor checks in the
[deployment runbook](../../docs/deployment-gcp.md#15-verify-health-logs-and-workflow-behavior).

## Teardown

```bash
kubectl delete -k infra/k8s/overlays/gcp
```

The out-of-band `agentcare-secrets` Secret and backend Kubernetes service
account are not owned by this kustomization. Delete them separately only when
the environment is being removed.
