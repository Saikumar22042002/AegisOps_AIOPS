# Azure Kubernetes Service (org-approved template: azure/aks). RG + managed cluster with a
# system-assigned identity. kube_config is a sensitive output (never logged).
terraform {
  required_version = ">= 1.6"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 3.110" }
  }
  backend "local" {}
}

provider "azurerm" {
  features {}
  skip_provider_registration = true
}

variable "name" { type = string }
variable "location" {
  type    = string
  default = "eastus"
}
variable "node_count" {
  type    = number
  default = 2
}
variable "node_size" {
  type    = string
  default = "Standard_B2s"
}
variable "kubernetes_version" {
  type    = string
  default = "" # empty → AKS default
}
variable "resource_group" {
  type    = string
  default = ""
}

# MODSEED MS-13: Log Analytics + OMS agent, network_policy=calico, azure_policy_enabled —
# all variable-driven. BACKCOMPAT (B1/B2): the platform schema defaults every option OFF
# (existing clusters re-plan unchanged); the module's own defaults are the observable/
# governed ones (monitoring on, calico, Azure Policy on).
variable "enable_monitoring" {
  type        = bool
  default     = true
  description = "Log Analytics workspace + OMS agent. Secure by default here; the platform schema defaults this OFF (B2)."
}

variable "network_policy" {
  type        = string
  default     = "calico"
  description = "Kubernetes network policy engine. \"\" leaves the pre-enhancement rendering (no network_profile block)."

  validation {
    condition     = contains(["", "calico", "azure"], var.network_policy)
    error_message = "network_policy must be empty, calico, or azure."
  }
}

variable "azure_policy_enabled" {
  type        = bool
  default     = true
  description = "AKS Azure Policy add-on. Schema defaults OFF (B2)."
}

locals {
  rg_name = var.resource_group != "" ? var.resource_group : "${var.name}-rg"
}

resource "azurerm_resource_group" "this" {
  name     = local.rg_name
  location = var.location
  tags     = { ManagedBy = "AegisOps" }
}

resource "azurerm_log_analytics_workspace" "aks" {
  for_each            = var.enable_monitoring ? toset(["monitoring"]) : toset([])
  name                = "${var.name}-logs"
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = { ManagedBy = "AegisOps" }
}

resource "azurerm_kubernetes_cluster" "this" {
  name                = var.name
  location            = var.location
  resource_group_name = azurerm_resource_group.this.name
  dns_prefix          = var.name
  kubernetes_version  = var.kubernetes_version != "" ? var.kubernetes_version : null
  # Explicit form of the azurerm default (tfsec AVD-AZU-0042) — zero plan impact.
  role_based_access_control_enabled = true
  azure_policy_enabled              = var.azure_policy_enabled

  default_node_pool {
    name       = "default"
    node_count = var.node_count
    vm_size    = var.node_size
  }

  identity {
    type = "SystemAssigned"
  }

  dynamic "oms_agent" {
    for_each = var.enable_monitoring ? [1] : []
    content {
      log_analytics_workspace_id = azurerm_log_analytics_workspace.aks["monitoring"].id
    }
  }

  dynamic "network_profile" {
    for_each = var.network_policy != "" ? [1] : []
    content {
      network_plugin = "kubenet"
      network_policy = var.network_policy
    }
  }

  tags = { ManagedBy = "AegisOps" }
}

output "cluster_id" { value = azurerm_kubernetes_cluster.this.id }
output "fqdn" { value = azurerm_kubernetes_cluster.this.fqdn }
output "node_count" { value = var.node_count }
output "resource_group" { value = azurerm_resource_group.this.name }
output "kube_config" {
  value     = azurerm_kubernetes_cluster.this.kube_config_raw
  sensitive = true
}
output "log_analytics_workspace_id" {
  value = one(values(azurerm_log_analytics_workspace.aks)[*].id)
}
output "network_policy" { value = var.network_policy }
output "azure_policy_enabled" { value = var.azure_policy_enabled }
