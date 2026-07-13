# MODSEED MS-13 B1/B2 gate — native `terraform test`, mocked provider (offline).
# Old-shape stored inputs (schema-explicit B2 defaults: everything off) must render the
# EXACT pre-enhancement plan: RG + cluster only — no workspace, no oms_agent, no
# network_profile, Azure Policy off.

mock_provider "azurerm" {}

variables {
  name                 = "b1gate"
  location             = "eastus"
  node_count           = 2
  node_size            = "Standard_B2s"
  kubernetes_version   = ""
  resource_group       = ""
  enable_monitoring    = false
  network_policy       = ""
  azure_policy_enabled = false
}

run "b1_old_shape_renders_exactly_the_old_plan" {
  command = plan

  assert {
    condition     = length(azurerm_log_analytics_workspace.aks) == 0
    error_message = "old inputs must not render a Log Analytics workspace"
  }
  assert {
    condition     = length(azurerm_kubernetes_cluster.this.oms_agent) == 0
    error_message = "old inputs must not render an OMS agent"
  }
  assert {
    condition     = length(azurerm_kubernetes_cluster.this.network_profile) == 0
    error_message = "old inputs must not render a network profile"
  }
  assert {
    condition     = azurerm_kubernetes_cluster.this.azure_policy_enabled == false
    error_message = "old inputs keep Azure Policy off (the provider default)"
  }
  assert {
    condition     = azurerm_kubernetes_cluster.this.role_based_access_control_enabled == true
    error_message = "the RBAC pin stays"
  }
}

run "monitoring_renders_workspace_plus_oms" {
  command = plan

  variables {
    enable_monitoring = true
  }

  assert {
    condition     = length(azurerm_log_analytics_workspace.aks) == 1 && azurerm_log_analytics_workspace.aks["monitoring"].sku == "PerGB2018"
    error_message = "monitoring must render the Log Analytics workspace"
  }
  assert {
    condition     = length(azurerm_kubernetes_cluster.this.oms_agent) == 1
    error_message = "monitoring must wire the OMS agent"
  }
}

run "calico_network_policy_renders" {
  command = plan

  variables {
    network_policy = "calico"
  }

  assert {
    condition     = one(azurerm_kubernetes_cluster.this.network_profile[*]).network_policy == "calico" && one(azurerm_kubernetes_cluster.this.network_profile[*]).network_plugin == "kubenet"
    error_message = "calico must render on the kubenet plugin"
  }
}

run "azure_policy_addon_renders" {
  command = plan

  variables {
    azure_policy_enabled = true
  }

  assert {
    condition     = azurerm_kubernetes_cluster.this.azure_policy_enabled == true
    error_message = "the Azure Policy add-on must render when asked"
  }
}
