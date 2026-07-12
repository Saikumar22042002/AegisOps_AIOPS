variable "name" {
  type        = string
  description = "Network name (subnets, router, NAT and firewall derive from it)."
}

variable "project" {
  type        = string
  description = "GCP project id (auto-filled from the platform's configured project)."
}

variable "region" {
  type        = string
  default     = "us-central1"
  description = "Region for the subnets, router and NAT."
}

variable "subnet_cidrs" {
  type        = list(string)
  default     = ["10.10.0.0/20", "10.10.16.0/20"]
  description = "Primary CIDR per subnet (default: two /20s carved from 10.10.0.0/16)."

  validation {
    condition     = length(var.subnet_cidrs) >= 1
    error_message = "At least one subnet CIDR is required."
  }
}

variable "secondary_ranges" {
  type = list(object({
    pods_cidr     = string
    services_cidr = string
  }))
  default = [
    { pods_cidr = "10.64.0.0/18", services_cidr = "10.96.0.0/20" },
    { pods_cidr = "10.64.64.0/18", services_cidr = "10.96.16.0/20" },
  ]
  description = "Per-subnet secondary ranges (pods/services) for future GKE placement; aligned by index with subnet_cidrs."
}

variable "enable_nat" {
  type        = bool
  default     = true
  description = "Cloud Router + NAT for private-subnet egress (logging: errors only)."
}

variable "enable_flow_logs" {
  type        = bool
  default     = false
  description = "VPC flow logs on every subnet (5-min aggregation, 0.5 sampling)."
}
