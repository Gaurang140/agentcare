variable "project_id" {
  description = "GCP project ID."
  type        = string
}

variable "region" {
  description = "Region for the Docker repository."
  type        = string
}

variable "repository_id" {
  description = "Artifact Registry repository ID; also the path segment in the image URL."
  type        = string
  default     = "agentcare"
}

variable "keep_count" {
  description = "Number of most-recent image versions the cleanup policy keeps per package; everything else is deleted by the paired DELETE policy."
  type        = number
  default     = 10
}
