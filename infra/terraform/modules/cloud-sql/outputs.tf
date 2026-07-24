output "connection_name" {
  description = "Cloud SQL connection name for the Cloud SQL Auth Proxy; null while enable_cloud_sql is false."
  value       = var.enable_cloud_sql ? google_sql_database_instance.postgres[0].connection_name : null
}

output "private_ip_address" {
  description = "Private IP address of the instance; null while enable_cloud_sql is false."
  value       = var.enable_cloud_sql ? google_sql_database_instance.postgres[0].private_ip_address : null
}
