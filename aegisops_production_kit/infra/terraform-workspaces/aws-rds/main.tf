# AWS RDS instance (org-approved template: aws/rds). Master password is RDS-managed.
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

variable "identifier" { type = string }
variable "engine" {
  type    = string
  default = "postgres"
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

resource "aws_db_instance" "this" {
  identifier                  = var.identifier
  engine                      = var.engine
  instance_class              = var.instance_class
  allocated_storage           = var.allocated_storage
  username                    = "aegisadmin"
  manage_master_user_password = true
  storage_encrypted           = true
  skip_final_snapshot         = true
  publicly_accessible         = false

  tags = {
    ManagedBy = "AegisOps"
  }
}

output "endpoint" { value = aws_db_instance.this.endpoint }
output "identifier" { value = aws_db_instance.this.identifier }
