output "network_id" {
  value = google_compute_network.this.id
}

output "network_name" {
  value = google_compute_network.this.name
}

output "network_self_link" {
  value = google_compute_network.this.self_link
}

output "subnet_ids" {
  value = google_compute_subnetwork.this[*].id
}

output "subnet_names" {
  value = google_compute_subnetwork.this[*].name
}

output "subnet_cidrs" {
  value = google_compute_subnetwork.this[*].ip_cidr_range
}

# Secondary range NAMES per subnet — what a future GKE placement references.
output "secondary_range_names" {
  value = [for s in google_compute_subnetwork.this : [for r in s.secondary_ip_range : r.range_name]]
}

output "nat_enabled" {
  value = var.enable_nat
}
