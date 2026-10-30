# DNS records in the sync.lt zone. The other hostnames still live only in the
# dashboard; adopt them here with import blocks (scripts/generate-imports.ps1).
# Every app hostname is a CNAME to the apex, which holds the home IP.

# Cleanuparr web UI (nginx Ingress + oauth2-proxy); cert-manager needs it for HTTP-01.
resource "cloudflare_dns_record" "cleanuparr" {
  zone_id = local.zone_id
  name    = "cleanuparr.sync.lt"
  type    = "CNAME"
  content = "sync.lt"
  ttl     = 1
  proxied = false
  comment = "OpenTofu iac/cloudflare: gitops/cleanuparr"
}
