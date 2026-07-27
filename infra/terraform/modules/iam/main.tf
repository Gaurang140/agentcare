data "google_project" "current" {
  project_id = var.project_id
}

# -----------------------------------------------------------------------
# Runtime service accounts - one per workload, never the default compute SA
# -----------------------------------------------------------------------

resource "google_service_account" "backend" {
  project      = var.project_id
  account_id   = "agentcare-backend"
  display_name = "AgentCare backend runtime"
  description  = "Identity for the FastAPI/LangGraph backend pod. Binding it to a Kubernetes service account through GKE Workload Identity is a manual step, not wired in infra/k8s yet (see docs/deployment-gcp.md); no key is ever downloaded for it."
}

resource "google_service_account" "frontend" {
  project      = var.project_id
  account_id   = "agentcare-frontend"
  display_name = "AgentCare frontend runtime"
  description  = "Identity for the Next.js pod. Calls the backend over the cluster network only, so it carries no extra IAM roles today."
}

resource "google_project_iam_member" "backend_cloud_sql_client" {
  count   = var.enable_cloud_sql ? 1 : 0
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.backend.email}"
}

# Bucket-scoped, not roles/storage.objectAdmin at the project level: the
# backend can read and write objects in the documents bucket and nowhere
# else in the project.
resource "google_storage_bucket_iam_member" "backend_bucket_object_admin" {
  bucket = var.bucket_name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.backend.email}"
}

# Project-scoped: this Terraform layer creates no Secret Manager secret
# resources (secret VALUES are added out-of-band with gcloud, see
# docs/deployment-gcp.md), so there is no per-secret resource to attach a
# narrower binding to yet. Revisit with a
# google_secret_manager_secret_iam_member per secret once the secret IDs
# exist and are imported here.
# roles/modelarmor.user is the calling role: it allows sanitizeUserPrompt and
# sanitizeModelResponse against an existing template and nothing else.
# Administering templates is roles/modelarmor.admin, which no runtime identity
# here holds - Terraform creates the template, the pod only calls it.
resource "google_project_iam_member" "backend_model_armor_user" {
  count   = var.enable_model_armor ? 1 : 0
  project = var.project_id
  role    = "roles/modelarmor.user"
  member  = "serviceAccount:${google_service_account.backend.email}"
}

resource "google_project_iam_member" "backend_secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.backend.email}"
}

# -----------------------------------------------------------------------
# Workload Identity Federation for GitHub Actions (keyless CI, ADR-12)
# -----------------------------------------------------------------------

resource "google_iam_workload_identity_pool" "github" {
  project                   = var.project_id
  workload_identity_pool_id = "github-actions"
  display_name              = "GitHub Actions"
  description               = "Federates GitHub Actions OIDC tokens for keyless CI deploys."
}

# MANDATORY attribute_condition: without it, an OIDC token from ANY GitHub
# repository (not just var.github_repository) that satisfies this pool's
# audience check would qualify to impersonate the deploy service account
# below. Pinning to the exact repository is what makes creating this pool
# safe; do not remove or loosen this condition.
resource "google_iam_workload_identity_pool_provider" "github" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-actions"
  display_name                       = "GitHub Actions OIDC"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
  }

  # Pinned by repository name to match var.github_repository. Google's own
  # guidance prefers the numeric attribute.repository_id over
  # attribute.repository, because a renamed or deleted repository's name
  # can later be reclaimed by someone else; revisit if that risk matters
  # more than the readability of a name-based condition here.
  attribute_condition = "assertion.repository == \"${var.github_repository}\""

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "deploy" {
  project      = var.project_id
  account_id   = "agentcare-deploy"
  display_name = "AgentCare GitHub Actions deploy"
  description  = "Assumed by GitHub Actions through Workload Identity Federation to push images and roll out the GKE deployment. No long-lived key is ever created for it."
}

resource "google_service_account_iam_member" "deploy_workload_identity_user" {
  service_account_id = google_service_account.deploy.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/projects/${data.google_project.current.number}/locations/global/workloadIdentityPools/${google_iam_workload_identity_pool.github.workload_identity_pool_id}/attribute.repository/${var.github_repository}"
}

resource "google_project_iam_member" "deploy_artifact_registry_writer" {
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${google_service_account.deploy.email}"
}

resource "google_project_iam_member" "deploy_container_developer" {
  project = var.project_id
  role    = "roles/container.developer"
  member  = "serviceAccount:${google_service_account.deploy.email}"
}
