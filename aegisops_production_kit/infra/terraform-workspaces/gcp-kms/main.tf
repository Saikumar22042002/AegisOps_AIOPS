# MODSEED MS-6 — gcp.kms: key ring + crypto key(s) (90-day rotation, ENCRYPT_DECRYPT,
# SOFTWARE protection) + encrypter/decrypter IAM members. Keys, never secret values.
# HONEST DELETION SEMANTICS: GCP key rings are NOT deletable — destroying this module removes
# crypto-key versions and IAM bindings only; the ring name stays reserved in the project.
# No backend block (A3 injects).

terraform {
  required_version = ">= 1.6"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 5.40" }
  }
}

provider "google" {
  project = var.project
  region  = var.region
}

locals {
  key_names       = length(var.keys) > 0 ? var.keys : ["${var.name}-key"]
  rotation_period = "${var.rotation_days * 86400}s"
}

resource "google_kms_key_ring" "this" {
  name     = var.name
  location = var.region
}

resource "google_kms_crypto_key" "this" {
  for_each        = toset(local.key_names)
  name            = each.value
  key_ring        = google_kms_key_ring.this.id
  purpose         = "ENCRYPT_DECRYPT"
  rotation_period = local.rotation_period

  version_template {
    algorithm        = "GOOGLE_SYMMETRIC_ENCRYPTION"
    protection_level = "SOFTWARE"
  }

  labels = { managed_by = "aegisops" }
}

resource "google_kms_crypto_key_iam_member" "encrypter_decrypter" {
  for_each = {
    for pair in setproduct(local.key_names, var.encrypter_decrypters) :
    "${pair[0]}|${pair[1]}" => { key = pair[0], member = pair[1] }
  }
  crypto_key_id = google_kms_crypto_key.this[each.value.key].id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = each.value.member
}
