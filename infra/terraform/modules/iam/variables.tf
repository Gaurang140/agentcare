variable "project_id" {
  description = "GCP project ID."
  type        = string
}

variable "github_repository" {
  description = "GitHub \"owner/repo\" allowed to assume the deploy service account, for example \"Gaurang140/agentcare\"."
  type        = string
}

variable "bucket_name" {
  description = "Documents GCS bucket name. The backend service account gets objectAdmin scoped to exactly this bucket, never a project-wide storage role."
  type        = string
}

variable "enable_cloud_sql" {
  description = "Grant the backend service account roles/cloudsql.client. Mirrors the root enable_cloud_sql flag; the demo path (Neon) needs no GCP database role at all."
  type        = bool
  default     = false
}

variable "enable_model_armor" {
  description = "Grant the backend service account roles/modelarmor.user. Mirrors the root enable_model_armor flag; without the template there is nothing to call."
  type        = bool
  default     = true
}
