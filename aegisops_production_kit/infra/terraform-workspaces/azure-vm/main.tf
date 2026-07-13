# Azure VM (org-approved template: azure/vm). Self-contained: resource group + network + NSG +
# public IP + VM. Linux (Ubuntu/Debian, generated SSH key) AND Windows Server (generated admin
# password) are supported — matching what the platform actually allows (Phase 8 / N-05).
# Admin access (SSH 22 / RDP 3389) is opened ONLY to var.allowed_cidr; empty = closed (N-02).
# All credentials are sensitive outputs — revealed once via the product UI, never logged.
terraform {
  required_version = ">= 1.6"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 3.110" }
    tls     = { source = "hashicorp/tls", version = "~> 4.0" }
    random  = { source = "hashicorp/random", version = "~> 3.6" }
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
# B/D/E-series and other sizes the subscription allows (validated upstream as Standard_*/Basic_*).
variable "size" {
  type    = string
  default = "Standard_B1s"
}
variable "admin_username" {
  type    = string
  default = "azureuser"
}
# ubuntu-22.04 | ubuntu-24.04 | debian-12 | windows-2022
variable "os" {
  type    = string
  default = "ubuntu-22.04"
}
variable "resource_group" {
  type    = string
  default = "" # created as "<name>-rg" when empty (default-RG semantics, like the portal)
}
variable "ingress_ports" {
  type    = list(number)
  default = []
}
# Source CIDR allowed to reach SSH/RDP (e.g. requester's IP as x.x.x.x/32). Empty = closed.
variable "allowed_cidr" {
  type    = string
  default = ""
}

locals {
  rg_name    = var.resource_group != "" ? var.resource_group : "${var.name}-rg"
  is_windows = var.os == "windows-2022"
  admin_port = local.is_windows ? 3389 : 22
  images = {
    "ubuntu-22.04" = { publisher = "Canonical", offer = "0001-com-ubuntu-server-jammy", sku = "22_04-lts-gen2" }
    "ubuntu-24.04" = { publisher = "Canonical", offer = "ubuntu-24_04-lts", sku = "server" }
    "debian-12"    = { publisher = "Debian", offer = "debian-12", sku = "12-gen2" }
    "windows-2022" = { publisher = "MicrosoftWindowsServer", offer = "WindowsServer", sku = "2022-datacenter-g2" }
  }
  img = lookup(local.images, var.os, local.images["ubuntu-22.04"])
}

# Linux credential: generated SSH key (sensitive). Windows credential: generated password (sensitive).
resource "tls_private_key" "ssh" {
  count     = local.is_windows ? 0 : 1
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "random_password" "windows_admin" {
  count            = local.is_windows ? 1 : 0
  length           = 20
  special          = true
  override_special = "!@#%*-_=+"
  min_upper        = 2
  min_lower        = 2
  min_numeric      = 2
  min_special      = 1
}

resource "azurerm_resource_group" "this" {
  name     = local.rg_name
  location = var.location
  tags     = { ManagedBy = "AegisOps" }
}

# MS-13 (B4, BY DESIGN): the azure.vm→vnet DEP slot fills existing_subnet_id from a known
# azure.vnet — the dedicated vnet+subnet are then skipped and the VM lands in the existing
# network. `moved` blocks migrate the old unkeyed addresses so existing state re-plans as a
# no-op rename.
variable "existing_subnet_id" {
  type        = string
  default     = ""
  description = "Subnet id of an existing azure.vnet (filled by the DEP slot); empty keeps the module-created '<name>-vnet' (old behavior)."
}

locals {
  use_existing_net = var.existing_subnet_id != ""
}

moved {
  from = azurerm_virtual_network.this
  to   = azurerm_virtual_network.this[0]
}

moved {
  from = azurerm_subnet.this
  to   = azurerm_subnet.this[0]
}

resource "azurerm_virtual_network" "this" {
  count               = local.use_existing_net ? 0 : 1
  name                = "${var.name}-vnet"
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location
  address_space       = ["10.10.0.0/16"]
}

resource "azurerm_subnet" "this" {
  count                = local.use_existing_net ? 0 : 1
  name                 = "${var.name}-subnet"
  resource_group_name  = azurerm_resource_group.this.name
  virtual_network_name = azurerm_virtual_network.this[0].name
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

  # Admin access (SSH/RDP) — ONLY from the user's declared CIDR; no rule when closed (N-02).
  dynamic "security_rule" {
    for_each = var.allowed_cidr != "" ? [var.allowed_cidr] : []
    content {
      name                       = "aegisops-admin-${local.admin_port}"
      priority                   = 1001
      direction                  = "Inbound"
      access                     = "Allow"
      protocol                   = "Tcp"
      source_port_range          = "*"
      destination_port_range     = tostring(local.admin_port)
      source_address_prefix      = security_rule.value
      destination_address_prefix = "*"
    }
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
    subnet_id                     = local.use_existing_net ? var.existing_subnet_id : azurerm_subnet.this[0].id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.this.id
  }
}

resource "azurerm_network_interface_security_group_association" "this" {
  network_interface_id      = azurerm_network_interface.this.id
  network_security_group_id = azurerm_network_security_group.this.id
}

resource "azurerm_linux_virtual_machine" "this" {
  count                 = local.is_windows ? 0 : 1
  name                  = var.name
  resource_group_name   = azurerm_resource_group.this.name
  location              = var.location
  size                  = var.size
  admin_username        = var.admin_username
  network_interface_ids = [azurerm_network_interface.this.id]

  admin_ssh_key {
    username   = var.admin_username
    public_key = tls_private_key.ssh[0].public_key_openssh
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

resource "azurerm_windows_virtual_machine" "this" {
  count                 = local.is_windows ? 1 : 0
  name                  = var.name
  resource_group_name   = azurerm_resource_group.this.name
  location              = var.location
  size                  = var.size
  admin_username        = var.admin_username
  admin_password        = random_password.windows_admin[0].result
  network_interface_ids = [azurerm_network_interface.this.id]

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

output "vm_id" {
  value = local.is_windows ? azurerm_windows_virtual_machine.this[0].id : azurerm_linux_virtual_machine.this[0].id
}
output "public_ip" { value = azurerm_public_ip.this.ip_address }
output "private_ip" { value = azurerm_network_interface.this.private_ip_address }
output "login_user" { value = var.admin_username }
output "os_kind" { value = local.is_windows ? "windows" : "linux" }
output "admin_port" { value = var.allowed_cidr != "" ? local.admin_port : null }
output "allowed_cidr" { value = var.allowed_cidr }
output "key_name" { value = local.is_windows ? null : "${var.name}-ssh (generated)" }
output "resource_group" { value = azurerm_resource_group.this.name }
output "ingress_ports" { value = var.ingress_ports }
# Sensitive credentials — revealed once via the product UI, never logged or persisted plaintext.
output "private_key_pem" {
  value     = local.is_windows ? null : tls_private_key.ssh[0].private_key_pem
  sensitive = true
}
output "admin_password" {
  value     = local.is_windows ? random_password.windows_admin[0].result : null
  sensitive = true
}
