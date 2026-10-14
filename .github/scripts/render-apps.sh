#!/usr/bin/env bash
# Render every app ArgoCD deploys, with the same kustomize flags ArgoCD uses
# (see gitops/argocd/patch.configmap.yaml). Output: <outdir>/<app>.yaml
set -euo pipefail

out="${1:-rendered}"
flags=(--enable-helm --load-restrictor LoadRestrictionsNone)
mkdir -p "$out"

# The root app is the source of truth for what is deployed.
kustomize build "${flags[@]}" argocd-apps/base > "$out/argocd-apps.yaml"
mapfile -t apps < <(grep -oP '^\s+path: \Kgitops/\S+' "$out/argocd-apps.yaml" | sort -u)
echo "Deployed apps: ${#apps[@]}"

failed=()
for app in "${apps[@]}"; do
  name="${app#gitops/}"
  if [[ ! -f "$app/kustomization.yaml" ]]; then
    echo "::error file=argocd-apps/base::Application points to $app, which has no kustomization.yaml"
    failed+=("$name"); continue
  fi
  if ! kustomize build "${flags[@]}" "$app" > "$out/$name.yaml" 2> "$out/$name.err"; then
    echo "::error file=$app/kustomization.yaml::kustomize build failed: $(head -c 500 "$out/$name.err")"
    failed+=("$name")
  fi
  rm -f "$out/$name.err"
done

if (( ${#failed[@]} )); then
  echo "Render failed for: ${failed[*]}"
  exit 1
fi
echo "Rendered ${#apps[@]} apps + root app into $out/"
