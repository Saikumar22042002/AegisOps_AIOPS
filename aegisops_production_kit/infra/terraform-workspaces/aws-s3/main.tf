# AWS S3 bucket (org-approved template: aws/s3). Secure-by-default.
terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.60" }
  }
  backend "local" {}
}

provider "aws" {
  region = var.region
}

variable "bucket_name" { type = string }
variable "region" {
  type    = string
  default = "us-east-1"
}
variable "versioning" {
  type    = bool
  default = true
}
variable "block_public" {
  type    = bool
  default = true
}

# MOD: day-2 lifecycle — 0 keeps the old rendering (no lifecycle configuration at all).
# Never a module default: auto-expiring objects is a data-loss decision the user makes.
variable "lifecycle_expire_days" {
  type    = number
  default = 0

  validation {
    condition     = var.lifecycle_expire_days >= 0 && var.lifecycle_expire_days <= 3650
    error_message = "lifecycle_expire_days must be 0 (off) to 3650."
  }
}

variable "extra_tags" {
  type        = map(string)
  default     = {}
  description = "MOD: additional tags merged onto the bucket (day-2 tag updates are in-place)."
}

resource "aws_s3_bucket" "this" {
  bucket = var.bucket_name
  tags = merge({
    ManagedBy = "AegisOps"
  }, var.extra_tags)
}

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  for_each = var.lifecycle_expire_days > 0 ? toset(["expire"]) : toset([])
  bucket   = aws_s3_bucket.this.id

  rule {
    id     = "aegisops-expire"
    status = "Enabled"

    filter {}

    expiration {
      days = var.lifecycle_expire_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_s3_bucket_versioning" "this" {
  bucket = aws_s3_bucket.this.id
  versioning_configuration {
    status = var.versioning ? "Enabled" : "Suspended"
  }
}

resource "aws_s3_bucket_public_access_block" "this" {
  bucket                  = aws_s3_bucket.this.id
  block_public_acls       = var.block_public
  block_public_policy     = var.block_public
  ignore_public_acls      = var.block_public
  restrict_public_buckets = var.block_public
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = aws_s3_bucket.this.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

output "bucket_arn" { value = aws_s3_bucket.this.arn }
output "bucket_name" { value = aws_s3_bucket.this.id }
output "versioning" { value = var.versioning }
output "lifecycle_expire_days" { value = var.lifecycle_expire_days }
