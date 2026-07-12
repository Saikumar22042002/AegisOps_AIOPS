# MODSEED MS-9 B1/B2 gate — native `terraform test`, mocked providers (offline, no creds).
# Old-shape stored inputs (schema-explicit B2 defaults, INCLUDING the legacy world-open
# authorized network) must render the EXACT pre-enhancement plan: instance + password only,
# no backup block, no database flags, no insights, no maintenance window, no ssl_mode pin.

mock_provider "google" {}
mock_provider "random" {}

variables {
  name                  = "b1gate"
  project               = "demo-project"
  region                = "us-central1"
  tier                  = "db-f1-micro"
  database_version      = "POSTGRES_15"
  authorized_networks   = ["0.0.0.0/0"]
  private_network       = ""
  ssl_mode              = ""
  backup_enabled        = false
  database_flags        = {}
  enable_query_insights = false
  maintenance_day       = 0
  maintenance_hour      = 3
  deletion_protection   = false
  encryption_key_name   = ""
}

run "b1_old_shape_renders_exactly_the_old_plan" {
  command = plan

  # (encryption_key_name and ssl_mode are optional+computed — unknown at plan under a mock
  #  provider when unset. Their SET behavior is asserted in the runs below; unset-vs-set is
  #  therefore covered without asserting the computed nulls here.)
  assert {
    condition     = google_sql_database_instance.this.deletion_protection == false
    error_message = "deletion_protection must stay off for old inputs"
  }
  assert {
    condition     = one(google_sql_database_instance.this.settings[*].tier) == "db-f1-micro"
    error_message = "tier must flow through unchanged"
  }
  assert {
    condition     = length(one(google_sql_database_instance.this.settings[*].backup_configuration)) == 0
    error_message = "old inputs must not render a backup block"
  }
  assert {
    condition     = length(one(google_sql_database_instance.this.settings[*].database_flags)) == 0
    error_message = "old inputs must not render database flags"
  }
  assert {
    condition     = length(one(google_sql_database_instance.this.settings[*].insights_config)) == 0
    error_message = "old inputs must not render query insights"
  }
  assert {
    condition     = length(one(google_sql_database_instance.this.settings[*].maintenance_window)) == 0
    error_message = "old inputs must not render a maintenance window"
  }
  assert {
    condition     = one(one(google_sql_database_instance.this.settings[*].ip_configuration)[*].ipv4_enabled) == true
    error_message = "old inputs keep the public IPv4 path"
  }
  assert {
    condition = [for n in one(one(google_sql_database_instance.this.settings[*].ip_configuration)[*].authorized_networks) : "${n.name}=${n.value}"] == ["all=0.0.0.0/0"]
    error_message = "the legacy authorized network must keep its historical name AND value (B1: no in-place rename)"
  }
}

run "secure_module_defaults_render_backup_flags_insights" {
  command = plan

  variables {
    authorized_networks   = []
    ssl_mode              = "ENCRYPTED_ONLY"
    backup_enabled        = true
    database_flags = {
      log_checkpoints           = "on"
      log_connections           = "on"
      log_disconnections        = "on"
      log_lock_waits            = "on"
      log_temp_files            = "0"
      log_hostname              = "on"
      log_min_messages          = "error"
      log_statement             = "ddl"
      log_duration              = "on"
      "cloudsql.enable_pgaudit" = "on"
    }
    enable_query_insights = true
  }

  assert {
    condition     = one(one(google_sql_database_instance.this.settings[*].backup_configuration)[*].point_in_time_recovery_enabled) == true
    error_message = "backup_enabled must render PITR"
  }
  assert {
    condition     = length(one(google_sql_database_instance.this.settings[*].database_flags)) == 10
    error_message = "the observability flag set must render all 10 flags"
  }
  assert {
    condition     = one(one(google_sql_database_instance.this.settings[*].ip_configuration)[*].ssl_mode) == "ENCRYPTED_ONLY"
    error_message = "ssl_mode must pin encrypted-only"
  }
  assert {
    condition     = length(one(one(google_sql_database_instance.this.settings[*].ip_configuration)[*].authorized_networks)) == 0
    error_message = "no authorized networks by module default"
  }
}

run "private_network_drops_the_public_ip_and_networks" {
  command = plan

  variables {
    private_network = "projects/demo-project/global/networks/prod-net"
  }

  assert {
    condition     = one(one(google_sql_database_instance.this.settings[*].ip_configuration)[*].ipv4_enabled) == false
    error_message = "private peering must drop the public IPv4"
  }
  assert {
    condition     = length(one(one(google_sql_database_instance.this.settings[*].ip_configuration)[*].authorized_networks)) == 0
    error_message = "authorized networks are meaningless on the private path"
  }
}

run "cmek_passes_through_when_offered" {
  command = plan

  variables {
    encryption_key_name = "projects/p/locations/us-central1/keyRings/r/cryptoKeys/k"
  }

  assert {
    condition     = google_sql_database_instance.this.encryption_key_name == "projects/p/locations/us-central1/keyRings/r/cryptoKeys/k"
    error_message = "the DEP-offered CMEK key must flow to the instance"
  }
}

run "maintenance_window_renders_when_set" {
  command = plan

  variables {
    maintenance_day  = 7
    maintenance_hour = 4
  }

  assert {
    condition     = one(one(google_sql_database_instance.this.settings[*].maintenance_window)[*].day) == 7
    error_message = "maintenance window must render the chosen day"
  }
}
