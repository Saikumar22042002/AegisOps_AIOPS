# GCP GKE cluster (org-approved template: gcp/gke). Managed cluster + a dedicated node pool.
# deletion_protection disabled so day-2 destroy works.
terraform {
  required_version = ">= 1.6"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 5.40" }
  }
  backend "local" {}
}

variable "name" { type = string }
variable "project" { type = string }
variable "region" {
  type    = string
  default = "us-central1"
}
variable "node_count" {
  type    = number
  default = 2
}
variable "machine_type" {
  type    = string
  default = "e2-medium"
}

provider "google" {
  project = var.project
  region  = var.region
}

resource "google_container_cluster" "this" {
  name                     = var.name
  location                 = var.region
  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = false
  network                  = "default"
  subnetwork               = "default"
}

resource "google_container_node_pool" "this" {
  name       = "${var.name}-pool"
  cluster    = google_container_cluster.this.id
  location   = var.region
  node_count = var.node_count

  node_config {
    machine_type = var.machine_type
    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    labels       = { managed_by = "aegisops" }
  }
}

output "cluster_id" { value = google_container_cluster.this.id }
output "endpoint" { value = google_container_cluster.this.endpoint }
output "name" { value = google_container_cluster.this.name }
output "node_count" { value = var.node_count }
