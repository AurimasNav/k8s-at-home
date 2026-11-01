# DNS for sync.lt. The apex A record points at home; every other hostname is a CNAME
# to the apex, so a new public IP only changes one record, except hostnames served through the
# Cloudflare Tunnel (tunnel.tf), which are proxied CNAMEs to the tunnel. TLS ends in the cluster
# either way (Cilium gateway, wildcard certificate from cert-manager).
#
# Add a hostname to local.hostnames (key = hostname, value = what serves it; it becomes
# the record comment, so the dashboard shows which records are managed here). Removing a
# hostname deletes its record. Records in the zone that are not listed are left alone
# (tofu only manages what is in its state).

locals {
  # Served by apps in the cluster (gitops/<app> HTTPRoute).
  hostnames = {
    cleanuparr  = "gitops/cleanuparr"
    grafana     = "gitops/victoria-metrics-k8s-stack"
    hair        = "gitops/hairsera"
    ha          = "gitops/home-assistant"
    hermes      = "gitops/hermes-agent"
    home        = "gitops/homepage"
    homebox     = "gitops/homebox"
    login       = "gitops/oauth2-proxy"
    prowlarr    = "gitops/prowlarr"
    qbittorrent = "gitops/qbittorrent"
    radarr      = "gitops/radarr"
    radarr-lt   = "gitops/radarr-lt"
    radarr-ru   = "gitops/radarr-ru"
    seerr       = "gitops/seerr"
    sonarr      = "gitops/sonarr"
    sonarr-lt   = "gitops/sonarr-lt"
    sonarr-ru   = "gitops/sonarr-ru"
  }
}

resource "cloudflare_dns_record" "apex" {
  zone_id = local.zone_id
  name    = var.zone_name
  type    = "A"
  content = var.home_public_ipv4
  ttl     = 1 # automatic
  proxied = false
  comment = "OpenTofu iac/cloudflare: home IP"
}

# Hostnames in local.tunnel_hostnames (tunnel.tf) point at the Cloudflare Tunnel and are proxied
# (orange cloud): visitors reach Cloudflare, never the home IP. The rest stay DNS-only CNAMEs to
# the apex.
resource "cloudflare_dns_record" "cname" {
  for_each = local.hostnames

  zone_id = local.zone_id
  name    = "${each.key}.${var.zone_name}"
  type    = "CNAME"
  content = contains(local.tunnel_hostnames, each.key) ? "${cloudflare_zero_trust_tunnel_cloudflared.k3s.id}.cfargotunnel.com" : var.zone_name
  ttl     = 1 # automatic
  proxied = contains(local.tunnel_hostnames, each.key)
  comment = "OpenTofu iac/cloudflare: ${each.value}${contains(local.tunnel_hostnames, each.key) ? ", via tunnel k3s" : ""}"
}
