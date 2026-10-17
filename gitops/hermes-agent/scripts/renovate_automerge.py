#!/usr/bin/env python3
"""Daily Renovate auto-merge for the k8s-at-home GitOps repo.

Merges open Renovate PRs one at a time, waits for ArgoCD to roll each one out,
and prints a plain-text report. A Hermes cron job turns the report into a
Telegram summary.

A PR is merged only when:
- it was opened by Renovate, is not a draft, and has no merge conflict
- the PR checks (Lint, Render, Security) all passed on its head commit
- it is not a major update (those need a human)
- it changes more than comments (Renovate only bumps the URL comment of vendored
  HA integrations; the code must be updated by hand first)
- it does not roll the Hermes pod (a restart would cut this job off mid-run)

After each merge, every ArgoCD app whose path the PR touches must reach
Synced/Healthy on the new commit, with all pods Ready, before the next PR is
merged. A failed rollout stops the run; nothing is reverted automatically.

Conflicting PRs and PRs without checks get Renovate's rebase box ticked.

Usage:
  renovate_automerge.py                       merge eligible PRs (cron)
  renovate_automerge.py --dry-run             only evaluate open PRs
  renovate_automerge.py --dry-run --pr 817    evaluate given PRs (open or merged);
                                              for merged ones, also check their rollout
Secrets come from the environment only: GH_TOKEN or HERMES_GITHUB_TOKEN, HA_URL, HA_TOKEN.
"""
import argparse
import calendar
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = "AurimasNav/k8s-at-home"
API = "https://api.github.com"
RENOVATE_LOGINS = {"renovate[bot]"}
REQUIRED_CHECKS = ("Lint", "Render", "Security")
BAD_CONCLUSIONS = {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}
HERMES_NS, HERMES_DEPLOY, HERMES_APP_PATH = "hermes-agent", "hermes-agent", "gitops/hermes-agent/"
ROLLOUT_TIMEOUT = 15 * 60
RUN_BUDGET = 30 * 60
POLL = 15
REBASE_UNCHECKED = "- [ ] <!-- rebase-check -->"
KUBECTL = shutil.which("kubectl") or "/usr/local/sbin/kubectl"
START = time.monotonic()


# ---------- GitHub ----------

def gh(method, path, body=None):
    token = os.environ.get("GH_TOKEN") or os.environ.get("HERMES_GITHUB_TOKEN")
    if not token:
        raise RuntimeError("no GitHub token in env (GH_TOKEN / HERMES_GITHUB_TOKEN)")
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "hermes-renovate-automerge"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def gh_pages(path):
    out, page = [], 1
    while True:
        sep = "&" if "?" in path else "?"
        chunk = gh("GET", f"{path}{sep}per_page=100&page={page}")
        items = chunk.get("check_runs", chunk) if isinstance(chunk, dict) else chunk
        out.extend(items)
        if len(items) < 100:
            return out
        page += 1


def pr_details(num):
    """PR JSON with `mergeable` resolved (GitHub computes it lazily)."""
    pr = gh("GET", f"/repos/{REPO}/pulls/{num}")
    for _ in range(6):
        if pr.get("merged") or pr.get("mergeable") is not None:
            break
        time.sleep(3)
        pr = gh("GET", f"/repos/{REPO}/pulls/{num}")
    return pr


def check_state(sha):
    """('pass'|'pending'|'fail'|'none', detail) for the PR checks on a commit."""
    runs = gh_pages(f"/repos/{REPO}/commits/{sha}/check-runs")
    latest = {}
    for r in runs:
        if r["name"] not in latest or r["id"] > latest[r["name"]]["id"]:
            latest[r["name"]] = r
    if not latest:
        return "none", "no checks ran"
    bad = [n for n, r in latest.items() if r.get("conclusion") in BAD_CONCLUSIONS]
    if bad:
        return "fail", "failed: " + ", ".join(sorted(bad))
    pending = [n for n, r in latest.items() if r["status"] != "completed"]
    if pending:
        return "pending", "still running: " + ", ".join(sorted(pending))
    missing = [c for c in REQUIRED_CHECKS if not any(n.startswith(c) for n in latest)]
    if missing:
        return "none", "missing checks: " + ", ".join(missing)
    return "pass", f"{len(latest)} checks passed"


def update_type(pr):
    m = re.search(r"\|\s*(major|minor|patch|digest|pin|pinDigest|lockFileMaintenance)\s*\|", pr.get("body") or "")
    return m.group(1) if m else "unknown"


def change_summary(pr):
    m = re.search(r"`([^`]+)`\s*(?:->|→)\s*`([^`]+)`", pr.get("body") or "")
    return f"{m.group(1)} → {m.group(2)}" if m else ""


def changed_lines(patch):
    return [ln[1:] for ln in (patch or "").splitlines()
            if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]


def comment_only(files):
    lines = [ln.strip() for f in files for ln in changed_lines(f.get("patch"))]
    return bool(lines) and all(ln == "" or ln.startswith("#") for ln in lines)


# ---------- Kubernetes / ArgoCD ----------

def kubectl_json(*args):
    out = subprocess.run([KUBECTL, *args, "-o", "json"], capture_output=True, text=True, timeout=60, check=False)  # noqa: S603
    if out.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout)


def argo_apps():
    return {a["metadata"]["name"]: a for a in kubectl_json("-n", "argocd", "get", "applications")["items"]}


def image_repo(image):
    image = image.split("@", 1)[0]
    head, _, tail = image.rpartition(":")
    return head if head and "/" not in tail else image


def hermes_images():
    d = kubectl_json("-n", HERMES_NS, "get", "deploy", HERMES_DEPLOY)["spec"]["template"]["spec"]
    return {image_repo(c["image"]) for c in d.get("containers", []) + d.get("initContainers", [])}


def restarts_hermes(files):
    """True if merging these files would roll the Hermes pod."""
    mine = [f for f in files if f["filename"].startswith(HERMES_APP_PATH)]
    if not mine:
        return False
    try:
        used = hermes_images()
    except Exception:  # noqa: BLE001 - unknown means assume the worst
        return True
    for f in mine:
        if f["filename"] != HERMES_APP_PATH + "kustomization.yaml":
            return True
        name = None
        for ln in (f.get("patch") or "").splitlines():
            m = re.match(r"^[ +-]\s*- name:\s*(\S+)", ln)
            if m:
                name = m.group(1)
            if ln[:1] in "+-" and not ln.startswith(("+++", "---")):
                if not re.match(r"^[+-]\s*newTag:", ln) or name is None or name in used:
                    return True
    return False


def apps_for(files, apps):
    hit = {}
    for f in files:
        for name, a in apps.items():
            p = (a["spec"].get("source") or {}).get("path", "").rstrip("/")
            if p and (f["filename"] == p or f["filename"].startswith(p + "/")):
                hit[name] = a
    return hit


def workload_images(ns):
    try:
        items = kubectl_json("-n", ns, "get", "deploy,sts,ds")["items"]
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for w in items:
        spec = w["spec"]["template"]["spec"]
        for c in spec.get("containers", []):
            out[f"{w['kind']}/{w['metadata']['name']}/{c['name']}"] = c["image"].split("@", 1)[0]
    return out


def unready_pods(ns):
    bad = []
    for p in kubectl_json("-n", ns, "get", "pods")["items"]:
        phase = p["status"].get("phase")
        if phase == "Succeeded":
            continue
        cs = p["status"].get("containerStatuses") or []
        if phase != "Running" or not cs or not all(c.get("ready") for c in cs):
            bad.append(f"{p['metadata']['name']} ({phase})")
    return bad


def restarted_pods(ns, since_epoch):
    out = []
    for p in kubectl_json("-n", ns, "get", "pods")["items"]:
        for c in p["status"].get("containerStatuses") or []:
            fin = ((c.get("lastState") or {}).get("terminated") or {}).get("finishedAt")
            if fin and calendar.timegm(time.strptime(fin, "%Y-%m-%dT%H:%M:%SZ")) >= since_epoch:
                out.append(f"{p['metadata']['name']}/{c['name']} restarted ({c.get('restartCount')})")
    return out


def contains_commit(rev, sha):
    if rev == sha:
        return True
    try:
        return gh("GET", f"/repos/{REPO}/compare/{sha}...{rev}")["status"] in ("ahead", "identical")
    except Exception:  # noqa: BLE001
        return False


def wait_rollout(name, sha, deadline):
    """Wait until app `name` runs commit `sha` (or later) and is healthy. Returns (ok, detail)."""
    ok_streak, last = 0, ""
    while time.monotonic() < deadline:
        try:
            a = kubectl_json("-n", "argocd", "get", "application", name)
        except Exception as e:  # noqa: BLE001
            last = str(e)
            time.sleep(POLL)
            continue
        st = a.get("status", {})
        rev = st.get("sync", {}).get("revision", "")
        sync, health = st.get("sync", {}).get("status"), st.get("health", {}).get("status")
        op = st.get("operationState", {}) or {}
        phase = op.get("phase")
        last = f"rev {rev[:7]} {sync}/{health} op={phase}"
        if rev and contains_commit(rev, sha):
            if phase in ("Failed", "Error") and (op.get("syncResult") or {}).get("revision") == rev:
                return False, f"sync failed: {(op.get('message') or '')[:200]}"
            if health == "Degraded":
                return False, f"app Degraded on {rev[:7]}"
            ns = a["spec"]["destination"].get("namespace") or "default"
            pods_bad = unready_pods(ns) if sync == "Synced" and health == "Healthy" and phase == "Succeeded" else ["n/a"]
            ok_streak = ok_streak + 1 if not pods_bad else 0
            if ok_streak >= 2:
                return True, f"Synced/Healthy on {rev[:7]}, pods ready in {ns}"
            if pods_bad and pods_bad != ["n/a"]:
                last += " | not ready: " + ", ".join(pods_bad[:4])
        time.sleep(POLL)
    return False, f"timed out after {ROLLOUT_TIMEOUT // 60} min ({last})"


def ha_status():
    url, tok = os.environ.get("HA_URL"), os.environ.get("HA_TOKEN")
    if not url or not tok:
        return "HA check skipped (no HA_URL/HA_TOKEN)"
    try:
        req = urllib.request.Request(url.rstrip("/") + "/api/config", headers={"Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            c = json.loads(r.read())
        return f"HA {c.get('version')} {c.get('state')}"
    except Exception as e:  # noqa: BLE001
        return f"HA API error: {e}"


def cluster_health():
    lines = []
    try:
        bad = [f"{n} {a['status'].get('sync', {}).get('status')}/{a['status'].get('health', {}).get('status')}"
               for n, a in argo_apps().items()
               if (a["status"].get("sync", {}).get("status"), a["status"].get("health", {}).get("status")) != ("Synced", "Healthy")]
        lines.append("ArgoCD apps not Synced/Healthy: " + (", ".join(bad) if bad else "none"))
    except Exception as e:  # noqa: BLE001
        lines.append(f"ArgoCD apps: KLAIDA {e}")
    try:
        pods = kubectl_json("get", "pods", "-A")["items"]
        bad = [f"{p['metadata']['namespace']}/{p['metadata']['name']} ({p['status'].get('phase')})"
               for p in pods if p["status"].get("phase") not in ("Running", "Succeeded")]
        lines.append("Pods not Running/Completed: " + (", ".join(bad[:10]) if bad else "none"))
    except Exception as e:  # noqa: BLE001
        lines.append(f"Pods: KLAIDA {e}")
    return lines


# ---------- main ----------

def evaluate(pr, files, apps):
    """Return (action, reason). action: merge | wait | rebase | hold."""
    if pr.get("draft"):
        return "hold", "draft"
    if not pr.get("merged"):
        if pr.get("mergeable") is False or pr.get("mergeable_state") == "dirty":
            return "rebase", "merge conflict"
    state, detail = check_state(pr["head"]["sha"])
    if state == "fail":
        return "hold", f"checks {detail}"
    if state == "pending":
        return "wait", f"checks {detail}"
    if state == "none":
        return "rebase", f"{detail} (branch predates CI)"
    if update_type(pr) == "major":
        return "hold", "major update, needs a human review"
    if comment_only(files):
        return "hold", "only a comment changed (vendored component: code must be updated by hand)"
    if restarts_hermes(files):
        return "hold", "would restart the Hermes pod; merge it manually"
    return "merge", detail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pr", type=int, action="append", help="evaluate these PRs (dry-run only)")
    args = ap.parse_args()
    if args.pr and not args.dry_run:
        sys.exit("--pr is only allowed with --dry-run")

    try:
        if args.pr:
            prs = [gh("GET", f"/repos/{REPO}/pulls/{n}") for n in args.pr]
        else:
            prs = [p for p in gh_pages(f"/repos/{REPO}/pulls?state=open")
                   if p["user"]["login"] in RENOVATE_LOGINS]
    except Exception as e:  # noqa: BLE001
        print(f"KLAIDA: GitHub API: {e}")
        return
    if not prs:
        print("NO_RENOVATE_PRS")
        return
    prs.sort(key=lambda p: p["number"])

    try:
        apps = argo_apps()
    except Exception as e:  # noqa: BLE001
        print(f"KLAIDA: cannot read ArgoCD applications, nothing merged: {e}")
        return

    print(f"# Renovate auto-merge {'(DRY RUN) ' if args.dry_run else ''}— {REPO}")
    print(f"Open Renovate PRs: {len(prs)}")
    stopped = None
    merged_any = False
    for p in prs:
        num, title = p["number"], p["title"]
        print(f"\n## #{num} {title}")
        try:
            pr = pr_details(num)
            files = gh_pages(f"/repos/{REPO}/pulls/{num}/files")
            hit = apps_for(files, apps)
            chg = change_summary(pr)
            print(f"- type: {update_type(pr)}{' | ' + chg if chg else ''}")
            print(f"- files: {', '.join(f['filename'] for f in files)}")
            print(f"- ArgoCD apps: {', '.join(hit) if hit else 'none (CI/repo-only or app not deployed)'}")
            if stopped:
                print(f"- RESULT: NOT MERGED — run stopped after {stopped}")
                continue
            if time.monotonic() - START > RUN_BUDGET:
                print("- RESULT: NOT MERGED — run time budget used up, left for tomorrow")
                continue
            action, reason = evaluate(pr, files, apps)
            before, t0 = {}, time.time()
            if action != "merge":
                if action == "rebase" and not args.dry_run and REBASE_UNCHECKED in (pr.get("body") or ""):
                    gh("PATCH", f"/repos/{REPO}/pulls/{num}", {"body": pr["body"].replace(REBASE_UNCHECKED, "- [x] <!-- rebase-check -->", 1)})
                    reason += "; asked Renovate to rebase"
                label = {"wait": "WAITING", "rebase": "NOT MERGED", "hold": "HELD FOR HUMAN"}[action]
                print(f"- RESULT: {label} — {reason}")
                continue

            if pr.get("merged"):
                sha = pr["merge_commit_sha"]
                print(f"- already merged as {sha[:7]}; checking its rollout (dry run)")
            elif args.dry_run:
                print(f"- RESULT: WOULD MERGE — {reason}")
                continue
            else:
                before = {n: workload_images(a["spec"]["destination"].get("namespace") or "default") for n, a in hit.items()}
                res = gh("PUT", f"/repos/{REPO}/pulls/{num}/merge", {"merge_method": "squash", "sha": pr["head"]["sha"]})
                sha = res.get("sha")
                if not res.get("merged") or not sha:
                    print(f"- RESULT: MERGE FAILED — {res.get('message')}")
                    stopped = f"merge failure of #{num}"
                    continue
                merged_any = True
                print(f"- merged as {sha[:7]} ({reason})")

            if not hit:
                print("- RESULT: MERGED — nothing for ArgoCD to roll out")
                continue
            deadline = time.monotonic() + ROLLOUT_TIMEOUT
            all_ok = True
            for name, a in hit.items():
                ns = a["spec"]["destination"].get("namespace") or "default"
                ok, detail = wait_rollout(name, sha, deadline)
                print(f"- rollout {name}: {'OK' if ok else 'FAILED'} — {detail}")
                if not pr.get("merged"):
                    after = workload_images(ns)
                    for k, img in sorted(after.items()):
                        old = before.get(name, {}).get(k)
                        if old and old != img:
                            print(f"  - image {k}: {old.rsplit(':', 1)[-1]} → {img.rsplit(':', 1)[-1]}")
                    rs = restarted_pods(ns, t0)
                    if rs:
                        print("  - container restarts since merge: " + ", ".join(rs[:5]))
                if name == "home-assistant":
                    print(f"  - {ha_status()}")
                all_ok = all_ok and ok
            print(f"- RESULT: {'MERGED + ROLLED OUT' if all_ok else 'MERGED, ROLLOUT FAILED'}")
            if not all_ok:
                stopped = f"failed rollout of #{num}"
        except urllib.error.HTTPError as e:
            print(f"- RESULT: ERROR — GitHub {e.code}: {e.read()[:200]!r}")
            stopped = stopped or f"error on #{num}"
        except Exception as e:  # noqa: BLE001
            print(f"- RESULT: ERROR — {e}")
            stopped = stopped or f"error on #{num}"

    print("\n## Cluster after run")
    for ln in cluster_health():
        print(f"- {ln}")
    print(f"- merged anything: {'yes' if merged_any else 'no'}")
    print(f"- run time: {int(time.monotonic() - START)} s")


if __name__ == "__main__":
    main()
