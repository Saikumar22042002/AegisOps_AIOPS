variable "name" {
  type        = string
  description = "VNet name (RG, subnets, NAT and route tables derive from it)."
}

variable "location" {
  type        = string
  default     = "eastus"
  description = "Azure region."
}

variable "resource_group" {
  type        = string
  default     = "" # created as "<name>-rg" when empty (default-RG semantics, like azure-vm)
  description = "Existing resource group name, or empty to create '<name>-rg'."
}

variable "address_space" {
  type        = string
  default     = "10.20.0.0/16"
  description = "VNet address space (RFC1918)."
}

variable "subnet_cidrs" {
  type        = list(string)
  default     = ["10.20.1.0/24"]
  description = "Public-tier subnet CIDRs (default: one /24)."

  validation {
    condition     = length(var.subnet_cidrs) >= 1
    error_message = "At least one subnet CIDR is required."
  }
}

variable "private_subnet_cidrs" {
  type        = list(string)
  default     = []
  description = "Private-tier subnet CIDRs; a NAT gateway + private route table are created when non-empty."
}
