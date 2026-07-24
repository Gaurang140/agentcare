output "backend_service_account_email" {
  description = "Runtime SA the backend Kubernetes service account binds to via Workload Identity."
  value       = google_service_account.backend.email
}

output "frontend_service_account_email" {
  description = "Runtime SA the frontend Kubernetes service account binds to via Workload Identity."
  value       = google_service_account.frontend.email
}

output "deploy_service_account_email" {
  description = "SA GitHub Actions impersonates; set as `service_account` in google-github-actions/auth."
  value       = google_service_account.deploy.email
}

output "workload_identity_provider" {
  description = "Full provider resource name; set as `workload_identity_provider` in google-github-actions/auth."
  value       = google_iam_workload_identity_pool_provider.github.name
}
