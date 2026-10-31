# Token comes from CLOUDFLARE_API_TOKEN: "iac-reader" for plans, "iac" for applies.
# Both are defined in account-token-*.tf.
provider "cloudflare" {}

# Token comes from DOPPLER_TOKEN: the "k3s" service token (k8s-at-home/prod, read/write),
# set only in the apply job. PR plans and the drift check have no Doppler token and skip the
# Doppler resources with -exclude (see .github/workflows/iac.yaml).
provider "doppler" {}
