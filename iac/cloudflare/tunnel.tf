# Cloudflare Tunnel "k3s": public hostnames reach the cluster through an outbound-only
# connector (gitops/cloudflared) instead of the router's 80/443 port forward.
#
# Remotely managed: the ingress rules below live in Cloudflare, so the connector needs only
# its token (Doppler TUNNEL_TOKEN, doppler.tf). Every rule points at the Cilium gateway, which
# keeps doing the routing, the wildcard TLS certificate and the Google login (ExternalAuth); the
# tunnel only replaces the path from the internet to the gateway.
#
# Moving a hostname to the tunnel:
#   1. add it to local.tunnel_hostnames: ingress rule here, and dns.tf turns its record into a
#      proxied CNAME to the tunnel in the same apply;
#   2. gitops: attach its HTTPRoute to the gateway listener https-tunnel, and allow its backend in
#      gitops/cloudflared/networkpolicy.yaml;
#   3. after verifying through Cloudflare, drop the route's https-wildcard parent (home IP stops
#      serving it).
locals {
  tunnel_hostnames = toset([
    "cleanuparr",
  ])

  # In-cluster address of the gateway's tunnel-only listener (https-tunnel, port 10443; the router
  # forwards only 80/443, so routes attached only there are not reachable on the home IP).
  tunnel_origin = "https://cilium-gateway-main.cilium-gateway.svc.cluster.local:10443"
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "k3s" {
  account_id = var.cloudflare_account_id
  name       = "k3s"
  # "cloudflare": ingress rules come from the config resource below, not from a local file.
  config_src = "cloudflare"
  # No tunnel_secret: Cloudflare generates it; the connector token is read back below.
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "k3s" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.k3s.id

  config = {
    ingress = concat(
      [
        for h in sort(tolist(local.tunnel_hostnames)) : {
          hostname = "${h}.${var.zone_name}"
          service  = local.tunnel_origin
          # The gateway picks the certificate and the route by SNI and Host. Send the public
          # hostname as SNI so TLS is verified against the real certificate (no noTLSVerify).
          origin_request = {
            origin_server_name = "${h}.${var.zone_name}"
          }
        }
      ],
      # Anything else that reaches the tunnel is refused.
      [{ service = "http_status:404" }],
    )
  }
}

# The connector token. Reading it needs "Cloudflare Tunnel Write", so only the apply job
# (write token) can; PR plans and the drift check exclude it (TOFU_PLAN_EXCLUDE).
data "cloudflare_zero_trust_tunnel_cloudflared_token" "k3s" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.k3s.id
}
