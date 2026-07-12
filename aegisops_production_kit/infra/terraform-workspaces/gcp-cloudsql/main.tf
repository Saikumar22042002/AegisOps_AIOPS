# GCP Cloud SQL for PostgreSQL (org-approved template: gcp/cloudsql). Generated root password
# (sensitive output, never logged). deletion_protection is variable-driven (default off so the
# governed day-2 destroy keeps working).
# MODSEED MS-9: optional private-VPC-peering, backup/PITR, maintenance window, query insights,
# ssl_mode, deletion_protection var, optional CMEK (DEP slot on gcp.kms - never forced).
# BACKCOMPAT (B1/B2): the platform schema defaults every new capability to the OLD behavior
# and passes it explicitly, so pre-enhancement stored inputs render the exact old plan
# (including the legacy "all" authorized network). The module's OWN defaults are the secure
# ones for bare use - scanners evaluate module defaults.
terraform {
  required_version = ">= 1.6"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 5.40" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
  backend "local" {}
}

provider "google" {
  project = var.project
  region  = var.region
}

locals {
  private = var.private_network != ""
  # The legacy world-open network keeps its historical name so existing instances
  # re-plan without an in-place rename (B1).
  network_names = { for idx, cidr in var.authorized_networks :
  cidr => cidr == "0.0.0.0/0" ? "all" : "net-${idx}" }
}

resource "random_password" "root" {
  length           = 24
  special          = true
  override_special = "!#$%*-_=+"
}

resource "google_sql_database_instance" "this" {
  name                = var.name
  region              = var.region
  database_version    = var.database_version
  deletion_protection = var.deletion_protection
  root_password       = random_password.root.result
  encryption_key_name = var.encryption_key_name != "" ? var.encryption_key_name : null

  settings {
    tier = var.tier

    ip_configuration {
      ipv4_enabled    = !local.private
      private_network = local.private ? var.private_network : null
      ssl_mode        = var.ssl_mode != "" ? var.ssl_mode : null

      dynamic "authorized_networks" {
        for_each = local.private ? [] : var.authorized_networks
        content {
          name  = local.network_names[authorized_networks.value]
          value = authorized_networks.value
        }
      }
    }

    dynamic "backup_configuration" {
      for_each = var.backup_enabled ? [1] : []
      content {
        enabled                        = true
        point_in_time_recovery_enabled = true
      }
    }

    dynamic "database_flags" {
      for_each = var.database_flags
      content {
        name  = database_flags.key
        value = database_flags.value
      }
    }

    dynamic "insights_config" {
      for_each = var.enable_query_insights ? [1] : []
      content {
        query_insights_enabled  = true
        record_application_tags = false
        record_client_address   = false
      }
    }

    dynamic "maintenance_window" {
      for_each = var.maintenance_day > 0 ? [1] : []
      content {
        day          = var.maintenance_day
        hour         = var.maintenance_hour
        update_track = "stable"
      }
    }
  }
}
