output "bucket_name" {
  description = "Bucket name, for GCS_BUCKET."
  value       = google_storage_bucket.this.name
}
