variable "project_id" {
  description = "GCP project ID."
  type        = string
}

variable "bucket_name" {
  description = "Documents GCS bucket name. The backend service account gets objectAdmin scoped to exactly this bucket, never a project-wide storage role."
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
