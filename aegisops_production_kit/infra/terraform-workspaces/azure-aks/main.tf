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

locals {
  rg_name = var.resource_group != "" ? var.resource_group : "${var.name}-rg"
}

resource "azurerm_resource_group" "this" {
  name     = local.rg_name
  location = var.location
  tags     = { ManagedBy = "AegisOps" }
}

resource "azurerm_kubernetes_cluster" "this" {
  name                = var.name
  location            = var.location
  resource_group_name = azurerm_resource_group.this.name
  dns_prefix          = var.name
  kubernetes_version  = var.kubernetes_version != "" ? var.kubernetes_version : null
  # Explicit form of the azurerm default (tfsec AVD-AZU-0042) — zero plan impact.
  role_based_access_control_enabled = true

  default_node_pool {
    name       = "default"
    node_count = var.node_count
    vm_size    = var.node_size
  }

  identity {
    type = "SystemAssigned"
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
