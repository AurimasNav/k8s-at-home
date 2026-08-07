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

## Local patches

Exception to "never edit vendored files" — patches we carry until upstream fixes
land. **Re-apply (or drop, if fixed upstream) when re-vendoring:**

- `mammotion/camera.py` (since v0.6.4-beta11): the stream-open path sends
  `device_agora_join_channel_with_position` with `enter_state=1` before WebRTC
  negotiation (upstream only sends `enter_state=0` on close, so the mower's
  video encoder never starts). Marked with a `TEMP local patch` comment.
  Without it the live view is garbage audio and no video. Note: video is H.265 —
  renders in Chrome (hw decode) and iOS/WebKit, never in Firefox.
