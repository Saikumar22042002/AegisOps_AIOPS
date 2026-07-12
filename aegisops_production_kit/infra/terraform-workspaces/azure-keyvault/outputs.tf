output "vault_id" {
  value = azurerm_key_vault.this.id
}

output "vault_uri" {
  value = azurerm_key_vault.this.vault_uri
}

output "resource_group" {
  value = azurerm_resource_group.this.name
}

output "purge_protection" {
  value = azurerm_key_vault.this.purge_protection_enabled
}

output "soft_delete_days" {
  value = azurerm_key_vault.this.soft_delete_retention_days
}

output "key_names" {
  value = [for k in azurerm_key_vault_key.this : k.name]
}
