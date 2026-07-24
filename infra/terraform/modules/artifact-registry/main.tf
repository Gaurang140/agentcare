# Docker repository for the backend and frontend images.
#
# A KEEP policy alone protects the most recent versions from a matching
# DELETE policy; it does not delete anything by itself. The paired DELETE
# policy below targets every version (tagged or untagged), so in practice
# only the most recent `var.keep_count` versions of each package survive.
resource "google_artifact_registry_repository" "this" {
  project       = var.project_id
  location      = var.region
  repository_id = var.repository_id
  format        = "DOCKER"
  description   = "AgentCare backend and frontend container images."

  cleanup_policies {
    id     = "keep-last-${var.keep_count}"
    action = "KEEP"

    most_recent_versions {
      keep_count = var.keep_count
    }
  }

  cleanup_policies {
    id     = "delete-outside-keep-window"
    action = "DELETE"

    condition {
      tag_state = "ANY"
    }
  }
}
