# GCP Compute Engine VM (org-approved template: gcp/vm). A usable SSH key is generated (private
# key surfaced as a sensitive output, never logged). Day-2-modifiable inbound ports via firewall.
# MODSEED MS-12: shielded VM, OS Login, preemptible/spot (maintenance implications stated on the
# card), optional least-scope service account, and a var-driven network (the DEP slot places the
# VM into an existing gcp.vpc when one is known — B4, by design). KEEP: generated SSH key +
# one-time reveal.
# BACKCOMPAT (B1/B2): the platform schema defaults every option to the OLD behavior (public IP
# on, everything else off, network "default") and passes them explicitly; the module's own
# defaults are the secure ones (shielded on, project-wide keys blocked, no public IP).
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

variable "network" {
  type        = string
  default     = "default"
  description = "VPC network for the instance AND its firewalls. The DEP slot fills an existing gcp.vpc name when one is known; 'default' preserves the old placement."
}

variable "public_ip" {
  type        = bool
  default     = false
  description = "Ephemeral public IP. The module default is NONE (secure); the platform schema passes true to keep the demo SSH path (B2 old behavior)."
}

variable "enable_shielded" {
  type        = bool
  default     = true
  description = "Shielded VM (secure boot + vTPM + integrity monitoring). Secure by default here; schema defaults OFF (B2)."
}

variable "block_project_ssh_keys" {
  type        = bool
  default     = true
  description = "Block project-wide SSH keys (instance keys only). Secure by default here; schema defaults OFF (B2)."
}

variable "enable_oslogin" {
  type        = bool
  default     = false
  description = "OS Login (IAM-governed SSH). Note: replaces metadata SSH keys — the generated key becomes unused while enabled."
}

variable "spot" {
  type        = bool
  default     = false
  description = "Preemptible/Spot instance — may be stopped by GCP at any time, no automatic restart (the card states this)."
}

variable "service_account_email" {
  type        = string
  default     = ""
  description = "Optional dedicated service account (least scope: logging + monitoring writes only)."
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
  metadata = merge(
    { ssh-keys = "${var.ssh_user}:${tls_private_key.ssh.public_key_openssh}" },
    var.block_project_ssh_keys ? { block-project-ssh-keys = "true" } : {},
    var.enable_oslogin ? { enable-oslogin = "TRUE" } : {},
  )
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
    network = var.network

    dynamic "access_config" {
      for_each = var.public_ip ? [1] : []
      content {} # ephemeral public IP
    }
  }

  dynamic "shielded_instance_config" {
    for_each = var.enable_shielded ? [1] : []
    content {
      enable_secure_boot          = true
      enable_vtpm                 = true
      enable_integrity_monitoring = true
    }
  }

  dynamic "scheduling" {
    for_each = var.spot ? [1] : []
    content {
      preemptible                 = true
      automatic_restart           = false
      provisioning_model          = "SPOT"
      instance_termination_action = "STOP"
    }
  }

  dynamic "service_account" {
    for_each = var.service_account_email != "" ? [1] : []
    content {
      email  = var.service_account_email
      scopes = ["logging-write", "monitoring-write"]
    }
  }

  metadata = local.metadata

  # Network tags MUST match the firewall rules' target_tags — without this the firewalls
  # never attached to the instance (pre-existing defect found in the Phase-8 review).
  tags = [var.name]

  labels = { managed_by = "aegisops" }
}

resource "google_compute_firewall" "ingress" {
  count   = length(var.ingress_ports) > 0 ? 1 : 0
  name    = "${var.name}-aegisops-ingress"
  network = var.network
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
  network = var.network
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
  source_ranges = [var.allowed_cidr]
  target_tags   = ["${var.name}"]
}

output "instance_id" { value = google_compute_instance.this.instance_id }
output "self_link" { value = google_compute_instance.this.self_link }
output "public_ip" { value = try(google_compute_instance.this.network_interface[0].access_config[0].nat_ip, null) }
output "private_ip" { value = google_compute_instance.this.network_interface[0].network_ip }
output "network" { value = var.network }
output "login_user" { value = var.ssh_user }
output "zone" { value = var.zone }
output "ingress_ports" { value = var.ingress_ports }
output "allowed_cidr" { value = var.allowed_cidr }
output "admin_port" { value = var.allowed_cidr != "" ? 22 : null }
output "shielded" { value = var.enable_shielded }
output "spot" { value = var.spot }
output "private_key_pem" {
  value     = tls_private_key.ssh.private_key_pem
  sensitive = true
}
