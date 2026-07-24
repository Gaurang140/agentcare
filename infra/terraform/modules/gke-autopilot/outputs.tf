output "cluster_name" {
  description = "GKE cluster name, for `gcloud container clusters get-credentials`."
  value       = google_container_cluster.this.name
}

output "cluster_location" {
  description = "GKE cluster region."
  value       = google_container_cluster.this.location
}

output "cluster_endpoint" {
  description = "API server endpoint. Marked sensitive so it does not print by default; it is meaningless without matching cluster credentials."
  value       = google_container_cluster.this.endpoint
  sensitive   = true
}
