# Model Armor template: the screening policy the backend names on every call.
#
# It fills the injection guard's existing layer-2 slot on the GCP path and
# screens the drafted answer just before the deterministic sanitizer
# (backend/app/safety/model_armor.py). The deterministic layers keep deciding
# first and last, so this template is a second opinion, never the only one.
#
# Enabling the API is not managed here: this configuration enables no APIs at
# all, they are a one-time `gcloud services enable` per project (Step 2 of
# docs/deployment-gcp.md, which now lists modelarmor.googleapis.com).
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
