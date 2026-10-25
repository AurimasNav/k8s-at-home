# 0001 — Household inventory: Homebox on the shared PostgreSQL

- **Status:** **Accepted** 2026-10-24
- **Scope:** One place for what the household owns and where it is: installed or in storage. Starts with
  smart-home devices and spares, and grows into everything else (tools, electronics, documents).
- **Related:** `gitops/homebox`, `gitops/postgresql`

## Context

- Smart-home parts get lost track of: what is installed where, what spares sit in which box, which
  converter went into which node.
- The scope will grow beyond the smart home, so the tool must be a general home inventory, not a
  parts database or a network CMDB.
- It must be quick to update from a phone at the shelf, or it goes stale.

## Decision

| Part | Pick | Why |
|---|---|---|
| App | **Homebox** ([sysadminsmedia/homebox](https://github.com/sysadminsmedia/homebox), the maintained fork) | Built for home inventory: nested locations, labels, custom fields, attachments, QR labels, purchase/warranty fields, multi-user, REST API, CSV import/export. One container |
| Database | Its own role and database on the shared **`gitops/postgresql`** | Long-lived data belongs in the database that will be backed up, not a SQLite file on a PVC |
| DB provisioning | `gitops/postgresql/databases.job.yaml`, an idempotent Argo CD Sync hook | Creates the role and database once and re-applies the password from Doppler every sync. The pattern for later apps |
| Image | `ghcr.io/sysadminsmedia/homebox:<version>-rootless` | Runs as UID 65532 |
| Exposure | `homebox.sync.lt` via the Cilium gateway | Same pattern as the other apps |

**Model:** locations are physical places (room › furniture › box), so "installed" and "in storage"
are just where an item is. Labels carry the category (smart home, tools, electronics, documents).

## Why not the alternatives

- **Own app or a YAML file in this repo.** CRUD, photos, QR labels and search are solved problems; a
  file in git has no photos and is awkward to update from a phone.
- **InvenTree / Part-DB.** Parts databases with stock movement and BOMs. A fit for electronic
  components later, not for devices and household items.
- **Snipe-IT.** Company asset management with check-in/out to people; heavy for a household.
- **NetBox.** Network-centric (racks, IPAM, cabling).
- **Grocy.** Consumables and groceries; complementary if ever needed, not a replacement.

## Consequences

- One more app on the shared PostgreSQL, whose superuser is still `hairsera` (the first app's user).
- Item attachments (photos, receipts, manuals) live on the `homebox-data` PVC, not in the database,
  so a backup must cover both.
- Backed up nightly by [0002](0002-backups-k8up-restic-local.md): the database through the PostgreSQL
  dump, attachments through the `homebox` namespace schedule. Local disk only, no off-site copy.
- Homebox has a plain quantity field but no stock movement: splitting a multi-pack when one unit is
  installed is manual.

## Implementation notes

- Doppler (project `k8s-at-home`, Production) holds two plain secrets: `HBOX_DATABASE_PASSWORD`
  (e.g. `openssl rand -hex 24`), read by both `gitops/homebox` and `gitops/postgresql`, and
  `HBOX_AUTH_API_KEY_PEPPER` (`openssl rand -base64 48`; Homebox refuses to start below 32 bytes, and
  rotating it invalidates API keys).
- DNS: `homebox.sync.lt` needs a record like the other hosts (there is no wildcard).
- Registration starts enabled so the first account can be created. Set
  `HBOX_OPTIONS_ALLOW_REGISTRATION` to `"false"` in `gitops/homebox/configmap.yaml` right after; household
  members then join through invite links, which work with registration disabled.
- The provisioning script was tested 2026-10-24 against a throwaway `postgres:18-alpine`: it is
  idempotent, handles quotes in the password, and the new role can create tables.
- Upload limit is raised to 32 MB (`HBOX_WEB_MAX_UPLOAD_SIZE`) for manuals and receipts.
