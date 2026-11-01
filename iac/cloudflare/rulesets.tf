# Cache rules (http_request_cache_settings phase). Everything behind the tunnel is behind the
# Google login, which the gateway checks on every request; a response cached at Cloudflare's
# edge would be served to the next visitor without that check. So nothing is cached.
resource "cloudflare_ruleset" "cache" {
  zone_id = local.zone_id
  name    = "default"
  kind    = "zone"
  phase   = "http_request_cache_settings"

  rules = [
    {
      description = "No edge cache: every response depends on the Google login"
      expression  = "true"
      action      = "set_cache_settings"
      action_parameters = {
        cache = false
      }
      enabled = true
    },
  ]
}
