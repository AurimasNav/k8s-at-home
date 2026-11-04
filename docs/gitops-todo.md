# GitOps workaround follow-ups

From the workaround audit of 2026-11-04 (main `87f3bba`): every place where we patch, script
around or vendor an open-source product to cover a missing feature or a bug. Newest context
first; tick items off in the PR that does them.

## Act now

- [ ] **qBittorrent image is a dead end.** In progress: #1019 (image + init script) and #1018
      (Hermes `media_weekly.py` accepts the 5.2+ 204 login). `ghcr.io/onedr0p/qbittorrent` is archived (the
      project moved to `home-operations/containers`), so Renovate never sees a release after
      5.0.4. Switch to `ghcr.io/home-operations/qbittorrent` (same Dockerfile and entrypoint,
      5.3.0, libtorrent 2.1). Radarr has the qBittorrent 5.2 login fix since 2026-05
      (`b028efbe3e`, in our `7.0.1-develop`). Re-check `setup/webui_auth.py` and the
      arr/Cleanuparr/Hermes logins after the bump.
- [ ] **hikvision_next carries a local edit inside a vendored component.**
      `custom_components/hikvision_next/hikvision_device.py` sets `via_device_id` instead of
      `via_device` (HA 2026.9 raises on the old key). Upstream fix maciej-or/hikvision_next#369
      is open since 2026-09-03; last release v1.1.1 (2024-12). `custom_components/README.md`
      says vendored files are never edited, so a re-vendor would silently drop the fix and the
      camera entities would fail to load. Document the patch next to the Renovate comment (or
      keep it as a `.diff` applied at build time) until #369 ships.
- [ ] **Two workarounds for the ISP tracker DNS block.** `gitops/coredns-custom` forwards a
      hand-kept list of tracker domains to Unbound (#885); Prowlarr and FlareSolverr now use
      Blocky for all DNS (#1016). qBittorrent (tracker announces) still relies on the CoreDNS
      list. Pick one: move qBittorrent to Blocky too and delete the list, or keep both and say
      why in both places.

## Check

- [ ] **OTBR OMR route is added only at pod start** (`postStart` in `gitops/otbr`). If the
      Thread network gets a new OMR prefix while the pod runs, the route stays stale until a
      restart; `docs/matter-thread-setup.md` claims otherwise. Low risk; a loop in the
      container or a periodic check would cover it.
- [ ] **Argo CD finalizer `ignoreDifferences` (#825) may be obsolete.** argo-cd#27533 was closed
      as completed 2026-04; we run v3.6.0. Test by removing the rule on a branch and watching
      root-app for flapping. argo-cd#17433 is still open.

## Remove when upstream ships

- [ ] Hermes `patches/aux-rotation-144184.diff`: NousResearch/hermes-agent#144184 (open;
      not in v0.22.7). `apply.sh` reports "already in this image" when it lands.
- [ ] Hermes `scripts/anthropic_pool_watchdog.py`: NousResearch/hermes-agent#130459, #128670,
      #131360 (all open).
- [ ] `gitops/prowlarr-test` (and its hostname + network-policy entries): Prowlarr/Prowlarr#2765.
- [ ] VSeerr fork (`gitops/seerr`): seerr-team/seerr#1737 (multiple request profiles).
- [ ] `local-path-provisioner` readiness-probe patch: rancher/local-path-provisioner#208.
- Permanent: Immich `config/render-config.js` (OAuth secret from env was rejected upstream,
  immich-app/immich#31349 closed unmerged).

## Clean up

- [ ] Delete `gitops/victoria-metrics-k8s-stack/patches/deployment-operator.yaml` (unreferenced
      since 2024-08; pins operator v0.47.2).
- [ ] Delete `gitops/external-secrets/unused-vault-clustersecretstore.yaml` (unreferenced).
- [ ] Delete `src/akeyless_arch/` (old secret-store experiment).
- [ ] Delete the commented-out VueTorrent init container in `gitops/qbittorrent/deployment.yaml`
      (and the unused `vuetorrent` volume/mount, `WebUI__RootFolder`).
- [ ] Remove the Hermes `owner-1000` init container: the one-time chown finished on
      2026-11-02 (marker `/opt/data/.owner-1000`).
- [ ] Re-pin hass-eso: `bcb95c4fb8` is 10 commits behind main (Ignitis self-sign and SSO login,
      2026-08).
- [ ] Fix the `modbus_sungrow.yaml` provenance header: upstream now publishes pre-release tags
      (v0.1.0a25). Content is current (upstream file unchanged since 2026-06-19).

## Glue that stays (re-test on major upgrades)

Config-as-code and setup code we own because the products have no declarative config:
Cleanuparr `setup/setup.py` + `desired.json` (PostSync + daily CronJob); qBittorrent
`setup/webui_auth.py`; Hermes init containers (`mcp-config`, `cron-sync` (imports Hermes'
internal `cron.job_definition`, image `:latest`), `gh`, `kubectl`, `cad-venv`) and the
`approval-ping` plugin; Home Assistant `ha-mcp-setup` Job, Bambu Lab release installer,
reloader and UDP 9999 Service patches; k8up Hermes pre-backup pod and rest-server htpasswd;
`router-cert` SSH push with the forced-command script; Immich hourly Plex rescan and library
prep Job; `-lt`/`-ru` arr library-folder init; Seerr pre-VSeerr backup init; PostgreSQL
databases Job.
