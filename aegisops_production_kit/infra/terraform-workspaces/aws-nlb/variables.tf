variable "name" {
  type        = string
  description = "Load balancer name (target group and SG derive from it)."
}

variable "region" {
  type        = string
  default     = "us-east-1"
  description = "AWS region."
}

variable "vpc_id" {
  type        = string
  description = "VPC for the target group + auto SG (DEP-resolved from the world model)."
}

variable "subnets" {
  type        = list(string)
  description = "Subnets the NLB spans (DEP-resolved from the VPC's recorded outputs)."

  validation {
    condition     = length(var.subnets) >= 1
    error_message = "At least one subnet is required."
  }
}

variable "target_port" {
  type        = number
  default     = 80
  description = "Target group port (TCP)."
}

variable "listener_port" {
  type        = number
  default     = 80
  description = "Listener port (TCP)."
}

variable "internal" {
  type        = bool
  default     = false
  description = "Internal (true) or internet-facing (false) — stated on the approval card."
}

variable "deletion_protection" {
  type        = bool
  default     = false
  description = "Protect the LB from deletion (the platform defaults this ON for env=Production)."
}

variable "security_group_ids" {
  type        = list(string)
  default     = []
  description = "Existing SGs to attach; empty = create an egress-only SG (no ingress rules)."
}
