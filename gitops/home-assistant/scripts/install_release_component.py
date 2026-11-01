"""Install a pinned Home Assistant custom component from its GitHub release zip.

Runs as a Home Assistant init container (values.yaml initContainers). Used for
components whose release is too big for the ConfigMap vendoring pattern (a
ConfigMap holds at most 1 MiB; ha-bambulab ships a 3 MB frontend card).

Env:
  COMPONENT  integration domain, e.g. bambu_lab (also the target dir name)
  REPO       GitHub owner/repo
  ASSET      release asset name, e.g. bambu_lab.zip
  VERSION    release tag, e.g. v2.2.26
  SHA256     expected sha256 of the asset (Renovate updates it with VERSION)

The component lands in /config/custom_components/<COMPONENT> on the HA volume.
A .release marker skips the download when the pinned version is already there.
Never blocks HA: any failure is logged and the previous install (if any) stays.
"""

import hashlib
import io
import json
import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

CONFIG = Path(os.environ.get("HA_CONFIG", "/config"))


def log(msg):
    print(f"{os.environ.get('COMPONENT', '?')}: {msg}", flush=True)


def install():
    comp, repo, asset = os.environ["COMPONENT"], os.environ["REPO"], os.environ["ASSET"]
    version, want = os.environ["VERSION"], os.environ["SHA256"].lower()
    target = CONFIG / "custom_components" / comp
    marker = target / ".release"
    pinned = f"{version} {want}"
    if marker.is_file() and marker.read_text().strip() == pinned:
        log(f"{version} already installed")
        return

    url = f"https://github.com/{repo}/releases/download/{version}/{asset}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        data = resp.read()
    got = hashlib.sha256(data).hexdigest()
    if got != want:
        raise RuntimeError(f"sha256 mismatch for {url}: got {got}, want {want}")

    staging = target.with_name(f".{comp}.new")
    shutil.rmtree(staging, ignore_errors=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            dest = (staging / name).resolve()
            if not dest.is_relative_to(staging.resolve()):
                raise RuntimeError(f"unsafe path in zip: {name}")
        zf.extractall(staging)
    manifest = json.loads((staging / "manifest.json").read_text())
    if manifest.get("domain") != comp:
        raise RuntimeError(f"zip holds domain {manifest.get('domain')!r}, expected {comp!r}")
    (staging / ".release").write_text(pinned + "\n")

    old = target.with_name(f".{comp}.old")
    shutil.rmtree(old, ignore_errors=True)
    if target.exists():
        target.rename(old)
    staging.rename(target)
    shutil.rmtree(old, ignore_errors=True)
    log(f"installed {version} ({manifest.get('version')}, {len(data)} bytes, sha256 ok)")


def main():
    try:
        install()
    except Exception as exc:  # noqa: BLE001 - never block HA startup
        log(f"install failed, keeping what is there: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
