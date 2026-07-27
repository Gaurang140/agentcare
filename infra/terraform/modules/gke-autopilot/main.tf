# GKE Autopilot, not Standard: no node pools to size or patch, Google
# manages the nodes and bills per pod resource request instead of per VM.
resource "google_container_cluster" "this" {
  project    = var.project_id
  name       = var.cluster_name
  location   = var.region
  network    = var.network_name
  subnetwork = var.subnetwork_name

  enable_autopilot = true

  release_channel {
    channel = "REGULAR"
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  cluster_autoscaling {
    auto_provisioning_defaults {
      service_account = var.node_service_account_email
    }
  }

  # Hackathon/demo cluster: allow `tofu destroy` to actually tear it down.
  # Flip to true before this cluster ever holds anything that must survive
  # an accidental destroy.
  deletion_protection = false
}
