# GKE Autopilot, not Standard: no node pools to size or patch, Google
# manages the nodes and bills per pod resource request instead of per VM.
data "google_compute_subnetwork" "cluster" {
  project = var.project_id
  name    = var.subnetwork_name
  region  = var.region
}

resource "google_compute_router" "egress" {
  project = var.project_id
  name    = "${var.cluster_name}-egress"
  network = var.network_name
  region  = var.region
}

resource "google_compute_router_nat" "egress" {
  project                            = var.project_id
  name                               = "${var.cluster_name}-egress"
  router                             = google_compute_router.egress.name
  region                             = google_compute_router.egress.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"

  subnetwork {
    name                    = data.google_compute_subnetwork.cluster.id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

resource "google_container_cluster" "this" {
  project    = var.project_id
  name       = var.cluster_name
  location   = var.region
  network    = var.network_name
  subnetwork = var.subnetwork_name

  enable_autopilot = true

  ip_allocation_policy {}

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
  }

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

  # Hackathon/demo cluster: allow `terraform destroy` to actually tear it down.
  # Flip to true before this cluster ever holds anything that must survive
  # an accidental destroy.
  deletion_protection = false

  depends_on = [google_compute_router_nat.egress]
}
