# infra/k8s

Kustomize manifests for the GKE Autopilot cluster `infra/terraform` provisions.
`kubectl kustomize infra/k8s/base` and `kubectl kustomize infra/k8s/overlays/gcp`
render both without touching a cluster.

**Apply order:** get cluster credentials (`tofu output` values, see
`docs/deployment-gcp.md`) -> create the real `agentcare-secrets` Secret once
per cluster (command in `base/secret.example.yaml`, never rendered by
kustomize) -> push images (`docker buildx build --platform linux/amd64
--push`) -> `cd infra/k8s/overlays/gcp && kustomize edit set image
agentcare-backend=<repo>/backend:<tag> agentcare-frontend=<repo>/frontend:<tag>`
-> `kubectl apply -k infra/k8s/overlays/gcp` -> `kubectl rollout status
deployment/backend deployment/frontend`.

**Zero-LB demo path:** skip `ingress.yaml` (~18 USD/mo once provisioned) and
run `kubectl port-forward svc/frontend 3000:3000` instead.

**Teardown:** `kubectl delete -k infra/k8s/overlays/gcp`. The migration Job is
immutable once created, so a leftover `backend-migrate` blocks the next apply
until removed: `kubectl delete job backend-migrate --ignore-not-found` (CI
does this automatically, see `.github/workflows/deploy.yml`).
