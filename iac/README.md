# Infrastructure as code (OpenTofu)

Cloud resources that are not Kubernetes objects. Argo CD manages `gitops/`; OpenTofu manages `iac/`.
Decision record: [docs/decisions/0003-iac-opentofu-cloudflare.md](../docs/decisions/0003-iac-opentofu-cloudflare.md).

| Stack | Manages | State key |
|---|---|---|
| [`cloudflare/`](cloudflare/) | `sync.lt` zone (DNS, settings) and account API tokens | `cloudflare/terraform.tfstate` |

## Layout

```
iac/
├── .tflint.hcl            shared lint rules
├── tofu.sh                run tofu in a stack with the Doppler/GitHub secret names
└── cloudflare/            one root module = one state
    ├── versions.tf        OpenTofu range + exact provider pin
    ├── .terraform.lock.hcl provider checksums (committed, multi-platform)
    ├── backend.tf         R2 state, lock file, state/plan encryption
    ├── providers.tf       token from the environment, never in code
    ├── variables.tf
    ├── zone.tf            zone lookup (the zone itself is not managed)
    ├── permission-groups.tf  permission group name -> ID lookup
    ├── account-tokens.tf     API tokens (iac-reader, iac)
    ├── account-token-*.tf    each token's permissions, by name
    ├── imports.tf            one-time adoption of existing resources
    ├── outputs.tf
    └── scripts/generate-imports.sh
```

Conventions:
- One root module per provider/account, one state each. Split by blast radius, not by file type.
- Files by topic (`dns.tf`, `zone_settings.tf`, `rulesets.tf`) as resources are added.
- Resource names are `snake_case` and describe the thing (`cloudflare_dns_record.radarr`), never the ID.
- Exact provider version + committed lock file; Renovate updates both (OpenTofu registry).
- No secrets in code or `*.tfvars`. Values come from the environment (`TF_VAR_*`, `CLOUDFLARE_API_TOKEN`).
- Existing resources are adopted with `import` blocks in a PR, never with `tofu import` by hand,
  so the import shows up in the plan and in git history.

## State

- **Where:** Cloudflare R2 bucket `tofu-state` through the S3 backend. Free tier: 10 GB-month,
  1M writes and 10M reads per month; a state file is a few KB.
- **Locking:** lock file in the bucket (`use_lockfile`, conditional writes). CI also serialises
  runs with a `concurrency` group.
- **Encryption:** OpenTofu encrypts state and saved plans client-side (PBKDF2 + AES-GCM) before
  upload, so the bucket only ever holds ciphertext. The passphrase is in Doppler and GitHub.
- **No versioning:** R2 has no object versioning. If the state is lost or the passphrase is lost,
  rebuild it: everything here is importable, and the `import` blocks are in git history.

## Credentials

| What | Doppler (`k8s-at-home/prod`) | GitHub Actions | Hermes pod |
|---|---|---|---|
| Token `iac-reader` (all *Read*, account + all zones) | `CLOUDFLARE_IAC_READ_TOKEN` | repo secret `CLOUDFLARE_IAC_READ_TOKEN` | yes |
| Token `iac` (write, account admin level) | `CLOUDFLARE_API_TOKEN` | `cloudflare` environment secret `CLOUDFLARE_IAC_EDIT_TOKEN` | no |
| Account ID | `CLOUDFLARE_ACCOUNT_ID` | repo secret | yes |
| State passphrase | `TOFU_STATE_PASSPHRASE` | repo secret | yes |
| R2 state keys (bucket `tofu-state` only) | `TOFU_R2_ACCESS_KEY_ID`, `TOFU_R2_SECRET_ACCESS_KEY` | repo secrets | add once they exist |

- Both Cloudflare tokens are defined in this stack (`account-token-*.tf`, permissions by name).
  To change a token's permissions, edit the name list; the plan shows the change.
- The `cloudflare` environment only deploys from `main`, so PR jobs cannot read the write token.
  Add a required reviewer there to approve every apply by hand.
- Doppler is the backup of the passphrase.

## Bootstrap (once)

The bucket and keys that hold the state cannot be managed by the same state.

1. **R2:** enable R2 in the dashboard (asks for a payment method; usage stays in the free tier).
2. **Bucket + keys:** create bucket `tofu-state` and an R2 token limited to it with *Object Read &
   Write*; store its access key ID and secret in Doppler and as GitHub repo secrets (table above).
3. **Turn on CI:** repo variable `TOFU_CLOUDFLARE_ENABLED` = `true`.

## Workflow

- **PR touching `iac/`:** fmt, validate and tflint; then a plan with the read-only token. The job
  summary lists addresses and counts only, because Actions logs are public in this repo.
- **Merge to `main`:** plan + apply with the edit token.
- **Weekly (Monday):** plan; the run fails when Cloudflare was changed outside the code.
- **Local / Hermes:** `iac/tofu.sh cloudflare init`, then `iac/tofu.sh cloudflare plan`. Uses the
  read-only token; `TOFU_WRITE=1` switches to the write token. Applies normally go through CI.

## Adopting existing resources

1. `CLOUDFLARE_API_TOKEN="$CLOUDFLARE_IAC_READ_TOKEN" iac/cloudflare/scripts/generate-imports.sh`
   writes HCL and `import` blocks to `iac/cloudflare/generated/` (git-ignored).
   Other types: pass them as the first argument (`cloudflare_ruleset`). Zone settings need the
   setting names: `RESOURCE_IDS=cloudflare_zone_setting=ssl,always_use_https,min_tls_version
   generate-imports.sh cloudflare_zone_setting`.
2. Move the reviewed blocks into `dns.tf` / `imports.tf`, rename resources after the hostname, drop
   computed attributes (`id`, `created_on`, `modified_on`, ...).
3. Open a PR. The plan must say `N to import, 0 to add, 0 to change, 0 to destroy`.
4. Merge: CI applies the imports. Remove the `import` blocks in the next PR.

## Recovering

- **Stale lock** (a run died mid-way): `iac/tofu.sh cloudflare force-unlock <LOCK_ID>`; the ID is
  in the error message.
- **Lost state or passphrase:** delete the state object, set a new passphrase everywhere, restore
  the `import` blocks from git history and let CI apply them.
