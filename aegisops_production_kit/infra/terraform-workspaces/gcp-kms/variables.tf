variable "name" {
  type        = string
  description = "Key ring name (default key becomes '<name>-key'). Ring names are permanent in GCP."
}

variable "project" {
  type        = string
  description = "GCP project id (auto-filled from the platform's configured project)."
}

variable "region" {
  type        = string
  default     = "us-central1"
  description = "Key ring location."
}

variable "keys" {
  type        = list(string)
  default     = []
  description = "Crypto key names to create in the ring; empty = one '<name>-key'."
}

variable "rotation_days" {
  type        = number
  default     = 90
  description = "Automatic rotation period in days (minimum 1)."

  validation {
    condition     = var.rotation_days >= 1
    error_message = "rotation_days must be at least 1."
  }
}

variable "encrypter_decrypters" {
  type        = list(string)
  default     = []
  description = "IAM members (user:/serviceAccount:) granted cryptoKeyEncrypterDecrypter on every key."
}
