output "backend_service_account_email" {
  description = "Runtime SA the backend Kubernetes service account binds to via Workload Identity."
  value       = google_service_account.backend.email
}

output "gke_node_service_account_email" {
  description = "Dedicated node identity used by the GKE Autopilot cluster."
  value       = google_service_account.gke_nodes.email
}
