variable "name" {
  type        = string
  description = "Key name (the alias becomes alias/<name>)."
}

variable "region" {
  type        = string
  default     = "us-east-1"
  description = "AWS region."
}

variable "deletion_window" {
  type        = number
  default     = 30
  description = "Scheduled-deletion window in days (7–30) — a destroyed key is recoverable until it elapses."

  validation {
    condition     = var.deletion_window >= 7 && var.deletion_window <= 30
    error_message = "deletion_window must be between 7 and 30 days."
  }
}

variable "enable_rotation" {
  type        = bool
  default     = true
  description = "Automatic annual key rotation."
}

variable "allowed_services" {
  type        = list(string)
  default     = ["secretsmanager", "rds"]
  description = "AWS services granted Decrypt/DescribeKey/CreateGrant on the key."
}
