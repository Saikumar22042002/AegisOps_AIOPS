# MODSEED MS-8 B1/B2 gate — native `terraform test` with mocked providers (offline, no creds).
# Old-shape stored inputs (every new field explicit at its B2 schema default) must render the
# EXACT pre-enhancement plan: RG + generated password + ONE postgres flexible server + the
# allow-azure firewall rule — no mysql/mssql resources, no HA block, no private access.

mock_provider "azurerm" {}
mock_provider "random" {}

variables {
  name                 = "b1gate"
  engine               = "postgresql"
  location             = "eastus"
  admin_username       = "pgadmin"
  sku_name             = "B_Standard_B1ms"
  storage_mb           = 32768
  pg_version           = "15"
  resource_group       = ""
  engine_version       = ""
  ha_enabled           = false
  geo_redundant_backup = false
  delegated_subnet_id  = ""
  private_dns_zone_id  = ""
}

run "b1_old_shape_renders_exactly_the_old_plan" {
  command = plan

  assert {
    condition     = length(azurerm_postgresql_flexible_server.this) == 1
    error_message = "old inputs must render exactly one postgres flexible server"
  }
  assert {
    condition     = length(azurerm_postgresql_flexible_server_firewall_rule.azure) == 1
    error_message = "old inputs must keep the allow-azure firewall rule"
  }
  assert {
    condition     = length(azurerm_mysql_flexible_server.this) == 0 && length(azurerm_mssql_server.this) == 0 && length(azurerm_mssql_database.this) == 0
    error_message = "no other engine family may render for old inputs"
  }
  assert {
    condition     = azurerm_postgresql_flexible_server.this["postgresql"].version == "15"
    error_message = "pg_version must flow through unchanged"
  }
  assert {
    condition     = azurerm_postgresql_flexible_server.this["postgresql"].storage_mb == 32768
    error_message = "storage_mb must flow through unchanged"
  }
  assert {
    condition     = azurerm_postgresql_flexible_server.this["postgresql"].sku_name == "B_Standard_B1ms"
    error_message = "sku_name must flow through unchanged"
  }
  assert {
    condition     = azurerm_postgresql_flexible_server.this["postgresql"].zone == "1"
    error_message = "zone pin must stay"
  }
  assert {
    condition     = azurerm_postgresql_flexible_server.this["postgresql"].public_network_access_enabled == true
    error_message = "old inputs must keep the public access path (B1)"
  }
  assert {
    condition     = azurerm_postgresql_flexible_server.this["postgresql"].geo_redundant_backup_enabled == false
    error_message = "B2: the schema-explicit false must render false (old provider default)"
  }
  assert {
    condition     = azurerm_postgresql_flexible_server.this["postgresql"].delegated_subnet_id == null
    error_message = "old inputs must not set a delegated subnet"
  }
  assert {
    condition     = length(azurerm_postgresql_flexible_server.this["postgresql"].high_availability) == 0
    error_message = "old inputs must not render an HA block"
  }
}

run "mysql_engine_renders_only_the_mysql_family" {
  command = plan

  variables {
    engine = "mysql"
  }

  assert {
    condition     = length(azurerm_mysql_flexible_server.this) == 1 && length(azurerm_postgresql_flexible_server.this) == 0 && length(azurerm_mssql_server.this) == 0
    error_message = "engine=mysql must render exactly the mysql family"
  }
  assert {
    condition     = azurerm_mysql_flexible_server.this["mysql"].version == "8.0.21"
    error_message = "mysql default version must be 8.0.21"
  }
  assert {
    condition     = one(azurerm_mysql_flexible_server.this["mysql"].storage[*].size_gb) == 32
    error_message = "storage_mb must map to size_gb (32768 MB -> 32 GB)"
  }
}

run "mssql_engine_renders_server_plus_database" {
  command = plan

  variables {
    engine = "mssql"
  }

  assert {
    condition     = length(azurerm_mssql_server.this) == 1 && length(azurerm_mssql_database.this) == 1 && length(azurerm_postgresql_flexible_server.this) == 0
    error_message = "engine=mssql must render the logical server + one database"
  }
  assert {
    condition     = azurerm_mssql_server.this["mssql"].minimum_tls_version == "1.2"
    error_message = "mssql must enforce TLS 1.2"
  }
}

run "ha_renders_zone_redundant_for_postgres" {
  command = plan

  variables {
    ha_enabled = true
  }

  assert {
    condition     = one(azurerm_postgresql_flexible_server.this["postgresql"].high_availability[*].mode) == "ZoneRedundant"
    error_message = "ha_enabled must render the ZoneRedundant HA block"
  }
}

run "private_access_swaps_public_for_delegated_subnet" {
  command = plan

  variables {
    delegated_subnet_id = "/subscriptions/x/resourceGroups/r/providers/Microsoft.Network/virtualNetworks/v/subnets/s"
    private_dns_zone_id = "/subscriptions/x/resourceGroups/r/providers/Microsoft.Network/privateDnsZones/z"
  }

  assert {
    condition     = azurerm_postgresql_flexible_server.this["postgresql"].public_network_access_enabled == false
    error_message = "a delegated subnet must switch off public access"
  }
  assert {
    condition     = length(azurerm_postgresql_flexible_server_firewall_rule.azure) == 0
    error_message = "the allow-azure firewall rule must vanish on the private path"
  }
}

run "mssql_rejects_ha" {
  command = plan

  variables {
    engine     = "mssql"
    ha_enabled = true
  }

  expect_failures = [azurerm_mssql_server.this]
}
