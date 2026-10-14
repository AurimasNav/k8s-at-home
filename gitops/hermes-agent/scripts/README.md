# Hermes scheduled-job scripts

Source of truth for scripts used by Hermes cron jobs. Kustomize builds the
`hermes-agent-scripts` ConfigMap from these files. The ConfigMap is mounted
read-only at `/opt/data/scripts`, and a content change rolls the pod.
Make changes through PRs.

Credentials are never stored here. Scripts read `HA_URL` and `HA_TOKEN` from env (ExternalSecret).

- `heating_check.py` reads NIBE F1245 data, compressor/HW run hours and room climate from Home Assistant. Read-only.
- `apva_check.py` reads APVA open (APVIS) and planned calls and flags home battery storage. Read-only.
