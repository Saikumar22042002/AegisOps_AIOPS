# AWS EC2 instance (org-approved template: aws/ec2).
# Real, parameterised: OS image, instance type, name, key pair and root volume are driven by
# variables (collected + validated by the agent). VPC/subnet default to the account's default
# VPC unless explicitly supplied. IMDSv2 + encrypted root enforced. A usable SSH key is
# guaranteed: either an existing key pair (var.key_name) or one created here (var.create_key_pair).
terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.60" }
    tls = { source = "hashicorp/tls", version = "~> 4.0" }
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
# One of: amazon-linux-2023 | ubuntu-22.04 | ubuntu-24.04 | windows-2022
variable "os" {
  type    = string
  default = "amazon-linux-2023"
}
variable "ami" {
  type    = string
  default = "" # explicit AMI overrides the OS lookup when set
}
variable "subnet_id" {
  type    = string
  default = ""
}
variable "region" {
  type    = string
  default = "us-east-1"
}
# Existing EC2 key pair name to attach for SSH/RDP login.
variable "key_name" {
  type    = string
  default = ""
}
# When true, generate a new key pair named var.key_name (private key surfaced as a sensitive output).
variable "create_key_pair" {
  type    = bool
  default = false
}
variable "root_volume_size" {
  type    = number
  default = 0 # 0 → use the AMI's own root size (some images require ≥30GB); set to grow it
}
variable "root_volume_type" {
  type    = string
  default = "gp3"
}
# Inbound TCP ports opened on the instance's managed security group (day-2 modifiable).
variable "ingress_ports" {
  type    = list(number)
  default = []
}
# Source CIDR allowed to reach the instance's admin port (SSH 22, or RDP 3389 on Windows) —
# e.g. the requester's public IP as x.x.x.x/32. Empty = closed (default-deny): the VM applies
# but is not remotely reachable until a CIDR is granted (N-02).
variable "allowed_cidr" {
  type    = string
  default = ""
}

# ── OS → AMI resolution (owners + name filters per OS) ──
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

data "aws_ami" "ubuntu2204" {
  most_recent = true
  owners      = ["099720109477"] # Canonical
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }
}

data "aws_ami" "ubuntu2404" {
  most_recent = true
  owners      = ["099720109477"]
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
}

data "aws_ami" "windows2022" {
  most_recent = true
  owners      = ["amazon"]
  filter {
    name   = "name"
    values = ["Windows_Server-2022-English-Full-Base-*"]
  }
}

data "aws_vpc" "default" {
  default = true
}

# AZs that actually offer the requested instance type.
data "aws_ec2_instance_type_offerings" "supported" {
  filter {
    name   = "instance-type"
    values = [var.instance_type]
  }
  location_type = "availability-zone"
}

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
  ami_by_os = {
    "amazon-linux-2023" = data.aws_ami.al2023.id
    "ubuntu-22.04"      = data.aws_ami.ubuntu2204.id
    "ubuntu-24.04"      = data.aws_ami.ubuntu2404.id
    "windows-2022"      = data.aws_ami.windows2022.id
  }
  login_user_by_os = {
    "amazon-linux-2023" = "ec2-user"
    "ubuntu-22.04"      = "ubuntu"
    "ubuntu-24.04"      = "ubuntu"
    "windows-2022"      = "Administrator"
  }
  ami_id     = var.ami != "" ? var.ami : lookup(local.ami_by_os, var.os, data.aws_ami.al2023.id)
  login_user = lookup(local.login_user_by_os, var.os, "ec2-user")
  admin_port = var.os == "windows-2022" ? 3389 : 22
  subnet_id  = var.subnet_id != "" ? var.subnet_id : data.aws_subnets.default.ids[0]
  # Effective key pair: created one wins, else the supplied existing name, else null (no key).
  key_name = var.create_key_pair ? aws_key_pair.generated[0].key_name : (var.key_name != "" ? var.key_name : null)
}

# Resolve the chosen subnet's VPC (for the security group + recorded in inventory).
data "aws_subnet" "selected" {
  id = local.subnet_id
}

# Dedicated, day-2-modifiable security group for this instance (add/remove inbound ports later).
resource "aws_security_group" "this" {
  name_prefix = "${var.name}-aegisops-"
  description = "AegisOps-managed SG for ${var.name}"
  vpc_id      = data.aws_subnet.selected.vpc_id

  dynamic "ingress" {
    for_each = toset(var.ingress_ports)
    content {
      from_port   = ingress.value
      to_port     = ingress.value
      protocol    = "tcp"
      cidr_blocks = ["0.0.0.0/0"]
    }
  }
  # Admin access (SSH/RDP) — scoped to the user's declared CIDR only, never 0.0.0.0/0 (N-02).
  dynamic "ingress" {
    for_each = var.allowed_cidr != "" ? [var.allowed_cidr] : []
    content {
      description = "AegisOps admin access (${local.admin_port})"
      from_port   = local.admin_port
      to_port     = local.admin_port
      protocol    = "tcp"
      cidr_blocks = [ingress.value]
    }
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${var.name}-sg", ManagedBy = "AegisOps" }
}

# ── Optional key-pair creation (private key returned as a sensitive output) ──
resource "tls_private_key" "generated" {
  count     = var.create_key_pair ? 1 : 0
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "aws_key_pair" "generated" {
  count      = var.create_key_pair ? 1 : 0
  key_name   = var.key_name
  public_key = tls_private_key.generated[0].public_key_openssh
  tags       = { ManagedBy = "AegisOps" }
}

resource "aws_instance" "this" {
  ami                    = local.ami_id
  instance_type          = var.instance_type
  subnet_id              = local.subnet_id
  key_name               = local.key_name
  vpc_security_group_ids = [aws_security_group.this.id]

  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }
  root_block_device {
    encrypted   = true
    volume_size = var.root_volume_size > 0 ? var.root_volume_size : null # null → AMI default size
    volume_type = var.root_volume_type
  }

  tags = {
    Name      = var.name
    ManagedBy = "AegisOps"
  }
}

output "instance_id" { value = aws_instance.this.id }
output "private_ip" { value = aws_instance.this.private_ip }
output "public_ip" { value = aws_instance.this.public_ip }
output "public_dns" { value = aws_instance.this.public_dns }
output "vpc_id" { value = data.aws_subnet.selected.vpc_id }
output "subnet_id" { value = local.subnet_id }
output "security_group_id" { value = aws_security_group.this.id }
output "ingress_ports" { value = var.ingress_ports }
output "allowed_cidr" { value = var.allowed_cidr }
output "admin_port" { value = var.allowed_cidr != "" ? local.admin_port : null }
output "ami_used" { value = local.ami_id }
output "login_user" { value = local.login_user }
output "key_name" { value = local.key_name }
# Sensitive: retrievable via `terraform output -raw private_key_pem`, never printed to logs/chat.
output "private_key_pem" {
  value     = var.create_key_pair ? tls_private_key.generated[0].private_key_pem : null
  sensitive = true
}
