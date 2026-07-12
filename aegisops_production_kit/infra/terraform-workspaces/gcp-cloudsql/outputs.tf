output "connection_name" { value = google_sql_database_instance.this.connection_name }
output "endpoint" { value = google_sql_database_instance.this.public_ip_address }
output "public_ip" { value = google_sql_database_instance.this.public_ip_address }
output "private_ip" { value = google_sql_database_instance.this.private_ip_address }
output "admin_username" { value = "postgres" }
output "backup_enabled" { value = var.backup_enabled }
output "encryption_key_name" { value = var.encryption_key_name }
output "root_password" {
  value     = random_password.root.result
  sensitive = true
}
