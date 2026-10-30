#!/usr/bin/env bash
# Run OpenTofu in a stack with the credential names used in Doppler and GitHub.
#   iac/tofu.sh cloudflare init
#   iac/tofu.sh cloudflare plan
# Needs: TOFU_R2_ACCESS_KEY_ID, TOFU_R2_SECRET_ACCESS_KEY, TOFU_STATE_PASSPHRASE,
#        CLOUDFLARE_ACCOUNT_ID, and CLOUDFLARE_API_TOKEN or CLOUDFLARE_IAC_READ_TOKEN.
set -euo pipefail
stack="${1:?usage: iac/tofu.sh <stack> <tofu args...>}"
shift
dir="$(cd "$(dirname "$0")" && pwd)/$stack"
[ -d "$dir" ] || { echo "no stack at $dir" >&2; exit 1; }

: "${TOFU_R2_ACCESS_KEY_ID:?}" "${TOFU_R2_SECRET_ACCESS_KEY:?}" "${TOFU_STATE_PASSPHRASE:?}" "${CLOUDFLARE_ACCOUNT_ID:?}"
export AWS_ACCESS_KEY_ID="$TOFU_R2_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$TOFU_R2_SECRET_ACCESS_KEY"
export TF_VAR_state_passphrase="$TOFU_STATE_PASSPHRASE"
export TF_VAR_cloudflare_account_id="$CLOUDFLARE_ACCOUNT_ID"
# Local runs default to the read-only token; applies go through CI.
export CLOUDFLARE_API_TOKEN="${CLOUDFLARE_API_TOKEN:-${CLOUDFLARE_IAC_READ_TOKEN:?}}"

exec tofu -chdir="$dir" "$@"
