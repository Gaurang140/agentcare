variable "project_id" {
  description = "GCP project ID that hosts every resource this configuration manages."
  type        = string
}

variable "region" {
  description = "Primary Google Cloud region for regional resources (Artifact Registry, GKE Autopilot, Cloud SQL)."
  type        = string
  default     = "europe-west3"
}

variable "gcs_location" {
  description = "Location for the documents bucket. Defaults to europe-west3 to match the deployment region and EU data-residency choice."
  type        = string
  default     = "europe-west3"
}

variable "enable_cloud_sql" {
  description = "Provision the private-IP Cloud SQL for PostgreSQL instance. Set to false only when validating the remaining infrastructure or supplying another database explicitly."
  type        = bool
  default     = true
}

variable "enable_model_armor" {
  description = "Create the Model Armor screening template and grant the backend service account roles/modelarmor.user. Set to false to run the deterministic safety layers without provider screening."
  type        = bool
  default     = true
}

variable "enable_vertex_ai" {
  description = "Grant the backend service account roles/aiplatform.user for the optional Vertex model profile. The Vertex AI API must also be enabled."
  type        = bool
  default     = false
}

variable "domain" {
  description = "Optional custom domain for the frontend. Not consumed by this Terraform layer - reserved for the k8s kustomize overlay (infra/k8s) that owns the Ingress and ManagedCertificate resources."
  type        = string
  default     = null
}
