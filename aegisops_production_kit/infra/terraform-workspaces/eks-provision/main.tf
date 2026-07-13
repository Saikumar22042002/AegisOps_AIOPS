# Real EKS provisioning workspace used by the CloudOps agent (eks-provision v3).
# Reuses an existing production VPC/subnets (discovered via cloud SDK reads) — no new VPC.
# Apply/Destroy only run after the human-approval interrupt in the LangGraph graph.
# MODSEED MS-11: eks_mode = standard | auto.
#   standard → the pre-enhancement managed-node-group path, byte-for-byte (B1/B2 default).
#   auto     → EKS Auto Mode: API authentication, compute_config with the general-purpose
#              node pool (the registry module wires the elastic-LB + block-storage configs
#              and attaches the auto-mode IAM policy set to the cluster role), no
#              self-managed bootstrap addons, no managed node groups.
# The approval card states the mode (see _eks_policy).

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

locals {
  auto_mode = var.eks_mode == "auto"

  # Auto Mode: the API authentication mode is mandatory; compute_config carries the
  # general-purpose pool. Standard: null leaves the module's pre-enhancement defaults
  # untouched (B1 — the rendered module inputs are identical to the old ones).
  authentication_mode = local.auto_mode ? "API" : null
  compute_config = local.auto_mode ? {
    enabled    = true
    node_pools = ["general-purpose"]
  } : null

  # Standard keeps the exact pre-enhancement node group; Auto Mode runs none.
  node_groups = local.auto_mode ? {} : {
    app = {
      desired_size   = var.desired_size
      min_size       = var.min_size
      max_size       = var.max_size
      instance_types = var.instance_types
      subnet_ids     = var.subnet_ids
    }
  }
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

  # MS-11 mode wiring (nulls fall back to the module's own defaults on the standard path).
  authentication_mode           = local.authentication_mode
  cluster_compute_config        = local.compute_config
  bootstrap_self_managed_addons = local.auto_mode ? false : null

  eks_managed_node_groups = local.node_groups

  tags = {
    Project     = "payments-platform"
    Environment = "production"
    ManagedBy   = "AegisOps"
  }
}
