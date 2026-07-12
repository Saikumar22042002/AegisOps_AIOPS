output "vnet_id" {
  value = azurerm_virtual_network.this.id
}

output "vnet_name" {
  value = azurerm_virtual_network.this.name
}

output "resource_group" {
  value = azurerm_resource_group.this.name
}

output "subnet_ids" {
  value = azurerm_subnet.public[*].id
}

output "private_subnet_ids" {
  value = azurerm_subnet.private[*].id
}

output "subnet_cidrs" {
  value = var.subnet_cidrs
}

output "nat_enabled" {
  value = length(var.private_subnet_cidrs) > 0
}
