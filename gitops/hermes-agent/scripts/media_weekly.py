#!/usr/bin/env python3
"""Weekly media report data: what was requested, downloaded, upgraded, cleaned up and watched.

Read-only. Sources (all in-cluster):
  - VSeerr requests (who asked for what)            SEERR_API_KEY from Doppler via DOPPLER_TOKEN
  - Radarr/Sonarr (+ RU instances) history          RADARR_*/SONARR_* URL + API key env vars
  - Cleanuparr events (cleaned / removed / searched) CLEANUPARR_PASSWORD from Doppler
  - qBittorrent transfer totals and torrent count   QBITTORRENT_USERNAME/PASSWORD from Doppler
  - Plex play history (what was watched)            PLEX_URL + PLEX_TOKEN
  - VictoriaMetrics free space on the media disks (now and 7 days ago)

Prints a compact text block for the cron agent. Never prints a credential.
A section that fails prints "KLAIDA <section>: <error>" and the rest still runs.
Week totals for qBittorrent upload/download are deltas against the previous run,
kept in $HERMES_HOME/cache/media_weekly_state.json.

  media_weekly.py            # last 7 days
  media_weekly.py --days 3   # other window (state file is not updated)
"""

import argparse
import collections
import datetime as dt
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TZ = dt.timezone(dt.timedelta(hours=2))  # replaced below with Europe/Vilnius when zoneinfo works
try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Europe/Vilnius")
except Exception:  # noqa: BLE001,S110 - fixed offset fallback is fine for a weekly report
    pass

HOME = os.environ.get("HERMES_HOME", "/opt/data")
STATE = os.path.join(HOME, "cache", "media_weekly_state.json")
VM = "http://vmsingle-victoria-metrics-k8s-stack.victoriametrics.svc:8428/api/v1/query"
SEERR = "http://seerr.seerr/api/v1"
CLEANUPARR = os.environ.get("CLEANUPARR_URL", "http://cleanuparr.cleanuparr")
QBIT = os.environ.get("QBIT_URL", "http://qbittorrent.qbittorrent").rstrip("/") + "/api/v2"
TIMEOUT = 60


# ---------- helpers ----------

def get_json(url, headers=None, data=None, opener=None):
    h = {"Accept": "application/json", **(headers or {})}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h)
    with (opener.open(req, timeout=TIMEOUT) if opener else urllib.request.urlopen(req, timeout=TIMEOUT)) as r:
        text = r.read()
    return json.loads(text) if text else {}


def doppler(name):
    q = urllib.parse.urlencode({"project": "k8s-at-home", "config": "prod", "name": name})
    r = get_json("https://api.doppler.com/v3/configs/config/secret?" + q,
                 headers={"Authorization": "Bearer " + os.environ["DOPPLER_TOKEN"]})
    return r["value"]["raw"].strip()


def gb(n):
    return f"{(n or 0) / 1e9:,.1f} GB"


def size_h(n):
    n = n or 0
    return f"{n / 1e12:.2f} TB" if n >= 1e12 else f"{n / 1e9:.1f} GB"


def local(ts):
    """ISO string or epoch -> 'Mon 03 14:05' in Vilnius time."""
    if isinstance(ts, (int, float)):
        d = dt.datetime.fromtimestamp(ts, dt.UTC)
    else:
        d = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.UTC)
    return d.astimezone(TZ).strftime("%a %d %H:%M")


def section(name):
    print(f"\n== {name} ==")


def fail(name, e):
    print(f"KLAIDA {name}: {type(e).__name__}: {str(e)[:200]}")


def user_name(u):
    return (u or {}).get("displayName") or (u or {}).get("plexUsername") or (u or {}).get("username") or (u or {}).get("email", "?").split("@")[0]


# ---------- sources ----------

def arr_instances():
    out = []
    for app, kind in (("RADARR", "radarr"), ("SONARR", "sonarr"), ("RADARR_RU", "radarr"), ("SONARR_RU", "sonarr")):
        url, key = os.environ.get(app + "_URL"), os.environ.get(app + "_API_KEY")
        if url and key:
            out.append((app.lower().replace("_", "-"), kind, url.rstrip("/"), key))
    return out


def arr_get(inst, path):
    _, _, url, key = inst
    return get_json(url + path, headers={"X-Api-Key": key})


def seerr_requests(since):
    key = os.environ.get("SEERR_API_KEY") or doppler("SEERR_API_KEY")
    h = {"X-Api-Key": key}
    reqs, skip = [], 0
    while True:
        r = get_json(f"{SEERR}/request?take=100&skip={skip}&sort=added&filter=all", headers=h)
        page = r.get("results", [])
        reqs += page
        if not page or page[-1]["createdAt"] < since.isoformat() or len(page) < 100:
            break
        skip += 100
    reqs = [q for q in reqs if q["createdAt"] >= since.strftime("%Y-%m-%dT%H:%M:%S")]
    titles = {}
    for q in reqs:
        m = q["media"]
        k = (m["mediaType"], m["tmdbId"])
        if k not in titles:
            try:
                d = get_json(f"{SEERR}/{m['mediaType']}/{m['tmdbId']}", headers=h)
                year = (d.get("releaseDate") or d.get("firstAirDate") or "")[:4]
                titles[k] = f"{d.get('title') or d.get('name')} ({year})" if year else (d.get("title") or d.get("name"))
            except Exception:  # noqa: BLE001
                titles[k] = f"tmdb {m['tmdbId']}"
    return reqs, titles


def find_inst(kind, ru):
    want = f"{kind}-ru" if ru else kind
    return next((i for i in arr_instances() if i[0] == want), None)


_queues = {}


def queue_ids(inst):
    if inst[0] not in _queues:
        q = arr_get(inst, "/api/v3/queue?pageSize=500")
        _queues[inst[0]] = {r.get("movieId") or r.get("seriesId") for r in q.get("records", [])}
    return _queues[inst[0]]


def waiting_reason(q):
    """Why a request is not in the library yet, from the *arr side."""
    m = q["media"]
    try:
        if q["type"] == "movie":
            inst = find_inst("radarr", q.get("serverId") == 1)
            if not inst:
                return ""
            mv = arr_get(inst, f"/api/v3/movie?tmdbId={m['tmdbId']}")
            if not mv:
                return "not in Radarr"
            mv = mv[0]
            if mv.get("hasFile"):
                return "file in Radarr, Plex scan pending"
            if mv["id"] in queue_ids(inst):
                return "downloading"
            if not mv.get("isAvailable"):
                return f"not released yet ({mv.get('status')}, digital {str(mv.get('digitalRelease') or mv.get('physicalRelease') or '?')[:10]})"
            return "released, nothing grabbed yet (no release matched the profile so far)"
        inst = find_inst("sonarr", q.get("serverId") == 1)
        if not inst or not m.get("tvdbId"):
            return ""
        se = arr_get(inst, f"/api/v3/series?tvdbId={m['tvdbId']}")
        if not se:
            return "not in Sonarr"
        se = se[0]
        wanted = {s["seasonNumber"] for s in q.get("seasons") or []}
        st = [x["statistics"] for x in se.get("seasons", []) if not wanted or x["seasonNumber"] in wanted]
        have, total = sum(x.get("episodeFileCount", 0) for x in st), sum(x.get("totalEpisodeCount", 0) for x in st)
        dl = " , downloading" if se["id"] in queue_ids(inst) else ""
        return f"{have}/{total} episodes on disk{dl}"
    except Exception as e:  # noqa: BLE001
        return f"status unknown ({type(e).__name__})"


def cleanuparr_events(since):
    pw = doppler("CLEANUPARR_PASSWORD")
    tok = get_json(CLEANUPARR + "/api/auth/login", data={"username": os.environ.get("CLEANUPARR_USER", "aurimas"), "password": pw})["tokens"]["accessToken"]
    key = get_json(CLEANUPARR + "/api/account/api-key", headers={"Authorization": "Bearer " + tok})["apiKey"]
    h = {"X-Api-Key": key}
    q = urllib.parse.urlencode({"fromDate": since.isoformat(), "pageSize": 1000})
    events, page = [], 1
    while True:
        r = get_json(f"{CLEANUPARR}/api/events?{q}&page={page}", headers=h)
        events += r.get("items", [])
        if page >= (r.get("totalPages") or 1):
            break
        page += 1
    seeker = get_json(f"{CLEANUPARR}/api/seeker/search-stats/summary", headers=h)
    general = get_json(f"{CLEANUPARR}/api/configuration/general", headers=h)
    return events, seeker, general


def qbit():
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    data = urllib.parse.urlencode({"username": doppler("QBITTORRENT_USERNAME"), "password": doppler("QBITTORRENT_PASSWORD")}).encode()
    body = opener.open(urllib.request.Request(QBIT + "/auth/login", data=data, headers={"Referer": QBIT}), timeout=TIMEOUT).read()
    if body.strip() != b"Ok.":
        raise RuntimeError("qBittorrent login failed")
    state = json.loads(opener.open(QBIT + "/sync/maindata", timeout=TIMEOUT).read())["server_state"]
    torrents = json.loads(opener.open(QBIT + "/torrents/info", timeout=TIMEOUT).read())
    return state, torrents


def plex_history(since):
    base, tok = os.environ["PLEX_URL"].rstrip("/"), os.environ["PLEX_TOKEN"]

    def plex(path, **q):
        q["X-Plex-Token"] = tok
        return get_json(base + path + "?" + urllib.parse.urlencode(q))["MediaContainer"]

    hist = plex("/status/sessions/history/all", sort="viewedAt:desc", **{"viewedAt>": int(since.timestamp())}).get("Metadata", [])
    accounts = {a["id"]: a["name"] for a in plex("/accounts").get("Account", [])}
    return hist, accounts


def vm(query):
    r = get_json(VM + "?" + urllib.parse.urlencode({"query": query}))["data"]["result"]
    return float(r[0]["value"][1]) if r else None


# ---------- report ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    a = ap.parse_args()
    now = dt.datetime.now(dt.UTC)
    since = now - dt.timedelta(days=a.days)
    print(f"MEDIA WEEKLY {since.astimezone(TZ):%Y-%m-%d %H:%M} -> {now.astimezone(TZ):%Y-%m-%d %H:%M} (Vilnius), {a.days} d")

    # --- requests (VSeerr) ---
    section("REQUESTS (VSeerr)")
    requested_tmdb = {}
    try:
        reqs, titles = seerr_requests(since)
        status = {1: "pending", 2: "approved", 3: "declined", 4: "failed", 5: "completed"}
        mstatus = {1: "unknown", 2: "pending", 3: "processing", 4: "partly available", 5: "available", 6: "blocklisted", 7: "deleted"}
        per_user = collections.Counter(user_name(q["requestedBy"]) for q in reqs)
        print(f"requests: {len(reqs)} (movies {sum(q['type'] == 'movie' for q in reqs)}, tv {sum(q['type'] == 'tv' for q in reqs)}) from {len(per_user)} users")
        for u, n in per_user.most_common():
            print(f"  user {u}: {n}")
        for q in sorted(reqs, key=lambda q: q["createdAt"]):
            m = q["media"]
            t = titles[(m["mediaType"], m["tmdbId"])]
            seasons = ",".join(str(s["seasonNumber"]) for s in q.get("seasons") or [])
            version = "RU" if q.get("serverId") == 1 else ("4K" if q.get("is4k") else "")
            requested_tmdb[(m["mediaType"], m["tmdbId"])] = (user_name(q["requestedBy"]), q["createdAt"])
            why = waiting_reason(q) if m.get("status") not in (5,) and q["status"] in (1, 2) else ""
            print(f"  {local(q['createdAt'])} | {user_name(q['requestedBy'])} | {q['type']} | {t}"
                  f"{' S' + seasons if seasons else ''}{' [' + version + ']' if version else ''}"
                  f" | request {status.get(q['status'], q['status'])}, now {mstatus.get(m.get('status'), m.get('status'))}"
                  f"{' | ' + why if why else ''}")
    except Exception as e:  # noqa: BLE001
        fail("seerr", e)

    # --- downloads / upgrades / deletions (arr history) ---
    section("LIBRARY CHANGES (Radarr/Sonarr history)")
    imported_bytes = 0
    for inst in arr_instances():
        name, kind, _, _ = inst
        try:
            extra = "&includeMovie=true" if kind == "radarr" else "&includeSeries=true&includeEpisode=true"
            hist = arr_get(inst, f"/api/v3/history/since?date={since.strftime('%Y-%m-%dT%H:%M:%SZ')}{extra}")
        except Exception as e:  # noqa: BLE001
            fail(name, e)
            continue
        ev = collections.Counter(h["eventType"] for h in hist)
        print(f"{name}: " + (", ".join(f"{k} {v}" for k, v in ev.most_common()) or "no activity"))
        if not hist:
            continue
        # upgrades = an import whose item also had a file deleted with reason Upgrade in the window
        upgraded_ids = {(h.get("movieId") or h.get("episodeId")) for h in hist
                        if h["eventType"] in ("movieFileDeleted", "episodeFileDeleted") and (h.get("data") or {}).get("reason") == "Upgrade"}
        old_size = collections.defaultdict(int)
        for h in hist:
            if h["eventType"] in ("movieFileDeleted", "episodeFileDeleted") and (h.get("data") or {}).get("reason") == "Upgrade":
                old_size[h.get("movieId") or h.get("episodeId")] += int((h.get("data") or {}).get("size") or 0)
        groups = collections.OrderedDict()
        for h in sorted(hist, key=lambda h: h["date"]):
            if h["eventType"] != "downloadFolderImported":
                continue
            d = h.get("data") or {}
            size = int(d.get("size") or 0)
            imported_bytes += size
            if kind == "radarr":
                mv = h.get("movie") or {}
                title = f"{mv.get('title')} ({mv.get('year')})"
                key = ("movie", h.get("movieId"))
                tmdb = ("movie", mv.get("tmdbId"))
            else:
                se, ep = h.get("series") or {}, h.get("episode") or {}
                title = f"{se.get('title')} S{ep.get('seasonNumber', 0):02d}"
                key = ("season", h.get("seriesId"), ep.get("seasonNumber"))
                tmdb = ("tv", se.get("tmdbId"))
            item_id = h.get("movieId") or h.get("episodeId")
            g = groups.setdefault(key, {"title": title, "n": 0, "size": 0, "old": 0, "upg": 0, "quality": set(),
                                        "indexer": set(), "when": h["date"], "tmdb": tmdb, "cf": set()})
            g["n"] += 1
            g["size"] += size
            g["quality"].add(((h.get("quality") or {}).get("quality") or {}).get("name", "?"))
            g["cf"].update(c.get("name") for c in h.get("customFormats") or [])
            if item_id in upgraded_ids:
                g["upg"] += 1
                g["old"] += old_size.get(item_id, 0)
        grabs = {}
        for h in hist:
            if h["eventType"] == "grabbed":
                grabs[h.get("downloadId")] = (h.get("data") or {}).get("indexer", "").replace(" (Prowlarr)", "")
        for h in hist:
            if h["eventType"] == "downloadFolderImported":
                key = ("movie", h.get("movieId")) if kind == "radarr" else ("season", h.get("seriesId"), (h.get("episode") or {}).get("seasonNumber"))
                if h.get("downloadId") in grabs and key in groups:
                    groups[key]["indexer"].add(grabs[h["downloadId"]])
        for g in groups.values():
            unit = "" if kind == "radarr" else f" {g['n']} ep"
            tag = "UPGRADE" if g["upg"] else "NEW"
            upg = f", replaced {gb(g['old'])}" if g["upg"] and g["old"] else ""
            who, took = requested_tmdb.get(g["tmdb"], (None, None)), ""
            if who[1]:
                secs = (dt.datetime.fromisoformat(g["when"].replace("Z", "+00:00")) - dt.datetime.fromisoformat(who[1].replace("Z", "+00:00"))).total_seconds()
                if secs > 0:
                    took = f", ready {secs / 3600:.1f} h after the request" if secs < 172800 else f", ready {secs / 86400:.1f} d after the request"
            who = who[0]
            cfs = ", ".join(sorted(c for c in g["cf"] if c))[:80]
            print(f"  {tag} {local(g['when'])} | {g['title']}{unit} | {'/'.join(sorted(g['quality']))} | {gb(g['size'])}{upg}"
                  f"{' | via ' + '/'.join(sorted(i for i in g['indexer'] if i)) if g['indexer'] else ''}"
                  f"{' | CF ' + cfs if cfs else ''}{' | requested by ' + who + took if who else ''}")
        deleted = [h for h in hist if h["eventType"] in ("movieFileDeleted", "episodeFileDeleted") and (h.get("data") or {}).get("reason") != "Upgrade"]
        if deleted:
            dsz = sum(int((h.get("data") or {}).get("size") or 0) for h in deleted)
            reasons = collections.Counter((h.get("data") or {}).get("reason") for h in deleted)
            print(f"  library files deleted (not upgrades): {len(deleted)}, {gb(dsz)}, reasons {dict(reasons)}")
        failed = [h for h in hist if h["eventType"] in ("downloadFailed", "importFailed")]
        if failed:
            print(f"  failed downloads/imports: {len(failed)}: " + "; ".join(sorted({h.get('sourceTitle', '?')[:50] for h in failed}))[:300])
        ignored = [h for h in hist if h["eventType"] == "downloadIgnored"]
        if ignored:
            print(f"  downloads ignored by hand: {len(ignored)} episodes, " + "; ".join(sorted({h.get('sourceTitle', '?')[:50] for h in ignored}))[:200])
    print(f"total imported into the library: {size_h(imported_bytes)}")

    # --- Cleanuparr ---
    section("CLEANUPARR")
    try:
        events, seeker, general = cleanuparr_events(since)
        live = [e for e in events if not e.get("isDryRun")]
        print(f"dry run: {general.get('dryRun')} | events this week: {len(live)} live, {len(events) - len(live)} dry-run")
        cleaned = [e for e in live if e["eventType"] == "DownloadCleaned"]
        if cleaned:
            reasons = collections.Counter(e.get("cleanReason") for e in cleaned)
            cats = collections.Counter(e.get("cleanedCategory") for e in cleaned)
            print(f"download cleaner: {len(cleaned)} torrents deleted after meeting their seeding rule; reasons {dict(reasons)}; categories {dict(cats)}")
            avg_ratio = sum(e.get("seedRatio") or 0 for e in cleaned) / len(cleaned)
            avg_days = sum(e.get("seedingTimeHours") or 0 for e in cleaned) / len(cleaned) / 24
            print(f"  average ratio at deletion {avg_ratio:.2f}, average seeding {avg_days:.0f} days")
            for e in sorted(cleaned, key=lambda e: -(e.get("seedingTimeHours") or 0))[:3]:
                print(f"  longest seeded: {e['itemTitle'][:60]} ({(e.get('seedingTimeHours') or 0) / 24:.0f} d, ratio {e.get('seedRatio') or 0:.2f})")
            for e in sorted(cleaned, key=lambda e: -(e.get("seedRatio") or 0))[:3]:
                print(f"  best ratio: {e['itemTitle'][:60]} (ratio {e.get('seedRatio') or 0:.2f})")
        removed = [e for e in live if e["eventType"] in ("QueueItemDeleted", "DownloadMarkedForDeletion")]
        for e in removed:
            print(f"  queue removal {local(e['timestamp'])}: {e.get('itemTitle', '?')[:70]} | {e.get('deleteReason') or e.get('message', '')[:60]}")
        strikes = collections.Counter(e["eventType"] for e in live if e["eventType"].endswith("Strike"))
        if strikes:
            top = collections.Counter(e.get("itemTitle") for e in live if e["eventType"].endswith("Strike"))
            print(f"  strikes: {dict(strikes)}; most struck: " + "; ".join(f"{t[:50]} x{n}" for t, n in top.most_common(3)))
        searches = [e for e in live if e["eventType"] == "SearchTriggered"]
        grabbed = [e for e in searches if e.get("grabbedItems")]
        reasons = collections.Counter(e.get("searchReason") for e in searches)
        print(f"seeker: {len(searches)} searches ({dict(reasons)}), {len(grabbed)} found something")
        for e in grabbed[:8]:
            print(f"  search found: {e.get('itemTitle', '?')[:60]} -> {', '.join(str(g)[:60] for g in e['grabbedItems'])[:120]}")
        for i in seeker.get("perInstanceStats", []):
            print(f"  {i['instanceName']}: cycle {i.get('cycleItemsSearched')}/{i.get('cycleItemsTotal')} items")
        print(f"  pending replacement searches: {seeker.get('pendingReplacementSearches')}")
        other = collections.Counter(e["eventType"] for e in live if e["eventType"] not in ("DownloadCleaned", "SearchTriggered") and not e["eventType"].endswith("Strike"))
        if other:
            print(f"  other events: {dict(other)}")
    except Exception as e:  # noqa: BLE001
        fail("cleanuparr", e)

    # --- qBittorrent ---
    section("QBITTORRENT")
    state_file = {}
    try:
        with open(STATE) as f:
            state_file = json.load(f)
    except (OSError, ValueError):
        pass
    try:
        s, torrents = qbit()
        ul, dl = s.get("alltime_ul") or 0, s.get("alltime_dl") or 0
        prev = state_file.get("qbit")
        if prev and a.days == 7 and ul >= prev["ul"]:
            days = (time.time() - prev["ts"]) / 86400
            print(f"this week (since {local(prev['ts'])}, {days:.1f} d): uploaded {size_h(ul - prev['ul'])}, downloaded {size_h(dl - prev['dl'])}")
        else:
            print("this week: no previous snapshot yet (first run or counters reset); all-time totals only")
        print(f"all-time: uploaded {size_h(ul)}, downloaded {size_h(dl)}, ratio {s.get('global_ratio')}")
        private = sum(1 for t in torrents if t.get("private"))
        by_state = collections.Counter(t["state"] for t in torrents)
        print(f"torrents: {len(torrents)} ({private} private), total {size_h(sum(t['size'] for t in torrents))}; states {dict(by_state.most_common(6))}")
        prev_t = state_file.get("qbit_torrents") or {}
        if prev_t and a.days == 7:
            deltas = sorted(((t.get("uploaded", 0) - prev_t[t["hash"]], t) for t in torrents if t["hash"] in prev_t), key=lambda x: -x[0])
            active = [(d, t) for d, t in deltas if d > 0]
            print(f"torrents that uploaded this week: {len(active)} of {len(torrents)}")
            for d, t in active[:5]:
                print(f"  top seeder: {t['name'][:55]} {size_h(d)} ({t.get('category')})")
        new_t = [t for t in torrents if t.get("added_on", 0) >= since.timestamp()]
        if new_t:
            print(f"torrents added this week: {len(new_t)}, {size_h(sum(t['size'] for t in new_t))}")
        stuck = [t for t in torrents if t["state"] in ("stalledDL", "metaDL") and t["progress"] < 1]
        if stuck:
            print(f"stuck downloads: {len(stuck)}: " + "; ".join(f"{t['name'][:45]} {t['progress'] * 100:.0f}%" for t in stuck[:5]))
        missing = [t for t in torrents if t["state"] == "missingFiles"]
        if missing:
            print(f"missingFiles (dead, files gone): {len(missing)}")
        if a.days == 7:
            state_file["qbit"] = {"ts": time.time(), "ul": ul, "dl": dl}
            state_file["qbit_torrents"] = {t["hash"]: t.get("uploaded", 0) for t in torrents}
    except Exception as e:  # noqa: BLE001
        fail("qbittorrent", e)

    # --- disk ---
    section("DISK (/data pool)")
    try:
        sel = '{mountpoint=~"/mnt/hdd.*"}'
        size = vm(f"sum(node_filesystem_size_bytes{sel})")
        free = vm(f"sum(node_filesystem_avail_bytes{sel})")
        free_then = vm(f"sum(node_filesystem_avail_bytes{sel} offset {a.days}d)")
        used = size - free
        print(f"used {size_h(used)} of {size_h(size)} ({used / size * 100:.0f}%), free {size_h(free)}")
        if free_then is not None:
            delta = free - free_then
            print(f"free space change over the window: {'+' if delta >= 0 else '-'}{size_h(abs(delta))}")
    except Exception as e:  # noqa: BLE001
        fail("disk", e)

    # --- library totals ---
    section("LIBRARY SIZE")
    try:
        lib = {}
        for inst in arr_instances():
            name, kind, _, _ = inst
            if kind == "radarr":
                mv = arr_get(inst, "/api/v3/movie")
                lib[name] = {"items": sum(1 for m in mv if m.get("hasFile")), "bytes": sum(m.get("sizeOnDisk") or 0 for m in mv), "unit": "movies"}
            else:
                se = arr_get(inst, "/api/v3/series")
                lib[name] = {"items": sum((x.get("statistics") or {}).get("episodeFileCount", 0) for x in se),
                             "bytes": sum((x.get("statistics") or {}).get("sizeOnDisk", 0) for x in se), "unit": f"episodes in {len(se)} series"}
        prev = state_file.get("library") or {}
        for name, v in lib.items():
            p = prev.get(name)
            chg = ""
            if p and a.days == 7:
                db = v["bytes"] - p["bytes"]
                chg = f" | since last report {v['items'] - p['items']:+d} items, {'+' if db >= 0 else '-'}{size_h(abs(db))}"
            print(f"{name}: {v['items']} {v['unit']}, {size_h(v['bytes'])}{chg}")
        if a.days == 7:
            state_file["library"] = {k: {"items": v["items"], "bytes": v["bytes"]} for k, v in lib.items()}
    except Exception as e:  # noqa: BLE001
        fail("library", e)

    # --- Plex ---
    section("PLEX PLAYS")
    try:
        hist, accounts = plex_history(since)
        users = collections.Counter(accounts.get(h.get("accountID"), str(h.get("accountID"))) for h in hist)
        titles = collections.Counter(h.get("grandparentTitle") or h.get("title") for h in hist)
        types = collections.Counter(h.get("type") for h in hist)
        print(f"plays: {len(hist)} ({types.get('episode', 0)} episodes, {types.get('movie', 0)} movies) by {len(users)} accounts")
        for u, n in users.most_common(6):
            print(f"  viewer {u}: {n}")
        for t, n in titles.most_common(6):
            print(f"  watched {t}: {n}")
        movies = [h["title"] for h in hist if h.get("type") == "movie"]
        if movies:
            print("  movies watched: " + "; ".join(sorted(set(movies)))[:300])
    except Exception as e:  # noqa: BLE001
        fail("plex", e)

    if a.days == 7 and state_file:
        try:
            os.makedirs(os.path.dirname(STATE), exist_ok=True)
            with open(STATE, "w") as f:
                json.dump(state_file, f)
        except OSError as e:
            fail("state", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
