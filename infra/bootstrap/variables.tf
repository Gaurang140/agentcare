variable "project_id" {
  description = "GCP project containing AgentCare and its deployment identity."
  type        = string
}

variable "region" {
  description = "Location for the Terraform state bucket."
  type        = string
  default     = "europe-west3"
}

variable "state_bucket_name" {
  description = "Optional globally unique state bucket name. Empty derives one from project_id."
  type        = string
  default     = ""
}

variable "github_repository_id" {
  description = "Immutable numeric GitHub repository ID, not owner/name."
  type        = string

  validation {
    condition     = can(regex("^[0-9]+$", var.github_repository_id))
    error_message = "github_repository_id must contain digits only."
  }
}

variable "github_repository_owner_id" {
  description = "Immutable numeric GitHub user or organization ID."
  type        = string

  validation {
    condition     = can(regex("^[0-9]+$", var.github_repository_owner_id))
    error_message = "github_repository_owner_id must contain digits only."
  }
}

variable "deploy_branch" {
  description = "Only this branch may exchange GitHub OIDC credentials."
  type        = string
  default     = "main"

  validation {
    condition     = can(regex("^[A-Za-z0-9._/-]+$", var.deploy_branch))
    error_message = "deploy_branch contains unsupported characters."
  }
}

variable "deploy_environment" {
  description = "GitHub environment required on the application deployment job."
  type        = string
  default     = "production"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]+$", var.deploy_environment))
    error_message = "deploy_environment contains unsupported characters."
  }
}

variable "deploy_workflow_file" {
  description = "Workflow filename allowed to exchange deployment credentials."
  type        = string
  default     = "ci.yml"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]+\\.ya?ml$", var.deploy_workflow_file))
    error_message = "deploy_workflow_file must be a YAML filename."
  }
}
