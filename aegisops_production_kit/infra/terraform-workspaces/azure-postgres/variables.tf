variable "name" { type = string }

variable "engine" {
  type        = string
  default     = "postgresql"
  description = "Database engine: postgresql, mysql, or mssql. Default preserves the pre-enhancement behavior."

  validation {
    condition     = contains(["postgresql", "mysql", "mssql"], var.engine)
    error_message = "engine must be one of: postgresql, mysql, mssql."
  }
}

variable "location" {
  type    = string
  default = "eastus"
}

variable "admin_username" {
  type    = string
  default = "pgadmin"
}

variable "sku_name" {
  type        = string
  default     = "B_Standard_B1ms"
  description = "Flexible-server SKU (postgresql/mysql). The mssql database uses S0."
}

variable "storage_mb" {
  type    = number
  default = 32768
}

variable "pg_version" {
  type        = string
  default     = "15"
  description = "PostgreSQL major version (kept verbatim from the pre-enhancement module)."
}

variable "engine_version" {
  type        = string
  default     = ""
  description = "MySQL version override (default 8.0.21). mssql is fixed at 12.0; postgresql keeps pg_version."
}

variable "resource_group" {
  type    = string
  default = ""
}

variable "ha_enabled" {
  type        = bool
  default     = false
  description = "ZoneRedundant high availability (postgresql/mysql). Off preserves old behavior."
}

variable "geo_redundant_backup" {
  type        = bool
  default     = true
  description = "Geo-redundant backups. Secure by default here; the platform schema defaults this OFF (B2) and always passes it explicitly."
}

variable "delegated_subnet_id" {
  type        = string
  default     = ""
  description = "Delegated subnet for private access (postgresql/mysql). Empty keeps the public path + allow-azure firewall rule."
}

variable "private_dns_zone_id" {
  type        = string
  default     = ""
  description = "Private DNS zone paired with delegated_subnet_id."
}
