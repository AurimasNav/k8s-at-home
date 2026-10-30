# DNS for sync.lt. The apex A record points at home; every other hostname is a CNAME
# to the apex, so a new public IP only changes one record. All records are "DNS only"
# (not proxied): TLS ends in the cluster (cert-manager + ingress-nginx / cilium gateway).
#
# Add a hostname to local.hostnames (key = hostname, value = what serves it; it becomes
# the record comment, so the dashboard shows which records are managed here). Removing a
# hostname deletes its record. Records in the zone that are not listed are left alone
# (tofu only manages what is in its state).

locals {
  # Served by apps in the cluster (gitops/<app> Ingress or HTTPRoute) or kept on purpose.
  # In-cluster-only apps (radarr-ru, sonarr-ru) get no record, public or LAN.
  hostnames = {
    argocd      = "gitops/argocd"
    assistant   = "gitops/home-assistant"
    cleanuparr  = "gitops/cleanuparr"
    flood       = "not deployed"
    grafana     = "gitops/victoria-metrics-k8s-stack"
    hair        = "gitops/hairsera"
    hermes      = "gitops/hermes-agent"
    home        = "gitops/homepage"
    homebox     = "gitops/homebox"
    login       = "gitops/oauth2-proxy"
    minecraft   = "not deployed"
    prowlarr    = "gitops/prowlarr"
    qb          = "not deployed"
    qbittorrent = "gitops/qbittorrent"
    radarr      = "gitops/radarr"
    seerr       = "gitops/seerr"
    sonarr      = "gitops/sonarr"
    vrising     = "not deployed"
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

resource "cloudflare_dns_record" "cname" {
  for_each = local.hostnames

  zone_id = local.zone_id
  name    = "${each.key}.${var.zone_name}"
  type    = "CNAME"
  content = var.zone_name
  ttl     = 1 # automatic
  proxied = false
  comment = "OpenTofu iac/cloudflare: ${each.value}"
}
