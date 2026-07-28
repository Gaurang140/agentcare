locals {
  state_bucket_name = var.state_bucket_name != "" ? var.state_bucket_name : "${var.project_id}-agentcare-tfstate"

  required_services = toset([
    "aiplatform.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "compute.googleapis.com",
    "container.googleapis.com",
    "dns.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "logging.googleapis.com",
    "modelarmor.googleapis.com",
    "monitoring.googleapis.com",
    "networkconnectivity.googleapis.com",
    "servicenetworking.googleapis.com",
    "serviceusage.googleapis.com",
    "sqladmin.googleapis.com",
    "storage.googleapis.com",
    "sts.googleapis.com",
  ])

  deployment_roles = toset([
    "roles/artifactregistry.writer",
    "roles/container.clusterViewer",
    "roles/container.developer",
    "roles/serviceusage.serviceUsageConsumer",
  ])

  # Everything the main stack needs to be applied AND destroyed, derived from
  # the resources it actually declares. Each role is here for a named reason:
  #
  #   artifactregistry.admin        repository create/delete, cleanup policies,
  #                                 and the repository-level reader binding
  #   cloudsql.admin                the optional private-IP Postgres instance
  #   compute.networkAdmin          global addresses, the Model Armor PSC
  #                                 address and the regional endpoint
  #   compute.securityAdmin         the SSL policy; sslPolicies permissions do
  #                                 not live under networkAdmin
  #   container.admin               the GKE Autopilot cluster
  #   dns.admin                     the private zone and record for the
  #                                 Model Armor regional endpoint
  #   iam.serviceAccountAdmin       the backend and node service accounts, plus
  #                                 the workloadIdentityUser binding on them
  #   iam.serviceAccountUser        actAs on the node service account, which
  #                                 Autopilot node auto-provisioning requires.
  #                                 Project-scoped because the target account
  #                                 does not exist until the main stack runs,
  #                                 and serviceAccountAdmin does not imply actAs
  #   modelarmor.admin              the Model Armor template. The backend
  #                                 runtime only ever gets modelarmor.user
  #   resourcemanager.projectIamAdmin  the three project-level bindings in the
  #                                 iam module. This is the sharp one: it can
  #                                 grant any role to any principal, which is
  #                                 why this identity is gated behind the
  #                                 protected production-infra environment
  #   servicenetworking.networksAdmin  the private services access peering for
  #                                 Cloud SQL, on both create and destroy
  #   storage.admin                 the documents bucket. No predefined role
  #                                 grants bucket creation at a narrower scope
  infrastructure_roles = toset([
    "roles/artifactregistry.admin",
    "roles/cloudsql.admin",
    "roles/compute.networkAdmin",
    "roles/compute.securityAdmin",
    "roles/container.admin",
    "roles/dns.admin",
    "roles/iam.serviceAccountAdmin",
    "roles/iam.serviceAccountUser",
    "roles/modelarmor.admin",
    "roles/resourcemanager.projectIamAdmin",
    "roles/servicenetworking.networksAdmin",
    "roles/storage.admin",
  ])
}

resource "google_project_service" "bootstrap" {
  for_each = local.required_services

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_storage_bucket" "terraform_state" {
  project                     = var.project_id
  name                        = local.state_bucket_name
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = true
  }

  depends_on = [google_project_service.bootstrap]
}

resource "google_service_account" "github_deployer" {
  project      = var.project_id
  account_id   = "agentcare-github-deployer"
  display_name = "AgentCare GitHub application deployer"
  description  = "Keyless identity for commit-addressed image pushes and GKE application releases."

  depends_on = [google_project_service.bootstrap]
}

# Second identity, on purpose. An application release runs on every push to the
# deploy branch and must never be able to reshape or tear down the project, so
# the deployer above stays at push-an-image and roll-a-Deployment level. The
# Terraform identity below carries project-admin roles including the power to
# change IAM, and it is only reachable from the manually dispatched
# infrastructure workflow behind the protected production-infra environment.
# One compromised application build therefore cannot delete the cluster.
resource "google_service_account" "github_infra" {
  project      = var.project_id
  account_id   = "agentcare-github-infra"
  display_name = "AgentCare GitHub infrastructure deployer"
  description  = "Keyless identity for Terraform apply and destroy of the main stack from GitHub Actions."

  depends_on = [google_project_service.bootstrap]
}

resource "google_iam_workload_identity_pool" "github" {
  project                   = var.project_id
  workload_identity_pool_id = "agentcare-github"
  display_name              = "AgentCare GitHub Actions"
  description               = "Trust boundary for the one AgentCare repository."

  depends_on = [google_project_service.bootstrap]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "agentcare-repository"
  display_name                       = "AgentCare production branch"

  attribute_mapping = {
    "google.subject"                = "assertion.sub"
    "attribute.repository_id"       = "assertion.repository_id"
    "attribute.repository_owner_id" = "assertion.repository_owner_id"
    "attribute.ref"                 = "assertion.ref"
  }

  attribute_condition = join(" && ", [
    "assertion.repository_id == '${var.github_repository_id}'",
    "assertion.repository_owner_id == '${var.github_repository_owner_id}'",
    "assertion.ref == 'refs/heads/${var.deploy_branch}'",
  ])

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com/"
  }
}

resource "google_service_account_iam_member" "github_workload_identity_user" {
  service_account_id = google_service_account.github_deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository_id/${var.github_repository_id}"
}

resource "google_project_iam_member" "github_deployment_roles" {
  for_each = local.deployment_roles

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.github_deployer.email}"
}

# Same pool, same provider, same repository attribute as the deployer. The
# provider's attribute_condition already pins the branch, and a
# workflow_dispatch run on the deploy branch still presents
# ref = refs/heads/<deploy_branch>, so the manual infrastructure workflow
# exchanges credentials under exactly the same trust boundary.
resource "google_service_account_iam_member" "github_infra_workload_identity_user" {
  service_account_id = google_service_account.github_infra.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository_id/${var.github_repository_id}"
}

resource "google_project_iam_member" "github_infrastructure_roles" {
  for_each = local.infrastructure_roles

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.github_infra.email}"
}

# State access is granted on the bucket, not the project. roles/storage.admin
# in the role set above already covers this bucket, so the binding is not what
# makes the pipeline work today; it is what keeps it working, and readable, if
# that project grant is ever narrowed. objectAdmin is the right level: reading,
# writing and deleting state objects and lock files, with no power over the
# bucket's own versioning or retention settings.
resource "google_storage_bucket_iam_member" "github_infra_state_object_admin" {
  bucket = google_storage_bucket.terraform_state.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.github_infra.email}"
}
