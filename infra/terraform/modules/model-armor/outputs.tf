output "template_name" {
  description = "Full resource name, projects/PROJECT/locations/LOCATION/templates/ID; null while enable_model_armor is false. This is the value MODEL_ARMOR_TEMPLATE takes in the k8s gcp overlay."
  value       = var.enable_model_armor ? google_model_armor_template.this[0].name : null
}

output "template_id" {
  description = "Template ID, the last segment of the resource name; null while enable_model_armor is false."
  value       = var.enable_model_armor ? google_model_armor_template.this[0].template_id : null
}

output "endpoint_address" {
  description = "Private endpoint address; null while Model Armor is disabled."
  value       = var.enable_model_armor ? google_compute_address.endpoint[0].address : null
}

output "endpoint_hostname" {
  description = "Regional hostname resolved to the private endpoint."
  value       = var.enable_model_armor ? local.target_google_api : null
}
