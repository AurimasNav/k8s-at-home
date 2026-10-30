# Infrastructure as code (OpenTofu)

Cloud resources that are not Kubernetes objects. Argo CD manages `gitops/`; OpenTofu manages `iac/`.
Decision record: [docs/decisions/0003-iac-opentofu-cloudflare.md](../docs/decisions/0003-iac-opentofu-cloudflare.md).

| Stack | Manages | State key |
|---|---|---|
| [`cloudflare/`](cloudflare/) | `sync.lt` zone: DNS records and zone settings | `cloudflare/terraform.tfstate` |

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

## Bootstrap (once, by hand)

The bucket and keys that hold the state cannot be managed by the same state.

1. **R2:** in the Cloudflare dashboard, enable R2 (asks for a payment method; usage stays in the
   free tier) and create the bucket `tofu-state` (location: Europe/automatic).
2. **R2 API token** (R2 → Manage API tokens): *Object Read & Write*, **only** bucket `tofu-state`.
   Gives an access key ID and secret.
3. **Cloudflare API tokens** (My Profile → API Tokens → Custom token), zone `sync.lt` only:
   - `iac-read`: Zone *Read*, DNS *Read*, Zone Settings *Read*, Zone WAF *Read*, Page Rules *Read*.
   - `iac-edit`: the same permissions with *Edit*.
4. **Passphrase:** `openssl rand -base64 32`.
5. **Doppler** (project used by external-secrets): `CLOUDFLARE_ACCOUNT_ID`,
   `CLOUDFLARE_IAC_READ_TOKEN`, `TOFU_R2_ACCESS_KEY_ID`, `TOFU_R2_SECRET_ACCESS_KEY`,
   `TOFU_STATE_PASSPHRASE`. Doppler is the backup of the passphrase.
6. **GitHub** (repo Settings):
   - Secrets → Actions: `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_IAC_READ_TOKEN`,
     `TOFU_R2_ACCESS_KEY_ID`, `TOFU_R2_SECRET_ACCESS_KEY`, `TOFU_STATE_PASSPHRASE`.
   - Environments → new `cloudflare`: *Deployment branches* = `main` only; secret
     `CLOUDFLARE_IAC_EDIT_TOKEN`. Only the apply job on `main` can read the edit token.
   - Variables → Actions: `TOFU_CLOUDFLARE_ENABLED` = `true` (turns on the plan/apply jobs).

## Workflow

- **PR touching `iac/`:** fmt, validate and tflint; then a plan with the read-only token. The job
  summary lists addresses and counts only, because Actions logs are public in this repo.
- **Merge to `main`:** plan + apply with the edit token.
- **Weekly (Monday):** plan; the run fails when Cloudflare was changed outside the code.
- **Local:** `iac/tofu.sh cloudflare init`, then `iac/tofu.sh cloudflare plan` (read-only token
  by default). Applies go through CI.

## Adopting existing resources

1. `CLOUDFLARE_API_TOKEN=<read token> CLOUDFLARE_ACCOUNT_ID=... iac/cloudflare/scripts/generate-imports.sh`
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
