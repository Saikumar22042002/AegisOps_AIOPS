variable "identifier" { type = string }

variable "engine" {
  type        = string
  default     = "postgres"
  description = "Database engine: postgres, mysql, or mariadb."

  validation {
    condition     = contains(["postgres", "mysql", "mariadb"], var.engine)
    error_message = "engine must be one of: postgres, mysql, mariadb."
  }
}

variable "engine_version" {
  type        = string
  default     = ""
  description = "\"\" = provider default (old behavior), \"latest\" = resolved via the engine-version data source, or an explicit version pin."
}

variable "instance_class" {
  type    = string
  default = "db.t3.medium"
}

variable "allocated_storage" {
  type    = number
  default = 20
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "allowed_cidr" {
  type        = string
  default     = ""
  description = "Client CIDR allowed to reach the DB port. MANDATORY to get the dedicated SG; there is no open default and a world-open CIDR is rejected."

  validation {
    condition     = var.allowed_cidr == "" || !endswith(var.allowed_cidr, "/0")
    error_message = "allowed_cidr must never be world-open (no /0 CIDR is accepted)."
  }
}

variable "subnet_ids" {
  type        = list(string)
  default     = []
  description = "Subnets for an optional DB subnet group; empty keeps the old placement."
}

variable "enable_log_exports" {
  type        = bool
  default     = true
  description = "Engine-aware CloudWatch log exports + query-logging parameter group. Secure by default here; the platform schema defaults this OFF (B2) and always passes it explicitly."
}
