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
  description = <<-EOT
    Location for the documents bucket. Defaults to the multi-region "US"
    because GCP's Always-Free Cloud Storage tier (5 GB-months) applies only
    to the US regions (us-east1, us-west1, us-central1) - see
    docs/decisions.md ADR-04. Set this to "EU" or a specific europe-west
    region when data residency outweighs the free-tier saving; at
    hackathon-demo volume the difference is a few cents a month either way.
  EOT
  type        = string
  default     = "US"
}

variable "github_repository" {
  description = "GitHub \"owner/repo\" allowed to assume the deploy service account through Workload Identity Federation, for example \"Gaurang140/agentcare\"."
  type        = string
}

variable "enable_cloud_sql" {
  description = "Provision a Cloud SQL for PostgreSQL instance. Off by default: the demo path runs on Neon free-tier Postgres (ADR-03); enable only for the enterprise-path demo."
  type        = bool
  default     = false
}

variable "domain" {
  description = "Optional custom domain for the frontend. Not consumed by this Terraform layer - reserved for the k8s kustomize overlay (infra/k8s) that owns the Ingress and ManagedCertificate resources."
  type        = string
  default     = null
}
