# Azure Database — multi-engine (org-approved template: azure/db; MODSEED MS-8).
# PostgreSQL Flexible / MySQL Flexible / SQL Server (mssql), one engine per instance,
# selected by var.engine. RG + generated admin password (sensitive output, never logged).
# BACKCOMPAT: the workspace DIR NAME is immutable (B3) and the postgresql path renders the
# EXACT pre-enhancement shape for old stored inputs (B1) — `moved` blocks migrate the old
# resource addresses to the engine-keyed ones so existing state re-plans as a no-op rename.
# B2: plan-shape options (HA, geo-backup, delegated subnet) default OLD at the schema level;
# the module's own geo-backup default is the secure one (scanners evaluate module defaults).
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

locals {
  rg_name       = var.resource_group != "" ? var.resource_group : "${var.name}-rg"
  is_pg         = var.engine == "postgresql"
  is_mysql      = var.engine == "mysql"
  is_mssql      = var.engine == "mssql"
  pg_set        = local.is_pg ? toset(["postgresql"]) : toset([])
  mysql_set     = local.is_mysql ? toset(["mysql"]) : toset([])
  mssql_set     = local.is_mssql ? toset(["mssql"]) : toset([])
  private       = var.delegated_subnet_id != ""
  port          = { postgresql = 5432, mysql = 3306, mssql = 1433 }
  mysql_version = var.engine_version != "" ? var.engine_version : "8.0.21"
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

# ── PostgreSQL Flexible Server (the pre-enhancement family; addresses migrated) ──
moved {
  from = azurerm_postgresql_flexible_server.this
  to   = azurerm_postgresql_flexible_server.this["postgresql"]
}

moved {
  from = azurerm_postgresql_flexible_server_firewall_rule.azure
  to   = azurerm_postgresql_flexible_server_firewall_rule.azure["postgresql"]
}

resource "azurerm_postgresql_flexible_server" "this" {
  for_each                      = local.pg_set
  name                          = var.name
  resource_group_name           = azurerm_resource_group.this.name
  location                      = var.location
  version                       = var.pg_version
  administrator_login           = var.admin_username
  administrator_password        = random_password.admin.result
  storage_mb                    = var.storage_mb
  sku_name                      = var.sku_name
  zone                          = "1"
  public_network_access_enabled = !local.private
  geo_redundant_backup_enabled  = var.geo_redundant_backup
  delegated_subnet_id           = local.private ? var.delegated_subnet_id : null
  private_dns_zone_id           = local.private ? var.private_dns_zone_id : null
  tags                          = { ManagedBy = "AegisOps" }

  dynamic "high_availability" {
    for_each = var.ha_enabled ? [1] : []
    content {
      mode = "ZoneRedundant"
    }
  }
}

# Allow connections from Azure-hosted services (demo default; tighten per environment).
resource "azurerm_postgresql_flexible_server_firewall_rule" "azure" {
  for_each         = local.private ? toset([]) : local.pg_set
  name             = "allow-azure"
  server_id        = azurerm_postgresql_flexible_server.this[each.value].id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}

# ── MySQL Flexible Server ──
resource "azurerm_mysql_flexible_server" "this" {
  for_each                     = local.mysql_set
  name                         = var.name
  resource_group_name          = azurerm_resource_group.this.name
  location                     = var.location
  version                      = local.mysql_version
  administrator_login          = var.admin_username
  administrator_password       = random_password.admin.result
  sku_name                     = var.sku_name
  zone                         = "1"
  geo_redundant_backup_enabled = var.geo_redundant_backup
  delegated_subnet_id          = local.private ? var.delegated_subnet_id : null
  private_dns_zone_id          = local.private ? var.private_dns_zone_id : null
  tags                         = { ManagedBy = "AegisOps" }

  dynamic "high_availability" {
    for_each = var.ha_enabled ? [1] : []
    content {
      mode = "ZoneRedundant"
    }
  }

  dynamic "storage" {
    for_each = [1]
    content {
      size_gb = max(20, floor(var.storage_mb / 1024))
    }
  }
}

resource "azurerm_mysql_flexible_server_firewall_rule" "azure" {
  for_each            = local.private ? toset([]) : local.mysql_set
  name                = "allow-azure"
  resource_group_name = azurerm_resource_group.this.name
  server_name         = azurerm_mysql_flexible_server.this[each.value].name
  start_ip_address    = "0.0.0.0"
  end_ip_address      = "0.0.0.0"
}

# ── SQL Server (mssql): logical server + one database ──
resource "azurerm_mssql_server" "this" {
  for_each                      = local.mssql_set
  name                          = var.name
  resource_group_name           = azurerm_resource_group.this.name
  location                      = var.location
  version                       = "12.0"
  administrator_login           = var.admin_username
  administrator_login_password  = random_password.admin.result
  minimum_tls_version           = "1.2"
  public_network_access_enabled = !local.private
  tags                          = { ManagedBy = "AegisOps" }

  lifecycle {
    precondition {
      condition     = !var.ha_enabled
      error_message = "HA (ZoneRedundant) applies to the postgresql/mysql flexible servers - pick one of those engines or drop ha_enabled."
    }
    precondition {
      condition     = !local.private
      error_message = "Delegated-subnet private access applies to the postgresql/mysql flexible servers; SQL Server private connectivity uses private endpoints (not in this module)."
    }
  }
}

resource "azurerm_mssql_firewall_rule" "azure" {
  for_each         = local.mssql_set
  name             = "allow-azure"
  server_id        = azurerm_mssql_server.this[each.value].id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}

resource "azurerm_mssql_database" "this" {
  for_each  = local.mssql_set
  name      = "${var.name}-db"
  server_id = azurerm_mssql_server.this[each.value].id
  sku_name  = "S0"
  tags      = { ManagedBy = "AegisOps" }
}
