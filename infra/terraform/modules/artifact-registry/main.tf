# Docker repository for the backend and frontend images.
#
# Immutable tags make a release tag permanent: once the pipeline pushes the
# full 40-char commit SHA as a tag, that tag can never be moved to a different
# version, removed or overwritten. The SHA tag therefore identifies one exact
# image the way a digest does, and the image CI built and scanned is provably
# the image the cluster pulls later. Only new tags may still be created.
#
# That guarantee conflicts with a blanket DELETE cleanup policy, and the
# conflict is silent rather than loud. Artifact Registry refuses to delete a
# tagged version while immutability is on, so the previous
# `tag_state = "ANY"` DELETE policy would have kept reporting itself as
# configured while collecting nothing except untagged leftovers. The policy is
# scoped to UNTAGGED to say out loud what actually happens: superseded
# manifests and build-cache layers are swept, released SHA tags are kept.
# Reclaiming space from released images is a deliberate operator action, not
# something a background policy does to the release history.
resource "google_artifact_registry_repository" "this" {
  project       = var.project_id
  location      = var.region
  repository_id = var.repository_id
  format        = "DOCKER"
  description   = "AgentCare backend and frontend container images."

  docker_config {
    immutable_tags = true
  }

  # A KEEP policy deletes nothing by itself; it shields the most recent
  # versions of each package from the DELETE policy below, including recent
  # untagged ones that an in-flight rollback may still be pulling.
  cleanup_policies {
    id     = "keep-last-${var.keep_count}"
    action = "KEEP"

    most_recent_versions {
      keep_count = var.keep_count
    }
  }

  cleanup_policies {
    id     = "delete-untagged"
    action = "DELETE"

    condition {
      tag_state = "UNTAGGED"
    }
  }
}
