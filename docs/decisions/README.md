# Homelab decisions

Decision records for homelab-wide choices: apps, data and operations that are not specific to the
smart home. Smart-home, IoT and home-network decisions live in
[`docs/smart-home/decisions/`](../smart-home/decisions/README.md) and use the same format.

## Index

| # | Decision | Status |
|---|---|---|
| [0001](0001-household-inventory-homebox.md) | Household inventory: Homebox on the shared PostgreSQL | Accepted 2026-10-24 |
| [0002](0002-backups-k8up-restic-local.md) | Backups: K8up + restic to a local REST server (`/var/backups/restic`) | Accepted 2026-10-24 |
| [0003](0003-iac-opentofu-cloudflare.md) | Cloudflare as code: OpenTofu, state in Cloudflare R2 (encrypted) | Proposed 2026-10-30 |
