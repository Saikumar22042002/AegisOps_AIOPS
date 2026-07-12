# MODSEED MS-2 — azure.vnet: VNet + subnets + NAT gateway (static IP) + public/private route
# tables with name-based association. RG handling mirrors azure-vm (auto "<name>-rg" or an
# existing one). Deliberately NO NSG here — never a world-open admin rule; the VM module owns
# admin ingress via its allowed_cidr. No backend block (A3 injects backend config).

terraform {
  required_version = ">= 1.6"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 3.110" }
  }
}

provider "azurerm" {
  features {}
}

locals {
  rg_name = var.resource_group != "" ? var.resource_group : "${var.name}-rg"
}

resource "azurerm_resource_group" "this" {
  name     = local.rg_name
  location = var.location
  tags     = { ManagedBy = "AegisOps" }
}

resource "azurerm_virtual_network" "this" {
  name                = var.name
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location
  address_space       = [var.address_space]
  tags                = { ManagedBy = "AegisOps" }
}

resource "azurerm_subnet" "public" {
  count                = length(var.subnet_cidrs)
  name                 = "${var.name}-public-${count.index}"
  resource_group_name  = azurerm_resource_group.this.name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [var.subnet_cidrs[count.index]]
}

resource "azurerm_subnet" "private" {
  count                = length(var.private_subnet_cidrs)
  name                 = "${var.name}-private-${count.index}"
  resource_group_name  = azurerm_resource_group.this.name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [var.private_subnet_cidrs[count.index]]
}

# ── NAT gateway for private-subnet egress (only when private subnets exist) ──
resource "azurerm_public_ip" "nat" {
  count               = length(var.private_subnet_cidrs) > 0 ? 1 : 0
  name                = "${var.name}-nat-ip"
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location
  allocation_method   = "Static"
  sku                 = "Standard"
}

resource "azurerm_nat_gateway" "this" {
  count               = length(var.private_subnet_cidrs) > 0 ? 1 : 0
  name                = "${var.name}-nat"
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location
  sku_name            = "Standard"
}

resource "azurerm_nat_gateway_public_ip_association" "this" {
  count                = length(var.private_subnet_cidrs) > 0 ? 1 : 0
  nat_gateway_id       = azurerm_nat_gateway.this[0].id
  public_ip_address_id = azurerm_public_ip.nat[0].id
}

resource "azurerm_subnet_nat_gateway_association" "private" {
  count          = length(var.private_subnet_cidrs)
  subnet_id      = azurerm_subnet.private[count.index].id
  nat_gateway_id = azurerm_nat_gateway.this[0].id
}

# ── Route tables, associated by NAME per subnet tier ──
# No explicit default route: Azure system routes already send public egress to Internet; the
# tables exist (and are name-associated) so day-2 routes attach to the right tier.
resource "azurerm_route_table" "public" {
  name                = "${var.name}-rt-public"
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location
}

resource "azurerm_route_table" "private" {
  count               = length(var.private_subnet_cidrs) > 0 ? 1 : 0
  name                = "${var.name}-rt-private"
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location
  # Private egress flows through the NAT gateway association; routes can be added day-2.
}

resource "azurerm_subnet_route_table_association" "public" {
  count          = length(var.subnet_cidrs)
  subnet_id      = azurerm_subnet.public[count.index].id
  route_table_id = azurerm_route_table.public.id
}

resource "azurerm_subnet_route_table_association" "private" {
  count          = length(var.private_subnet_cidrs)
  subnet_id      = azurerm_subnet.private[count.index].id
  route_table_id = azurerm_route_table.private[0].id
}
