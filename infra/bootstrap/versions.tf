terraform {
  required_version = ">= 1.7.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.41"
    }
  }

  # This small state is the unavoidable trust bootstrap for the bucket that
  # holds the main stack's remote state. Keep it local, encrypted and ignored.
  backend "local" {}
}

provider "google" {
  project = var.project_id
}
