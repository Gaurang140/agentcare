output "terraform_state_bucket" {
  description = "Set as TF_STATE_BUCKET when initializing the main stack."
  value       = google_storage_bucket.terraform_state.name
}

output "workload_identity_provider" {
  description = "Set as the GitHub production variable GCP_WORKLOAD_IDENTITY_PROVIDER."
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "deployer_service_account_email" {
  description = "Set as the GitHub production variable GCP_DEPLOYER_SERVICE_ACCOUNT."
  value       = google_service_account.github_deployer.email
}

output "infra_service_account_email" {
  description = "Set as the GitHub production-infra variable GCP_INFRA_SERVICE_ACCOUNT. Keep it out of the production environment: only the infrastructure workflow may use it."
  value       = google_service_account.github_infra.email
}
