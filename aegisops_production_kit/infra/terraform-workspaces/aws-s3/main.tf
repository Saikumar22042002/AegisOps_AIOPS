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

resource "aws_s3_bucket" "this" {
  bucket = var.bucket_name
  tags = {
    ManagedBy = "AegisOps"
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
