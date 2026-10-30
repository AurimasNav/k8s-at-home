# The zone is looked up, not managed: deleting or re-creating it from here
# would take every hostname offline. Records and settings refer to local.zone_id.
data "cloudflare_zone" "main" {
  filter = {
    name = var.zone_name
    account = {
      id = var.cloudflare_account_id
    }
  }
}

locals {
  zone_id = data.cloudflare_zone.main.zone_id
}
