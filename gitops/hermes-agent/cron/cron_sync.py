#!/usr/bin/env python3
"""Sync cron job definitions from git (jobs.yaml) into the Hermes cron store.

Runs in an init container before the gateway starts. It uses Hermes' own
cron.job_definition merge, so authored fields (prompt, schedule, deliver,
script, ...) come from git while scheduler state (next run, run counters,
last status) stays on the PVC. Jobs that are not in jobs.yaml are left alone.

Usage: cron_sync.py [jobs.yaml] [--home DIR] [--dry-run]
"""
import argparse
import json
import sys

sys.path.insert(0, "/opt/hermes")

import yaml  # noqa: E402
from cron import jobs as cron_jobs  # noqa: E402
from cron.job_definition import JOB_DEFINITION_FIELDS, import_job_definitions  # noqa: E402

PAUSED_REASON = "created from git (cron-sync)"
AUTHORED_KEYS = {"name", "prompt", "schedule", "deliver", "script", "skills", "model", "provider",
                 "enabled_toolsets", "context_from", "workdir", "no_agent", "failure_deliver",
                 "attach_to_session", "reasoning_effort"}


def build(local, spec):
    unknown = set(spec) - AUTHORED_KEYS - {"enabled", "repeat"}
    if unknown:
        raise SystemExit(f"cron-sync: unknown keys {sorted(unknown)} in job {spec.get('name')!r}")
    # Start from the live definition so fields not managed in git keep their values.
    authored = {k: v for k, v in (local or {}).items() if k in JOB_DEFINITION_FIELDS}
    authored.update({k: v for k, v in spec.items() if k in AUTHORED_KEYS})
    if isinstance(spec.get("prompt"), str):
        authored["prompt"] = spec["prompt"].strip()
    authored["schedule_display"] = spec["schedule"]
    authored["repeat"] = {"times": spec.get("repeat")}
    return authored


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("jobs_file", nargs="?", default="/cron-config/jobs.yaml")
    ap.add_argument("--home", help="Hermes home whose cron store is synced (default: HERMES_HOME)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    wanted = (yaml.safe_load(open(args.jobs_file)) or {}).get("jobs") or {}
    ctx = cron_jobs.use_cron_store(args.home) if args.home else None
    if ctx:
        ctx.__enter__()
    try:
        live = {j["id"]: j for j in cron_jobs.load_jobs()}
        shipped = {jid: build(live.get(jid), spec) for jid, spec in wanted.items()}
        if args.dry_run:
            for jid, a in shipped.items():
                changed = sorted(k for k in a if live.get(jid, {}).get(k) != a[k])
                print(f"{jid} {'update' if jid in live else 'create'}: {changed}")
            return
        import_job_definitions(shipped, paused_reason=PAUSED_REASON)
        for jid, spec in wanted.items():
            job = cron_jobs.get_job(jid)
            enabled = spec.get("enabled", True)
            if enabled and not cron_jobs.is_job_runnable(job):
                cron_jobs.resume_job(jid)
            elif not enabled and cron_jobs.is_job_runnable(job):
                cron_jobs.pause_job(jid, reason="disabled in git (cron-sync)")
        for jid in wanted:
            j = cron_jobs.get_job(jid)
            print(json.dumps({"id": jid, "name": j["name"], "state": cron_jobs.effective_job_state(j),
                              "script": j.get("script"), "deliver": j.get("deliver"),
                              "next_run_at": j.get("next_run_at")}, ensure_ascii=False))
    finally:
        if ctx:
            ctx.__exit__(None, None, None)


if __name__ == "__main__":
    main()
