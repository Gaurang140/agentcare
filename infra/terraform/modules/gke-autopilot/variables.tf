variable "project_id" {
  description = "GCP project ID."
  type        = string
}

variable "region" {
  description = "Region for the regional Autopilot cluster."
  type        = string
}

variable "network_name" {
  description = "VPC network for the cluster."
  type        = string
}

variable "subnetwork_name" {
  description = "Regional subnetwork for the cluster."
  type        = string
}

variable "cluster_name" {
  description = "GKE cluster name."
  type        = string
  default     = "agentcare"
}
