# MODSEED MS-1 — gcp.vpc: custom-mode network + regional subnets (secondary pods/services
# ranges recorded for future GKE placement) + Cloud Router/NAT for private egress + an
# internal firewall scoped to the subnet CIDRs ONLY. No admin/SSH rules here — the VM module
# owns admin ingress via its allowed_cidr. No backend block (A3 injects backend config).

terraform {
  required_version = ">= 1.6"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 5.40" }
  }
}

provider "google" {
  project = var.project
  region  = var.region
}

resource "google_compute_network" "this" {
  name                    = var.name
  auto_create_subnetworks = false # custom mode — subnets are explicit, policy-checked
  routing_mode            = "REGIONAL"
}

resource "google_compute_subnetwork" "this" {
  count                    = length(var.subnet_cidrs)
  name                     = "${var.name}-subnet-${count.index}"
  network                  = google_compute_network.this.id
  region                   = var.region
  ip_cidr_range            = var.subnet_cidrs[count.index]
  private_ip_google_access = true

  # Secondary ranges (pods/services) recorded in outputs/attributes so GKE can be DEP-placed
  # into this network later without re-planning the VPC.
  dynamic "secondary_ip_range" {
    for_each = count.index < length(var.secondary_ranges) ? [var.secondary_ranges[count.index]] : []
    content {
      range_name    = "${var.name}-pods-${count.index}"
      ip_cidr_range = secondary_ip_range.value.pods_cidr
    }
  }
  dynamic "secondary_ip_range" {
    for_each = count.index < length(var.secondary_ranges) ? [var.secondary_ranges[count.index]] : []
    content {
      range_name    = "${var.name}-services-${count.index}"
      ip_cidr_range = secondary_ip_range.value.services_cidr
    }
  }

  dynamic "log_config" {
    for_each = var.enable_flow_logs ? [1] : []
    content {
      aggregation_interval = "INTERVAL_5_MIN"
      flow_sampling        = 0.5
      metadata             = "INCLUDE_ALL_METADATA"
    }
  }
}

# Private egress: per-region Cloud Router + NAT (logging errors only — cost-aware default).
resource "google_compute_router" "this" {
  count   = var.enable_nat ? 1 : 0
  name    = "${var.name}-router"
  network = google_compute_network.this.id
  region  = var.region
}

resource "google_compute_router_nat" "this" {
  count                              = var.enable_nat ? 1 : 0
  name                               = "${var.name}-nat"
  router                             = google_compute_router.this[0].name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

# Internal traffic between this network's OWN subnets only — never a world-open rule, never
# an admin/SSH rule (the VM module's firewall owns admin ingress, bound to its allowed_cidr).
resource "google_compute_firewall" "internal" {
  name          = "${var.name}-allow-internal"
  network       = google_compute_network.this.id
  direction     = "INGRESS"
  source_ranges = var.subnet_cidrs

  allow {
    protocol = "tcp"
    ports    = ["0-65535"]
  }
  allow {
    protocol = "udp"
    ports    = ["0-65535"]
  }
  allow {
    protocol = "icmp"
  }
}
