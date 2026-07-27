# Root module: wires the GCP resources. Secret values stay outside Terraform
# state and are supplied to the one Kubernetes Secret used by the workloads.

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

module "model_armor" {
  source = "./modules/model-armor"

  project_id         = var.project_id
  location           = var.region
  network_name       = var.network_name
  subnetwork_name    = var.subnetwork_name
  enable_model_armor = var.enable_model_armor
}

module "iam" {
  source = "./modules/iam"

  project_id         = var.project_id
  bucket_name        = module.gcs.bucket_name
  enable_model_armor = var.enable_model_armor
  enable_vertex_ai   = var.enable_vertex_ai
}

module "gke" {
  source = "./modules/gke-autopilot"

  project_id      = var.project_id
  region          = var.region
  network_name    = var.network_name
  subnetwork_name = var.subnetwork_name
}

module "cloud_sql" {
  source = "./modules/cloud-sql"

  project_id       = var.project_id
  region           = var.region
  enable_cloud_sql = var.enable_cloud_sql
  network_name     = var.network_name
}
