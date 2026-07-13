# MODSEED MS-13 B4 slot gate (azure.vm→vnet) — native `terraform test`, mocked providers.
# Old-shape stored inputs render the module-created vnet+subnet exactly as before (with
# `moved` blocks covering the count-index migration for existing state); a slot-filled
# existing_subnet_id skips both and lands the NIC in the existing network.

mock_provider "azurerm" {}
mock_provider "tls" {}
mock_provider "random" {}

variables {
  name               = "b1gate"
  location           = "eastus"
  size               = "Standard_B1s"
  os                 = "ubuntu-22.04"
  resource_group     = ""
  ingress_ports      = []
  allowed_cidr       = ""
  existing_subnet_id = ""
}

run "b1_old_shape_creates_its_own_vnet" {
  command = plan

  assert {
    condition     = length(azurerm_virtual_network.this) == 1 && length(azurerm_subnet.this) == 1
    error_message = "old inputs must keep creating the dedicated vnet + subnet"
  }
}

run "slot_filled_subnet_skips_the_dedicated_network" {
  command = plan

  variables {
    existing_subnet_id = "/subscriptions/x/resourceGroups/net-rg/providers/Microsoft.Network/virtualNetworks/prod-vnet/subnets/app"
  }

  assert {
    condition     = length(azurerm_virtual_network.this) == 0 && length(azurerm_subnet.this) == 0
    error_message = "an existing subnet must skip the dedicated vnet + subnet entirely"
  }
  assert {
    condition     = one(azurerm_network_interface.this.ip_configuration[*]).subnet_id == "/subscriptions/x/resourceGroups/net-rg/providers/Microsoft.Network/virtualNetworks/prod-vnet/subnets/app"
    error_message = "the NIC must land in the existing subnet"
  }
}
