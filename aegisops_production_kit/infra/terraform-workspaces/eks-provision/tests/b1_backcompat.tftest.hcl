# MODSEED MS-11 B1/B2 gate — native `terraform test` with the registry module OVERRIDDEN
# (its internals are the upstream project's own tested product; what WE own is the mode
# wiring, asserted here through the root locals). Offline, no creds.

mock_provider "aws" {}

override_module {
  target = module.eks
  outputs = {
    cluster_name            = "mock-cluster"
    cluster_endpoint        = "https://mock.eks.local"
    cluster_arn             = "arn:aws:eks:us-east-1:000000000000:cluster/mock-cluster"
    eks_managed_node_groups = {}
  }
}

variables {
  region             = "us-east-1"
  cluster_name       = "b1gate"
  kubernetes_version = "1.29"
  vpc_id             = "vpc-mock0000000000001"
  subnet_ids         = ["subnet-mock000000000001", "subnet-mock000000000002"]
  instance_types     = ["m6i.xlarge"]
  desired_size       = 3
  min_size           = 3
  max_size           = 6
  eks_mode           = "standard"
}

run "b1_standard_mode_is_the_exact_old_wiring" {
  command = plan

  assert {
    condition     = local.authentication_mode == null && local.compute_config == null
    error_message = "standard mode must leave auth/compute at the module's own defaults (null = unset)"
  }
  assert {
    condition     = keys(local.node_groups) == tolist(["app"])
    error_message = "standard mode must keep the pre-enhancement 'app' node group"
  }
  assert {
    condition     = local.node_groups["app"].desired_size == 3 && local.node_groups["app"].instance_types == tolist(["m6i.xlarge"])
    error_message = "the node group must carry the old inputs verbatim"
  }
}

run "auto_mode_wires_api_auth_and_the_general_purpose_pool" {
  command = plan

  variables {
    eks_mode = "auto"
  }

  assert {
    condition     = local.authentication_mode == "API"
    error_message = "auto mode must force the API authentication mode"
  }
  assert {
    condition     = local.compute_config.enabled == true && local.compute_config.node_pools == ["general-purpose"]
    error_message = "auto mode must enable compute_config with the general-purpose pool"
  }
  assert {
    condition     = length(local.node_groups) == 0
    error_message = "auto mode must run no managed node groups"
  }
}
