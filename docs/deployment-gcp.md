# Deploy AgentCare to Google Cloud

This is the canonical infrastructure and first-release runbook. Daily code
releases are documented in [CI/CD](ci-cd.md).

## Current truth

| Claim | State on 2026-07-28 |
|---|---|
| Public application health | verified at the existing public `/api/health` endpoint |
| Database connectivity | public health returned `db: true` |
| Last recorded cloud model profile | Vertex with `gemini-2.5-flash` |
| Automatic GitHub deployment | implemented in source, not active until the GitHub and Workload Identity setup is completed |
| Langfuse export | implemented and disabled by default, not live-verified |
| Live GCP configuration inspection | blocked until the intended Google account is active locally |

Configured source is not live evidence. A new account must complete every
verification in this guide before its environment is described as deployed.

## The operating model

```mermaid
flowchart LR
    OP["Operator"] --> TF["Terraform plan and apply"]
    TF --> INFRA["GKE, Cloud SQL, GCS,<br/>IAM, Model Armor and registry"]
    DEV["Push to GitHub main"] --> CI["All CI gates"]
    CI --> CD["Build SHA images,<br/>migrate and roll out"]
    CD --> GKE["Existing GKE environment"]
```

Terraform owns infrastructure. It runs when infrastructure changes and always
requires an operator-reviewed plan. GitHub Actions owns application releases.
After one-time activation, a successful push to `main` updates GKE without a
manual Terraform command.

The repository configures:

| Layer | Resource |
|---|---|
| Images | Artifact Registry |
| Compute | regional GKE Autopilot |
| Database | private-IP Cloud SQL for PostgreSQL 17 |
| Documents | private GCS bucket |
| Runtime identity | GKE Workload Identity |
| Safety provider | Model Armor template with a regional private endpoint |
| Metrics | Google Managed Service for Prometheus |
| Public entry | HTTPS GCE Ingress with managed certificate |
| Release | ordered migration Job followed by backend and frontend |

The backend stays at one replica because reminders run in-process without a
distributed lock. Move scheduled work to a durable worker before scaling it
horizontally.

## Cost before deployment

Set a budget alert before applying:

```bash
gcloud billing projects describe "$PROJECT_ID"
```

The committed demo requests 0.35 vCPU and 0.75 GiB across two long-running
pods. It also creates a shared-core Cloud SQL instance, an HTTPS load balancer
and a Private Service Connect endpoint.

At public list prices checked on 2026-07-28, a quiet environment is roughly
USD 50 to 60 per month when the billing account can use the USD 74.40 monthly
GKE management credit. It is roughly USD 123 to 133 without that credit.
Traffic, model calls, logs, stored data, backups and tax are additional. This
is an estimate, not an invoice.

Important recurring items are:

- Autopilot pod requests, roughly USD 14 per month at the committed requests
- Cloud SQL shared-core compute plus storage, roughly USD 9 to 12 per month
- HTTPS forwarding rule, roughly USD 18 per month before traffic
- Model Armor Private Service Connect forwarding rule, roughly USD 7 per month
- GKE management, roughly USD 73 per month before the eligible credit

Check actual spend:

`Google Cloud Console → Billing → Reports`

Official prices change. Use the
[GKE pricing page](https://cloud.google.com/kubernetes-engine/pricing),
[Cloud SQL pricing page](https://cloud.google.com/sql/pricing) and
[VPC pricing page](https://cloud.google.com/vpc/pricing) before leaving the
environment running.

## 1. Select the intended account and project

Run from the repository root:

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project "YOUR_PROJECT_ID"
```

The CLI login and Application Default Credentials are separate. Terraform
uses ADC.

```bash
gcloud auth list --filter=status:ACTIVE --format='value(account)'
gcloud auth application-default print-access-token >/dev/null
gcloud config get-value project
gcloud billing projects describe "YOUR_PROJECT_ID"
```

Stop if the account, project or billing account is not the intended one.

Set deployment values:

```bash
export PROJECT_ID="YOUR_PROJECT_ID"
export REGION="europe-west3"
export NETWORK_NAME="default"
export SUBNETWORK_NAME="default"
export ENABLE_VERTEX_AI="true"
```

Confirm them:

```bash
printf 'project=%s\nregion=%s\nnetwork=%s\nsubnetwork=%s\nvertex=%s\n' \
  "$PROJECT_ID" "$REGION" "$NETWORK_NAME" "$SUBNETWORK_NAME" \
  "$ENABLE_VERTEX_AI"
```

## 2. Check local tools

```bash
gcloud --version
terraform -version
docker --version
docker buildx version
kubectl version --client
kustomize version
gke-gcloud-auth-plugin --version
curl --version
openssl version
```

## 3. Create the public GitHub repository

Create an empty public repository. Do not initialize it with another README.

```bash
git remote add origin https://github.com/OWNER/REPOSITORY.git
git remote -v
gh api repos/OWNER/REPOSITORY \
  --jq '{repository_id: .id, owner_id: .owner.id}'
```

Keep the numeric repository ID and owner ID. The Google trust rule uses the
immutable IDs plus `refs/heads/main`.

## 4. Apply the Terraform bootstrap

The bootstrap creates:

- the versioned GCS state bucket
- all Google APIs used by the stack, including `aiplatform.googleapis.com`
- a dedicated GitHub deployer service account
- a Workload Identity pool and branch-restricted provider
- only the roles needed to push images and update GKE

Terraform needs Service Usage enabled before it can manage the remaining
services:

```bash
gcloud services enable serviceusage.googleapis.com --project="$PROJECT_ID"
```

Set the GitHub IDs:

```bash
export GITHUB_REPOSITORY_ID="NUMERIC_REPOSITORY_ID"
export GITHUB_OWNER_ID="NUMERIC_OWNER_ID"
```

Plan and apply:

```bash
terraform -chdir=infra/bootstrap init
terraform -chdir=infra/bootstrap fmt -check
terraform -chdir=infra/bootstrap validate
terraform -chdir=infra/bootstrap plan \
  -out=/tmp/agentcare-bootstrap.tfplan \
  -var="project_id=$PROJECT_ID" \
  -var="region=$REGION" \
  -var="github_repository_id=$GITHUB_REPOSITORY_ID" \
  -var="github_repository_owner_id=$GITHUB_OWNER_ID"
terraform -chdir=infra/bootstrap apply /tmp/agentcare-bootstrap.tfplan
```

Capture the non-secret outputs:

```bash
export TF_STATE_BUCKET="$(
  terraform -chdir=infra/bootstrap output -raw terraform_state_bucket
)"
export GCP_WORKLOAD_IDENTITY_PROVIDER="$(
  terraform -chdir=infra/bootstrap output -raw workload_identity_provider
)"
export GCP_DEPLOYER_SERVICE_ACCOUNT="$(
  terraform -chdir=infra/bootstrap output -raw deployer_service_account_email
)"
```

The bootstrap creates no service-account key. Keep its ignored local state
encrypted and backed up because it is the trust root for remote state.

Confirm the services Terraform now manages:

```bash
gcloud services list --enabled \
  --filter='NAME:(aiplatform.googleapis.com artifactregistry.googleapis.com cloudresourcemanager.googleapis.com compute.googleapis.com container.googleapis.com dns.googleapis.com iam.googleapis.com iamcredentials.googleapis.com logging.googleapis.com modelarmor.googleapis.com monitoring.googleapis.com networkconnectivity.googleapis.com servicenetworking.googleapis.com serviceusage.googleapis.com sqladmin.googleapis.com storage.googleapis.com sts.googleapis.com)'
```

## 5. Initialize remote state and create infrastructure

For a new account:

```bash
terraform -chdir=infra/terraform init \
  -backend-config="bucket=$TF_STATE_BUCKET"
terraform -chdir=infra/terraform fmt -check -recursive
terraform -chdir=infra/terraform validate
terraform -chdir=infra/terraform plan \
  -out=/tmp/agentcare.tfplan \
  -var="project_id=$PROJECT_ID" \
  -var="region=$REGION" \
  -var="gcs_location=$REGION" \
  -var="network_name=$NETWORK_NAME" \
  -var="subnetwork_name=$SUBNETWORK_NAME" \
  -var="enable_vertex_ai=$ENABLE_VERTEX_AI"
```

Review the project, region, resources and estimated changes. Apply only that
saved plan:

```bash
terraform -chdir=infra/terraform apply /tmp/agentcare.tfplan
terraform -chdir=infra/terraform output
```

If this checkout already has local state for an existing account, do not make
a new remote state and apply over it. Back it up and migrate:

```bash
umask 077
cp infra/terraform/terraform.tfstate \
  /tmp/agentcare-terraform-state.backup
terraform -chdir=infra/terraform init \
  -migrate-state \
  -backend-config="bucket=$TF_STATE_BUCKET"
terraform -chdir=infra/terraform state list
```

## 6. Capture infrastructure outputs

```bash
export IMAGE_REPO="$(
  terraform -chdir=infra/terraform output -raw artifact_registry_repository_url
)"
export DOCUMENTS_BUCKET="$(
  terraform -chdir=infra/terraform output -raw documents_bucket_name
)"
export MODEL_ARMOR_TEMPLATE="$(
  terraform -chdir=infra/terraform output -raw model_armor_template_name
)"
export DB_HOST="$(
  terraform -chdir=infra/terraform output -raw cloud_sql_private_ip_address
)"
export GKE_CLUSTER="$(
  terraform -chdir=infra/terraform output -raw gke_cluster_name
)"
export INGRESS_IP="$(
  terraform -chdir=infra/terraform output -raw ingress_ip_address
)"
```

```bash
printf 'images=%s\nbucket=%s\nmodel-armor=%s\ndb-host=%s\ncluster=%s\ningress-ip=%s\n' \
  "$IMAGE_REPO" "$DOCUMENTS_BUCKET" "$MODEL_ARMOR_TEMPLATE" "$DB_HOST" \
  "$GKE_CLUSTER" "$INGRESS_IP"
```

These are resource addresses, not secrets and not readiness evidence.

Terraform reserves `INGRESS_IP` before the application exists. Point the
chosen domain's A record at this address now. DNS can propagate while the
database and first release are prepared.

For a hackathon link without buying a domain, use the same `sslip.io` pattern
as the existing demo:

```bash
export PUBLIC_HOST="agentcare.${INGRESS_IP//./-}.sslip.io"
export PUBLIC_URL="https://${PUBLIC_HOST}"
printf 'public-url=%s\n' "$PUBLIC_URL"
```

`sslip.io` resolves the embedded address. For a company-owned domain, create
an A record and set `PUBLIC_URL` to that HTTPS origin instead.

## 7. Create the database and runtime Secret

Terraform creates the Cloud SQL instance. Create the application database and
user once:

```bash
gcloud sql databases create agentcare \
  --instance=agentcare-postgres
read -s DB_PASSWORD
export DB_PASSWORD
gcloud sql users create agentcare \
  --instance=agentcare-postgres \
  --password="$DB_PASSWORD"
```

Construct the URL locally with a percent-encoded password:

```text
postgresql+psycopg://agentcare:URL_ENCODED_PASSWORD@PRIVATE_IP:5432/agentcare?sslmode=require
```

Create a deployment-specific signing secret:

```bash
openssl rand -hex 32
```

Prepare an ignored temporary file:

```bash
umask 077
"${EDITOR:-vi}" /tmp/agentcare-secrets.env
```

```dotenv
LLM_API_KEY=
LLM_FALLBACK_API_KEY=
JWT_SECRET=
DATABASE_URL=
INTERNAL_TASK_TOKEN=
LANGFUSE_SECRET_KEY=
```

For Vertex, leave `LLM_API_KEY` empty. The backend pod uses Workload Identity.
Validate the required values:

```bash
grep -Eq '^JWT_SECRET=.{32,}$' /tmp/agentcare-secrets.env || {
  echo "JWT_SECRET must contain at least 32 characters" >&2
  exit 1
}
grep -Eq '^DATABASE_URL=postgresql\+psycopg://.+' \
  /tmp/agentcare-secrets.env || {
  echo "DATABASE_URL must be a PostgreSQL psycopg URL" >&2
  exit 1
}
```

Connect to GKE:

```bash
gcloud container clusters get-credentials "$GKE_CLUSTER" \
  --region "$REGION" \
  --project "$PROJECT_ID"
kubectl get nodes
```

Apply the Secret without saving a manifest:

```bash
kubectl create secret generic agentcare-secrets \
  --from-env-file=/tmp/agentcare-secrets.env \
  --dry-run=client -o yaml |
  kubectl apply -f -
rm /tmp/agentcare-secrets.env
unset DB_PASSWORD
```

## 8. Make the first application release

The normal path is GitHub Actions. Complete every environment variable and
secret in [CI/CD](ci-cd.md), then push `main`.

For a first manual release or CI/CD diagnosis, build commit-addressed images:

```bash
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
export IMAGE_TAG="$(git rev-parse HEAD)"
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

Set the public origin and model profile. Keep the `PUBLIC_URL` created above:

```bash
export LLM_PROFILE="vertex"
export LANGFUSE_PUBLIC_KEY=""
export LANGFUSE_BASE_URL="https://cloud.langfuse.com"
export LANGFUSE_SAMPLE_RATE="0"
export RENDERED_K8S="/tmp/agentcare-k8s-$IMAGE_TAG"
```

Render a temporary copy. The source manifests remain unchanged:

```bash
.venv/bin/python scripts/render_gcp_manifests.py \
  --output "$RENDERED_K8S"
kubectl kustomize "$RENDERED_K8S/overlays/gcp-migration" \
  >/tmp/agentcare-migration-rendered.yaml
kubectl kustomize "$RENDERED_K8S/overlays/gcp" \
  >/tmp/agentcare-rendered.yaml
```

Run the ordered release:

```bash
kubectl delete job backend-migrate --ignore-not-found=true --wait=true
kubectl apply -k "$RENDERED_K8S/overlays/gcp-migration"
kubectl wait \
  --for=condition=complete job/backend-migrate \
  --timeout=600s
kubectl logs job/backend-migrate --all-containers=true
kubectl apply -k "$RENDERED_K8S/overlays/gcp"
kubectl rollout status deployment/backend --timeout=600s
kubectl rollout status deployment/frontend --timeout=600s
```

Do not apply the application overlay if the migration fails.

## 9. Verify DNS and HTTPS

Confirm the Ingress uses the Terraform-reserved address:

```bash
test "$(
  kubectl get ingress agentcare \
    -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
)" = "$INGRESS_IP"
kubectl describe managedcertificate agentcare-cert
```

Wait for the managed certificate to become active, then verify:

```bash
curl -fsS --max-time 10 "https://YOUR_DOMAIN/api/health"
curl -sSI --max-time 10 "http://YOUR_DOMAIN/" | head -n 1
```

The health response must contain:

```json
{"status":"ok","db":true}
```

The HTTP response must redirect to HTTPS. Health proves API and database
reachability. It does not prove a model call, document storage or Model Armor.

## 10. Verify real end-to-end behavior

Port-forward the backend:

```bash
kubectl port-forward svc/backend 8000:8000
```

Log in with a synthetic seeded patient:

```bash
curl -sS -c /tmp/agentcare-cookies.txt \
  -X POST http://localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"patient@agentcare-demo.com","password":"demo1234"}'
```

Exercise deterministic safety plus a real GCS upload:

```bash
export SMOKE_FILENAME="agentcare-smoke-$(date -u +%Y%m%dT%H%M%SZ).txt"
printf 'synthetic AgentCare deployment smoke test\n' \
  >"/tmp/${SMOKE_FILENAME}"
curl -fsS -b /tmp/agentcare-cookies.txt \
  -X POST http://localhost:8000/api/requests \
  -F 'text=I have severe chest pain and cannot breathe' \
  -F "files=@/tmp/${SMOKE_FILENAME};type=text/plain"
gcloud storage ls "gs://${DOCUMENTS_BUCKET}/**${SMOKE_FILENAME}"
```

Exercise the selected LLM, graph, SQL tools and checkpointer:

```bash
curl -sS -b /tmp/agentcare-cookies.txt \
  -X POST http://localhost:8000/api/requests \
  -F 'text=Book me a cardiology appointment next week'
```

Inspect the returned workflow from the UI and the staff audit page. A model
failure should create a staff escalation instead of invented output.

Verify Model Armor and Workload Identity:

```bash
kubectl get pod \
  -l app=backend \
  -o jsonpath='{.items[0].spec.serviceAccountName}'
kubectl exec deployment/backend -- printenv MODEL_ARMOR_TEMPLATE
kubectl logs deployment/backend --tail=200
```

A controlled injection fixture should create
`safety.injection_blocked` with `via: model_armor`. Provider-side evidence or
that audit row is required before claiming a live Model Armor call.

Remove local smoke files:

```bash
rm /tmp/agentcare-cookies.txt "/tmp/${SMOKE_FILENAME}"
```

## 11. Observe the deployment

Cloud logs:

`Google Cloud Console → Logging → Logs Explorer`

Use:

```text
resource.type="k8s_container"
resource.labels.container_name="backend"
```

Prometheus metrics:

`Google Cloud Console → Monitoring → Metrics Explorer → PromQL`

Langfuse setup, masking and sampling are documented in
[Observability](observability.md). AgentCare does not deploy a second Grafana
instance into GCP because Cloud Monitoring reads the same managed Prometheus
data.

## 12. Activate automatic releases

Complete [CI/CD](ci-cd.md) once. The required path is:

```text
push main → all CI gates → keyless Google auth → build SHA images
→ migration Job → GKE rollout → public health check
```

Terraform does not run in this application release. A failed gate leaves the
current release running.

## 13. Roll back application code

Prefer an auditable Git revert:

```bash
git log --oneline -n 10
git revert BAD_COMMIT_SHA
git push origin main
```

The new commit passes the same CI/CD flow. Kubernetes rollout undo is an
emergency option:

```bash
kubectl rollout undo deployment/backend
kubectl rollout undo deployment/frontend
kubectl rollout status deployment/backend --timeout=600s
kubectl rollout status deployment/frontend --timeout=600s
```

An image rollback does not reverse an Alembic migration. Back up Cloud SQL
before a schema change:

```bash
gcloud sql backups create --instance=agentcare-postgres
gcloud sql backups list --instance=agentcare-postgres
```

## 14. Destroy the environment

First remove Kubernetes resources so GCP can delete the load balancer:

```bash
kubectl delete -k "$RENDERED_K8S/overlays/gcp"
kubectl delete job backend-migrate --ignore-not-found
kubectl delete secret agentcare-secrets
kubectl get ingress
```

Create and review a destroy plan:

```bash
terraform -chdir=infra/terraform plan -destroy \
  -out=/tmp/agentcare-destroy.tfplan \
  -var="project_id=$PROJECT_ID" \
  -var="region=$REGION" \
  -var="gcs_location=$REGION" \
  -var="network_name=$NETWORK_NAME" \
  -var="subnetwork_name=$SUBNETWORK_NAME" \
  -var="enable_vertex_ai=$ENABLE_VERTEX_AI"
terraform -chdir=infra/terraform apply \
  /tmp/agentcare-destroy.tfplan
```

The documents bucket refuses deletion while it contains objects. Review its
contents and retention requirements. Only then remove the exact bucket:

```bash
FRESH_DOCUMENTS_BUCKET="$(
  terraform -chdir=infra/terraform output -raw documents_bucket_name
)"
EXPECTED_DOCUMENTS_BUCKET="${PROJECT_ID}-agentcare-documents"
test "$FRESH_DOCUMENTS_BUCKET" = "$EXPECTED_DOCUMENTS_BUCKET"
BUCKET_URI="gs://${FRESH_DOCUMENTS_BUCKET}"
printf 'resolved purge target: %s\n' "$BUCKET_URI"
gcloud storage ls "$BUCKET_URI"
printf 'Type DELETE %s to continue: ' "$BUCKET_URI"
read -r PURGE_CONFIRMATION
test "$PURGE_CONFIRMATION" = "DELETE $BUCKET_URI"
gcloud storage rm --recursive "${BUCKET_URI}/**"
```

Object deletion is irreversible. Re-run the saved destroy plan only if its
configuration is still current. Otherwise create and review a new plan.

Keep the bootstrap state bucket and GitHub identity if you will deploy again.
Destroy `infra/bootstrap` only after the main stack is gone and its state
history is no longer required.

Confirm expensive resources are gone:

```bash
gcloud container clusters list
gcloud sql instances list
gcloud compute forwarding-rules list
gcloud billing projects describe "$PROJECT_ID"
```

## Troubleshooting

### GitHub push runs CI but no deployment

Confirm the push is on `main`, every gate passed and the workflow contains
`deploy production`. Then check the `production` environment variables in
[CI/CD](ci-cd.md).

### GitHub cannot authenticate to Google

Confirm the numeric repository ID, owner ID and `main` branch match the
bootstrap values. Confirm `id-token: write` remains limited to the deployment
job.

### Migration fails

Read:

```bash
kubectl logs job/backend-migrate --all-containers=true
```

Check the database, user, private IP and URL encoding in `DATABASE_URL`.

### Pods report `CreateContainerConfigError`

```bash
kubectl get secret agentcare-secrets -o name
kubectl describe pod -l app=backend
```

### Images cannot be pulled

```bash
kubectl describe pod -l app=backend
kubectl get deployment backend \
  -o jsonpath='{.spec.template.spec.containers[0].image}'
```

The image tag must be the full Git commit SHA, never `latest`.

### Model calls become staff escalations

Check `LLM_PROFILE`, Vertex API enablement, runtime Workload Identity, model
quota and backend logs. Escalation is the intended safe failure mode.

### Langfuse shows no traces

Check that both keys exist, `LANGFUSE_SAMPLE_RATE` is above zero and the
request was sampled. Tracing failure never stops workflow processing.
