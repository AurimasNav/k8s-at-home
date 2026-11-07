# Vendored custom components

Home Assistant custom components (integrations that would normally be installed
via HACS) are vendored here byte-for-byte from their upstream release tarballs,
shipped as ConfigMaps (`../kustomization.yaml`) and mounted into
`/config/custom_components/` (`../values.yaml` additionalVolumes/additionalMounts).
No HACS runs in the pod. Don't edit vendored files by hand — the directory must
stay identical to upstream plus the patches in `patches/` (below), so an
upgrade is a clean replacement.

| Component | Upstream |
|---|---|
| `bhyve` | [sebr/bhyve-home-assistant](https://github.com/sebr/bhyve-home-assistant) |
| `mammotion` | [mikey0000/Mammotion-HA](https://github.com/mikey0000/Mammotion-HA) |
| `byd_vehicle` | [jkaberg/hass-byd-vehicle](https://github.com/jkaberg/hass-byd-vehicle) (en.json only) |
| `eso` | [algirdasc/hass-eso](https://github.com/algirdasc/hass-eso) (pinned to a commit: upstream has no tags; no `brand/`) |
| `hikvision_bridge` | [yannik2480/hikvision_bridge](https://github.com/yannik2480/hikvision_bridge) (Apache-2.0, [licence](LICENSES/hikvision_bridge.txt); en.json only; replaced `hikvision_next` on 2026-11-07, see #1029) |

## Version tracking

The version of record is the source-tarball URL in the comment above each
component's ConfigMap generators in `../kustomization.yaml`
(`.../archive/refs/tags/<tag>.tar.gz`). A regex manager in
`renovate.json` at the repo root parses those URLs against the
`github-tags` datasource, so Renovate opens a PR when upstream tags a new
release. **That PR only bumps the comment** — the component itself is upgraded
by re-vendoring the files (below) on the same branch before merging.

## How to upgrade (re-vendor)

1. Download the new tag's tarball, e.g.
   `curl -sL https://github.com/sebr/bhyve-home-assistant/archive/refs/tags/<tag>.tar.gz | tar -xz`
2. Delete the component directory here and copy in the tarball's
   `custom_components/<name>/` wholesale — no merging, no local edits. Then
   re-apply that component's patches from `patches/` (see Local patches);
   drop a patch once the new release contains the fix.
   Leaving files out is allowed only when the comment above that component's
   ConfigMap generator in `../kustomization.yaml` says which and why: non-English
   translations (bhyve, byd_vehicle, hikvision_bridge), or `brand/` when home-assistant/brands
   serves the same images (eso).
3. Diff the file list against the previous version. If upstream added or
   removed files, update the matching ConfigMap generator lists in
   `../kustomization.yaml`; only if a whole new subdirectory appeared, add a
   ConfigMap + mount for it in `../kustomization.yaml` and `../values.yaml`
   (ConfigMaps can't nest directories, hence one per subdirectory).
   ConfigMap keys allow only `[-._a-zA-Z0-9]`: a file name with any other
   character (`brand/icon@2x.png`) gets a renamed key in the generator
   (`icon-2x.png=<path>`) and an `items` entry on its volume in `../values.yaml`
   mapping the key back to the upstream name.
   Size limits: a ConfigMap holds at most 1 MiB, and one bigger than about
   256 KiB can't be synced with client-side apply, because its
   `kubectl.kubernetes.io/last-applied-configuration` annotation goes over the
   apiserver's 256 KiB annotation limit. Those generators carry
   `argocd.argoproj.io/sync-options: ServerSideApply=true` (mammotion,
   byd_vehicle, hikvision_bridge `brand/` and `frontend/`), plus ones close to
   the limit that keep growing (hikvision_bridge top level). Measure the
   rendered objects after adding files. A component too big for 1 MiB ConfigMaps
   goes through the init-container installer instead (bambu_lab, `../values.yaml`).
4. Check the component's `manifest.json` for a `requirements` key — those pip
   packages get installed by HA at startup, which needs outbound internet and
   slows the first boot.
5. Commit together with the Renovate comment bump. On Argo sync, the reloader
   annotation restarts HA with the new version.

Sanity check after wiring changes: `kustomize build --enable-helm .` from
`gitops/home-assistant/`.

## Local patches

Fixes we need before upstream releases them. Each patch is a plain diff
against the vendored release, applied from this directory:
`git apply patches/<file>.diff`. Check that the repo copy still matches with
`git apply --check -R patches/<file>.diff`.

None at the moment.

## Known quirks

- **Mammotion camera** (Luba mini AWD 800, fw 1.30.29.8): the live view is
  browser-dependent. Works in Chrome (hardware H.265 decode) and the iOS/Android
  companion app; in Firefox there is no video (Firefox has no H.265 support in
  WebRTC) and occasionally a short burst of noise on the audio track at stream
  start — rare and hard to reproduce. Use Chrome or the app. A local patch
  arming the encoder on stream open (commit c424b15) was tried and reverted: it
  made no measurable difference once the browser variable was accounted for.
