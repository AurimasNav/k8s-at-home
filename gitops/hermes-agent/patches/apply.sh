#!/bin/sh
# Apply local hotfixes to Hermes source files before the gateway starts.
#
# Each *.diff here is an upstream PR hunk that isn't in a release yet. For every
# file a diff touches, this copies the image's original into /patched/<path>,
# applies the diff there, and the main container mounts the result over
# /opt/hermes/<path> (subPath). The init container never fails the pod:
#   - diff applies            -> patched file
#   - already in the image    -> image file (upstream shipped the fix; delete the diff)
#   - doesn't apply           -> image file + a warning in this container's log
set -u
SRC=/opt/hermes
OUT=/patched
mkdir -p "$OUT"
for diff in /patches/*.diff; do
  [ -e "$diff" ] || continue
  name=$(basename "$diff")
  for f in $(sed -n 's#^+++ b/##p' "$diff"); do
    mkdir -p "$OUT/$(dirname "$f")"
    cp "$SRC/$f" "$OUT/$f"
  done
  if (cd "$OUT" && git apply --check "$diff" 2>/dev/null); then
    (cd "$OUT" && git apply "$diff") && echo "patch $name: applied"
  elif (cd "$OUT" && git apply --check --reverse "$diff" 2>/dev/null); then
    echo "patch $name: already in this image, nothing to do (remove it from git)"
  else
    echo "WARNING patch $name: does not apply to this image, running unpatched"
  fi
done
