output "cluster_name" {
  description = "GKE cluster name, for `gcloud container clusters get-credentials`."
  value       = google_container_cluster.this.name
}

output "cluster_location" {
  description = "GKE cluster region."
  value       = google_container_cluster.this.location
}
