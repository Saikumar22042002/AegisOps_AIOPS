# MODSEED MS-10 B1/B2 gate — native `terraform test`, mocked providers (offline).
# Old-shape stored inputs (enable_ssm explicit false = the schema's B2 default) must render
# ZERO SSM/IAM resources and no instance profile — the exact pre-enhancement plan.

mock_provider "aws" {
  # The default-VPC subnet discovery indexes [0] — a bare mock returns an empty list.
  override_data {
    target = data.aws_subnets.default
    values = {
      ids = ["subnet-mock000000000001"]
    }
  }
}
mock_provider "tls" {}

variables {
  name             = "b1gate"
  instance_type    = "t3.micro"
  os               = "amazon-linux-2023"
  ami              = ""
  subnet_id        = ""
  region           = "us-east-1"
  key_name         = ""
  create_key_pair  = true
  root_volume_size = 0
  root_volume_type = "gp3"
  ingress_ports    = []
  allowed_cidr     = "10.0.0.0/16"
  enable_ssm       = false
  power_state      = ""
  extra_tags       = {}
}

run "b1_old_shape_renders_no_ssm_resources" {
  command = plan

  assert {
    condition     = length(aws_iam_role.ssm) == 0 && length(aws_iam_instance_profile.ssm) == 0
    error_message = "old inputs must not render any IAM role or instance profile"
  }
  assert {
    condition     = length(aws_ec2_instance_state.power) == 0
    error_message = "old inputs must not render a managed power state (MOD Option A)"
  }
  assert {
    condition     = length(aws_iam_role_policy_attachment.ssm_core) == 0 && length(aws_iam_role_policy_attachment.cloudwatch_agent) == 0
    error_message = "old inputs must not render policy attachments"
  }
  # (aws_instance.iam_instance_profile is optional+computed — unknown at plan under a mock
  #  when null. Zero profile resources above means nothing can be attached: the module wires
  #  it as one(values(aws_iam_instance_profile.ssm)[*].name), null by construction here.)
}

run "enable_ssm_renders_the_profile_chain" {
  command = plan

  variables {
    enable_ssm = true
  }

  assert {
    condition     = length(aws_iam_role.ssm) == 1 && length(aws_iam_instance_profile.ssm) == 1
    error_message = "enable_ssm must render the role + instance profile"
  }
  assert {
    condition     = aws_iam_role_policy_attachment.ssm_core["ssm"].policy_arn == "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
    error_message = "the SSM core managed policy must be attached"
  }
  assert {
    condition     = aws_iam_role_policy_attachment.cloudwatch_agent["ssm"].policy_arn == "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
    error_message = "the CloudWatch agent managed policy must be attached"
  }
}

run "mod_power_state_is_terraform_managed" {
  command = plan

  variables {
    power_state = "stopped"
  }

  assert {
    condition     = aws_ec2_instance_state.power["power"].state == "stopped"
    error_message = "a managed power state must render aws_ec2_instance_state (never an SDK call)"
  }
}

run "mod_extra_tags_merge" {
  command = plan

  variables {
    extra_tags = { env = "prod" }
  }

  assert {
    condition     = aws_instance.this.tags["env"] == "prod" && aws_instance.this.tags["ManagedBy"] == "AegisOps"
    error_message = "extra tags must merge without displacing the managed tags"
  }
}
