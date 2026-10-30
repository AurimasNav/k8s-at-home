#!/usr/bin/env bash
# Generate HCL and import blocks for resources that already exist in Cloudflare.
# Output goes to iac/cloudflare/generated/ (git-ignored) for review; nothing is
# written to state. Move the reviewed blocks into the stack under readable
# names, then plan: the imports are applied by CI like any other change.
#
#   iac/cloudflare/scripts/generate-imports.sh                      # DNS records
#   iac/cloudflare/scripts/generate-imports.sh cloudflare_ruleset   # other zone resources
#   RESOURCE_IDS=cloudflare_zone_setting=ssl,always_use_https,min_tls_version \
#     iac/cloudflare/scripts/generate-imports.sh cloudflare_zone_setting  # settings need IDs
#
# Needs cf-terraforming and tofu on PATH, CLOUDFLARE_API_TOKEN (read is enough)
# and CLOUDFLARE_ACCOUNT_ID.
set -euo pipefail
types="${1:-cloudflare_dns_record}"
zone_name="${ZONE_NAME:-sync.lt}"
: "${CLOUDFLARE_API_TOKEN:?}" "${CLOUDFLARE_ACCOUNT_ID:?}"

stack="$(cd "$(dirname "$0")/.." && pwd)"
out="$stack/generated"
mkdir -p "$out"

zone_id="$(curl -fsS -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  "https://api.cloudflare.com/client/v4/zones?name=$zone_name&account.id=$CLOUDFLARE_ACCOUNT_ID" |
  python3 -c 'import json,sys; r=json.load(sys.stdin)["result"]; print(r[0]["id"] if r else "")')"
[ -n "$zone_id" ] || { echo "zone $zone_name not found in the account" >&2; exit 1; }

# cf-terraforming reads provider schemas from an initialised working directory.
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cp "$stack/versions.tf" "$stack/.terraform.lock.hcl" "$work/"
tofu -chdir="$work" init -backend=false -input=false -lockfile=readonly >/dev/null

common=(--zone "$zone_id" --token "$CLOUDFLARE_API_TOKEN" --resource-type "$types"
  --terraform-install-path "$work" --terraform-binary-path "$(command -v tofu)")
if [ -n "${RESOURCE_IDS:-}" ]; then common+=(--resource-id "$RESOURCE_IDS"); fi
cf-terraforming generate "${common[@]}" > "$out/${types//,/_}.tf"
cf-terraforming import --modern-import-block "${common[@]}" > "$out/${types//,/_}_imports.tf"
echo "Wrote $out/${types//,/_}.tf and ${types//,/_}_imports.tf for review."
