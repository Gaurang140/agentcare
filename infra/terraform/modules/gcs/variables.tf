variable "project_id" {
  description = "GCP project ID."
  type        = string
}

variable "bucket_name" {
  description = "Globally unique bucket name."
  type        = string
}

variable "location" {
  description = "Bucket location (e.g. \"US\", \"EU\", or a single region)."
  type        = string
}

variable "abort_incomplete_upload_days" {
  description = "Days an incomplete multipart upload is left before it is aborted and its parts deleted."
  type        = number
  default     = 7
}
