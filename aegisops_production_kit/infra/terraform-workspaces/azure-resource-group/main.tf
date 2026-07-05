# Azure Resource Group (org-approved template: azure/resource_group).
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

resource "azurerm_resource_group" "this" {
  name     = var.name
  location = var.location
  tags = {
    ManagedBy = "AegisOps"
  }
}

output "resource_group_id" { value = azurerm_resource_group.this.id }
