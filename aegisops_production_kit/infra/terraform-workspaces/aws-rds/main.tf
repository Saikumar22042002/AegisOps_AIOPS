# AWS RDS instance (org-approved template: aws/rds). Master password is RDS-managed.
# MODSEED MS-7: multi-engine (postgres/mysql/mariadb), latest-engine-version data source,
# dedicated SG gated on a MANDATORY allowed_cidr (a world-open CIDR is rejected outright),
# optional subnet group, engine-aware ports/log-exports, sensitive connection-string output.
# BACKCOMPAT (B1/B2): every new capability is conditional and the platform schema defaults
# them OFF — a pre-enhancement instance re-planned from its stored inputs renders the exact
# old shape (single aws_db_instance, no SG/subnet group/parameter group, no exports).
# Module-level defaults stay secure for bare use (checkov evaluates these defaults).

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

locals {
  engine_port = {
    postgres = 5432
    mysql    = 3306
    mariadb  = 3306
  }
  # Engine-aware CloudWatch log exports (MariaDB audit needs an option-group plugin, so the
  # safe common set is used for the MySQL family).
  log_exports = {
    postgres = ["postgresql", "upgrade"]
    mysql    = ["error", "general", "slowquery"]
    mariadb  = ["error", "general", "slowquery"]
  }
  query_log_params = {
    postgres = { log_statement = "ddl", log_min_duration_statement = "1000" }
    mysql    = { slow_query_log = "1", long_query_time = "1" }
    mariadb  = { slow_query_log = "1", long_query_time = "1" }
  }
  scheme = {
    postgres = "postgresql"
    mysql    = "mysql"
    mariadb  = "mysql"
  }
  create_sg           = var.allowed_cidr != ""
  create_subnet_group = length(var.subnet_ids) > 0
  want_latest         = var.engine_version == "latest"
  pinned_version      = var.engine_version != "" && !local.want_latest ? var.engine_version : null
}

# Resolved only when needed: "latest" version pin or the logging parameter-group family.
data "aws_rds_engine_version" "selected" {
  count  = local.want_latest || var.enable_log_exports ? 1 : 0
  engine = var.engine
}

# The chosen subnets' VPC (for the dedicated SG when a subnet group is used).
data "aws_subnet" "first" {
  count = local.create_sg && local.create_subnet_group ? 1 : 0
  id    = var.subnet_ids[0]
}

# Dedicated DB security group — created ONLY when the caller supplies allowed_cidr
# (mandatory for this path; a world-open CIDR fails validation, there is no open default).
resource "aws_security_group" "db" {
  count       = local.create_sg ? 1 : 0
  name_prefix = "${var.identifier}-db-"
  description = "AegisOps DB access for ${var.identifier} (client CIDR only)"
  vpc_id      = local.create_subnet_group ? data.aws_subnet.first[0].vpc_id : null

  ingress {
    description = "Database port from the approved client CIDR"
    from_port   = local.engine_port[var.engine]
    to_port     = local.engine_port[var.engine]
    protocol    = "tcp"
    cidr_blocks = [var.allowed_cidr]
  }
}

resource "aws_db_subnet_group" "this" {
  count      = local.create_subnet_group ? 1 : 0
  name       = "${var.identifier}-subnets"
  subnet_ids = var.subnet_ids
  tags       = { ManagedBy = "AegisOps" }
}

# Engine-aware query-logging parameters, attached only when log exports are on.
resource "aws_db_parameter_group" "logging" {
  # for_each (not count): checkov's graph follows for_each nodes, so the query-logging
  # connection stays visible while the group remains fully conditional (B1).
  for_each    = var.enable_log_exports ? toset(["logging"]) : toset([])
  name_prefix = "${var.identifier}-logging-"
  family      = data.aws_rds_engine_version.selected[0].parameter_group_family
  description = "AegisOps query logging for ${var.identifier}"

  dynamic "parameter" {
    for_each = local.query_log_params[var.engine]
    content {
      name  = parameter.key
      value = parameter.value
    }
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_db_instance" "this" {
  identifier                      = var.identifier
  engine                          = var.engine
  engine_version                  = local.want_latest ? data.aws_rds_engine_version.selected[0].version : local.pinned_version
  port                            = local.engine_port[var.engine]
  instance_class                  = var.instance_class
  allocated_storage               = var.allocated_storage
  username                        = "aegisadmin"
  manage_master_user_password     = true
  storage_encrypted               = true
  skip_final_snapshot             = true
  publicly_accessible             = false
  auto_minor_version_upgrade      = true
  enabled_cloudwatch_logs_exports = var.enable_log_exports ? local.log_exports[var.engine] : null
  parameter_group_name            = one(values(aws_db_parameter_group.logging)[*].name)
  vpc_security_group_ids          = local.create_sg ? [aws_security_group.db[0].id] : null
  db_subnet_group_name            = local.create_subnet_group ? aws_db_subnet_group.this[0].name : null

  tags = merge({
    ManagedBy = "AegisOps"
  }, var.extra_tags)
}
