# GCP bootstrap

Apply this stack once from an authenticated operator shell. It creates the
resources the main Terraform stack and GitHub application release need.

## Resources

| Resource | Purpose |
|---|---|
| versioned GCS bucket | remote state for `infra/terraform` |
| required Google APIs | GKE, SQL, storage, IAM, Model Armor and telemetry |
| `agentcare-github-deployer` | narrow keyless application release identity |
| Workload Identity pool/provider | accepts only the intended repository, branch, environment and workflow |

No service-account key is created. No GitHub identity can apply or destroy
Terraform.

## Trust condition and boundary

The production release job requests `id-token: write`, then presents a GitHub
OIDC JWT to Workload Identity Federation. WIF issues a short-lived credential
for `agentcare-github-deployer` only when the JWT has all of these exact
properties:

- immutable repository ID
- immutable owner ID
- `refs/heads/main`
- environment `production`
- workflow ref `.github/workflows/ci.yml@refs/heads/main`

The deployer receives only Google IAM needed before Kubernetes authorization:

- `roles/artifactregistry.writer`
- `roles/container.clusterViewer`
- `roles/serviceusage.serviceUsageConsumer`

Those roles upload immutable images and discover the cluster. They do not grant
Kubernetes writes. `make gcp-up` installs the separate operator-owned platform
bundle, whose `agentcare` namespace RoleBinding authorizes the exact release
operations. The deployer cannot change project IAM, apply Terraform, delete the
cluster or delete Cloud SQL. It cannot read or create Secrets, change RBAC,
namespaces or KSAs, or use `exec`, `attach`, `portforward` or impersonation.

## Apply

Get the numeric GitHub IDs:

```bash
gh api repos/OWNER/REPOSITORY \
  --jq '{repository_id: .id, owner_id: .owner.id}'
```

Run:

```bash
make gcp-bootstrap \
  PROJECT_ID=your-project \
  GITHUB_REPOSITORY_ID=NUMERIC_REPOSITORY_ID \
  GITHUB_OWNER_ID=NUMERIC_OWNER_ID
```

The target initializes Terraform, creates a saved plan, displays it and
requires `apply` before changing GCP.

## Outputs

| Output | Consumer |
|---|---|
| `terraform_state_bucket` | main Terraform backend |
| `workload_identity_provider` | GitHub production variable |
| `deployer_service_account_email` | GitHub production variable |

`make gcp-github-vars` copies the two deployment outputs to GitHub after the
main infrastructure and public URL exist.

## State

Bootstrap uses local state because it creates the remote state bucket. The
state file is ignored. Back it up to encrypted storage outside the repository.
Main infrastructure state lives in the versioned GCS bucket.

Keeping bootstrap after `make gcp-down` is normal. It lets the main environment
be recreated without rebuilding trust.

## Validation

```bash
terraform fmt -check -recursive infra/bootstrap
terraform -chdir=infra/bootstrap init -backend=false -input=false
terraform -chdir=infra/bootstrap validate
```

See [GCP deployment](../../docs/deployment-gcp.md) for the complete order and
[CI/CD](../../docs/ci-cd.md) for GitHub release behavior.
