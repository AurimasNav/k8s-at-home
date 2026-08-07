# Vendored custom components

Home Assistant custom components (integrations that would normally be installed
via HACS) are vendored here byte-for-byte from their upstream release tarballs,
shipped as ConfigMaps (`../kustomization.yaml`) and mounted into
`/config/custom_components/` (`../values.yaml` additionalVolumes/additionalMounts).
No HACS runs in the pod. Never edit vendored files — the directory must stay
identical to upstream so an upgrade is a clean replacement.

| Component | Upstream |
|---|---|
| `hikvision_next` | [maciej-or/hikvision_next](https://github.com/maciej-or/hikvision_next) |
| `bhyve` | [sebr/bhyve-home-assistant](https://github.com/sebr/bhyve-home-assistant) |
| `mammotion` | [mikey0000/Mammotion-HA](https://github.com/mikey0000/Mammotion-HA) |

## Version tracking

The version of record is the source-tarball URL in the comment above each
component's ConfigMap generators in `../kustomization.yaml`
(`.../archive/refs/tags/<tag>.tar.gz`). A regex manager in
`gitops/renovate-bot/ConfigMap.yaml` parses those URLs against the
`github-tags` datasource, so Renovate opens a PR when upstream tags a new
release. **That PR only bumps the comment** — the component itself is upgraded
by re-vendoring the files (below) on the same branch before merging.

## How to upgrade (re-vendor)

1. Download the new tag's tarball, e.g.
   `curl -sL https://github.com/sebr/bhyve-home-assistant/archive/refs/tags/<tag>.tar.gz | tar -xz`
2. Delete the component directory here and copy in the tarball's
   `custom_components/<name>/` wholesale — no merging, no local edits.
3. Diff the file list against the previous version. If upstream added or
   removed files, update the matching ConfigMap generator lists in
   `../kustomization.yaml`; only if a whole new subdirectory appeared, add a
   ConfigMap + mount for it in `../kustomization.yaml` and `../values.yaml`
   (ConfigMaps can't nest directories, hence one per subdirectory).
4. Check the component's `manifest.json` for a `requirements` key — those pip
   packages get installed by HA at startup, which needs outbound internet and
   slows the first boot.
5. Commit together with the Renovate comment bump. On Argo sync, the reloader
   annotation restarts HA with the new version.

Sanity check after wiring changes: `kustomize build --enable-helm .` from
`gitops/home-assistant/`.

## Known quirks

No local patches are carried — everything here is stock upstream.

- **Mammotion camera** (Luba mini AWD 800, fw 1.30.29.8): the live view is
  browser-dependent. Works in Chrome (hardware H.265 decode) and the iOS/Android
  companion app; in Firefox there is no video (Firefox has no H.265 support in
  WebRTC) and occasionally a short burst of noise on the audio track at stream
  start — rare and hard to reproduce. Use Chrome or the app. A local patch
  arming the encoder on stream open (commit c424b15) was tried and reverted: it
  made no measurable difference once the browser variable was accounted for.
