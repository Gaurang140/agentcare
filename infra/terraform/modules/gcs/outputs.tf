output "bucket_name" {
  description = "Bucket name, for GCS_BUCKET."
  value       = google_storage_bucket.this.name
}

output "bucket_url" {
  description = "gs:// URL of the bucket."
  value       = google_storage_bucket.this.url
}
