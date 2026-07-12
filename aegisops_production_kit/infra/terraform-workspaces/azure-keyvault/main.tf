# MODSEED MS-5 — azure.keyvault: vault (standard SKU, soft-delete, purge protection,
# network ACLs with AzureServices bypass) + current-SP access policy + optional additional
# policies and RSA-2048 keys. Secret VALUES are permanently out of scope — this module manages
# the VAULT and KEYS, never secrets. RG semantics mirror azure-vm/vnet. No backend block.

terraform {
  required_version = ">= 1.6"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 3.110" }
  }
}

provider "azurerm" {
  features {}
}

data "azurerm_client_config" "current" {}

locals {
  rg_name = var.resource_group != "" ? var.resource_group : "${var.name}-rg"
}

resource "azurerm_resource_group" "this" {
  name     = local.rg_name
  location = var.location
  tags     = { ManagedBy = "AegisOps" }
}

resource "azurerm_key_vault" "this" {
  name                       = var.name
  location                   = var.location
  resource_group_name        = azurerm_resource_group.this.name
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  soft_delete_retention_days = var.soft_delete_days
  purge_protection_enabled   = var.purge_protection
  tags                       = { ManagedBy = "AegisOps" }

  network_acls {
    bypass         = "AzureServices"
    default_action = var.network_default_action # stated on the approval card when "Allow"
  }
}

# The deploying service principal gets a working policy (keys + secrets management — the
# PLATFORM never reads or writes secret VALUES through chat; this is vault administration).
resource "azurerm_key_vault_access_policy" "current" {
  key_vault_id = azurerm_key_vault.this.id
  tenant_id    = data.azurerm_client_config.current.tenant_id
  object_id    = data.azurerm_client_config.current.object_id

  key_permissions    = ["Get", "List", "Create", "Update", "Delete"]
  secret_permissions = ["Get", "List", "Set", "Delete"]
}

resource "azurerm_key_vault_access_policy" "additional" {
  for_each     = var.additional_policies
  key_vault_id = azurerm_key_vault.this.id
  tenant_id    = data.azurerm_client_config.current.tenant_id
  object_id    = each.key

  key_permissions    = each.value.key_permissions
  secret_permissions = each.value.secret_permissions
}

resource "azurerm_key_vault_key" "this" {
  for_each     = toset(var.keys)
  name         = each.value
  key_vault_id = azurerm_key_vault.this.id
  key_type     = "RSA"
  key_size     = 2048
  key_opts     = ["decrypt", "encrypt", "sign", "verify", "wrapKey", "unwrapKey"]

  depends_on = [azurerm_key_vault_access_policy.current]
}
