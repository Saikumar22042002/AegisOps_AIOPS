variable "name" {
  type        = string
  description = "Vault name (globally unique, 3-24 alphanumerics/hyphens)."
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

variable "soft_delete_days" {
  type        = number
  default     = 90
  description = "Soft-delete retention (7-90 days) — deleted vaults/keys are recoverable until it elapses."

  validation {
    condition     = var.soft_delete_days >= 7 && var.soft_delete_days <= 90
    error_message = "soft_delete_days must be between 7 and 90."
  }
}

variable "purge_protection" {
  type        = bool
  default     = true
  description = "Block permanent purge until soft-delete elapses (recommended on)."
}

variable "network_default_action" {
  type        = string
  default     = "Allow"
  description = "network_acls default action (Allow|Deny). Allow is STATED on the approval card."

  validation {
    condition     = contains(["Allow", "Deny"], var.network_default_action)
    error_message = "network_default_action must be Allow or Deny."
  }
}

variable "additional_policies" {
  type = map(object({
    key_permissions    = list(string)
    secret_permissions = list(string)
  }))
  default     = {}
  description = "Extra access policies keyed by object id."
}

variable "keys" {
  type        = list(string)
  default     = []
  description = "RSA-2048 keys to create in the vault (names)."
}
