output "keyring_id" {
  value = google_kms_key_ring.this.id
}

output "keyring_name" {
  value = google_kms_key_ring.this.name
}

output "key_ids" {
  value = [for k in google_kms_crypto_key.this : k.id]
}

output "key_names" {
  value = [for k in google_kms_crypto_key.this : k.name]
}

output "rotation_days" {
  value = var.rotation_days
}
