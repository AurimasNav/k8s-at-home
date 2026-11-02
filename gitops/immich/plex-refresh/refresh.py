"""Refresh the Plex photo library that shows the Immich folder.

Immich writes straight to media disk hdd0, not through the mergerfs pool Plex reads, so Plex's
file watcher never sees new photos. This asks Plex to rescan just that library. The library is
found by its location, so its id is not hard-coded. Exits 0 when the library doesn't exist yet.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

PLEX_URL = os.environ["PLEX_URL"].rstrip("/")
TOKEN = os.environ["HERMES_PLEX_TOKEN"].strip()
LOCATION = os.environ["LIBRARY_LOCATION"].rstrip("/")


def plex(path):
    sep = "&" if "?" in path else "?"
    req = urllib.request.Request(f"{PLEX_URL}{path}{sep}X-Plex-Token={urllib.parse.quote(TOKEN)}",
                                 headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
        return json.loads(body) if body else None


sections = (plex("/library/sections") or {}).get("MediaContainer", {}).get("Directory", [])
match = [s for s in sections if any(loc["path"].rstrip("/") == LOCATION for loc in s.get("Location", []))]
if not match:
    print(f"no Plex library has location {LOCATION}; nothing to do")
    sys.exit(0)
for s in match:
    plex(f"/library/sections/{s['key']}/refresh")
    print(f"refresh requested for library {s['title']!r} (section {s['key']})")
