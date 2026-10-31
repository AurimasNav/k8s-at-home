# Account-owned API tokens. Permissions are listed by name in account-token-*.tf.
# Token values are never in code, outputs or logs. Imported tokens have no value in
# state (Cloudflare only returns it on creation); tokens created here have it in the
# encrypted state, and doppler.tf copies the ones the cluster needs into Doppler.
locals {
  account_tokens = {
    "iac-reader"   = local.iac_reader_token
    "iac"          = local.iac_token
    "cert-manager" = local.cert_manager_token
  }

  # Policy resources: the whole account, every zone in it, or only the managed zone.
  token_resources = {
    account = jsonencode({ "com.cloudflare.api.account.${var.cloudflare_account_id}" = "*" })
    zone = jsonencode({
      "com.cloudflare.api.account.${var.cloudflare_account_id}" = { "com.cloudflare.api.account.zone.*" = "*" }
    })
    managed_zone = jsonencode({ "com.cloudflare.api.account.zone.${local.zone_id}" = "*" })
  }

  # Permission catalog (permission-groups.tf) that each resources key takes its names from.
  token_permission_scope = {
    account      = "account"
    zone         = "zone"
    managed_zone = "zone"
  }

  # Permission names that do not exist in the catalog (typo, or renamed by Cloudflare).
  unknown_permissions = {
    for token, scopes in local.account_tokens : token => flatten([
      for scope, perms in scopes : [
        for name in perms : "${scope}: ${name}"
        if !contains(keys(local.permission_group_id[local.token_permission_scope[scope]]), name)
      ]
    ])
  }
}

resource "cloudflare_account_token" "this" {
  for_each = local.account_tokens

  account_id = var.cloudflare_account_id
  name       = each.key

  # The provider stores policies sorted by resources and permission groups sorted by ID
  # (cloudflare/terraform-provider-cloudflare#7125). Emitting the same order keeps plans
  # clean after import and after the API reorders them.
  policies = [
    for scope in sort([for s, perms in each.value : s if length(perms) > 0]) : {
      effect = "allow"
      permission_groups = [
        for id in sort([
          for name in each.value[scope] : local.permission_group_id[local.token_permission_scope[scope]][name]
          if contains(keys(local.permission_group_id[local.token_permission_scope[scope]]), name)
        ]) : { id = id }
      ]
      resources = local.token_resources[scope]
    }
  ]

  lifecycle {
    # Replacing a token changes its value and breaks whatever uses it.
    prevent_destroy = true

    precondition {
      condition     = length(local.unknown_permissions[each.key]) == 0
      error_message = "Unknown permission group(s) for token ${each.key}: ${join(", ", local.unknown_permissions[each.key])}"
    }
  }
}
