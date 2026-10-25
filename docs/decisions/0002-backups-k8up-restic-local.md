# 0002 — Backups: K8up + restic to a local REST server

- **Status:** **Accepted** 2026-10-24
- **Scope:** Scheduled, encrypted backups of application data on the single k3s node: databases and
  the PVCs that cannot be recreated from git.
- **Related:** [0001](0001-household-inventory-homebox.md) (Homebox, the first data worth keeping),
  `gitops/k8up`

## Context

- Manifests are in git and Argo CD rebuilds them, so the cluster itself is reproducible. Its **data**
  is not: the shared PostgreSQL (Homebox, hairsera), Homebox attachments, Home Assistant config,
  the Matter fabric and Thread network credentials, Hermes state.
- **No off-site storage.** The best available target is a second disk in the same server, added later.
  Until then backups land on the existing disk.
- Everything is `local-path` (no volume snapshots) on one node.

## Decision

| Part | Pick | Why |
|---|---|---|
| Operator | **K8up** (Helm chart via kustomize `helmCharts`, CRDs from the release asset) | restic underneath; per-namespace `Schedule`s for backup, check and prune; app-aware backups via a pod annotation; metrics |
| Repository host | **restic `rest-server`** in namespace `k8up`, data on hostPath **`/var/backups/restic`** | One endpoint every namespace can reach without mounting the disk into each backup pod. Moving to the second disk is a hostPath change |
| Layout | One restic repository per namespace (`/var/backups/restic/<namespace>`) | Independent prune/check per app; a broken repo affects one app |
| PostgreSQL | `pg_dumpall -U hairsera` via `k8up.io/backupcommand` on the pod; the raw data PVC is excluded | Copying a live data directory without snapshots is not consistent; a dump is |
| Scope | Namespaces with a `Schedule`: postgresql, homebox, home-assistant, hermes-agent, matter-server, otbr. Inside them every PVC, unless annotated `k8up.io/backup: "false"` | Opt-in per namespace keeps the 1 Ti Plex transcode volume and re-downloadable media out |
| Timing | Backup daily 03:00, prune Sunday 04:00, check Sunday 05:00 (Europe/Vilnius) | Quiet hours |
| Retention | 7 daily, 4 weekly, 6 monthly | |
| Secrets | Doppler `RESTIC_PASSWORD` (repository encryption) and `REST_PASSWORD` (rest-server login, user `k8up`), copied into each namespace by a `ClusterExternalSecret` | The repository password lives off the server, so backups stay readable if the server dies |

## Why not the alternatives

- **Velero.** Mainly backs up cluster objects, which are already in git; heavier.
- **VolSync.** No pre-backup hook for database dumps, and consistent copies need CSI snapshots that
  `local-path` does not provide.
- **MinIO as the target.** The community edition stopped publishing images in October 2025 and is
  unmaintained.
- **K8up `local` backend.** Would mount the backup disk into backup pods in every namespace.
- **RAID1 instead of a backup disk.** A mirror copies deletions and corruption to both disks instantly;
  a backup keeps history.
- **Off-site (B2, Hetzner, R2).** Ruled out for now.

## Consequences

- **What this protects against:** deleted or corrupted data, bad upgrades and migrations, app bugs,
  and (once the second disk exists) one disk dying.
- **What it does not:** theft, fire, water, a surge killing both disks, or anything with root on the
  host. The restic repository is encrypted, so an occasional copy to a USB disk kept elsewhere is a
  cheap hedge.
- **Until the second disk is added, backups share the disk with the data** and only cover logical loss.
- Home Assistant's recorder database is SQLite and is copied live, so that file may restore
  inconsistent. HA's own backups in `/config/backups` are consistent and are included.
- `data-postgresql-0` was annotated `k8up.io/backup=false` with kubectl on 2026-10-24. It comes from the
  StatefulSet's `volumeClaimTemplates`, which cannot be changed in git on an existing StatefulSet.
  Re-apply the annotation if the PVC is ever recreated.
- Adding the backup annotations to the PostgreSQL pod template restarts PostgreSQL once.
- **Alerts don't notify anyone yet:** vmalert evaluates `K8upJobFailed` and `K8upBackupStale`, but
  Alertmanager has no receiver configured.

## Implementation notes

- Doppler (project `k8s-at-home`, Production) holds `RESTIC_PASSWORD` and `REST_PASSWORD` as plain
  secrets (e.g. `openssl rand -base64 32` each). **Also keep `RESTIC_PASSWORD` in a password
  manager.** Never change it in Doppler alone: the repositories are encrypted with it (rotate with
  `restic key` instead).
- To back up another namespace: add a `Schedule` to `gitops/k8up/schedules.yaml` and the namespace to
  `restic.clusterexternalsecret.yaml`.
- **Restore** with a K8up `Restore` resource, or directly on the node, no cluster needed:
  `restic -r /var/backups/restic/<namespace> snapshots`, then `restic ... restore latest --target <dir>`.
  PostgreSQL restores from the `.sql` dump with `psql -U hairsera -f <dump>` into an empty instance.
- **Moving to the second disk:** scale `rest-server` to 0, copy `/var/backups/restic` to the new
  mount, change the hostPath in `gitops/k8up/rest-server.deployment.yaml`, push.
- **Test a restore** after the first nightly run, e.g. Homebox's attachments into a scratch directory.
- The K8up chart's `k8up-cleanup` Helm hook (post-install/upgrade) only deletes legacy `pod-executor`
  roles; nothing in this repo uses that name.
- Local `kubectl kustomize --enable-helm` fails with Helm 4 (`helm version -c` was removed); Argo CD's
  build is unaffected. Render locally with `helm template` instead.
