variable "project_id" {
  description = "GCP project ID."
  type        = string
}

variable "region" {
  description = "Region for the regional Autopilot cluster."
  type        = string
}

variable "cluster_name" {
  description = "GKE cluster name."
  type        = string
  default     = "agentcare"
}
