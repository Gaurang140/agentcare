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

variable "network_name" {
  description = "VPC network shared by GKE, Cloud SQL private access and the Model Armor private endpoint."
  type        = string
  default     = "default"
}

variable "subnetwork_name" {
  description = "Regional subnetwork shared by GKE and the Model Armor private endpoint."
  type        = string
  default     = "default"
}

variable "enable_cloud_sql" {
  description = "Provision the canonical private-IP Cloud SQL for PostgreSQL instance. Set to false only for infrastructure validation that intentionally omits the database."
  type        = bool
  default     = true
}

variable "enable_model_armor" {
  description = "Create the Model Armor template, regional endpoint, private DNS and runtime IAM. Set to false to run deterministic safety layers without provider screening."
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
