# 0003 — Cloudflare as code: OpenTofu, state in R2

- **Status:** **Proposed** 2026-10-30
- **Scope:** Cloudflare settings for `sync.lt` (DNS records, zone settings, later rules) managed from
  git instead of the dashboard. Other non-Kubernetes resources can follow the same pattern.
- **Related:** `iac/`, `.github/workflows/iac.yaml`, `gitops/cloudflared`

## Context

- New apps need DNS records (e.g. `radarr-ru`, `sonarr-ru`). Today they are added by hand in the
  dashboard, with no review and no history.
- The repo is public. Cloudflare tokens, R2 keys and the state must never reach git or Actions logs.
- Budget: free. The repo already uses Doppler for secrets and GitHub Actions for checks.

## Decision

| Part | Pick | Why |
|---|---|---|
| Tool | **OpenTofu** | Open-source (MPL), client-side state encryption, `import` blocks, works with the official Cloudflare provider |
| Provider | `cloudflare/cloudflare` v5, exact pin + lock file | v5 is the supported line; Renovate updates pin and lock from the OpenTofu registry |
| State | **Cloudflare R2**, S3 backend, `use_lockfile` | Free tier is far above our use, S3-compatible, conditional writes give locking without DynamoDB, same vendor as the resources |
| State protection | OpenTofu **state + plan encryption** (PBKDF2 passphrase, AES-GCM), enforced | The bucket only holds ciphertext; a leaked R2 key does not leak the state |
| Runs | GitHub Actions: plan on PR (read token), apply on `main` (edit token in a `main`-only environment), weekly drift check | Every change is reviewed in a PR; the write token is out of reach of PR branches |
| Adoption | `cf-terraforming` generates HCL + `import` blocks; applied by CI | Imports are visible in the plan and in git |

## Why not the alternatives

- **HCP Terraform (free tier):** HashiCorp's service for Terraform, not OpenTofu; another account
  and token, and state at a third party in plain text.
- **GitLab-managed state:** free, versioned, with locking. A good fallback if R2 is not wanted, but
  it adds a GitLab account and token to a GitHub-only setup.
- **Kubernetes backend (Secret in k3s) or the shared PostgreSQL:** free and local, but GitHub
  Actions cannot reach the cluster, so runs would have to happen in-cluster.
- **Encrypted state committed to git:** free and versioned, but no locking, and the ciphertext would
  be public, exposed to offline passphrase guessing forever.
- **Terraform:** BSL licence, no client-side state encryption.

## Consequences

- R2 needs a payment method on the Cloudflare account, even inside the free tier.
- R2 has no object versioning. Mitigated by encryption, CI serialisation and by keeping every
  resource importable (state can be rebuilt from the `import` blocks in git history).
- Losing the passphrase makes the state unreadable. It is kept in Doppler and GitHub.
- The `tofu-state` bucket, its R2 token and the API tokens are created by hand once (`iac/README.md`).
- Dashboard edits to managed resources show up as drift in the weekly run and are reverted on the
  next apply. Changes go through PRs from now on.
- An R2 lock can be left behind after a failed run (seen with S3-compatible stores,
  opentofu/opentofu#4405); `force-unlock` is documented.
