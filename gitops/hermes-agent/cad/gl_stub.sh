#!/bin/sh
# Give the CAD venv a stand-in libGL.so.1 when the Hermes image has none (cad-venv init container).
#
# build123d needs OCP (cadquery-ocp-novtk), whose bundled libTKOpenGl links libGL.so.1. Hermes images
# up to v2026.9.24 carried libGL only as a dependency of xvfb; from v0.21.6 on they don't, so
# `import build123d` fails with "libGL.so.1: cannot open shared object file". The CAD tools only model
# and export shapes; they never draw, so a library that defines every gl* symbol OCP asks for as
# `return 0` is enough (the same idea bambu_studio.py uses for Bambu Studio's GUI libraries).
#
# The stub goes into the wheel's own bundled-library dir, which is on that library's RUNPATH, so
# nothing else in the pod sees it. A real libGL in the image always wins: then this does nothing.
# Usage: gl_stub.sh <venv>. Exits non-zero only when the venv still can't import build123d.
set -eu
venv=$1
py="$venv/bin/python"
if "$py" -c "import build123d" 2>/dev/null; then
  exit 0
fi
libs=; gl=
for d in "$venv"/lib/python3*/site-packages/cadquery_ocp_novtk.libs; do [ -d "$d" ] && libs=$d && break; done
for f in "$libs"/libTKOpenGl*.so*; do [ -e "$f" ] && gl=$f && break; done
if [ -z "$gl" ] || ! ldd "$gl" | grep -q "libGL.so.1 => not found"; then
  echo "gl-stub: build123d does not import, and not because of a missing libGL" >&2
  "$py" -c "import build123d" 2>&1 | tail -n 1 >&2
  exit 1
fi
src=$(mktemp)
nm -D --undefined-only "$gl" | awk '{print $NF}' | sed 's/@.*//' | grep -E '^gl' | sort -u |
  sed 's/.*/long &(void) { return 0; }/' > "$src"
gcc -x c -shared -fPIC -O0 -w -Wl,-soname,libGL.so.1 -o "$libs/libGL.so.1" "$src"
rm -f "$src"
"$py" -c "import build123d"
echo "gl-stub: added a stand-in libGL.so.1 ($(nm -D --defined-only "$libs/libGL.so.1" | grep -c ' T gl') gl symbols)"
