output "cluster_name" {
  description = "GKE cluster name, for `gcloud container clusters get-credentials`."
  value       = google_container_cluster.this.name
}

output "cluster_location" {
  description = "GKE cluster region."
  value       = google_container_cluster.this.location
}

output "workload_pool" {
  description = "Workload Identity pool created with the GKE cluster."
  value       = google_container_cluster.this.workload_identity_config[0].workload_pool
}
