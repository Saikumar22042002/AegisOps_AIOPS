# Real EKS provisioning workspace used by the CloudOps agent (eks-provision v3).
# Reuses an existing production VPC/subnets (discovered via cloud SDK reads) — no new VPC.
# Apply/Destroy only run after the human-approval interrupt in the LangGraph graph.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }
  # Local backend by default; configure S3+DynamoDB via -backend-config for remote state.
  backend "local" {}
}

provider "aws" {
  region = var.region
}

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.8"

  cluster_name    = var.cluster_name
  cluster_version = var.kubernetes_version

  # Reuse the existing production VPC + private subnets.
  vpc_id     = var.vpc_id
  subnet_ids = var.subnet_ids

  # Hardening per the EKS Production Hardening runbook.
  cluster_endpoint_public_access  = false
  cluster_endpoint_private_access = true

  cluster_encryption_config = {
    resources = ["secrets"]
  }

  enable_irsa = true

  eks_managed_node_groups = {
    app = {
      desired_size   = var.desired_size
      min_size       = var.min_size
      max_size       = var.max_size
      instance_types = var.instance_types
      subnet_ids     = var.subnet_ids
    }
  }

  tags = {
    Project     = "payments-platform"
    Environment = "production"
    ManagedBy   = "AegisOps"
  }
}
