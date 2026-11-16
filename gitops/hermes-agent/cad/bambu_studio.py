#!/usr/bin/env python3
"""Bambu Studio command-line slicer for Hermes: install (bambu-studio init container) and slice.

  bambu_studio.py install            init container: make the pinned version ready, always exit 0
  bambu_studio.py status             what is installed and whether it passed its self-test
  bambu_studio.py slice IN.3mf OUT/  slice with the installed version, print time + filament as JSON

Install keeps one extracted AppImage per version under /opt/data/apps/bambu-studio/<version>/ on the
PVC; `current` points at the last version that passed the self-test. A normal start only checks a
fingerprint and returns in about a second. The download (~256 MB, sha256 from the GitHub release
asset digest) happens only when BAMBU_STUDIO_VERSION changes.

The binary is a GUI build, linked against GTK/WebKit/GStreamer, which the Hermes image lacks. The
CLI slicer never calls them, but the binary is linked with BIND_NOW, so every undefined symbol must
resolve at load time. stubs/ holds stand-in libraries that define each symbol no installed library
provides as `return 0`. They are rebuilt when the image's libraries change (fingerprint), e.g.
after a Hermes image bump. Every new version or fingerprint is proven by slicing a 20 mm cube for
the P2S before `current` moves, so a broken release never replaces a working one.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess  # noqa: S404 - runs our own pinned binaries with fixed arguments
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("BAMBU_STUDIO_ROOT", "/opt/data/apps/bambu-studio"))
REPO = "bambulab/BambuStudio"
ASSET = re.compile(r"^BambuStudio_ubu24-v.*\.AppImage$")   # Ubuntu 24.04 build; runs on the Debian 13 image
DOWNLOAD_DEADLINE = 900                                     # seconds; then give up until the next start
HERE = Path(__file__).resolve().parent


def log(msg):
    print(f"bambu-studio: {msg}", flush=True)


def run(args, env=None, timeout=600, cwd=None):
    return subprocess.run(args, capture_output=True, text=True, env=env, timeout=timeout, cwd=cwd)  # noqa: S603


def lib_env(vdir, stubs=True):
    sroot = vdir / "squashfs-root"
    paths = ([str(vdir / "stubs")] if stubs else []) + [str(sroot / "lib"), str(sroot / "usr" / "lib")]
    return dict(os.environ, LD_LIBRARY_PATH=":".join(paths))


def binary(vdir):
    return vdir / "squashfs-root" / "bin" / "bambu-studio"


# ---------------- stubs ----------------
def fingerprint(vdir):
    """The image libraries the binary resolves against (path + size) and the stub recipe."""
    ldd = run(["ldd", str(binary(vdir))], env=lib_env(vdir, stubs=False), timeout=60).stdout
    parts = []
    for line in ldd.splitlines():
        m = re.match(r"\s*(\S+) => (\S+)", line)
        if m:
            p = m.group(2)
            parts.append(f"{m.group(1)} {p} {Path(p).stat().st_size if p.startswith('/') else ''}")
    parts.append(hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    return hashlib.sha256("\n".join(sorted(parts)).encode()).hexdigest()[:16]


def build_stubs(vdir):
    env = lib_env(vdir, stubs=False)
    ldd = run(["ldd", str(binary(vdir))], env=env, timeout=60).stdout
    missing = sorted(set(re.findall(r"^\s*(\S+) => not found", ldd, re.M)))
    # include the dynamic loader itself: stubbing __tls_get_addr makes every start segfault
    present = sorted(set(re.findall(r"=> (/\S+) \(", ldd)) | set(re.findall(r"^\s*(/\S*ld-linux\S*) \(", ldd, re.M)))
    undef = {ln.split()[-1].split("@")[0] for ln in run(["nm", "-D", "--undefined-only", str(binary(vdir))]).stdout.splitlines()}
    defined = set()
    for lib in present:
        for ln in run(["nm", "-D", "--defined-only", lib]).stdout.splitlines():
            parts = ln.split()
            if len(parts) >= 3:
                defined.add(parts[2].split("@")[0])
    need = sorted(s for s in undef - defined if not s.startswith(("__gmon_start__", "_ITM_", "__cxa_finalize")))
    out = vdir / "stubs"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir()
    src = out / "stub.c"
    src.write_text("".join(f"long {s}(void) {{ return 0; }}\n" for s in need))
    for name in missing:
        r = run(["gcc", "-shared", "-fPIC", "-O0", "-w", "-o", str(out / name), f"-Wl,-soname,{name}", str(src)])
        if r.returncode:
            raise RuntimeError(f"gcc failed for {name}: {r.stderr[-500:]}")
    left = run(["ldd", str(binary(vdir))], env=lib_env(vdir), timeout=60).stdout.count("not found")
    if left:
        raise RuntimeError(f"{left} libraries still not found after stubbing")
    log(f"stubbed {len(missing)} missing GUI libraries ({len(need)} symbols)")


# ---------------- download ----------------
def release_asset(version):
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/tags/v{version}",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "hermes-bambu-studio"})
    with urllib.request.urlopen(req, timeout=60) as r:
        rel = json.load(r)
    for a in rel["assets"]:
        if ASSET.match(a["name"]):
            digest = a.get("digest") or ""
            if not digest.startswith("sha256:"):
                raise RuntimeError(f"{a['name']} has no sha256 digest in the release API; refusing an unverified download")
            return a["name"], a["browser_download_url"], digest.split(":", 1)[1], a["size"]
    raise RuntimeError(f"release v{version} has no asset matching {ASSET.pattern}")


def download(url, dest, sha256, size):
    h, done, t0 = hashlib.sha256(), 0, time.time()
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-bambu-studio"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        while chunk := r.read(1 << 20):
            h.update(chunk)
            f.write(chunk)
            done += len(chunk)
            if time.time() - t0 > DOWNLOAD_DEADLINE:
                raise RuntimeError(f"download slower than {DOWNLOAD_DEADLINE}s ({done >> 20} of {size >> 20} MB)")
    if done != size or h.hexdigest() != sha256:
        raise RuntimeError(f"checksum mismatch: got {done} bytes sha256 {h.hexdigest()[:16]}…, want {size} {sha256[:16]}…")
    log(f"downloaded {done >> 20} MB in {time.time() - t0:.0f}s, sha256 ok")


def fetch_and_extract(version, vdir):
    name, url, sha256, size = release_asset(version)
    shutil.rmtree(vdir, ignore_errors=True)
    vdir.mkdir(parents=True)
    try:
        img = vdir / name
        download(url, img, sha256, size)
        img.chmod(0o755)
        t0 = time.time()
        r = run([str(img), "--appimage-extract"], cwd=vdir, timeout=900)
        img.unlink()
        if r.returncode or not binary(vdir).is_file():
            raise RuntimeError(f"--appimage-extract failed ({r.returncode}): {r.stderr[-300:]}")
    except BaseException:
        shutil.rmtree(vdir, ignore_errors=True)   # don't leave a 250 MB partial or unverified download behind
        raise
    (vdir / "asset.json").write_text(json.dumps({"version": version, "asset": name, "sha256": sha256}))
    log(f"extracted in {time.time() - t0:.0f}s")


# ---------------- slice ----------------
def slice_3mf(vdir, src, out, timeout=900):
    """Slice plate 1..n of a project 3MF. Returns a summary dict; raises on failure."""
    out = Path(out)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    data = vdir / "data"                     # Bambu Studio's per-user dir (logs, cache), kept out of $HOME
    data.mkdir(exist_ok=True)
    args = [str(binary(vdir)), "--datadir", str(data), "--debug", "2", "--slice", "0",
            "--export-settings", str(out / "settings.json"), "--export-3mf", "sliced.3mf",
            "--outputdir", str(out), str(Path(src).resolve())]
    t0 = time.time()
    r = run(args, env=lib_env(vdir), timeout=timeout)
    (out / "log.txt").write_text(r.stdout + r.stderr)
    res = json.loads((out / "result.json").read_text()) if (out / "result.json").is_file() else {}
    if r.returncode or res.get("return_code") != 0:
        raise RuntimeError(f"slicing failed (exit {r.returncode}, {res.get('error_string', 'no result.json')}); log {out / 'log.txt'}")
    plates = []
    for p in res.get("sliced_plates", []):
        plates.append({"plate": p["id"], "time_s": round(p["total_predication"]),
                       "time": time.strftime("%Hh %Mm", time.gmtime(p["total_predication"])),
                       "filament_g": round(sum(f["total_used_g"] for f in p["filaments"]), 1),
                       "objects": [o["name"] for o in p.get("objects", [])],
                       "warning": p.get("warning_message", "")})
    s = json.loads((out / "settings.json").read_text())
    return {"bambu_studio": vdir.name, "printer": s.get("printer_settings_id"), "process": s.get("print_settings_id"),
            "filament": s.get("filament_settings_id"), "plates": plates, "slice_s": round(time.time() - t0, 1),
            "gcode": sorted(str(p) for p in out.glob("plate_*.gcode")), "sliced_3mf": str(out / "sliced.3mf")}


def self_test(vdir):
    sys.path.insert(0, str(HERE))
    import bambu3mf
    v = [(x, y, z) for z in (0, 20) for y in (118, 138) for x in (118, 138)]
    f = [(0, 2, 1), (1, 2, 3), (4, 5, 6), (5, 7, 6), (0, 1, 4), (1, 5, 4),
         (2, 6, 3), (3, 6, 7), (0, 4, 2), (2, 4, 6), (1, 3, 5), (3, 7, 5)]
    with tempfile.TemporaryDirectory(dir=vdir) as tmp:
        src = Path(tmp) / "cube.3mf"
        bambu3mf.write_project(src, [("self-test cube", v, f)], {"wall_loops": "3"}, app=vdir)
        probs = bambu3mf.check_project(src, app=vdir)
        if probs:
            raise RuntimeError(f"project 3MF check: {probs}")
        res = slice_3mf(vdir, src, Path(tmp) / "out", timeout=300)
    p = res["plates"][0]
    if res["printer"] != bambu3mf.PRINTER or not p["filament_g"] > 0:
        raise RuntimeError(f"unexpected self-test result {res}")
    return {"printer": res["printer"], "filament_g": p["filament_g"], "time": p["time"], "slice_s": res["slice_s"]}


# ---------------- install ----------------
def install(version):
    t0 = time.time()
    vdir = ROOT / version
    ROOT.mkdir(parents=True, exist_ok=True)
    ok = vdir / "ok.json"
    if not (vdir / "asset.json").is_file():
        fetch_and_extract(version, vdir)
    fp = fingerprint(vdir)
    state = json.loads(ok.read_text()) if ok.is_file() else {}
    if state.get("fingerprint") != fp:
        ok.unlink(missing_ok=True)
        build_stubs(vdir)
        test = self_test(vdir)
        ok.write_text(json.dumps({"version": version, "fingerprint": fp, "self_test": test,
                                  "tested": time.strftime("%Y-%m-%d %H:%M:%S %Z")}, indent=1))
        log(f"self-test ok: 20 mm cube on {test['printer']}, {test['filament_g']} g, sliced in {test['slice_s']}s")
    cur = ROOT / "current"
    if not cur.is_symlink() or os.readlink(cur) != version:
        tmp = ROOT / ".current.tmp"
        tmp.unlink(missing_ok=True)
        tmp.symlink_to(version)
        tmp.replace(cur)
    for old in ROOT.iterdir():                # keep only the version in use
        if old.is_dir() and not old.is_symlink() and old.name != version and re.fullmatch(r"[\d.]+", old.name):
            shutil.rmtree(old, ignore_errors=True)
            log(f"removed old version {old.name}")
    log(f"{version} ready in {time.time() - t0:.0f}s")


def status():
    cur = ROOT / "current"
    if not cur.exists():
        print(json.dumps({"installed": False, "root": str(ROOT)}))
        return 1
    vdir = cur.resolve()
    ok = vdir / "ok.json"
    st = json.loads(ok.read_text()) if ok.is_file() else {}
    st["fingerprint_now"] = fingerprint(vdir)
    st["fingerprint_ok"] = st.get("fingerprint") == st["fingerprint_now"]
    print(json.dumps(st, indent=1))
    return 0 if st["fingerprint_ok"] else 1


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "install":
        version = os.environ.get("BAMBU_STUDIO_VERSION", "").lstrip("v")
        try:
            if not version:
                raise RuntimeError("BAMBU_STUDIO_VERSION is not set")
            install(version)
        except Exception as e:   # never block the pod: Hermes starts without the slicer
            cur = ROOT / "current"
            if cur.is_symlink() and (cur / "ok.json").is_file():
                log(f"install of {version or '(no version)'} failed, still using {os.readlink(cur)}: {e}")
            else:
                log(f"install failed, slicer unavailable until the next restart: {e}")
        return 0
    if cmd == "status":
        return status()
    if cmd == "slice" and len(sys.argv) == 4:
        cur = ROOT / "current"
        if not (cur / "ok.json").is_file():
            sys.exit("Bambu Studio is not installed (see `bambu_studio.py status` / the bambu-studio init container log)")
        print(json.dumps(slice_3mf(cur.resolve(), sys.argv[2], sys.argv[3]), indent=1, ensure_ascii=False))
        return 0
    sys.exit(__doc__)


if __name__ == "__main__":
    sys.exit(main())
