# Hermes scheduled-job scripts

Backups of scripts used by Hermes cron jobs (live copies: `/opt/data/scripts/`).
Credentials are never stored here; they come from env vars (`HA_URL`, `HA_TOKEN`) injected via ExternalSecret.

- `heating_check.py` - NIBE F1245 + room temperatures from Home Assistant (read-only)
- `apva_check.py` - APVA published/planned calls, flags energy-storage ones (read-only)
