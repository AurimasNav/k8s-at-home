# Old locally managed tunnel "sync.lt" (created 2023-02-18 with cloudflared tunnel create, down
# since that day; replaced by k3s above). Adopted here only so OpenTofu can delete it: the next PR
# removes this block, and the apply destroys the tunnel. Looked up by name, so its ID stays out of git.
data "cloudflare_zero_trust_tunnel_cloudflareds" "legacy" {
  account_id = var.cloudflare_account_id
  name       = "sync.lt"
  is_deleted = false
}

import {
  for_each = { for t in data.cloudflare_zero_trust_tunnel_cloudflareds.legacy.result : t.name => t.id }
  to       = cloudflare_zero_trust_tunnel_cloudflared.legacy[each.key]
  id       = "${var.cloudflare_account_id}/${each.value}"
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "legacy" {
  for_each = toset(["sync.lt"])

  account_id = var.cloudflare_account_id
  name       = each.key
  config_src = "local"
}
