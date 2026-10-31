# DNS for sync.lt. The apex A record points at home; every other hostname is a CNAME
# to the apex, so a new public IP only changes one record. All records are "DNS only"
# (not proxied): TLS ends in the cluster (cert-manager + ingress-nginx / cilium gateway).
#
# Add a hostname to local.hostnames (key = hostname, value = what serves it; it becomes
# the record comment, so the dashboard shows which records are managed here). Records in
# the zone that are not listed are left alone (tofu only manages what is in its state).

locals {
  # Served by apps in the cluster (gitops/<app> Ingress or HTTPRoute) or kept on purpose.
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
    radarr-ru   = "gitops/radarr-ru"
    seerr       = "gitops/seerr"
    sonarr      = "gitops/sonarr"
    sonarr-ru   = "gitops/sonarr-ru"
    vrising     = "not deployed"
  }

  # Served by nothing any more. Adopted only so their deletion goes through a
  # reviewed plan: the follow-up PR empties this list, which deletes them.
  stale_hostnames = toset([
    "huntarr",
    "ombi",
    "radarr-no4k",
    "radarr1",
    "radarr2",
    "radarr3",
    "sonarr1",
    "sonarr2",
    "sonarr3",
  ])

  cname_records = merge(
    { for h in local.stale_hostnames : h => "stale, to be deleted" },
    local.hostnames,
  )
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
  for_each = local.cname_records

  zone_id = local.zone_id
  name    = "${each.key}.${var.zone_name}"
  type    = "CNAME"
  content = var.zone_name
  ttl     = 1 # automatic
  proxied = false
  comment = "OpenTofu iac/cloudflare: ${each.value}"
}

# Created on its own by #878: same record, new address (no delete and re-create).
moved {
  from = cloudflare_dns_record.cleanuparr
  to   = cloudflare_dns_record.cname["cleanuparr"]
}
