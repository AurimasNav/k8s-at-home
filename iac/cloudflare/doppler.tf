# Secrets this stack creates go straight into Doppler; the cluster reads them through
# ExternalSecrets (ClusterSecretStore doppler-secret-store, k8s-at-home/prod).
# Doppler key = the env/secret key name in the cluster.
#
# Only the apply job has a Doppler token, so PR plans and the weekly drift check exclude
# these resources (TOFU_PLAN_EXCLUDE in .github/workflows/iac.yaml); the apply job summary
# shows them. Rotating a token here updates its Doppler key in the same apply.
locals {
  doppler_project = "k8s-at-home"
  doppler_config  = "prod"

  # Account token name (account-tokens.tf) => Doppler key.
  doppler_account_tokens = {
    "cert-manager" = "CLOUDFLARE_DNS_API_TOKEN"
  }
}

resource "doppler_secret" "account_token" {
  for_each = local.doppler_account_tokens

  project = local.doppler_project
  config  = local.doppler_config
  name    = each.value
  value   = cloudflare_account_token.this[each.key].value
}
