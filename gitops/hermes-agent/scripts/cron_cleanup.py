#!/usr/bin/env python3
"""Delete Hermes cron jobs that can never run again.

Runs as a no_agent cron job (no LLM turns). A job is removed when it has no
future run:
  - it is finished (state "completed"): a one-shot that ran, or a recurring
    job that used up its repeat count;
  - or it is a paused one-shot whose time has passed (Hermes refuses to resume
    those, so they would sit in the list forever).

Never removed: recurring jobs that still have a schedule (including paused
ones and ones in the "error" state, which Hermes re-arms), anything with a run
in flight, jobs another job reads through context_from, and jobs whose last
activity is younger than --min-age-hours (default 1), so a run that just
finished can still deliver. Removal goes through Hermes' own remove_job, under
its lock, which also deletes the job's output files and notepad.

Prints one line per removed job (delivered to Telegram); prints nothing when
there was nothing to do, so a quiet run is silent.

Usage: cron_cleanup.py [--dry-run] [--min-age-hours H] [--home DIR]
"""
import argparse
import contextlib
import datetime
import os
import sys
import zoneinfo

# Cron scripts get a sanitized env; pin the home before importing Hermes, so the notepad and config
# lookups inside remove_job hit the same store as the jobs file (default /opt/data, or --home).
_home_arg = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--home=")), None)
if "--home" in sys.argv[:-1]:
    _home_arg = sys.argv[sys.argv.index("--home") + 1]
os.environ["HERMES_HOME"] = _home_arg or os.environ.get("HERMES_HOME") or "/opt/data"
sys.path.insert(0, "/opt/hermes")

from cron import jobs as cron_jobs  # noqa: E402

TZ = zoneinfo.ZoneInfo("Europe/Vilnius")
ONESHOT_GRACE = datetime.timedelta(seconds=cron_jobs.ONESHOT_GRACE_SECONDS)


def parse(value):
    try:
        dt = datetime.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


def why_removable(job, now, min_age, referenced):
    """Reason the job has no future run, or None to keep it."""
    if job.get("run_claim") or job.get("fire_claim"):
        return None  # a run is in flight
    if job.get("id") in referenced:
        return None  # another job reads its output (context_from)
    schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
    kind = schedule.get("kind")
    state = job.get("state")
    if state == "completed":
        reason = "finished" if kind == "once" else "repeat count used up"
    elif kind == "once" and not job.get("enabled", True) and not job.get("next_run_at"):
        run_at = parse(schedule.get("run_at"))
        if run_at is None or now - run_at <= ONESHOT_GRACE:
            return None
        reason = "paused one-shot, its time has passed"
    else:
        return None
    # Age from the newest activity we know of; an unparseable record is kept (never guess into deletion).
    times = [parse(job.get(k)) for k in ("last_run_at", "paused_at", "created_at")]
    times.append(parse(schedule.get("run_at")))
    times = [t for t in times if t]
    if not times or now - max(times) < min_age:
        return None
    return reason


def label(job):
    last = parse(job.get("last_run_at"))
    when = f"last run {last.astimezone(TZ):%Y-%m-%d %H:%M}" if last else "never ran"
    status = job.get("last_status")
    return f"{job.get('name') or '(unnamed)'} ({job['id']}, {when}{', ' + status if status else ''})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="list what would be removed, remove nothing")
    ap.add_argument("--min-age-hours", type=float, default=1.0)
    ap.add_argument("--home", help="Hermes home whose cron store is cleaned (default: HERMES_HOME)")
    args = ap.parse_args()
    home = os.environ["HERMES_HOME"]
    min_age = datetime.timedelta(hours=args.min_age_hours)

    removed, failed = [], []
    # Decide and remove under one hold of Hermes' jobs lock, so no job changes between the check and
    # the delete (remove_job takes the same lock again; nested use in one thread is allowed). The lock
    # is private API: if a Hermes upgrade renames it, run unlocked rather than fail.
    jobs_lock = getattr(cron_jobs, "_jobs_lock", None) or contextlib.nullcontext
    with cron_jobs.use_cron_store(home), jobs_lock():
        now = datetime.datetime.now(datetime.UTC)
        jobs = cron_jobs.load_jobs()
        live_ids = {j.get("id") for j in jobs if j.get("state") != "completed"}
        referenced = {ref for j in jobs if j.get("id") in live_ids for ref in (j.get("context_from") or [])}
        for job in jobs:
            reason = why_removable(job, now, min_age, referenced)
            if not reason:
                continue
            if args.dry_run or cron_jobs.remove_job(job["id"]):
                removed.append(f"{label(job)}: {reason}")
            else:
                failed.append(label(job))

    if args.dry_run:
        print(f"DRY RUN: would remove {len(removed)} of {len(jobs)} jobs")
        for line in removed:
            print(f"- {line}")
        return 0
    if removed:
        print(f"🧹 Removed {len(removed)} cron job{'s' if len(removed) != 1 else ''} with no future run:")
        for line in removed:
            print(f"- {line}")
    if failed:
        print("⚠️ Could not remove: " + "; ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
