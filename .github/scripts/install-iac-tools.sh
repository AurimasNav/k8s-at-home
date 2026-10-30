#!/usr/bin/env bash
# Installs OpenTofu and tflint into ~/bin, verifying release checksums.
# Versions come from TOFU_VERSION / TFLINT_VERSION (pinned in .github/workflows/iac.yaml).
set -euo pipefail
: "${TOFU_VERSION:?}" "${TFLINT_VERSION:?}"

bin="$HOME/bin"
mkdir -p "$bin"
work="$(mktemp -d)"
cd "$work"
dl() { curl -fsSL --retry 3 -o "$2" "$1"; }

f="tofu_${TOFU_VERSION}_linux_amd64.tar.gz"
base="https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}"
dl "$base/$f" "$f"
dl "$base/tofu_${TOFU_VERSION}_SHA256SUMS" tofu.sum
grep " $f\$" tofu.sum | sha256sum -c -
tar xzf "$f" -C "$bin" tofu

f="tflint_linux_amd64.zip"
base="https://github.com/terraform-linters/tflint/releases/download/v${TFLINT_VERSION}"
dl "$base/$f" "$f"
dl "$base/checksums.txt" tflint.sum
grep " $f\$" tflint.sum | sha256sum -c -
unzip -q -o "$f" tflint -d "$bin"

if [ -n "${GITHUB_PATH:-}" ]; then echo "$bin" >> "$GITHUB_PATH"; fi
# Print versions without pipes: "| head" would SIGPIPE under pipefail.
"$bin/tofu" version > "$work/tofu.version"
"$bin/tflint" --version > "$work/tflint.version"
sed -n 1p "$work/tofu.version"
sed -n 1p "$work/tflint.version"
