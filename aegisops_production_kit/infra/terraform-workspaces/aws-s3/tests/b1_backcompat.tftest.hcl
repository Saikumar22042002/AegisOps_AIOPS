# MOD B1 gate (aws-s3) — native `terraform test`, mocked provider (offline).
# Old-shape stored inputs render the exact pre-MOD plan: no lifecycle configuration,
# managed tags only. Lifecycle expiry is ALWAYS an explicit user choice (data loss).

mock_provider "aws" {}

variables {
  bucket_name           = "b1gate-bucket"
  region                = "us-east-1"
  versioning            = true
  block_public          = true
  lifecycle_expire_days = 0
  extra_tags            = {}
}

run "b1_old_shape_renders_no_lifecycle" {
  command = plan

  assert {
    condition     = length(aws_s3_bucket_lifecycle_configuration.this) == 0
    error_message = "old inputs must not render a lifecycle configuration"
  }
  assert {
    condition     = length(keys(aws_s3_bucket.this.tags)) == 1 && aws_s3_bucket.this.tags["ManagedBy"] == "AegisOps"
    error_message = "old inputs carry only the managed tag"
  }
}

run "mod_lifecycle_expiry_renders_when_chosen" {
  command = plan

  variables {
    lifecycle_expire_days = 30
  }

  assert {
    condition     = one(aws_s3_bucket_lifecycle_configuration.this["expire"].rule[*].expiration[0].days) == 30
    error_message = "the chosen expiry must render as the aegisops-expire rule"
  }
  assert {
    condition     = one(aws_s3_bucket_lifecycle_configuration.this["expire"].rule[*].status) == "Enabled"
    error_message = "the lifecycle rule must be enabled"
  }
}

run "mod_versioning_off_and_tags" {
  command = plan

  variables {
    versioning = false
    extra_tags = { env = "prod" }
  }

  assert {
    condition     = one(aws_s3_bucket_versioning.this.versioning_configuration[*].status) == "Suspended"
    error_message = "versioning off must render Suspended"
  }
  assert {
    condition     = aws_s3_bucket.this.tags["env"] == "prod" && aws_s3_bucket.this.tags["ManagedBy"] == "AegisOps"
    error_message = "extra tags must merge without displacing the managed tag"
  }
}
