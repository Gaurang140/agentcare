# Runtime service account for the backend. The frontend calls the backend over
# the cluster network and needs no Google service account of its own.
resource "google_service_account" "backend" {
  project      = var.project_id
  account_id   = "agentcare-backend"
  display_name = "AgentCare backend runtime"
  description  = "Identity for the FastAPI/LangGraph backend pod. Terraform binds the agentcare-backend Kubernetes service account to it without downloading a key."
}

# Autopilot still needs a node identity for system workloads and image pulls.
# Use a dedicated account instead of relying on the project's default Compute
# Engine account or legacy automatic Editor grants.
resource "google_service_account" "gke_nodes" {
  project      = var.project_id
  account_id   = "agentcare-gke-nodes"
  display_name = "AgentCare GKE node runtime"
  description  = "Node identity for the AgentCare GKE Autopilot cluster."
}

resource "google_project_iam_member" "gke_nodes_default_role" {
  project = var.project_id
  role    = "roles/container.defaultNodeServiceAccount"
  member  = "serviceAccount:${google_service_account.gke_nodes.email}"
}

resource "google_artifact_registry_repository_iam_member" "gke_nodes_reader" {
  project    = var.project_id
  location   = var.region
  repository = var.artifact_registry_repository_id
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.gke_nodes.email}"
}

# Bucket-scoped and create-only: uploads use UUID object names, and the runtime
# has no read, list, overwrite or delete operation against GCS.
resource "google_storage_bucket_iam_member" "backend_bucket_object_creator" {
  bucket = var.bucket_name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.backend.email}"
}

# The runtime can call an existing Model Armor template but cannot administer
# templates. Terraform owns template creation.
resource "google_project_iam_member" "backend_model_armor_user" {
  count   = var.enable_model_armor ? 1 : 0
  project = var.project_id
  role    = "roles/modelarmor.user"
  member  = "serviceAccount:${google_service_account.backend.email}"
}

# Vertex authorization is opt-in because Groq remains the default model
# profile. API enablement is an explicit operator step in the GCP runbook.
resource "google_project_iam_member" "backend_vertex_ai_user" {
  count   = var.enable_vertex_ai ? 1 : 0
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.backend.email}"
}

# Declarative KSA-to-GSA impersonation for the backend application workload.
# The migration Job connects directly to private-IP PostgreSQL and does not
# mount a Kubernetes service-account token.
resource "google_service_account_iam_member" "backend_workload_identity_user" {
  service_account_id = google_service_account.backend.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[default/agentcare-backend]"
}
