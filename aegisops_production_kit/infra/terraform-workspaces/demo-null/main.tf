# Cloud-credential-free workspace used to exercise the TerraformRunner end-to-end locally
# (init/validate/plan/apply/destroy) without any cloud provider. Uses the null + random
# providers only. Not used by production workflows.

terraform {
  required_version = ">= 1.6"
  required_providers {
    null   = { source = "hashicorp/null", version = "~> 3.2" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
  backend "local" {}
}

variable "resource_name" {
  type    = string
  default = "aegisops-demo"
}

variable "replica_count" {
  type    = number
  default = 3
}

resource "random_pet" "name" {
  prefix = var.resource_name
  length = 2
}

resource "null_resource" "demo" {
  count = var.replica_count
  triggers = {
    name = "${random_pet.name.id}-${count.index}"
  }
}

output "demo_name" {
  value = random_pet.name.id
}

output "replicas" {
  value = var.replica_count
}
