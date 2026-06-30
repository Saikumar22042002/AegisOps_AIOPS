# Azure Storage Account (org-approved template: azure/storage).
terraform {
  required_version = ">= 1.6"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 3.110" }
  }
  backend "local" {}
}

provider "azurerm" {
  features {}
}

variable "account_name" { type = string }
variable "resource_group" { type = string }
variable "location" {
  type    = string
  default = "eastus"
}
variable "account_tier" {
  type    = string
  default = "Standard"
}
variable "replication" {
  type    = string
  default = "LRS"
}

resource "azurerm_storage_account" "this" {
  name                            = var.account_name
  resource_group_name             = var.resource_group
  location                        = var.location
  account_tier                    = var.account_tier
  account_replication_type        = var.replication
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false

  tags = {
    ManagedBy = "AegisOps"
  }
}

output "storage_account_id" { value = azurerm_storage_account.this.id }
output "primary_blob_endpoint" { value = azurerm_storage_account.this.primary_blob_endpoint }
