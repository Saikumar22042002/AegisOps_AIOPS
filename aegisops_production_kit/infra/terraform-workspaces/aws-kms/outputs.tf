output "key_id" {
  value = aws_kms_key.this.key_id
}

output "key_arn" {
  value = aws_kms_key.this.arn
}

output "alias" {
  value = aws_kms_alias.this.name
}

output "rotation_enabled" {
  value = aws_kms_key.this.enable_key_rotation
}

output "deletion_window_days" {
  value = aws_kms_key.this.deletion_window_in_days
}
