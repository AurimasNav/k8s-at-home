# Hermes source hotfixes

Upstream fixes that aren't in a Hermes release yet. The `hermes-patches` init
container copies each file a `*.diff` touches from the image, applies the diff
(`apply.sh`), and the main container mounts the result over `/opt/hermes/<path>`
with a `subPath` mount (one mount per file in `deployment.yaml`).

`apply.sh` never blocks the pod: if a diff no longer applies, or the image
already contains it, the container log says so and Hermes runs the image's file.
After a Hermes image bump, check `kubectl logs deploy/hermes-agent -c hermes-patches`
and delete diffs that report "already in this image" (plus their mount).

- `aux-rotation-144184.diff`: NousResearch/hermes-agent#144183 / PR #144184.
  After an auxiliary call (compression, approvals, titles) hits a 429 and the
  credential pool rotates, the retry still sent the old key, so the second 429
  was blamed on the fresh key and both keys were parked for an hour. Tested on
  v2026.9.24 with the upstream regression test
  (`tests/agent/test_auxiliary_rotation_uses_rotated_key.py`): fails before, passes after.
