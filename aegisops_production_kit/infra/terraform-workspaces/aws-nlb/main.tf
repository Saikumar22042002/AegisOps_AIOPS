# MODSEED MS-3 — aws.nlb: network load balancer (cross-zone) + TCP target group with TCP
# health checks + TCP listener. When no security groups are given, an EGRESS-ONLY SG is
# created (no ingress rules here — never a world-open admin surface; targets attach their own
# SGs). Placement (vpc_id + subnets) comes from the DEP resolver: an existing aws.vpc's
# recorded outputs, or a create-first DAG. No backend block (A3 injects).

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.60" }
  }
}

provider "aws" {
  region = var.region
}

locals {
  make_sg = length(var.security_group_ids) == 0
}

# Egress-only security group (outbound only — an NLB's client traffic is governed by the
# listeners; this SG deliberately carries ZERO ingress rules).
resource "aws_security_group" "egress_only" {
  count       = local.make_sg ? 1 : 0
  name        = "${var.name}-nlb-egress"
  description = "AegisOps egress-only SG for NLB ${var.name} (zero ingress rules)"
  vpc_id      = var.vpc_id
  tags        = { ManagedBy = "aegisops", Name = "${var.name}-nlb-egress" }

  egress {
    description = "Outbound default route (health checks, target traffic)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"] # EGRESS default route — outbound only, never an ingress rule
  }
}

resource "aws_lb" "this" {
  name                             = var.name
  load_balancer_type               = "network"
  internal                         = var.internal
  subnets                          = var.subnets
  security_groups                  = local.make_sg ? [aws_security_group.egress_only[0].id] : var.security_group_ids
  enable_cross_zone_load_balancing = true
  enable_deletion_protection       = var.deletion_protection
  tags                             = { ManagedBy = "aegisops" }
}

resource "aws_lb_target_group" "this" {
  name        = "${var.name}-tg"
  port        = var.target_port
  protocol    = "TCP"
  vpc_id      = var.vpc_id
  target_type = "instance"

  health_check {
    protocol            = "TCP"
    port                = "traffic-port"
    interval            = 30
    healthy_threshold   = 3
    unhealthy_threshold = 3
  }

  tags = { ManagedBy = "aegisops" }
}

resource "aws_lb_listener" "this" {
  load_balancer_arn = aws_lb.this.arn
  port              = var.listener_port
  protocol          = "TCP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.this.arn
  }
}
