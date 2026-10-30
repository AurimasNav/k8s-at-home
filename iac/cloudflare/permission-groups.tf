# Cloudflare API permission groups by name, so token policies list names instead of IDs.
#   local.permission_group_id["zone"]["DNS Read"]  => "82e64a83756745bbbb1c9c2701bf816b"
# Names are unique within a scope, but the same name can exist in both scopes with
# different IDs (e.g. "Access: Apps and Policies Read"), so the lookup is per scope.
data "cloudflare_account_api_token_permission_groups_list" "all" {
  account_id = var.cloudflare_account_id
}

locals {
  permission_scopes = {
    account = "com.cloudflare.api.account"
    zone    = "com.cloudflare.api.account.zone"
  }

  permission_group_id = {
    for scope, urn in local.permission_scopes : scope => {
      for g in data.cloudflare_account_api_token_permission_groups_list.all.result : g.name => g.id
      if contains(g.scopes, urn)
    }
  }
}
