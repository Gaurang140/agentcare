output "backend_service_account_email" {
  description = "Runtime SA the backend Kubernetes service account binds to via Workload Identity."
  value       = google_service_account.backend.email
}
