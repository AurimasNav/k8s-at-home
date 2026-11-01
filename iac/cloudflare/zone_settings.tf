# Zone settings. They only act on proxied (orange-cloud) traffic, i.e. the hostnames served
# through the tunnel (tunnel.tf); DNS-only records never pass Cloudflare's proxy.
locals {
  zone_settings = {
    # http:// at the edge answers a redirect to https://; the tunnel only carries https.
    always_use_https = "on"
    # Browsers to the edge: TLS 1.2 or newer (the gateway itself is TLS 1.2+).
    min_tls_version = "1.2"
    # Edge to a proxied origin other than the tunnel: full TLS with a valid certificate.
    # No such origin today; this keeps "flexible" (plain http to the origin) from ever applying.
    ssl = "strict"
  }
}

resource "cloudflare_zone_setting" "this" {
  for_each = local.zone_settings

  zone_id    = local.zone_id
  setting_id = each.key
  value      = each.value
}
