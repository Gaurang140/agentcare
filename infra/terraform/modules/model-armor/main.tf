# Model Armor template: the screening policy the backend names on every call.
#
# It fills the injection guard's existing layer-2 slot on the GCP path and
# screens the drafted answer just before the deterministic sanitizer
# (backend/app/safety/model_armor.py). The deterministic layers keep deciding
# first and last, so this template is a second opinion, never the only one.
#
# Enabling the API is an operator prerequisite documented in
# docs/deployment-gcp.md; this configuration does not enable project APIs.
locals {
  target_google_api = "modelarmor.${var.location}.rep.googleapis.com"
}

data "google_compute_network" "endpoint" {
  count   = var.enable_model_armor ? 1 : 0
  project = var.project_id
  name    = var.network_name
}

data "google_compute_subnetwork" "endpoint" {
  count   = var.enable_model_armor ? 1 : 0
  project = var.project_id
  name    = var.subnetwork_name
  region  = var.location
}

resource "google_model_armor_template" "this" {
  count       = var.enable_model_armor ? 1 : 0
  project     = var.project_id
  location    = var.location
  template_id = var.template_id

  # Off by Google's default, and this clinic screens German patient text, so
  # it has to be on for the EN/DE claim to hold (the RAI and injection
  # filters are tested on German among their nine languages).
  template_metadata {
    multi_language_detection {
      enable_multi_language_detection = true
    }
  }

  filter_config {
    # The one filter this app actually wants from the service: prompt
    # injection and jailbreak detection by model, which is what layer 2 is
    # for. HIGH confidence on purpose (see the variable's description).
    pi_and_jailbreak_filter_settings {
      filter_enforcement = "ENABLED"
      confidence_level   = var.confidence_level
    }

    # Additive: AgentCare screens no URLs anywhere today, so a malicious link
    # pasted into a request or sitting in an uploaded document has nothing
    # else looking at it.
    malicious_uri_filter_settings {
      filter_enforcement = "ENABLED"
    }

    # sdp_settings is deliberately absent: Presidio detects and redacts PII
    # in-process before any text leaves the backend, and a second PII detector
    # in the cloud would duplicate that job while making it depend on a network
    # call. Do not add it.
  }
}

resource "google_compute_address" "endpoint" {
  count        = var.enable_model_armor ? 1 : 0
  project      = var.project_id
  name         = "agentcare-model-armor-psc"
  region       = var.location
  address_type = "INTERNAL"
  purpose      = "GCE_ENDPOINT"
  subnetwork   = data.google_compute_subnetwork.endpoint[0].id
}

resource "google_network_connectivity_regional_endpoint" "this" {
  count             = var.enable_model_armor ? 1 : 0
  project           = var.project_id
  name              = "agentcare-model-armor"
  location          = var.location
  target_google_api = local.target_google_api
  access_type       = "REGIONAL"
  address           = google_compute_address.endpoint[0].id
  network           = data.google_compute_network.endpoint[0].id
  subnetwork        = data.google_compute_subnetwork.endpoint[0].id
}

resource "google_dns_managed_zone" "this" {
  count       = var.enable_model_armor ? 1 : 0
  project     = var.project_id
  name        = "agentcare-model-armor-rep"
  dns_name    = "${local.target_google_api}."
  description = "Private resolution for the AgentCare Model Armor regional endpoint"
  visibility  = "private"

  private_visibility_config {
    networks {
      network_url = data.google_compute_network.endpoint[0].id
    }
  }
}

resource "google_dns_record_set" "this" {
  count        = var.enable_model_armor ? 1 : 0
  project      = var.project_id
  managed_zone = google_dns_managed_zone.this[0].name
  name         = "${local.target_google_api}."
  type         = "A"
  ttl          = 300
  rrdatas      = [google_compute_address.endpoint[0].address]

  depends_on = [google_network_connectivity_regional_endpoint.this]
}
