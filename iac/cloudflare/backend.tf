# State lives in a Cloudflare R2 bucket (S3 API), locked with a lock file
# (conditional writes, no DynamoDB) and encrypted client-side before upload.
# The bucket and its access keys are created by hand once: see ../README.md.
terraform {
  backend "s3" {
    bucket = "tofu-state"
    key    = "cloudflare/terraform.tfstate"
    region = "auto"
    endpoints = {
      s3 = "https://${var.cloudflare_account_id}.r2.cloudflarestorage.com"
    }
    use_lockfile = true

    # R2 is not AWS: skip the AWS-only calls and checksums.
    skip_credentials_validation = true
    skip_metadata_api_check     = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    skip_s3_checksum            = true
    use_path_style              = true
  }

  # State and saved plans are unreadable without TOFU_STATE_PASSPHRASE, even
  # with access to the bucket. Losing the passphrase loses the state (it can be
  # rebuilt with import blocks, see ../README.md).
  encryption {
    key_provider "pbkdf2" "state" {
      passphrase = var.state_passphrase
    }
    method "aes_gcm" "state" {
      keys = key_provider.pbkdf2.state
    }
    state {
      method   = method.aes_gcm.state
      enforced = true
    }
    plan {
      method   = method.aes_gcm.state
      enforced = true
    }
  }
}
