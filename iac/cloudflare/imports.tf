# One-time adoption of the DNS records created in the dashboard, matched by name
# (record IDs are looked up, not committed). Records tofu created itself (comment
# "OpenTofu ...", already in state) are skipped. Remove this file after the apply.
# (The API token imports that were here ran with PR #872.)
data "cloudflare_dns_records" "existing" {
  zone_id   = local.zone_id
  max_items = 1000
}

locals {
  adoptable_records = [
    for r in data.cloudflare_dns_records.existing.result : r
    if !startswith(coalesce(r.comment, "-"), "OpenTofu")
  ]
}

import {
  for_each = {
    for r in local.adoptable_records : r.name => r.id
    if r.type == "A" && r.name == var.zone_name
  }
  to = cloudflare_dns_record.apex
  id = "${local.zone_id}/${each.value}"
}

import {
  for_each = {
    for r in local.adoptable_records : trimsuffix(r.name, ".${var.zone_name}") => r.id
    if r.type == "CNAME" && contains(keys(local.cname_records), trimsuffix(r.name, ".${var.zone_name}"))
  }
  to = cloudflare_dns_record.cname[each.key]
  id = "${local.zone_id}/${each.value}"
}
