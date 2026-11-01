# DNS for sync.lt. Every record, the apex included, is a proxied (orange-cloud) CNAME to the
# Cloudflare Tunnel (tunnel.tf): visitors reach Cloudflare, and no record carries the home IP.
# The apex CNAME is flattened by Cloudflare. TLS still ends in the cluster (Cilium gateway,
# wildcard certificate from cert-manager); the tunnel only carries the traffic there.
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
  type    = "CNAME"
  content = local.tunnel_cname
  ttl     = 1 # automatic
  proxied = true
  comment = "OpenTofu iac/cloudflare: gitops/seerr, via tunnel k3s"
}

# A hostname must also be in local.tunnel_hostnames (tunnel.tf), which gives it its ingress rule.
resource "cloudflare_dns_record" "cname" {
  for_each = local.hostnames

  zone_id = local.zone_id
  name    = "${each.key}.${var.zone_name}"
  type    = "CNAME"
  content = local.tunnel_cname
  ttl     = 1 # automatic
  proxied = true
  comment = "OpenTofu iac/cloudflare: ${each.value}, via tunnel k3s"

  lifecycle {
    precondition {
      condition     = contains(local.tunnel_hostnames, each.key)
      error_message = "Add ${each.key} to local.tunnel_hostnames (tunnel.tf) too: without an ingress rule the tunnel answers 404."
    }
  }
}
