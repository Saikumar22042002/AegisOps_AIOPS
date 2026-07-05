# GCP Cloud SQL for PostgreSQL (org-approved template: gcp/cloudsql). Generated root password
# (sensitive output, never logged). deletion_protection disabled so day-2 destroy works.
terraform {
  required_version = ">= 1.6"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 5.40" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
  backend "local" {}
}

variable "name" { type = string }
variable "project" { type = string }
variable "region" {
  type    = string
  default = "us-central1"
}
variable "tier" {
  type    = string
  default = "db-f1-micro"
}
variable "database_version" {
  type    = string
  default = "POSTGRES_15"
}

provider "google" {
  project = var.project
  region  = var.region
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
  deletion_protection = false
  root_password       = random_password.root.result

  settings {
    tier = var.tier
    ip_configuration {
      ipv4_enabled = true
      # Demo default: reachable from anywhere. Tighten authorized_networks per environment.
      authorized_networks {
        name  = "all"
        value = "0.0.0.0/0"
      }
    }
  }
}

output "connection_name" { value = google_sql_database_instance.this.connection_name }
output "endpoint" { value = google_sql_database_instance.this.public_ip_address }
output "public_ip" { value = google_sql_database_instance.this.public_ip_address }
output "admin_username" { value = "postgres" }
output "root_password" {
  value     = random_password.root.result
  sensitive = true
}
