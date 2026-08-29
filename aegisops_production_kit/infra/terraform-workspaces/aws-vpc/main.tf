# AWS VPC (org-approved template: aws/vpc) using the official module.
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

variable "name" { type = string }
variable "cidr_block" {
  type    = string
  default = "10.0.0.0/16"
}
variable "region" {
  type    = string
  default = "us-east-1"
}
variable "az_count" {
  type    = number
  default = 3
}
variable "enable_nat" {
  type    = bool
  default = true
}

# Standard AZs only. Without the opt-in filter, accounts with Local Zones enabled (common
# in lab sandboxes) sort zones like us-east-1-atl-2a BEFORE us-east-1a, so every subnet
# landed in a Local Zone — where NAT gateways and t2/t3 instances are unsupported (proven
# live 2026-08-16: NotAvailableInZone on NAT, Unsupported on RunInstances).
data "aws_availability_zones" "available" {
  state = "available"
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, var.az_count)
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.8"

  name = var.name
  cidr = var.cidr_block
  azs  = local.azs

  private_subnets = [for i, az in local.azs : cidrsubnet(var.cidr_block, 4, i)]
  public_subnets  = [for i, az in local.azs : cidrsubnet(var.cidr_block, 4, i + 8)]

  # Public subnets must actually behave publicly (module v5 defaults this to false, which
  # made every "public" subnet render as private in the console — audit 2026-08-16).
  map_public_ip_on_launch = true

  enable_nat_gateway = var.enable_nat
  single_nat_gateway = var.enable_nat

  tags = {
    ManagedBy = "AegisOps"
  }
}

output "vpc_id" { value = module.vpc.vpc_id }
output "private_subnet_ids" { value = module.vpc.private_subnets }
output "public_subnet_ids" { value = module.vpc.public_subnets }
