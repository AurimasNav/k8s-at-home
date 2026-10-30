variable "cloudflare_account_id" {
  description = "Cloudflare account ID (TF_VAR_cloudflare_account_id). Used for the R2 state endpoint and account-level resources."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[0-9a-f]{32}$", var.cloudflare_account_id))
    error_message = "Must be the 32-character hex Cloudflare account ID."
  }
}

variable "state_passphrase" {
  description = "Passphrase that encrypts state and plans (TF_VAR_state_passphrase, Doppler TOFU_STATE_PASSPHRASE)."
  type        = string
  sensitive   = true
  nullable    = false

  validation {
    condition     = length(var.state_passphrase) >= 32
    error_message = "Use at least 32 characters (e.g. openssl rand -base64 32)."
  }
}

variable "zone_name" {
  description = "DNS zone managed by this stack."
  type        = string
  default     = "sync.lt"
}

variable "home_public_ipv4" {
  description = "Public IPv4 of home, the target of the apex A record (TF_VAR_home_public_ipv4, Doppler HOME_PUBLIC_IPV4). Kept out of git; masked in plans."
  type        = string
  sensitive   = true
  nullable    = false

  validation {
    condition     = can(cidrhost("${var.home_public_ipv4}/32", 0))
    error_message = "Must be an IPv4 address."
  }
}
