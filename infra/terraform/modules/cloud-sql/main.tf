# Primary database path while the GCP trial credit covers it (docs/decisions.md
# ADR-03); enable_cloud_sql defaults to true in the root variables. Neon
# free-tier Postgres is the post-credit swap: set enable_cloud_sql=false and
# point DATABASE_URL there. Every resource below is count-gated on that flag.

data "google_compute_network" "vpc" {
  count   = var.enable_cloud_sql ? 1 : 0
  project = var.project_id
  name    = var.network_name
}

# Cloud SQL with no public IP still needs a private IP range peered to
# Google's managed services - this is the one-time networking "no public
# IP" depends on, not optional plumbing.
resource "google_compute_global_address" "private_ip_alloc" {
  count         = var.enable_cloud_sql ? 1 : 0
  project       = var.project_id
  name          = "agentcare-sql-private-ip"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = data.google_compute_network.vpc[0].id
}

resource "google_service_networking_connection" "private_vpc_connection" {
  count                   = var.enable_cloud_sql ? 1 : 0
  network                 = data.google_compute_network.vpc[0].id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_ip_alloc[0].name]
}

resource "google_sql_database_instance" "postgres" {
  count            = var.enable_cloud_sql ? 1 : 0
  project          = var.project_id
  name             = "agentcare-postgres"
  region           = var.region
  database_version = "POSTGRES_17"

  settings {
    # Smallest tier available. ENTERPRISE (not the newer default
    # ENTERPRISE_PLUS) is required for shared-core machine types like
    # db-f1-micro. About $8/mo, dev-only, no SLA (docs/decisions.md ADR-03).
    tier    = "db-f1-micro"
    edition = "ENTERPRISE"

    ip_configuration {
      ipv4_enabled    = false
      private_network = data.google_compute_network.vpc[0].id
    }
  }

  depends_on = [google_service_networking_connection.private_vpc_connection]

  # Hackathon/demo path: allow `tofu destroy` to actually tear it down.
  deletion_protection = false
}
