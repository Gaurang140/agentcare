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
    Location for the documents bucket. Defaults to "europe-west3" for EU
    data residency, the deployment's actual requirement while it runs under
    the owner's GCP trial credit - see docs/decisions.md ADR-04. GCP's
    Always-Free Cloud Storage tier (5 GB-months) applies only to the US
    regions (us-east1, us-west1, us-central1); set this to "US" or
    "us-central1" to ride that permanent free tier once the trial credit is
    gone. At hackathon-demo volume the difference is a few cents a month
    either way.
  EOT
  type        = string
  default     = "europe-west3"
}

variable "github_repository" {
  description = "GitHub \"owner/repo\" allowed to assume the deploy service account through Workload Identity Federation, for example \"Gaurang140/agentcare\"."
  type        = string
}

variable "enable_cloud_sql" {
  description = "Provision a Cloud SQL for PostgreSQL instance. On by default: Cloud SQL is the primary database path while the GCP trial credit covers it (ADR-03). Set to false and point DATABASE_URL at Neon free-tier Postgres for the post-credit swap - one connection string, no code change."
  type        = bool
  default     = true
}

variable "enable_model_armor" {
  description = "Create the Model Armor screening template and grant the backend service account roles/modelarmor.user. On by default: it fills the injection guard's existing layer-2 slot on the GCP path and screens the drafted answer, both around the deterministic layers that keep deciding first and last (docs/decisions.md ADR-15). Set to false to run those deterministic layers alone, which is what every non-GCP deployment does. The API has to be enabled once per project either way, see Step 2 of docs/deployment-gcp.md."
  type        = bool
  default     = true
}

variable "domain" {
  description = "Optional custom domain for the frontend. Not consumed by this Terraform layer - reserved for the k8s kustomize overlay (infra/k8s) that owns the Ingress and ManagedCertificate resources."
  type        = string
  default     = null
}
