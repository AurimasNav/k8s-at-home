# Hermes scheduled-job scripts

Source of truth for scripts used by Hermes cron jobs. Kustomize builds the
`hermes-agent-scripts` ConfigMap from these files. The ConfigMap is mounted
read-only at `/opt/data/scripts`. It has a fixed name (no hash suffix) and
`reloader.stakater.com/ignore`, so a script edit does not restart Hermes: the
kubelet updates the mounted files within about a minute and the next cron run
uses them. Adding a new job still needs `cron/jobs.yaml`, which does restart.
Make changes through PRs.

Credentials are never stored here. Scripts read `HA_URL` and `HA_TOKEN` from env (ExternalSecret).

- `heating_check.py` reads NIBE F1245 data, compressor/HW run hours and room climate from Home Assistant. Read-only.
- `apva_check.py` reads APVA open (APVIS) and planned calls and flags home battery storage. Read-only.
- `renovate_automerge.py` merges green Renovate PRs in this repo one at a time and waits for each ArgoCD rollout. It holds major updates, comment-only bumps of vendored HA integrations, and anything that would restart the Hermes pod. Needs `HERMES_GITHUB_TOKEN` and the Hermes service account's read access to ArgoCD apps and pods. Test with `--dry-run` (add `--pr N` to replay merged PRs).
- `anthropic_pool_watchdog.py` (no_agent job, every minute, no LLM turns) clears false rate-limit blocks on the Anthropic credential pool. Hermes parks a Claude key for a fixed hour after any 429 and ignores Anthropic's 5-hour/weekly headers. For each blocked key the script sends a 1-token request on the blocked model; if Anthropic says `allowed`, it runs `hermes auth reset anthropic <key>` and posts one line. Really-limited keys are left alone, and a key is reset at most once per 5 min. Reads the key from the gateway process env (cron scripts get a sanitized env); never prints it. Test with `--dry-run --verbose`.
- `media_weekly.py` (weekly media report, Fri 19:00, topic 1128) collects the last 7 days: VSeerr requests (who asked for what, and why a request is still waiting), Radarr/Sonarr imports and upgrades with sizes, Cleanuparr cleanups and searches, qBittorrent transfer and seeding, `/data` free space (VictoriaMetrics), library size per instance and Plex plays. Read-only. Logins (VSeerr, Cleanuparr, qBittorrent) come from Doppler via `DOPPLER_TOKEN`; the *arr and Plex keys from env. Weekly upload/download and library changes are deltas against the previous run, kept in `$HERMES_HOME/cache/media_weekly_state.json` (only `--days 7` runs update it). Test with `--days N`.
- `cron_cleanup.py` (no_agent job, daily 09:30, topic 501, no LLM turns) deletes cron jobs that can never run again: finished one-shots, recurring jobs that used up their repeat count, and paused one-shots whose time has passed. It keeps every job with a future run (paused or error-state recurring jobs too), jobs with a run in flight, jobs another job reads via `context_from`, and anything active in the last hour. It removes through Hermes' own `remove_job` under the jobs lock, which also deletes the job's output and notepad. Prints one line per removed job, nothing otherwise. Hermes itself only prunes finished one-shots after 7 days. Test with `--dry-run`.
