"""Refresh the data that GitHub's runners can't download, in AurimasNav/atlieku-surinkimo-grafikas.

Šiukšliavežė builds its schedule data on GitHub Actions, but some sources refuse GitHub's
runners: the Address Register (get.data.gov.lt answers them 500/403) and the municipal sites
listed in the app's `snapshot.site_copies()` (403, at the network level). The build reads
committed copies of them in pipeline/data instead. This cluster is in Lithuania, so it can take
those copies:

1. Shallow-clone main (a public repo: no token), `uv sync --locked`, and run
   `python -m schedule_pipeline.snapshot --copies` (or `--register`) in pipeline/. A snapshot
   run that fails is retried (--attempts, --retry-wait).
2. If `git status --porcelain -- pipeline/data` is empty, stop (and close the job's PR if one is
   still open: main already has that data).
3. Otherwise commit pipeline/data to the job's bot branch, recreated from main on every run,
   force-push it, and open a PR unless one is open for that branch.
4. Wait up to --wait-minutes for the head commit's `check` and `build` check runs (main's
   required checks). When both succeed, merge the PR with a merge commit. If either fails or
   they time out, leave the PR open and exit 1, so the Job fails (KubeJobFailed).

If the last snapshot attempt fails, what it did save is still committed (each copy and each
municipality's snapshot is saved whole), and the script exits 1 at the end.

Without --push it is a dry run: it clones, runs the snapshot and lists what changed, but pushes
nothing and opens, closes or merges no PR. The token (GITHUB_TOKEN, a fine-grained PAT for that
one repository) reaches git through the environment of `git push` only, never a command line or
a file, is removed from the environment of every other command, and is never printed.
"""

import argparse
import base64
import json
import os
import subprocess  # noqa: S404 - runs git and uv with fixed arguments
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import NamedTuple

REPO = "AurimasNav/atlieku-surinkimo-grafikas"
BASE = "main"
DATA = "pipeline/data"
REQUIRED_CHECKS = ("check", "build")  # the required status checks of main's ruleset
FAILED = {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}
# No person's name or address: .invalid is reserved and belongs to no one (RFC 2606).
BOT_NAME, BOT_EMAIL = "siuksliaveze-snapshot", "siuksliaveze-snapshot@noreply.invalid"
POLL_SECONDS = 30


class Job(NamedTuple):
    cronjob: str
    option: str
    branch: str
    title: str
    why: str


JOBS = {
    "copies": Job(
        "site-copies",
        "--copies",
        "data/site-copies",
        "Refresh the copies of the sites that refuse GitHub's runners",
        "the sites listed in `snapshot.site_copies()` refuse GitHub's runners",
    ),
    "register": Job(
        "address-register",
        "--register",
        "data/address-register",
        "Refresh the Address Register snapshots",
        "get.data.gov.lt and registrucentras.lt refuse GitHub's runners",
    ),
}


class Failure(Exception):
    """A step that failed: the message goes to the log and the script exits 1."""


def secret() -> str:
    return os.environ.get("GITHUB_TOKEN", "").strip()


def redact(text: str) -> str:
    value = secret()
    return text.replace(value, "***") if value else text


def clean_env() -> dict[str, str]:
    """The environment for git and uv: everything but the token."""
    return {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}


# No user config is read or written (HOME is an emptyDir): the identity comes with each git
# command, and paths with ą, č, ... are printed as they are.
GIT = ["git", "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}", "-c", "core.quotePath=false"]


def git(*args: str) -> list[str]:
    return [*GIT, *args]


def shown(args: list[str]) -> str:
    """A command as the log shows it, without the options every git command gets."""
    return " ".join(["git", *args[len(GIT):]] if args[: len(GIT)] == GIT else args)


def run(
    args: list[str], cwd: Path, env: dict[str, str] | None = None, check: bool = True
) -> subprocess.CompletedProcess:
    print(f"+ {shown(args)}", flush=True)
    result = subprocess.run(args, cwd=cwd, env=env or clean_env(), check=False)  # noqa: S603
    if check and result.returncode:
        raise Failure(f"{shown(args)} exited {result.returncode}")
    return result


def output(args: list[str], cwd: Path) -> str:
    result = subprocess.run(  # noqa: S603
        args, cwd=cwd, env=clean_env(), check=True, capture_output=True, text=True
    )
    return result.stdout


# --- GitHub's REST API ---------------------------------------------------------------


class Api:
    def __init__(self, base: str, token: str):
        self.base, self.token = base.rstrip("/"), token
        self.public_checks = False

    def call(self, method: str, path: str, body: dict | None = None, ok=(200, 201), auth: bool = True):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "siuksliaveze-snapshot",
        }
        if self.token and auth:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(body).encode() if body is not None else None
        # A GET is tried again after a network error or a 5xx; a write is not (it may have
        # happened).
        for attempt in range(3 if method == "GET" else 1):
            if attempt:
                time.sleep(10)
            request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    status, raw = response.status, response.read()
            except urllib.error.HTTPError as error:
                status, raw = error.code, error.read()
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                status, raw = 0, str(getattr(error, "reason", error)).encode()
            if status in ok or (status and status < 500):
                break
        if status not in ok:
            detail = redact(raw[:300].decode(errors="replace"))
            raise Failure(f"GitHub API {method} {path}: HTTP {status} {detail}")
        return status, (json.loads(raw) if raw.strip() else None)

    def open_pr(self, branch: str) -> dict | None:
        owner = REPO.split("/")[0]
        query = urllib.parse.urlencode({"state": "open", "head": f"{owner}:{branch}", "base": BASE})
        _, pulls = self.call("GET", f"/repos/{REPO}/pulls?{query}")
        return pulls[0] if pulls else None

    def check_runs(self, sha: str) -> dict[str, dict]:
        """The latest check run of each required check on the commit, by name. No fine-grained
        token permission covers check runs; the repository is public, so they are public data.
        Should GitHub refuse the token here, they are read without it."""
        path = f"/repos/{REPO}/commits/{sha}/check-runs?filter=latest&per_page=100"
        try:
            _, data = self.call("GET", path, auth=not self.public_checks)
        except Failure as error:
            if self.public_checks or not any(f"HTTP {code}" in str(error) for code in (401, 403, 404)):
                raise
            print(f"Reading the checks without the token ({error}).", flush=True)
            self.public_checks = True
            _, data = self.call("GET", path, auth=False)
        latest: dict[str, dict] = {}
        for check_run in data["check_runs"]:
            name = check_run["name"]
            if name in REQUIRED_CHECKS and (name not in latest or check_run["id"] > latest[name]["id"]):
                latest[name] = check_run
        return latest


# --- the steps -----------------------------------------------------------------------


def clone(source: str, workdir: Path) -> Path:
    repo = workdir / "repo"
    if repo.exists():
        raise Failure(f"{repo} exists: the work directory should be empty")
    workdir.mkdir(parents=True, exist_ok=True)
    run(git("clone", "--depth", "1", "--single-branch", "--branch", BASE, source, str(repo)), cwd=workdir)
    return repo


def snapshot(repo: Path, job: Job, attempts: int, retry_wait: int) -> int:
    """Runs the snapshot command until it succeeds, at most `attempts` times; its last exit
    code."""
    pipeline = repo / "pipeline"
    run(["uv", "sync", "--locked", "--no-dev"], cwd=pipeline)
    command = ["uv", "run", "--no-sync", "python", "-m", "schedule_pipeline.snapshot", job.option]
    attempts = max(1, attempts)
    for attempt in range(1, attempts + 1):
        code = run(command, cwd=pipeline, check=False).returncode
        if code == 0:
            return 0
        print(f"The snapshot failed (exit {code}), attempt {attempt} of {attempts}.", flush=True)
        if attempt < attempts:
            print(f"Trying again in {retry_wait // 60} minutes.", flush=True)
            time.sleep(retry_wait)
    return code


def changes(repo: Path) -> list[str]:
    """The changed paths under pipeline/data, as `git status --porcelain` lists them."""
    status = output(git("status", "--porcelain", "--untracked-files=all", "--", DATA), cwd=repo)
    return [line for line in status.splitlines() if line.strip()]


def commit(repo: Path, job: Job, snapshot_code: int) -> tuple[str, list[str]]:
    """Commits pipeline/data on the job's branch; (commit SHA, "M path" lines)."""
    run(git("switch", "--force-create", job.branch), cwd=repo)
    run(git("add", "--all", "--", DATA), cwd=repo)
    files = output(git("diff", "--cached", "--name-status", "--", DATA), cwd=repo).splitlines()
    body = f"Taken by the {job.cronjob} CronJob in k8s-at-home (gitops/siuksliaveze-snapshot)."
    if snapshot_code:
        body += f" The snapshot command failed (exit {snapshot_code}); these are the parts it saved."
    run(git("commit", "--quiet", "--message", job.title, "--message", body), cwd=repo)
    return output(git("rev-parse", "HEAD"), cwd=repo).strip(), files


def push(repo: Path, branch: str) -> None:
    """Force-pushes HEAD to the bot branch. The token goes in an HTTP header that git reads from
    its environment (GIT_CONFIG_*), as actions/checkout does: no command line, no file."""
    basic = base64.b64encode(f"x-access-token:{secret()}".encode()).decode()
    env = {
        **clean_env(),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
    }
    print(f"+ git push --force origin HEAD:refs/heads/{branch}", flush=True)
    result = subprocess.run(  # noqa: S603
        git("push", "--force", "origin", f"HEAD:refs/heads/{branch}"),
        cwd=repo, env=env, check=False, capture_output=True, text=True,
    )
    print(redact(result.stdout + result.stderr), end="", flush=True)
    if result.returncode:
        raise Failure(f"git push exited {result.returncode}")


def pr_body(job: Job, base_sha: str, files: list[str], snapshot_code: int) -> str:
    listed = [f"- `{status}` `{path}`" for status, _, path in (f.partition("\t") for f in files)]
    if len(listed) > 100:
        listed = [*listed[:100], f"- and {len(listed) - 100} more"]
    lines = [
        f"From the `{job.cronjob}` CronJob in the maintainer's k3s cluster in Lithuania "
        "([k8s-at-home](https://github.com/AurimasNav/k8s-at-home), "
        f"`gitops/siuksliaveze-snapshot`): {job.why}, so their data is taken from there.",
        "",
        f"It ran `uv run python -m schedule_pipeline.snapshot {job.option}` on `{BASE}` at "
        f"{base_sha[:7]}. Changed files:",
        "",
        *listed,
        "",
    ]
    if snapshot_code:
        lines += [
            f"The snapshot command failed (exit {snapshot_code}) after saving these; the rest is "
            "unchanged. See the job's log.",
            "",
        ]
    lines.append(
        "The job merges this PR once `check` and `build` pass on its head commit. If they fail, "
        "it leaves the PR open and the job fails; the next run replaces the branch."
    )
    return "\n".join(lines)


def ensure_pr(api: Api, job: Job, body: str) -> int:
    pr = api.open_pr(job.branch)
    if pr:
        api.call("PATCH", f"/repos/{REPO}/pulls/{pr['number']}", {"title": job.title, "body": body})
        print(f"Updated PR #{pr['number']}: {pr['html_url']}", flush=True)
        return pr["number"]
    _, pr = api.call(
        "POST", f"/repos/{REPO}/pulls",
        {"title": job.title, "head": job.branch, "base": BASE, "body": body},
    )
    print(f"Opened PR #{pr['number']}: {pr['html_url']}", flush=True)
    return pr["number"]


def wait_for_checks(api: Api, sha: str, timeout: float, poll: float = POLL_SECONDS) -> None:
    """Returns when every required check succeeded on `sha`; Failure if one fails or time runs
    out."""
    deadline = time.monotonic() + timeout
    last = ""
    while True:
        runs = api.check_runs(sha)
        states = {name: runs[name]["conclusion"] or runs[name]["status"] if name in runs else "not started"
                  for name in REQUIRED_CHECKS}
        summary = ", ".join(f"{name}: {state}" for name, state in states.items())
        if summary != last:
            print(f"Checks on {sha[:7]}: {summary}", flush=True)
            last = summary
        failed = [name for name in REQUIRED_CHECKS if name in runs and runs[name]["conclusion"] in FAILED]
        if failed:
            urls = ", ".join(runs[name]["html_url"] for name in failed)
            raise Failure(f"{', '.join(failed)} failed on {sha[:7]} ({urls}); the PR stays open")
        if all(states[name] == "success" for name in REQUIRED_CHECKS):
            return
        if time.monotonic() > deadline:
            raise Failure(f"the checks didn't finish in time ({summary}); the PR stays open")
        # Without the token GitHub allows 60 requests an hour.
        time.sleep(max(poll, 60) if api.public_checks else poll)


def merge(api: Api, number: int, sha: str, pause: float = 15) -> None:
    """Merges the PR with a merge commit, only at `sha` (the commit whose checks passed).
    GitHub may need a moment after the checks to see the PR as mergeable (HTTP 405)."""
    for attempt in range(8):
        try:
            api.call("PUT", f"/repos/{REPO}/pulls/{number}/merge", {"merge_method": "merge", "sha": sha})
            print(f"Merged PR #{number}.", flush=True)
            return
        except Failure as error:
            if "HTTP 405" not in str(error) or attempt == 7:
                raise
            time.sleep(pause)


def close_stale_pr(api: Api, job: Job, push_enabled: bool) -> None:
    """A PR still open for the branch when main already has the data is out of date."""
    pr = api.open_pr(job.branch)
    if not pr:
        return
    if not push_enabled:
        print(f"Dry run: would close PR #{pr['number']}, main already has this data.", flush=True)
        return
    api.call(
        "POST", f"/repos/{REPO}/issues/{pr['number']}/comments",
        {"body": f"The `{job.cronjob}` CronJob found no change against `{BASE}`: closing."},
    )
    api.call("PATCH", f"/repos/{REPO}/pulls/{pr['number']}", {"state": "closed"})
    api.call("DELETE", f"/repos/{REPO}/git/refs/heads/{job.branch}", ok=(204, 422))
    print(f"Closed PR #{pr['number']}: main already has this data.", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("job", choices=JOBS, help="copies (daily) or register (monthly)")
    parser.add_argument("--push", action="store_true", help="push, open and merge the PR (without it: dry run)")
    parser.add_argument("--attempts", type=int, default=1, help="snapshot runs before giving up")
    parser.add_argument("--retry-wait", type=int, default=600, help="seconds between snapshot runs")
    parser.add_argument("--wait-minutes", type=float, default=25, help="how long to wait for the checks")
    parser.add_argument("--workdir", type=Path, default=Path("/work"), help="an empty directory")
    parser.add_argument("--source", default=f"https://github.com/{REPO}.git", help="the repository to clone and push to")
    # For a test against a fake API.
    parser.add_argument("--api", default="https://api.github.com", help=argparse.SUPPRESS)
    parser.add_argument("--poll-seconds", type=float, default=POLL_SECONDS, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    job = JOBS[args.job]
    if args.push and not secret():
        raise Failure("--push needs GITHUB_TOKEN")
    api = Api(args.api, secret())

    repo = clone(args.source, args.workdir)
    base_sha = output(git("rev-parse", "HEAD"), cwd=repo).strip()
    code = snapshot(repo, job, args.attempts, args.retry_wait)
    changed = changes(repo)
    if not changed:
        print(f"No change in {DATA}.", flush=True)
        if code == 0:  # with a failed run, an open PR may hold what this run couldn't take
            close_stale_pr(api, job, args.push)
        return 1 if code else 0
    print(f"Changed in {DATA}:", *changed, sep="\n  ", flush=True)

    sha, files = commit(repo, job, code)
    body = pr_body(job, base_sha, files, code)
    if not args.push:
        print(f"Dry run: would push {sha[:7]} to {job.branch} and open or update a PR:\n\n{body}", flush=True)
        return 1 if code else 0
    push(repo, job.branch)
    number = ensure_pr(api, job, body)
    wait_for_checks(api, sha, args.wait_minutes * 60, args.poll_seconds)
    merge(api, number, sha, min(15, args.poll_seconds))
    return 1 if code else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (Failure, subprocess.CalledProcessError, OSError) as error:
        print(f"Failed: {redact(str(error))}", file=sys.stderr, flush=True)
        sys.exit(1)
