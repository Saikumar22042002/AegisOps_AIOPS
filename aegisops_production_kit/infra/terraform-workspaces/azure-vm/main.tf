# Azure Linux VM (org-approved template: azure/vm). Self-contained: resource group + network +
# NSG + public IP + VM. A usable SSH key is guaranteed (generated here; private key surfaced as a
# sensitive output, never logged). Day-2-modifiable inbound ports via the NSG.
terraform {
  required_version = ">= 1.6"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 3.110" }
    tls     = { source = "hashicorp/tls", version = "~> 4.0" }
  }
  backend "local" {}
}

provider "azurerm" {
  features {}
  # RPs are pre-registered out-of-band; the SP may lack */register/action, so skip auto-registration.
  skip_provider_registration = true
}

variable "name" { type = string }
variable "location" {
  type    = string
  default = "eastus"
}
variable "size" {
  type    = string
  default = "Standard_B1s"
}
variable "admin_username" {
  type    = string
  default = "azureuser"
}
# ubuntu-22.04 | ubuntu-24.04
variable "os" {
  type    = string
  default = "ubuntu-22.04"
}
variable "resource_group" {
  type    = string
  default = "" # created as "<name>-rg" when empty
}
variable "ingress_ports" {
  type    = list(number)
  default = []
}

locals {
  rg_name = var.resource_group != "" ? var.resource_group : "${var.name}-rg"
  images = {
    "ubuntu-22.04" = { publisher = "Canonical", offer = "0001-com-ubuntu-server-jammy", sku = "22_04-lts-gen2" }
    "ubuntu-24.04" = { publisher = "Canonical", offer = "ubuntu-24_04-lts", sku = "server" }
  }
  img = lookup(local.images, var.os, local.images["ubuntu-22.04"])
}

resource "tls_private_key" "ssh" {
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "azurerm_resource_group" "this" {
  name     = local.rg_name
  location = var.location
  tags     = { ManagedBy = "AegisOps" }
}

resource "azurerm_virtual_network" "this" {
  name                = "${var.name}-vnet"
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location
  address_space       = ["10.10.0.0/16"]
}

resource "azurerm_subnet" "this" {
  name                 = "${var.name}-subnet"
  resource_group_name  = azurerm_resource_group.this.name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = ["10.10.1.0/24"]
}

resource "azurerm_public_ip" "this" {
  name                = "${var.name}-pip"
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location
  allocation_method   = "Static"
  sku                 = "Standard"
}

resource "azurerm_network_security_group" "this" {
  name                = "${var.name}-nsg"
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location

  security_rule {
    name                       = "SSH"
    priority                   = 1001
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "22"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }

  dynamic "security_rule" {
    for_each = { for i, p in var.ingress_ports : tostring(p) => { idx = i, port = p } }
    content {
      name                       = "port-${security_rule.value.port}"
      priority                   = 1100 + security_rule.value.idx
      direction                  = "Inbound"
      access                     = "Allow"
      protocol                   = "Tcp"
      source_port_range          = "*"
      destination_port_range     = tostring(security_rule.value.port)
      source_address_prefix      = "*"
      destination_address_prefix = "*"
    }
  }
}

resource "azurerm_network_interface" "this" {
  name                = "${var.name}-nic"
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location

  ip_configuration {
    name                          = "ipconfig"
    subnet_id                     = azurerm_subnet.this.id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.this.id
  }
}

resource "azurerm_network_interface_security_group_association" "this" {
  network_interface_id      = azurerm_network_interface.this.id
  network_security_group_id = azurerm_network_security_group.this.id
}

resource "azurerm_linux_virtual_machine" "this" {
  name                  = var.name
  resource_group_name   = azurerm_resource_group.this.name
  location              = var.location
  size                  = var.size
  admin_username        = var.admin_username
  network_interface_ids = [azurerm_network_interface.this.id]

  admin_ssh_key {
    username   = var.admin_username
    public_key = tls_private_key.ssh.public_key_openssh
  }

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
  }

  source_image_reference {
    publisher = local.img.publisher
    offer     = local.img.offer
    sku       = local.img.sku
    version   = "latest"
  }

  tags = { ManagedBy = "AegisOps" }
}

output "vm_id" { value = azurerm_linux_virtual_machine.this.id }
output "public_ip" { value = azurerm_public_ip.this.ip_address }
output "private_ip" { value = azurerm_network_interface.this.private_ip_address }
output "login_user" { value = var.admin_username }
output "key_name" { value = "${var.name}-ssh (generated)" }
output "resource_group" { value = azurerm_resource_group.this.name }
output "ingress_ports" { value = var.ingress_ports }
output "private_key_pem" {
  value     = tls_private_key.ssh.private_key_pem
  sensitive = true
}
