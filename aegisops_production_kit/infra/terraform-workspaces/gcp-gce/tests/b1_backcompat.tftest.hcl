# MODSEED MS-12 B1/B2 gate — native `terraform test`, mocked providers (offline).
# Old-shape stored inputs (schema-explicit B2 defaults: public IP ON, everything else off,
# network "default") must render the EXACT pre-enhancement plan.

mock_provider "google" {}
mock_provider "tls" {}

variables {
  name                   = "b1gate"
  project                = "demo-project"
  region                 = "us-central1"
  zone                   = "us-central1-a"
  machine_type           = "e2-micro"
  os                     = "debian-12"
  ssh_user               = "aegis"
  ingress_ports          = []
  allowed_cidr           = ""
  network                = "default"
  public_ip              = true
  enable_shielded        = false
  block_project_ssh_keys = false
  enable_oslogin         = false
  spot                   = false
  service_account_email  = ""
}

run "b1_old_shape_renders_exactly_the_old_plan" {
  command = plan

  assert {
    condition     = length(one(google_compute_instance.this.network_interface[*]).access_config) == 1
    error_message = "old inputs keep the ephemeral public IP"
  }
  assert {
    condition     = one(google_compute_instance.this.network_interface[*]).network == "default"
    error_message = "old inputs keep the default network"
  }
  assert {
    condition     = length(google_compute_instance.this.shielded_instance_config) == 0
    error_message = "old inputs must not render a shielded config"
  }
  assert {
    condition     = length(google_compute_instance.this.scheduling) == 0
    error_message = "old inputs must not render a scheduling (spot) block"
  }
  assert {
    condition     = length(google_compute_instance.this.service_account) == 0
    error_message = "old inputs must not render a service account"
  }
  assert {
    condition     = length(keys(google_compute_instance.this.metadata)) == 1 && contains(keys(google_compute_instance.this.metadata), "ssh-keys")
    error_message = "old inputs carry ONLY the generated ssh key in metadata"
  }
}

run "secure_module_defaults_shield_and_block_keys" {
  command = plan

  variables {
    public_ip              = false
    enable_shielded        = true
    block_project_ssh_keys = true
  }

  assert {
    condition     = length(one(google_compute_instance.this.network_interface[*]).access_config) == 0
    error_message = "module secure default: no public IP"
  }
  assert {
    condition     = one(google_compute_instance.this.shielded_instance_config[*]).enable_secure_boot == true && one(google_compute_instance.this.shielded_instance_config[*]).enable_vtpm == true
    error_message = "shielded VM must render secure boot + vTPM"
  }
  assert {
    condition     = google_compute_instance.this.metadata["block-project-ssh-keys"] == "true"
    error_message = "project-wide SSH keys must be blocked"
  }
}

run "spot_renders_preemptible_no_restart" {
  command = plan

  variables {
    spot = true
  }

  assert {
    condition     = one(google_compute_instance.this.scheduling[*]).preemptible == true && one(google_compute_instance.this.scheduling[*]).automatic_restart == false
    error_message = "spot must render preemptible with no automatic restart"
  }
  assert {
    condition     = one(google_compute_instance.this.scheduling[*]).provisioning_model == "SPOT"
    error_message = "spot must use the SPOT provisioning model"
  }
}

run "oslogin_and_dedicated_service_account" {
  command = plan

  variables {
    enable_oslogin        = true
    service_account_email = "least-scope@demo-project.iam.gserviceaccount.com"
  }

  assert {
    condition     = google_compute_instance.this.metadata["enable-oslogin"] == "TRUE"
    error_message = "OS Login must render its metadata flag"
  }
  assert {
    condition     = one(google_compute_instance.this.service_account[*]).scopes == toset(["logging-write", "monitoring-write"])
    error_message = "the dedicated service account must carry ONLY the least scopes"
  }
}

run "dep_slot_network_flows_to_instance_and_firewalls" {
  command = plan

  variables {
    network       = "prod-network"
    ingress_ports = [8080]
    allowed_cidr  = "10.9.0.0/16"
  }

  assert {
    condition     = one(google_compute_instance.this.network_interface[*]).network == "prod-network"
    error_message = "the slot-filled network must place the instance"
  }
  assert {
    condition     = google_compute_firewall.ingress[0].network == "prod-network" && google_compute_firewall.admin[0].network == "prod-network"
    error_message = "BOTH firewalls must follow the same network"
  }
}
