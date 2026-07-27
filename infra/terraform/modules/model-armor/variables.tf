variable "project_id" {
  description = "GCP project ID."
  type        = string
}

variable "location" {
  description = "Region the template lives in. Model Armor is regional and has no global endpoint, so this must match MODEL_ARMOR_LOCATION in the backend's config: the client builds its endpoint as modelarmor.LOCATION.rep.googleapis.com (backend/app/safety/model_armor.py)."
  type        = string
}

variable "enable_model_armor" {
  description = "Create the screening template. Mirrors the root enable_model_armor flag; when false the backend runs its deterministic layers without provider screening."
  type        = bool
  default     = true
}

variable "template_id" {
  description = "Template ID; the last segment of projects/PROJECT/locations/LOCATION/templates/ID."
  type        = string
  default     = "agentcare"
}

variable "confidence_level" {
  description = "Detection confidence the prompt-injection and jailbreak filter enforces at. HIGH is Google's own recommendation for minimizing false positives, and a false positive here blocks a real patient from booking an appointment."
  type        = string
  default     = "HIGH"
}
