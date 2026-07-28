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
