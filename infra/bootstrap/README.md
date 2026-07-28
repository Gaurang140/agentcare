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

## Trust condition

A GitHub token must match:

- immutable repository ID
- immutable owner ID
- `refs/heads/main`
- environment `production`
- workflow `.github/workflows/ci.yml` on `main`

The deployer receives only:

- `roles/artifactregistry.writer`
- `roles/container.clusterViewer`
- `roles/container.developer`
- `roles/serviceusage.serviceUsageConsumer`

It cannot change project IAM, delete the cluster or delete Cloud SQL.

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
