output "private_ip_address" {
  description = "Private IP address of the instance; null while enable_cloud_sql is false."
  value       = var.enable_cloud_sql ? google_sql_database_instance.postgres[0].private_ip_address : null
}
