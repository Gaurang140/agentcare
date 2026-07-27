# Patient document storage. No public access:
# uniform bucket-level access turns off per-object ACLs (IAM only), and
# public_access_prevention enforces that no binding can ever grant
# allUsers/allAuthenticatedUsers, even by mistake.
resource "google_storage_bucket" "this" {
  project  = var.project_id
  name     = var.bucket_name
  location = var.location

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  # Uploaded documents are never overwritten in place (the storage adapter
  # always writes a new ref), so object versioning would only add storage
  # cost with no recovery benefit.
  versioning {
    enabled = false
  }

  # A crashed or interrupted client leaves an incomplete multipart upload
  # sitting in the bucket, billed like any other object, forever, unless
  # something aborts it.
  lifecycle_rule {
    action {
      type = "AbortIncompleteMultipartUpload"
    }
    condition {
      age = var.abort_incomplete_upload_days
    }
  }
}
