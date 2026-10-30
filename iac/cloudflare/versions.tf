terraform {
  # OpenTofu only: state encryption and early-evaluated variables in the
  # backend/encryption blocks are not available in Terraform.
  required_version = ">= 1.13.0, < 2.0.0"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "5.28.1"
    }
  }
}
