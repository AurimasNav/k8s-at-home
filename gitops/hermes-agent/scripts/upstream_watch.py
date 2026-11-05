#!/usr/bin/env python3
"""Daily upstream watch for the k8s-at-home issue board. Driven by labels, nothing hard-coded.

The board is the set of open issues in AurimasNav/k8s-at-home that carry one status label:
  needs-review      waiting for Aurimas to read (a draft, or upstream moved)
  needs-decision    waiting for Aurimas to choose
  waiting-upstream  reviewed; nothing for us to do until upstream moves
Closed means done; the script drops a status label left on a closed issue.

An issue lists what it watches in a "## Watching upstream" section, one owner/repo#N per line.
For every waiting-upstream issue the script checks each watched ref for activity by someone
else (not the token owner, not bots) since the issue last got the waiting-upstream label:
comments, reviews, commits, merge/close/reopen, draft <-> ready, labels, and a release that
contains a merged PR. If anything moved, it comments the details on the issue and swaps the
label to needs-review, so the next review starts from that comment.

Prints a short report (what moved, what waits for Aurimas) for Telegram; prints nothing when
nothing moved and nothing waits. Needs GH_TOKEN or HERMES_GITHUB_TOKEN (repo scope).

Usage: upstream_watch.py [--dry-run] [--since ISO-TIME] [--issue N] [--repo owner/repo]
  For testing: --since replaces the baseline, --issue checks only that issue,
  --dry-run writes nothing.
"""
import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zoneinfo

API = "https://api.github.com"
TZ = zoneinfo.ZoneInfo("Europe/Vilnius")
REVIEW, DECISION, WAITING = "needs-review", "needs-decision", "waiting-upstream"
STATUS = (REVIEW, DECISION, WAITING)
WATCH_SECTION = re.compile(r"^##\s+Watching upstream\s*$(.*?)(?=^##\s|\Z)", re.I | re.M | re.S)
REF = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)\b")
PHRASE = {
    "commented": "commented",
    "line-commented": "commented on the code",
    "committed": "pushed a commit",
    "head_ref_force_pushed": "force-pushed the branch",
    "merged": "merged it",
    "closed": "closed it",
    "reopened": "reopened it",
    "ready_for_review": "marked it ready for review",
    "convert_to_draft": "turned it back into a draft",
    "review_requested": "requested a review",
    "labeled": "added a label",
}


def token():
    tok = os.environ.get("GH_TOKEN") or os.environ.get("HERMES_GITHUB_TOKEN")
    if not tok:
        raise SystemExit("no GitHub token in env (GH_TOKEN / HERMES_GITHUB_TOKEN)")
    return tok


def call(path, method="GET", body=None):
    """One GitHub REST call; returns (json, next_page_url). Retries 5xx and network errors."""
    url = path if path.startswith("http") else API + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token()}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "upstream-watch"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                nxt = re.search(r'<([^>]+)>;\s*rel="next"', r.headers.get("Link") or "")
                return (json.loads(raw) if raw else None), (nxt.group(1) if nxt else None)
        except urllib.error.HTTPError as exc:
            if exc.code < 500 or attempt == 3:
                raise
        except urllib.error.URLError:
            if attempt == 3:
                raise
        time.sleep(5 * (attempt + 1))
    raise RuntimeError("unreachable")


def get(path):
    return call(path)[0]


def pages(path):
    sep = "&" if "?" in path else "?"
    url, out = f"{path}{sep}per_page=100", []
    while url:
        chunk, url = call(url)
        out.extend(chunk)
    return out


def when(value):
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def local(dt):
    return dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M")


def one_line(text, n=90):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def watched_refs(body):
    m = WATCH_SECTION.search(body or "")
    refs = []
    for repo, num in REF.findall(m.group(1) if m else ""):
        if (repo, int(num)) not in refs:
            refs.append((repo, int(num)))
    return refs


def baseline(repo, number, fallback):
    """When the issue last got the waiting-upstream label."""
    times = [when(e["created_at"]) for e in pages(f"/repos/{repo}/issues/{number}/events")
             if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == WAITING]
    return max(times) if times else when(fallback)


def is_bot(user):
    return bool(user) and (user.get("type") == "Bot" or user.get("login", "").endswith("[bot]"))


def activity(repo, number, since, me):
    """Things someone else did on repo#number after `since`: list of (time, text, link)."""
    item = get(f"/repos/{repo}/issues/{number}")
    ref, link = f"{repo}#{number}", item["html_url"]
    found = []
    for ev in pages(f"/repos/{repo}/issues/{number}/timeline"):
        kind = ev.get("event")
        if kind == "line-commented":
            entries = [(when(c.get("created_at")), c.get("user"), c.get("html_url"), c.get("body"))
                       for c in ev.get("comments") or []]
        elif kind == "committed":
            author = ev.get("author") or {}
            if author.get("email") in me["emails"] or author.get("name") == me["name"]:
                continue
            entries = [(when((ev.get("committer") or {}).get("date")), {"login": author.get("name")},
                        ev.get("html_url"), ev.get("message"))]
        elif kind == "reviewed":
            entries = [(when(ev.get("submitted_at")), ev.get("user"), ev.get("html_url"), ev.get("body"))]
        elif kind in PHRASE:
            entries = [(when(ev.get("created_at")), ev.get("actor") or ev.get("user"),
                        ev.get("html_url"), ev.get("body"))]
        else:
            continue  # subscribed, mentioned, cross-referenced and the like are not progress
        for t, user, url, text in entries:
            login = (user or {}).get("login") or "someone"
            if not t or t <= since or login == me["login"] or is_bot(user):
                continue
            if kind == "reviewed":
                what = f"reviewed it ({(ev.get('state') or '').lower().replace('_', ' ')})"
            elif kind == "labeled":
                what = f"added the label `{(ev.get('label') or {}).get('name')}`"
            else:
                what = PHRASE[kind]
            note = f": “{one_line(text)}”" if text and kind in ("commented", "line-commented", "reviewed") else ""
            found.append((t, f"{ref}: {login} {what} on {local(t)}{note}", url or link))
    if item.get("pull_request"):
        found += released(repo, number, since)
    return sorted(found)


def released(repo, number, since):
    """A release published after `since` that contains the merged PR."""
    pr = get(f"/repos/{repo}/pulls/{number}")
    if not pr.get("merged"):
        return []
    merged_at, sha = when(pr["merged_at"]), pr["merge_commit_sha"]
    try:
        releases = get(f"/repos/{repo}/releases?per_page=20") or []
    except urllib.error.HTTPError:
        return []
    for rel in sorted(releases, key=lambda r: (bool(r.get("prerelease")), r.get("published_at") or "")):
        pub = when(rel.get("published_at"))
        if rel.get("draft") or not pub or pub <= max(since, merged_at):
            continue
        tag = urllib.parse.quote(rel["tag_name"], safe="")
        status = get(f"/repos/{repo}/compare/{tag}...{sha}").get("status")
        if status in ("behind", "identical"):  # the tag already contains the merge commit
            kind = "pre-release" if rel.get("prerelease") else "release"
            return [(pub, f"{repo}#{number}: in {kind} {rel['tag_name']} ({local(pub)})", rel["html_url"])]
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="AurimasNav/k8s-at-home")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--since", help="ISO time that replaces every issue's baseline (testing)")
    ap.add_argument("--issue", type=int, help="only check this issue (testing)")
    args = ap.parse_args()
    repo = args.repo
    user = get("/user")
    # Commits only carry a git name/email, not a login: skip the ones made as Aurimas.
    me = {"login": user["login"], "name": user.get("name") or "Aurimas Navardauskas",
          "emails": {"aurimas.navardauskas@outlook.com"}}

    board, seen = {s: [] for s in STATUS}, set()
    for status in STATUS:
        for issue in pages(f"/repos/{repo}/issues?state=open&labels={status}"):
            names = {lb["name"] for lb in issue["labels"]}
            if issue.get("pull_request") or "tracking" in names or issue["number"] in seen:
                continue
            seen.add(issue["number"])
            board[next(s for s in STATUS if s in names)].append(issue)

    moved, problems = [], []
    for issue in list(board[WAITING]):
        if args.issue and issue["number"] != args.issue:
            continue
        refs = watched_refs(issue.get("body"))
        if not refs:
            problems.append(f"#{issue['number']} has no owner/repo#N under '## Watching upstream'")
            continue
        since = when(args.since) if args.since else baseline(repo, issue["number"], issue["created_at"])
        if since.tzinfo is None:
            since = since.replace(tzinfo=TZ)
        news = []
        for ref_repo, num in refs:
            try:
                news += activity(ref_repo, num, since, me)
            except (urllib.error.URLError, KeyError, ValueError) as exc:
                problems.append(f"#{issue['number']} {ref_repo}#{num}: {exc}")
        if not news:
            continue
        moved.append((issue, news))
        board[WAITING].remove(issue)
        board[REVIEW].insert(0, issue)
        if args.dry_run:
            continue
        lines = [f"Upstream moved since {local(since)} (Vilnius), so this is back in **{REVIEW}**:"]
        lines += [f"- {text} → {url}" for _, text, url in news]
        call(f"/repos/{repo}/issues/{issue['number']}/comments", "POST", {"body": "\n".join(lines)})
        call(f"/repos/{repo}/issues/{issue['number']}/labels", "POST", {"labels": [REVIEW]})
        try:
            call(f"/repos/{repo}/issues/{issue['number']}/labels/{WAITING}", "DELETE")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise

    stale = []
    for status in STATUS if not args.issue else ():
        for issue in pages(f"/repos/{repo}/issues?state=closed&labels={status}"):
            if issue.get("pull_request"):
                continue
            stale.append(f"#{issue['number']}")
            if not args.dry_run:
                call(f"/repos/{repo}/issues/{issue['number']}/labels/{status}", "DELETE")

    waiting_for_you = board[REVIEW] + board[DECISION]
    if not moved and not waiting_for_you and not problems:
        return 0
    out = [("DRY RUN: " if args.dry_run else "")
           + f"🔭 Upstream watch: {len(moved)} moved, {len(waiting_for_you)} wait for you, "
           f"{len(board[WAITING])} waiting upstream"]
    if moved:
        out.append("🔔 Upstream moved (now needs-review):")
        for issue, news in moved:
            out.append(f"- #{issue['number']} {issue['title']}: {issue['html_url']}")
            out += [f"  • {text}" for _, text, _ in news[:6]]
            if len(news) > 6:
                out.append(f"  • …and {len(news) - 6} more (see the issue comment)")
    for status in (REVIEW, DECISION):
        for issue in board[status]:
            if issue not in [i for i, _ in moved]:
                out.append(f"👀 {status}: #{issue['number']} {issue['title']}: {issue['html_url']}")
    if board[WAITING]:
        out.append("⏳ waiting-upstream: " + ", ".join(f"#{i['number']}" for i in board[WAITING]))
    if stale:
        out.append(f"🧹 dropped status labels from closed issues: {', '.join(stale)}")
    if problems:
        out.append("⚠️ could not check: " + "; ".join(problems))
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
