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

data "aws_availability_zones" "available" {
  state = "available"
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

  enable_nat_gateway = var.enable_nat
  single_nat_gateway = var.enable_nat

  tags = {
    ManagedBy = "AegisOps"
  }
}

output "vpc_id" { value = module.vpc.vpc_id }
output "private_subnet_ids" { value = module.vpc.private_subnets }
output "public_subnet_ids" { value = module.vpc.public_subnets }
