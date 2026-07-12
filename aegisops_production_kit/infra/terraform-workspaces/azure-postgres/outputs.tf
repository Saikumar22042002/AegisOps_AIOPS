locals {
  fqdn = coalesce(
    one(values(azurerm_postgresql_flexible_server.this)[*].fqdn),
    one(values(azurerm_mysql_flexible_server.this)[*].fqdn),
    one(values(azurerm_mssql_server.this)[*].fully_qualified_domain_name),
  )
}

output "endpoint" { value = local.fqdn }
output "fqdn" { value = local.fqdn }
output "engine" { value = var.engine }
output "port" { value = local.port[var.engine] }
output "admin_username" { value = var.admin_username }
output "resource_group" { value = azurerm_resource_group.this.name }
output "admin_password" {
  value     = random_password.admin.result
  sensitive = true
}
