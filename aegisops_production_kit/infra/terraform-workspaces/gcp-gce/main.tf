# GCP Compute Engine VM (org-approved template: gcp/vm). A usable SSH key is generated (private
# key surfaced as a sensitive output, never logged). Day-2-modifiable inbound ports via firewall.
terraform {
  required_version = ">= 1.6"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 5.40" }
    tls    = { source = "hashicorp/tls", version = "~> 4.0" }
  }
  backend "local" {}
}

variable "name" { type = string }
variable "project" { type = string }
variable "region" {
  type    = string
  default = "us-central1"
}
variable "zone" {
  type    = string
  default = "us-central1-a"
}
variable "machine_type" {
  type    = string
  default = "e2-micro"
}
# debian-12 | ubuntu-22.04 | ubuntu-24.04
variable "os" {
  type    = string
  default = "debian-12"
}
variable "ssh_user" {
  type    = string
  default = "aegis"
}
variable "ingress_ports" {
  type    = list(number)
  default = []
}
# Source CIDR allowed to reach SSH (22) — e.g. requester's IP as x.x.x.x/32. Empty = closed (N-02).
variable "allowed_cidr" {
  type    = string
  default = ""
}

provider "google" {
  project = var.project
  region  = var.region
}

locals {
  images = {
    "debian-12"    = "debian-cloud/debian-12"
    "ubuntu-22.04" = "ubuntu-os-cloud/ubuntu-2204-lts"
    "ubuntu-24.04" = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
  }
  image = lookup(local.images, var.os, local.images["debian-12"])
}

resource "tls_private_key" "ssh" {
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "google_compute_instance" "this" {
  name         = var.name
  machine_type = var.machine_type
  zone         = var.zone

  boot_disk {
    initialize_params {
      image = local.image
    }
  }

  network_interface {
    network = "default"
    access_config {} # ephemeral public IP
  }

  metadata = {
    ssh-keys = "${var.ssh_user}:${tls_private_key.ssh.public_key_openssh}"
  }

  # Network tags MUST match the firewall rules' target_tags — without this the firewalls
  # never attached to the instance (pre-existing defect found in the Phase-8 review).
  tags = [var.name]

  labels = { managed_by = "aegisops" }
}

resource "google_compute_firewall" "ingress" {
  count   = length(var.ingress_ports) > 0 ? 1 : 0
  name    = "${var.name}-aegisops-ingress"
  network = "default"
  allow {
    protocol = "tcp"
    ports    = [for p in var.ingress_ports : tostring(p)]
  }
  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["${var.name}"]
}

# Admin SSH access — ONLY from the user's declared CIDR; no rule when closed (N-02).
resource "google_compute_firewall" "admin" {
  count   = var.allowed_cidr != "" ? 1 : 0
  name    = "${var.name}-aegisops-admin"
  network = "default"
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
  source_ranges = [var.allowed_cidr]
  target_tags   = ["${var.name}"]
}

output "instance_id" { value = google_compute_instance.this.instance_id }
output "self_link" { value = google_compute_instance.this.self_link }
output "public_ip" { value = google_compute_instance.this.network_interface[0].access_config[0].nat_ip }
output "private_ip" { value = google_compute_instance.this.network_interface[0].network_ip }
output "login_user" { value = var.ssh_user }
output "zone" { value = var.zone }
output "ingress_ports" { value = var.ingress_ports }
output "allowed_cidr" { value = var.allowed_cidr }
output "admin_port" { value = var.allowed_cidr != "" ? 22 : null }
output "private_key_pem" {
  value     = tls_private_key.ssh.private_key_pem
  sensitive = true
}
