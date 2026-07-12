# MODSEED MS-4 — aws.kms: KMS key (rotation ON, bounded deletion window) + alias + key policy
# (root admin via the caller identity; allowed services get Decrypt/DescribeKey/CreateGrant).
# Secret VALUES are permanently out of scope — this module manages KEYS, never secrets.
# No backend block (A3 injects).

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.60" }
  }
}

provider "aws" {
  region = var.region
}

data "aws_caller_identity" "current" {}

resource "aws_kms_key" "this" {
  description             = "AegisOps-managed key ${var.name}"
  deletion_window_in_days = var.deletion_window
  enable_key_rotation     = var.enable_rotation
  tags                    = { ManagedBy = "aegisops", Name = var.name }

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid       = "RootAdmin"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      }
      ], [
      for svc in var.allowed_services : {
        Sid       = "Allow${replace(title(svc), "-", "")}"
        Effect    = "Allow"
        Principal = { Service = "${svc}.amazonaws.com" }
        Action    = ["kms:Decrypt", "kms:DescribeKey", "kms:CreateGrant"]
        Resource  = "*"
      }
    ])
  })
}

resource "aws_kms_alias" "this" {
  name          = "alias/${var.name}"
  target_key_id = aws_kms_key.this.key_id
}
