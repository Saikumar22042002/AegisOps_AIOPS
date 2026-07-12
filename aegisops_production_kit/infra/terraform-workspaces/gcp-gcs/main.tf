# GCP Cloud Storage bucket (org-approved template: gcp/gcs).
terraform {
  required_version = ">= 1.6"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 5.40" }
  }
  backend "local" {}
}

provider "google" {
  project = var.project
}

variable "bucket_name" { type = string }
variable "location" {
  type    = string
  default = "US"
}
variable "project" { type = string }
variable "storage_class" {
  type    = string
  default = "STANDARD"
}

resource "google_storage_bucket" "this" {
  name                        = var.bucket_name
  location                    = var.location
  project                     = var.project
  storage_class               = var.storage_class
  uniform_bucket_level_access = true
  force_destroy               = false
  # Hardening aligned with the platform's no-public-bucket policy (U1): in-place change.
  public_access_prevention = "enforced"

  versioning {
    enabled = true
  }

  labels = {
    managed_by = "aegisops"
  }
}

output "bucket_url" { value = google_storage_bucket.this.url }
output "bucket_name" { value = google_storage_bucket.this.name }
