# Infrastructure as code (OpenTofu)

Cloud resources that are not Kubernetes objects. Argo CD manages `gitops/`; OpenTofu manages `iac/`.
Decision record: [docs/decisions/0003-iac-opentofu-cloudflare.md](../docs/decisions/0003-iac-opentofu-cloudflare.md).

| Stack | Manages | State key |
|---|---|---|
| [`cloudflare/`](cloudflare/) | `sync.lt` zone (DNS, settings, cache rules), Cloudflare Tunnel `k3s`, account API tokens, and the Doppler keys for tokens the cluster uses | `cloudflare/terraform.tfstate` |

## Layout

```
iac/
├── .tflint.hcl            shared lint rules
├── tofu.ps1               run tofu in a stack with the Doppler/GitHub secret names
└── cloudflare/            one root module = one state
    ├── versions.tf        OpenTofu range + exact provider pin
    ├── .terraform.lock.hcl provider checksums (committed, multi-platform)
    ├── backend.tf         R2 state, lock file, state/plan encryption
    ├── providers.tf       token from the environment, never in code
    ├── variables.tf
    ├── zone.tf            zone lookup (the zone itself is not managed)
    ├── permission-groups.tf  permission group name -> ID lookup
    ├── account-tokens.tf     API tokens (iac-reader, iac, cert-manager)
    ├── account-token-*.tf    each token's permissions, by name
    ├── tunnel.tf             Cloudflare Tunnel "k3s" + its ingress rules (gitops/cloudflared)
    ├── zone_settings.tf      zone settings (act on proxied/tunnel traffic only)
    ├── rulesets.tf           cache rules (nothing is cached at the edge)
    ├── doppler.tf            token values written to Doppler for the cluster
    ├── dns.tf                sync.lt records (apex A + CNAMEs)
    ├── imports.tf            one-time adoption of existing resources (only while adopting)
    ├── outputs.tf
    └── scripts/generate-imports.ps1
```

Conventions:
- One root module per provider/account, one state each. Split by blast radius, not by file type.
- Files by topic (`dns.tf`, `zone_settings.tf`, `rulesets.tf`) as resources are added.
- Resource names are `snake_case` and describe the thing (`cloudflare_dns_record.radarr`), never the ID.
- Exact provider version + committed lock file; Renovate updates both (OpenTofu registry).
- Scripts are PowerShell 7 (`pwsh`), linted with PSScriptAnalyzer in CI.
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
| R2 state keys (token `tofu-state-r2`: bucket `tofu-state`, object read/write only) | `TOFU_R2_ACCESS_KEY_ID`, `TOFU_R2_SECRET_ACCESS_KEY` | repo secrets | no |
| Token `cert-manager` (DNS Write + Zone Read, `sync.lt` only; created by this stack) | `CLOUDFLARE_DNS_API_TOKEN` (written by the apply) | none | no |
| Doppler service token `k3s` (`k8s-at-home/prod`, read/write; also the cluster's store token) | `DOPPLER_SERVICE_TOKEN` | `cloudflare` environment secret `DOPPLER_SERVICE_TOKEN` | yes (`DOPPLER_TOKEN`) |

- Both Cloudflare tokens are defined in this stack (`account-token-*.tf`, permissions by name).
  To change a token's permissions, edit the name list; the plan shows the change.
- The `cloudflare` environment only deploys from `main`, so PR jobs cannot read the write token.
  Add a required reviewer there to approve every apply by hand.
- Doppler is the backup of the passphrase.
- **Doppler writes:** only the apply job has a Doppler token. A PR job runs the PR branch's
  workflow, so anything it can read, anyone who can push a branch can read. PR plans and the
  drift check leave the `doppler_secret` resources out (`TOFU_PLAN_EXCLUDE`); the apply job's
  summary shows them. After regenerating the `k3s` token, update the GitHub environment secret too.

## Bootstrap (once)

The bucket and keys that hold the state cannot be managed by the same state.

Done on 2026-10-30:

1. **R2:** enabled in the dashboard (asks for a payment method; usage stays in the free tier).
2. **Bucket:** `tofu-state`, location hint EEUR, default jurisdiction, Standard storage class.
3. **Keys:** account token `tofu-state-r2` with *Workers R2 Storage Bucket Item Write* on
   `com.cloudflare.edge.r2.bucket.<account>_default_tofu-state` only. Its S3 keys: access key ID =
   token ID, secret = SHA-256 of the token value. Stored in Doppler and as GitHub repo secrets.
   Not managed by this stack yet (it guards the state itself); rotate it by hand.
4. **CI:** repo variable `TOFU_CLOUDFLARE_ENABLED` = `true`.

## Workflow

- **PR touching `iac/`:** fmt, validate and tflint; then a plan with the read-only token. The tunnel's
  connector token (and its Doppler copy) can only be read with the write token, so PR plans skip
  it (`TOFU_PLAN_EXCLUDE`); the apply job writes it to Doppler `TUNNEL_TOKEN`. The plan
  goes into the PR description (section *IaC (OpenTofu)*, replaced on every run) and the job
  summary. Both are public: `.github/scripts/New-TofuPlanReport.ps1` masks sensitive values, the
  account ID and token IDs, and names each permission group. The plan never goes to the log.
- **Merge to `main`:** plan + apply with the edit token.
- **Weekly (Monday):** plan; the run fails when Cloudflare was changed outside the code.
- **Local / Hermes:** `pwsh iac/tofu.ps1 cloudflare init`, then `pwsh iac/tofu.ps1 cloudflare plan`.
  Uses the read-only token; `-Write` switches to the write token. Applies normally go through CI.

## Adopting existing resources

1. `$env:CLOUDFLARE_API_TOKEN = $env:CLOUDFLARE_IAC_READ_TOKEN; iac/cloudflare/scripts/generate-imports.ps1`
   writes HCL and `import` blocks to `iac/cloudflare/generated/` (git-ignored).
   Other types: `-ResourceType cloudflare_ruleset`. Zone settings need the setting names:
   `-ResourceType cloudflare_zone_setting -ResourceId 'cloudflare_zone_setting=ssl,always_use_https'`.
2. Move the reviewed blocks into the resource file and a new `imports.tf`, rename resources after the hostname, drop
   computed attributes (`id`, `created_on`, `modified_on`, ...).
3. Open a PR. The plan must say `N to import, 0 to add, 0 to change, 0 to destroy`.
4. Merge: CI applies the imports. Delete `imports.tf` in the next PR.

## Recovering

- **Stale lock** (a run died mid-way): `pwsh iac/tofu.ps1 cloudflare force-unlock <LOCK_ID>`; the ID is
  in the error message.
- **Lost state or passphrase:** delete the state object, set a new passphrase everywhere, restore
  the `import` blocks from git history and let CI apply them.
