# Cache rules (http_request_cache_settings phase). The tunnel hosts are behind a login: the Google
# login, which the gateway checks on every request, or the app's own (ha, seerr, hair). A response
# cached at Cloudflare's edge would be served to the next visitor without that check, so nothing is
# cached. (The rule's description is left as is: changing it only rewrites the ruleset.)
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
