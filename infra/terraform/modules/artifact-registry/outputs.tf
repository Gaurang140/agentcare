output "repository_id" {
  description = "Artifact Registry repository ID."
  value       = google_artifact_registry_repository.this.repository_id
}

output "repository_url" {
  description = "Full image path prefix for `docker push`/`docker pull`, e.g. europe-west3-docker.pkg.dev/PROJECT/agentcare."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.this.repository_id}"
}
