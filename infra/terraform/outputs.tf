output "artifact_registry_repository_url" {
  description = "Image path prefix for `docker push`/`docker pull`."
  value       = module.artifact_registry.repository_url
}

output "documents_bucket_name" {
  description = "GCS bucket the backend's GCSStorage adapter writes to when STORAGE_BACKEND=gcs."
  value       = module.gcs.bucket_name
}

output "backend_service_account_email" {
  description = "Runtime SA the backend Deployment's Kubernetes service account binds to via Workload Identity."
  value       = module.iam.backend_service_account_email
}

output "frontend_service_account_email" {
  description = "Runtime SA the frontend Deployment's Kubernetes service account binds to via Workload Identity."
  value       = module.iam.frontend_service_account_email
}

output "deploy_service_account_email" {
  description = "SA GitHub Actions impersonates; set as `service_account` in google-github-actions/auth."
  value       = module.iam.deploy_service_account_email
}

output "workload_identity_provider" {
  description = "Full provider resource name; set as `workload_identity_provider` in google-github-actions/auth."
  value       = module.iam.workload_identity_provider
}

output "gke_cluster_name" {
  description = "GKE Autopilot cluster name, for `gcloud container clusters get-credentials`."
  value       = module.gke.cluster_name
}

output "gke_cluster_location" {
  description = "GKE Autopilot cluster region."
  value       = module.gke.cluster_location
}

output "model_armor_template_name" {
  description = "Full Model Armor template resource name; set as MODEL_ARMOR_TEMPLATE in the k8s gcp overlay. Null while enable_model_armor is false."
  value       = module.model_armor.template_name
}

output "cloud_sql_connection_name" {
  description = "Cloud SQL connection name for the Cloud SQL Auth Proxy; null while enable_cloud_sql is false."
  value       = module.cloud_sql.connection_name
}
