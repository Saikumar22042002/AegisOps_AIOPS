# Azure Database for PostgreSQL — Flexible Server (org-approved template: azure/postgres).
# RG + server with a generated admin password (surfaced as a sensitive output, never logged).
terraform {
  required_version = ">= 1.6"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 3.110" }
    random  = { source = "hashicorp/random", version = "~> 3.6" }
  }
  backend "local" {}
}

provider "azurerm" {
  features {}
  skip_provider_registration = true
}

variable "name" { type = string }
variable "location" {
  type    = string
  default = "eastus"
}
variable "admin_username" {
  type    = string
  default = "pgadmin"
}
variable "sku_name" {
  type    = string
  default = "B_Standard_B1ms"
}
variable "storage_mb" {
  type    = number
  default = 32768
}
variable "pg_version" {
  type    = string
  default = "15"
}
variable "resource_group" {
  type    = string
  default = ""
}

locals {
  rg_name = var.resource_group != "" ? var.resource_group : "${var.name}-rg"
}

resource "random_password" "admin" {
  length           = 24
  special          = true
  override_special = "!#$%*-_=+"
}

resource "azurerm_resource_group" "this" {
  name     = local.rg_name
  location = var.location
  tags     = { ManagedBy = "AegisOps" }
}

resource "azurerm_postgresql_flexible_server" "this" {
  name                          = var.name
  resource_group_name           = azurerm_resource_group.this.name
  location                      = var.location
  version                       = var.pg_version
  administrator_login           = var.admin_username
  administrator_password        = random_password.admin.result
  storage_mb                    = var.storage_mb
  sku_name                      = var.sku_name
  zone                          = "1"
  public_network_access_enabled = true
  tags                          = { ManagedBy = "AegisOps" }
}

# Allow connections from Azure-hosted services (demo default; tighten per environment).
resource "azurerm_postgresql_flexible_server_firewall_rule" "azure" {
  name             = "allow-azure"
  server_id        = azurerm_postgresql_flexible_server.this.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}

output "endpoint" { value = azurerm_postgresql_flexible_server.this.fqdn }
output "fqdn" { value = azurerm_postgresql_flexible_server.this.fqdn }
output "admin_username" { value = var.admin_username }
output "resource_group" { value = azurerm_resource_group.this.name }
output "admin_password" {
  value     = random_password.admin.result
  sensitive = true
}
