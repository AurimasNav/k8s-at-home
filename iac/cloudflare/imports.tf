# One-time adoption of the tokens created in the dashboard, matched by name so no
# token ID is committed (the ID doubles as the token's R2 access key ID).
# Remove this file in the PR after the first successful apply.
data "cloudflare_account_tokens" "existing" {
  account_id = var.cloudflare_account_id
}

import {
  for_each = {
    for t in data.cloudflare_account_tokens.existing.result : t.name => t.id
    if contains(keys(local.account_tokens), t.name)
  }
  to = cloudflare_account_token.this[each.key]
  id = "${var.cloudflare_account_id}/${each.value}"
}
