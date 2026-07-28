# Bootstrap stack

This stack is applied once per Google account and GitHub repository, by an
operator, from a local shell. It creates the trust that everything else depends
on: the remote state bucket, the enabled APIs and the two keyless GitHub
identities. Nothing here is applied by a workflow.

The main stack lives in `../terraform`. The release and infrastructure pipelines
are documented in [docs/ci-cd.md](../../docs/ci-cd.md), and the first-time
runbook in [docs/deployment-gcp.md](../../docs/deployment-gcp.md).

## What it creates

| Resource | Purpose |
|---|---|
| `google_storage_bucket.terraform_state` | versioned, uniform-access, public-access-prevented bucket for the main stack's remote state |
| `google_project_service.bootstrap` | the 17 APIs the stack uses, including `aiplatform.googleapis.com` |
| `google_service_account.github_deployer` | `agentcare-github-deployer`, the application release identity |
| `google_service_account.github_infra` | `agentcare-github-infra`, the Terraform identity |
| `google_iam_workload_identity_pool.github` | one trust boundary for one repository |
| `google_iam_workload_identity_pool_provider.github` | OIDC provider pinned to the numeric repository ID, owner ID and `refs/heads/<deploy_branch>` |

No service-account key is created. GitHub exchanges its short-lived OIDC token
for whichever identity the workflow is allowed to impersonate.

## Two service accounts, on purpose

| | `agentcare-github-deployer` | `agentcare-github-infra` |
|---|---|---|
| Display name | AgentCare GitHub application deployer | AgentCare GitHub infrastructure deployer |
| Used by | the `deploy-production` job in `ci.yml` | the `terraform` job in `infrastructure.yml` |
| Runs | on every push to the deploy branch | only from a manually gated workflow |
| Roles | `local.deployment_roles`, four roles: push an image and roll a Deployment | `local.infrastructure_roles`, twelve project admin roles, plus `roles/storage.objectAdmin` on the state bucket |
| Can change IAM | no | yes |
| Can delete the cluster or the database | no | yes |
| GitHub environment | `production` | `production-infra`, with a required reviewer |

An application release happens many times a day and is triggered by whoever
merged a commit, so it gets exactly what it needs and nothing more. One
compromised application build cannot reshape or tear down the project. The
Terraform identity carries `roles/resourcemanager.projectIamAdmin`, which can
grant any role to any principal. That is why it is only reachable from a
workflow behind a required reviewer.

Both accounts are bound to the same pool, provider and repository principal set.
The provider's `attribute_condition` already pins the branch, and a
`workflow_dispatch` run on that branch still presents
`ref = refs/heads/<deploy_branch>`. The separation is the two service accounts
and the two GitHub environments, not two trust boundaries.

Each role in `local.infrastructure_roles` has a comment naming the resource it
exists for. Read that block in `main.tf` before adding or removing one.

## Outputs

| Output | Set as |
|---|---|
| `terraform_state_bucket` | `TF_STATE_BUCKET` in the `production-infra` environment and in the operator's shell |
| `workload_identity_provider` | `GCP_WORKLOAD_IDENTITY_PROVIDER` in both `production` and `production-infra` |
| `deployer_service_account_email` | `GCP_DEPLOYER_SERVICE_ACCOUNT` in `production` |
| `infra_service_account_email` | `GCP_INFRA_SERVICE_ACCOUNT` in `production-infra` |

Keep `infra_service_account_email` out of the `production` environment. Only the
infrastructure workflow may use it.

## Apply it

```bash
make gcp-bootstrap \
  PROJECT_ID=your-project \
  GITHUB_REPOSITORY_ID=NUMERIC_REPOSITORY_ID \
  GITHUB_OWNER_ID=NUMERIC_OWNER_ID
```

The target enables `serviceusage.googleapis.com`, then runs `init`, a saved
`plan` and `apply` of that plan, and prints the outputs with the GitHub
environment each one belongs to. The raw commands are in
[docs/deployment-gcp.md](../../docs/deployment-gcp.md), section 4.

The numeric IDs come from:

```bash
gh api repos/OWNER/REPOSITORY --jq '{repository_id: .id, owner_id: .owner.id}'
```

Names can be renamed and reused. The immutable IDs cannot.

## State

The backend is `local` and the state file is gitignored. This small state is the
unavoidable trust bootstrap for the bucket that holds the main stack's state, so
it cannot live in that bucket. Keep it encrypted and backed up: losing it means
importing these resources again before the next change.

Terraform 1.7 or newer, `hashicorp/google` 7.41 or compatible.

## Variables

| Variable | Default | Notes |
|---|---|---|
| `project_id` | required | the project that holds AgentCare and its deployment identities |
| `region` | `europe-west3` | location of the state bucket |
| `state_bucket_name` | derived | empty derives `PROJECT_ID-agentcare-tfstate` |
| `github_repository_id` | required | digits only |
| `github_repository_owner_id` | required | digits only |
| `deploy_branch` | `main` | the only branch that may exchange GitHub OIDC credentials |

Changing `deploy_branch` changes the branch both identities can authenticate
from, including manual workflow runs.

## Destroying it

Destroy this stack only after the main stack is gone and its state history is no
longer needed. `force_destroy` is `false` on the state bucket, so a bucket that
still holds state objects refuses deletion. Keeping the bootstrap in place is
the normal choice: it lets `make gcp-up` rebuild the whole environment later
without repeating the GitHub trust setup.

## Checks

```bash
terraform fmt -check -recursive infra/bootstrap
terraform -chdir=infra/bootstrap init -backend=false -input=false
terraform -chdir=infra/bootstrap validate
```

The `infrastructure` job in `ci.yml` runs these on every push and pull request,
offline and with no credential.
