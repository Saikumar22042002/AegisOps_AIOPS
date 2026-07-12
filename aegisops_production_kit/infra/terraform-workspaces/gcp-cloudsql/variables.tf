variable "name" { type = string }
variable "project" { type = string }

variable "region" {
  type    = string
  default = "us-central1"
}

variable "tier" {
  type    = string
  default = "db-f1-micro"
}

variable "database_version" {
  type    = string
  default = "POSTGRES_15"
}

variable "authorized_networks" {
  type        = list(string)
  default     = []
  description = "Public authorized networks. The module default is NONE (secure); the platform schema preserves the legacy 0.0.0.0/0 'all' entry for existing resources (B1)."
}

variable "private_network" {
  type        = string
  default     = ""
  description = "VPC self-link for private-IP peering; set to drop the public IP entirely."
}

variable "ssl_mode" {
  type        = string
  default     = "ENCRYPTED_ONLY"
  description = "Connection encryption. Secure by default here; the platform schema passes \"\" (provider default) for old behavior (B2)."

  validation {
    condition     = contains(["", "ALLOW_UNENCRYPTED_AND_ENCRYPTED", "ENCRYPTED_ONLY", "TRUSTED_CLIENT_CERTIFICATE_REQUIRED"], var.ssl_mode)
    error_message = "ssl_mode must be empty (provider default) or a valid Cloud SQL ssl_mode."
  }
}

variable "backup_enabled" {
  type        = bool
  default     = true
  description = "Automated backups + point-in-time recovery. Secure by default here; schema defaults OFF (B2)."
}

variable "database_flags" {
  type        = map(string)
  description = "PostgreSQL database flags. The module default is the org observability set (secure for bare use); the platform schema passes {} for old behavior (B2)."
  default = {
    log_checkpoints           = "on"
    log_connections           = "on"
    log_disconnections        = "on"
    log_lock_waits            = "on"
    log_temp_files            = "0"
    log_hostname              = "on"
    log_min_messages          = "error"
    log_statement             = "ddl"
    log_duration              = "on"
    "cloudsql.enable_pgaudit" = "on"
  }
}

variable "enable_query_insights" {
  type        = bool
  default     = true
  description = "Cloud SQL query insights. Schema defaults OFF (B2)."
}

variable "maintenance_day" {
  type        = number
  default     = 0
  description = "1-7 (Mon-Sun) enables a maintenance window; 0 leaves it unset (old behavior)."

  validation {
    condition     = var.maintenance_day >= 0 && var.maintenance_day <= 7
    error_message = "maintenance_day must be 0 (unset) or 1-7."
  }
}

variable "maintenance_hour" {
  type    = number
  default = 3

  validation {
    condition     = var.maintenance_hour >= 0 && var.maintenance_hour <= 23
    error_message = "maintenance_hour must be 0-23."
  }
}

variable "deletion_protection" {
  type        = bool
  default     = false
  description = "TF-level deletion protection. Off by default: destroys are approval-gated by the platform."
}

variable "encryption_key_name" {
  type        = string
  default     = ""
  description = "Optional CMEK crypto-key id (projects/.../cryptoKeys/...). Offered by the DEP slot when a gcp.kms ring exists; never forced."
}
