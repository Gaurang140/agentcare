variable "project_id" {
  description = "GCP project ID."
  type        = string
}

variable "region" {
  description = "Region for the instance."
  type        = string
}

variable "enable_cloud_sql" {
  description = "Create the instance and its private-IP networking."
  type        = bool
  default     = false
}

variable "network_name" {
  description = "VPC network name to peer for Private Service Access. \"default\" assumes the project's auto-created default network exists; pass a custom network if an org policy disables default-network creation."
  type        = string
  default     = "default"
}
