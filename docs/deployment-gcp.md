# Deploy AgentCare to GCP

This is the only end-to-end cloud runbook for AgentCare.

> **Current status:** GCP infrastructure source, Kubernetes manifests and
> deployment adapters are committed. No live GCP project, Vertex response,
> Model Armor call, database migration or deployed workload has been verified
> from this repository. Treat every success check below as operator work and
> record its output for the environment you create.

GCP is the sole deployment target. Infrastructure source is in
`infra/terraform`, which is standard Terraform-compatible HCL. The preferred
executable in this guide is OpenTofu. Kubernetes source is in `infra/k8s`.

## What the repository configures

| Layer | Configured resource |
|---|---|
| Images | Artifact Registry repository |
| Compute | Regional GKE Autopilot cluster |
| Database | Cloud SQL for PostgreSQL 17 with private IP |
| Documents | GCS bucket with public access prevention |
| Runtime identity | Backend KSA/GSA pair through GKE Workload Identity |
| Safety provider | Model Armor template |
| Workloads | Ordered migration Job, then backend, frontend, Services and GCE Ingress |

The backend remains one replica because APScheduler jobs run in-process without
a distributed lock.

## Manual gaps

The committed configuration does not complete these items:

1. GCP project selection, billing and API enablement
2. Cloud SQL database, database user and connection URL
3. secret values and the `agentcare-secrets` Kubernetes Secret
4. GCS bucket, Model Armor template and project sentinels in the overlays
5. image registry names and immutable image tags in both overlays
6. `FRONTEND_ORIGIN`
7. optional Vertex API, IAM flag, profile and Google project/location
8. public DNS and a real domain for the managed TLS certificate
9. ordered migration, live health checks and workflow smoke tests

Runtime pods read one Kubernetes Secret. The runtime KSA, GSA annotation and
KSA-to-GSA IAM binding are declarative.

## Cost warning

GKE Autopilot workloads, Cloud SQL and a public GCE load balancer can accrue
charges while idle. Pricing and trial credits change. Check the current GCP
estimate and set a billing budget before applying.

Delete the Ingress and workloads before destroying infrastructure. The
documents bucket refuses destructive teardown while it contains objects.

## 1. Set local deployment variables

Run from the repository root:

```bash
export PROJECT_ID="your-gcp-project-id"
export REGION="europe-west3"
export ENABLE_VERTEX_AI="false"
```

Confirm the values before any mutating command:

```bash
printf 'project=%s\nregion=%s\nvertex=%s\n' \
  "$PROJECT_ID" "$REGION" "$ENABLE_VERTEX_AI"
```

## 2. Check local tools

Required tools:

```bash
gcloud --version
tofu -version
docker --version
kubectl version --client
kustomize version
curl --version
```

Plain Terraform can consume the same `infra/terraform` files. If using it,
substitute `terraform` for `tofu` consistently.

## 3. Authenticate and confirm billing

Authenticate the CLI and Application Default Credentials separately:

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project "$PROJECT_ID"
```

Verify both identities and the selected project:

```bash
gcloud auth list --filter=status:ACTIVE --format='value(account)'
gcloud auth application-default print-access-token >/dev/null
gcloud config get-value project
gcloud billing projects describe "$PROJECT_ID"
```

The billing response must show an enabled billing account before provisioning.
OpenTofu's Google provider reads ADC, not only the `gcloud` login.

## 4. Enable required APIs

API enablement is not managed by `infra/terraform`:

```bash
gcloud services enable \
  artifactregistry.googleapis.com \
  compute.googleapis.com \
  container.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  modelarmor.googleapis.com \
  servicenetworking.googleapis.com \
  sqladmin.googleapis.com \
  storage.googleapis.com \
  sts.googleapis.com
```

When `ENABLE_VERTEX_AI=true`, also enable Vertex AI:

```bash
gcloud services enable aiplatform.googleapis.com
```

Confirm the relevant services are enabled:

```bash
gcloud services list --enabled \
  --filter='NAME:(artifactregistry.googleapis.com container.googleapis.com sqladmin.googleapis.com modelarmor.googleapis.com aiplatform.googleapis.com)'
```

## 5. Initialize, validate and plan

OpenTofu state is local unless the operator configures the commented GCS
backend in `infra/terraform/backend.tf`.

```bash
tofu -chdir=infra/terraform init
tofu -chdir=infra/terraform validate
tofu -chdir=infra/terraform plan \
  -out=/tmp/agentcare.tfplan \
  -var="project_id=$PROJECT_ID" \
  -var="region=$REGION" \
  -var="gcs_location=$REGION" \
  -var="enable_vertex_ai=$ENABLE_VERTEX_AI"
```

Review the plan. It should target only the intended project and region.

## 6. Apply infrastructure and capture outputs

```bash
tofu -chdir=infra/terraform apply /tmp/agentcare.tfplan
tofu -chdir=infra/terraform output
```

Capture values used below:

```bash
export IMAGE_REPO="$(
  tofu -chdir=infra/terraform output -raw artifact_registry_repository_url
)"
export DOCUMENTS_BUCKET="$(
  tofu -chdir=infra/terraform output -raw documents_bucket_name
)"
export BACKEND_GSA="$(
  tofu -chdir=infra/terraform output -raw backend_service_account_email
)"
export MODEL_ARMOR_TEMPLATE="$(
  tofu -chdir=infra/terraform output -raw model_armor_template_name
)"
export DB_HOST="$(
  tofu -chdir=infra/terraform output -raw cloud_sql_private_ip_address
)"
```

Inspect them without exposing credentials:

```bash
printf 'images=%s\nbucket=%s\nbackend_identity=%s\nmodel_armor=%s\ndb_host=%s\n' \
  "$IMAGE_REPO" "$DOCUMENTS_BUCKET" "$BACKEND_GSA" "$MODEL_ARMOR_TEMPLATE" "$DB_HOST"
```

These outputs confirm resource addresses, not runtime readiness.

## 7. Create the Cloud SQL database and user

The module creates the Cloud SQL instance and private networking. It does not
create the application database or user.

```bash
gcloud sql databases create agentcare \
  --instance=agentcare-postgres
```

Create a strong database password outside the repository, then provide it to
the CLI through a local environment variable:

```bash
read -s DB_PASSWORD
export DB_PASSWORD
gcloud sql users create agentcare \
  --instance=agentcare-postgres \
  --password="$DB_PASSWORD"
```

The `cloud_sql_private_ip_address` output captured in section 6 is the direct
PostgreSQL host. Confirm it is non-empty before constructing the URL:

```bash
test -n "$DB_HOST"
printf 'cloud-sql-private-ip=%s\n' "$DB_HOST"
```

Construct the SQLAlchemy URL locally:

```text
postgresql+psycopg://agentcare:URL_ENCODED_PASSWORD@PRIVATE_IP:5432/agentcare
```

Percent-encode reserved characters in the password. Do not commit the URL.
The current root Terraform outputs do not generate it.

Before a migration against an environment that matters, create an on-demand
Cloud SQL backup:

```bash
gcloud sql backups create --instance=agentcare-postgres
gcloud sql backups list --instance=agentcare-postgres
```

## 8. Prepare secret material

Prepare the runtime Kubernetes values in a temporary file:

```bash
umask 077
"${EDITOR:-vi}" /tmp/agentcare-secrets.env
```

The file needs these keys. Values stay local:

```dotenv
LLM_API_KEY=
LLM_FALLBACK_API_KEY=
JWT_SECRET=
DATABASE_URL=
INTERNAL_TASK_TOKEN=
LANGFUSE_SECRET_KEY=
```

For the Groq profile, set `LLM_API_KEY`. For Vertex, leave that key empty and
configure workload identity plus the non-secret Google variables in the next
section.

## 9. Connect kubectl

```bash
gcloud container clusters get-credentials \
  "$(tofu -chdir=infra/terraform output -raw gke_cluster_name)" \
  --region "$(tofu -chdir=infra/terraform output -raw gke_cluster_location)"

kubectl config current-context
kubectl get nodes
```

Create or update the runtime Secret:

```bash
kubectl create secret generic agentcare-secrets \
  --from-env-file=/tmp/agentcare-secrets.env \
  --dry-run=client -o yaml |
  kubectl apply -f -

rm /tmp/agentcare-secrets.env
unset DB_PASSWORD
```

The generated manifest output is piped directly to the cluster. Do not save it
inside the repository.

## 10. Confirm declarative Workload Identity

Terraform grants `roles/iam.workloadIdentityUser` on the backend GSA to
`default/agentcare-backend`. The base Deployment sets
`serviceAccountName: agentcare-backend`. The GCP overlay adds the
`iam.gke.io/gcp-service-account` annotation.

Before rendering, replace the `PROJECT_ID` sentinel in
`infra/k8s/overlays/gcp/serviceaccount-workload-identity.yaml`. Do not create,
annotate, bind or patch the service account with imperative commands. Inspect
the rendered relationship in section 13 and verify the running pod after
rollout.

## 11. Fill non-secret deployment configuration

Before applying, replace the environment-specific values in the working copy:

| File | Required value |
|---|---|
| `infra/k8s/base/configmap.yaml` | Real `FRONTEND_ORIGIN` when the public origin is known |
| `infra/k8s/overlays/gcp/configmap-storage.yaml` | `GCS_BUCKET=$DOCUMENTS_BUCKET` |
| `infra/k8s/overlays/gcp/configmap-model-armor.yaml` | `MODEL_ARMOR_TEMPLATE=$MODEL_ARMOR_TEMPLATE` |
| `infra/k8s/overlays/gcp/serviceaccount-workload-identity.yaml` | Replace `PROJECT_ID` |
| `infra/k8s/overlays/gcp/ingress.yaml` | Replace the sample domain |

Confirm no sentinel remains in rendered configuration:

```bash
grep -R -n 'REPLACE_ME\|PLACEHOLDER\|PROJECT_ID\|example\.com' \
  infra/k8s/base infra/k8s/overlays/gcp infra/k8s/overlays/gcp-migration
```

The command should return no match before a public deployment.

### Local model and ADC truth

For local Compose use, an OpenAI-compatible `LLM_BASE_URL` must be reachable
from inside the backend container. Container `localhost` is not the host
machine; use a container DNS name or a supported host gateway. Host ADC from
`gcloud auth application-default login` is not automatically mounted by
Compose. Direct local backend execution is the documented local Vertex path
unless the operator explicitly mounts ADC.

### Groq profile

The YAML default is `groq`. The Kubernetes Secret must contain
`LLM_API_KEY`. The base ConfigMap selects only `LLM_PROFILE=groq`; endpoint and
model defaults remain in `backend/llm.yaml`.

### Vertex profile

Vertex uses ADC through the pod's Workload Identity, not a Google credential
value in the Kubernetes Secret. Set `ENABLE_VERTEX_AI=true` before the
OpenTofu plan, enable `aiplatform.googleapis.com` and add these non-secret
values to `agentcare-config` before rollout:

```yaml
LLM_PROFILE: "vertex"
GOOGLE_CLOUD_PROJECT: "your-gcp-project-id"
GOOGLE_CLOUD_LOCATION: "europe-west3"
```

The Vertex profile is configured and construction is unit-tested. This
procedure does not claim live authentication, quota or model response until
the operator completes the workflow smoke test.

## 12. Build and push immutable images

Configure Docker for the registry host:

```bash
gcloud auth configure-docker "${IMAGE_REPO%%/*}"
export IMAGE_TAG="$(git rev-parse HEAD)"
```

Build for the GKE node architecture:

```bash
docker buildx build \
  --platform linux/amd64 \
  -t "$IMAGE_REPO/backend:$IMAGE_TAG" \
  -f backend/Dockerfile \
  --push .

docker buildx build \
  --platform linux/amd64 \
  -t "$IMAGE_REPO/frontend:$IMAGE_TAG" \
  --push ./frontend
```

Point both overlays at those exact images:

```bash
cd infra/k8s/overlays/gcp-migration
kustomize edit set image \
  "agentcare-backend=$IMAGE_REPO/backend:$IMAGE_TAG"
cd ../../../..

cd infra/k8s/overlays/gcp
kustomize edit set image \
  "agentcare-backend=$IMAGE_REPO/backend:$IMAGE_TAG" \
  "agentcare-frontend=$IMAGE_REPO/frontend:$IMAGE_TAG"
cd ../../../..
```

Do not commit environment-specific image rewrites.

## 13. Render and apply

Render both stages before touching the cluster:

```bash
kubectl kustomize infra/k8s/overlays/gcp-migration \
  >/tmp/agentcare-migration-rendered.yaml
kubectl kustomize infra/k8s/overlays/gcp >/tmp/agentcare-rendered.yaml
if grep -Eq 'REPLACE_ME|PLACEHOLDER|PROJECT_ID|REGION-docker|/PROJECT/|:TAG|example\.com' \
  /tmp/agentcare-migration-rendered.yaml /tmp/agentcare-rendered.yaml; then
  echo "refusing apply: unresolved deployment sentinel" >&2
  exit 1
fi
kubectl apply --dry-run=server -f /tmp/agentcare-rendered.yaml
```

Inspect the application render for its declarative identity and the absence of
a Job:

```bash
grep -n 'kind: ServiceAccount\|serviceAccountName: agentcare-backend\|iam.gke.io/gcp-service-account' \
  /tmp/agentcare-rendered.yaml
if grep -q 'kind: Job' /tmp/agentcare-rendered.yaml; then
  echo "application render unexpectedly contains a Job" >&2
  exit 1
fi
```

The migration Job spec is immutable. Delete any prior Job, server-dry-run the
migration artifact, apply it and wait:

```bash
kubectl delete job backend-migrate --ignore-not-found
kubectl apply --dry-run=server -f /tmp/agentcare-migration-rendered.yaml
kubectl apply -f /tmp/agentcare-migration-rendered.yaml
kubectl wait \
  --for=condition=complete job/backend-migrate \
  --timeout=300s
kubectl logs job/backend-migrate
```

Do not deploy the application if the Job fails. Only after success, apply the
application artifact and wait for both workloads:

```bash
kubectl apply -f /tmp/agentcare-rendered.yaml
kubectl rollout status deployment/backend --timeout=300s
kubectl rollout status deployment/frontend --timeout=300s
kubectl get pods
```

## 14. Configure public DNS and TLS

`infra/k8s/overlays/gcp/ingress.yaml` carries a sample domain. Replace it with
a domain the operator controls and point the domain's A record at the Ingress
IP:

```bash
kubectl get ingress agentcare \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
kubectl describe managedcertificate agentcare-cert
```

The certificate remains provisioning until DNS resolves to the load balancer.
Public DNS and TLS have not been verified by the repository.

To test the frontend independently of public Ingress readiness, use port
forwarding:

```bash
kubectl port-forward svc/frontend 3000:3000
```

## 15. Verify health, logs and workflow behavior

### Health

In one terminal:

```bash
kubectl port-forward svc/backend 8000:8000
```

In another:

```bash
curl -sf --max-time 10 http://localhost:8000/api/health
```

Expected shape:

```json
{"status":"ok","db":true}
```

This verifies API and database reachability. It does not verify model access.

### Logs

```bash
kubectl logs job/backend-migrate
kubectl logs deployment/backend --tail=200
kubectl describe deployment backend
```

Look for database errors, identity failures, model configuration errors,
`model_armor_unavailable` or `model_armor_failed`.

### Deterministic workflow

Log in with the seeded synthetic patient:

```bash
curl -sS -c /tmp/agentcare-cookies.txt \
  -X POST http://localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"patient@agentcare-demo.com","password":"demo1234"}'
```

Submit an emergency test, which needs no model:

```bash
curl -sS -b /tmp/agentcare-cookies.txt \
  -X POST http://localhost:8000/api/requests \
  -F 'text=I have severe chest pain and cannot breathe'
```

Verify an escalated status, localized emergency guidance and an emergency row
in the staff queue.

### Model-assisted workflow

```bash
curl -sS -b /tmp/agentcare-cookies.txt \
  -X POST http://localhost:8000/api/requests \
  -F 'text=Book me a cardiology appointment next week'
```

Record the returned workflow identifier, then inspect:

```bash
curl -sS -b /tmp/agentcare-cookies.txt \
  "http://localhost:8000/api/workflows/WORKFLOW_ID"
```

A completed run verifies the selected model profile, graph, SQL tools and
checkpointer together. A staff escalation instead of guessed output is the
expected failure mode for unavailable model access.

### Model Armor

Confirm the configured resource and pod identity:

```bash
tofu -chdir=infra/terraform output -raw model_armor_template_name
kubectl get pod \
  -l app=backend \
  -o jsonpath='{.items[0].spec.serviceAccountName}'
kubectl exec deployment/backend -- printenv MODEL_ARMOR_TEMPLATE
```

Then submit an operator-approved injection fixture that deterministic rules do
not already match. A Model Armor block produces a safety escalation and
`safety.injection_blocked` with `via: model_armor` in the staff audit view.

A clean verdict produces no dedicated success audit event. Absence of an error
log alone is not proof of a live call. Preserve provider-side evidence or the
blocked audit row before claiming Model Armor verification.

Remove the temporary cookie file:

```bash
rm /tmp/agentcare-cookies.txt
```

## 16. Roll back

Inspect rollout history:

```bash
kubectl rollout history deployment/backend
kubectl rollout history deployment/frontend
```

Roll back application images:

```bash
kubectl rollout undo deployment/backend
kubectl rollout undo deployment/frontend
kubectl rollout status deployment/backend --timeout=300s
kubectl rollout status deployment/frontend --timeout=300s
```

Repeat the health and deterministic workflow checks after rollback.

Application rollback does not reverse an Alembic migration. If a migration is
not backward-compatible, stop writes and restore the pre-migration Cloud SQL
backup according to the operator's recovery procedure. Confirm backup
availability before rollout:

```bash
gcloud sql backups list --instance=agentcare-postgres
```

## 17. Troubleshooting

### OpenTofu cannot find credentials

Run the ADC login again and verify
`gcloud auth application-default print-access-token`.

### OpenTofu cannot find the default VPC

The Cloud SQL module currently reads a network named `default`. A project
without that network needs a reviewed infrastructure change before apply.

### Migration Job cannot connect

Check `DATABASE_URL`, the created database/user, URL encoding and the private
Cloud SQL address. Read `kubectl logs job/backend-migrate` first.

### Pods report `CreateContainerConfigError`

Confirm `agentcare-secrets` exists and contains every key consumed by
`envFrom`:

```bash
kubectl describe secret agentcare-secrets
kubectl describe pod -l app=backend
```

The first command shows keys and sizes, not plaintext values.

### Images cannot be pulled

Inspect the exact image and events:

```bash
kubectl describe pod -l app=backend
kubectl get deployment backend \
  -o jsonpath='{.spec.template.spec.containers[0].image}'
```

Confirm the image exists in Artifact Registry and targets `linux/amd64`.

### GCS or Model Armor returns permission denied

Confirm the pod uses `agentcare-backend`, the Kubernetes service account has
the annotation and the Google service account grants the expected role.

### SSE stops behind the Ingress

Confirm the backend Service references `backend-backendconfig` and its timeout
is 3600 seconds:

```bash
kubectl get service backend -o yaml
kubectl get backendconfig backend-backendconfig -o yaml
```

### Certificate remains provisioning

Confirm the managed certificate domain is real, DNS resolves to the Ingress IP
and the Ingress references `agentcare-cert`.

## 18. Tear down

Delete Kubernetes resources first so the load balancer is removed:

```bash
kubectl delete -k infra/k8s/overlays/gcp
kubectl delete job backend-migrate --ignore-not-found
kubectl delete secret agentcare-secrets
```

Confirm that no Ingress remains:

```bash
kubectl get ingress
```

Destroy managed infrastructure:

```bash
tofu -chdir=infra/terraform destroy \
  -var="project_id=$PROJECT_ID" \
  -var="region=$REGION" \
  -var="gcs_location=$REGION" \
  -var="enable_vertex_ai=$ENABLE_VERTEX_AI"
```

If the documents bucket contains objects, destroy stops. Review the bucket,
then delete its contents only when data retention permits. Re-read the output,
require the exact project-derived bucket name and type the resolved target:

```bash
FRESH_DOCUMENTS_BUCKET="$(
  tofu -chdir=infra/terraform output -raw documents_bucket_name
)"
EXPECTED_DOCUMENTS_BUCKET="${PROJECT_ID}-agentcare-documents"

test -n "$FRESH_DOCUMENTS_BUCKET" || {
  echo "refusing purge: empty bucket output" >&2
  exit 1
}
test "$FRESH_DOCUMENTS_BUCKET" = "$EXPECTED_DOCUMENTS_BUCKET" || {
  echo "refusing purge: expected $EXPECTED_DOCUMENTS_BUCKET, got $FRESH_DOCUMENTS_BUCKET" >&2
  exit 1
}

BUCKET_URI="gs://${FRESH_DOCUMENTS_BUCKET}"
printf 'resolved purge target: %s\n' "$BUCKET_URI"
gcloud storage ls "$BUCKET_URI"
printf 'Type DELETE %s to continue: ' "$BUCKET_URI"
read -r PURGE_CONFIRMATION
test "$PURGE_CONFIRMATION" = "DELETE $BUCKET_URI" || {
  echo "purge cancelled" >&2
  exit 1
}
gcloud storage rm --recursive "${BUCKET_URI}/**"
```

Object deletion is irreversible. Run destroy again after the bucket is empty.

Finally confirm the expensive resources are gone and inspect billing:

```bash
gcloud container clusters list
gcloud sql instances list
gcloud compute forwarding-rules list
gcloud billing projects describe "$PROJECT_ID"
```

Keep the deployment evidence with the environment record. Do not update
project documentation from configured to live-verified until these checks
have actually run.
