# Hermes cron jobs from git

`jobs.yaml` is the source of truth for the scheduled jobs listed in it. The `cron-sync` init
container runs `cron_sync.py` on every pod start. It merges these definitions into
`/opt/data/cron/jobs.json` through Hermes' own `cron.job_definition` merge.

- Fields set in git win: name, prompt, schedule, deliver, script and similar.
- Scheduler state stays on the PVC: next run, last status, run counts.
- A job id that is new to the PVC is created. Jobs not listed here, such as ad-hoc reminders made in chat, are left untouched.
- `enabled: false` pauses a job. Removing a job from this file does **not** delete it from the PVC; pause it first, then delete it in Hermes.
- Changes made in chat to a managed job are overwritten on the next pod restart. Change them here via PR.

Dry run against a copy of the store:
`python cron_sync.py jobs.yaml --home /path/to/copy --dry-run`
