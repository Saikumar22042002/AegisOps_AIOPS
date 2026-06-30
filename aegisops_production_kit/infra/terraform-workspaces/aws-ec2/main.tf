# AWS EC2 instance (org-approved template: aws/ec2).
# Auto-resolves a current Amazon Linux 2023 AMI and a default-VPC subnet when not supplied,
# so "create a t3.micro VM" needs no extra inputs. IMDSv2 + encrypted root enforced.
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

variable "name" {
  type    = string
  default = "aegisops-vm"
}
variable "instance_type" {
  type    = string
  default = "t3.micro"
}
variable "ami" {
  type    = string
  default = ""
}
variable "subnet_id" {
  type    = string
  default = ""
}
variable "region" {
  type    = string
  default = "us-east-1"
}

data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]
  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

data "aws_vpc" "default" {
  default = true
}

# AZs that actually offer the requested instance type (not every AZ does, e.g. t3.micro in us-east-1e).
data "aws_ec2_instance_type_offerings" "supported" {
  filter {
    name   = "instance-type"
    values = [var.instance_type]
  }
  location_type = "availability-zone"
}

# Default-VPC subnets restricted to AZs that support the instance type.
data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "availability-zone"
    values = data.aws_ec2_instance_type_offerings.supported.locations
  }
}

locals {
  ami_id    = var.ami != "" ? var.ami : data.aws_ami.al2023.id
  subnet_id = var.subnet_id != "" ? var.subnet_id : data.aws_subnets.default.ids[0]
}

resource "aws_instance" "this" {
  ami           = local.ami_id
  instance_type = var.instance_type
  subnet_id     = local.subnet_id

  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }
  root_block_device {
    encrypted = true
  }

  tags = {
    Name      = var.name
    ManagedBy = "AegisOps"
  }
}

output "instance_id" { value = aws_instance.this.id }
output "private_ip" { value = aws_instance.this.private_ip }
output "ami_used" { value = local.ami_id }
