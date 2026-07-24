# Root module: wires the five infra pieces for one GCP project. No secret
# VALUE is ever created here - the iam module grants access to Secret
# Manager, but the secrets themselves are created out-of-band with gcloud
# (see docs/deployment-gcp.md), so no credential ever passes through
# Terraform state.

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  documents_bucket_name = "${var.project_id}-agentcare-documents"
}

module "artifact_registry" {
  source = "./modules/artifact-registry"

  project_id = var.project_id
  region     = var.region
}

module "gcs" {
  source = "./modules/gcs"

  project_id  = var.project_id
  location    = var.gcs_location
  bucket_name = local.documents_bucket_name
}

module "iam" {
  source = "./modules/iam"

  project_id        = var.project_id
  github_repository = var.github_repository
  bucket_name       = module.gcs.bucket_name
  enable_cloud_sql  = var.enable_cloud_sql
}

module "gke" {
  source = "./modules/gke-autopilot"

  project_id = var.project_id
  region     = var.region
}

module "cloud_sql" {
  source = "./modules/cloud-sql"

  project_id       = var.project_id
  region           = var.region
  enable_cloud_sql = var.enable_cloud_sql
}
