output "lb_arn" {
  value = aws_lb.this.arn
}

output "lb_dns_name" {
  value = aws_lb.this.dns_name
}

output "target_group_arn" {
  value = aws_lb_target_group.this.arn
}

output "listener_arn" {
  value = aws_lb_listener.this.arn
}

# Honest next step, recorded with the resource: an NLB without targets serves nothing.
output "attach_targets_note" {
  value = "No targets are attached yet — register instances/IPs with target group ${aws_lb_target_group.this.arn} (port ${var.target_port})."
}
