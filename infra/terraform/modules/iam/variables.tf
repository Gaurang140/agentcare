variable "project_id" {
  description = "GCP project ID."
  type        = string
}

variable "region" {
  description = "Region containing the Artifact Registry repository."
  type        = string
}

variable "artifact_registry_repository_id" {
  description = "Repository the GKE node identity can pull images from."
  type        = string
}

variable "bucket_name" {
  description = "Documents GCS bucket name. The backend service account gets objectCreator scoped to exactly this bucket, never a project-wide storage role."
  type        = string
}

variable "enable_model_armor" {
  description = "Grant the backend service account roles/modelarmor.user. Mirrors the root enable_model_armor flag; without the template there is nothing to call."
  type        = bool
  default     = true
}

variable "enable_vertex_ai" {
  description = "Grant the backend service account roles/aiplatform.user for the optional Vertex profile."
  type        = bool
  default     = false
}
