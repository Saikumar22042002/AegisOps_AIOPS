output "endpoint" { value = aws_db_instance.this.endpoint }
output "identifier" { value = aws_db_instance.this.identifier }
output "engine" { value = aws_db_instance.this.engine }
output "port" { value = aws_db_instance.this.port }

output "security_group_id" {
  value = local.create_sg ? aws_security_group.db[0].id : null
}

output "master_user_secret_arn" {
  description = "Secrets Manager ARN of the RDS-managed master password (the value itself is never surfaced)."
  value       = one(aws_db_instance.this.master_user_secret[*].secret_arn)
}

output "connection_string" {
  description = "Engine-aware DSN without credentials — the password stays RDS-managed in Secrets Manager."
  value       = format("%s://aegisadmin@%s/%s", local.scheme[var.engine], aws_db_instance.this.endpoint, var.engine == "postgres" ? "postgres" : "")
  sensitive   = true
}
