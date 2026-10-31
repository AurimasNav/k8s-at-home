# Token "cert-manager": DNS-01 challenges for the sync.lt certificates (cert-manager
# ClusterIssuer). Only the managed zone: find it, and create/delete the
# _acme-challenge TXT records. The value goes to Doppler (doppler.tf).
locals {
  cert_manager_token = {
    managed_zone = [
      "DNS Write",
      "Zone Read",
    ]
  }
}
