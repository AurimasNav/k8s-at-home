#!/usr/bin/env python3
"""Clear false rate-limit blocks on Hermes' Anthropic credential pool.

Hermes parks a Claude subscription key for a fixed hour after a 429 and does
not read Anthropic's unified rate-limit headers, so it can't tell "5-hour or
weekly limit really reached" from "one burst 429" (or from the rotation bug
in NousResearch/hermes-agent#144183, where the second 429 is blamed on the
wrong key). This script runs every 5 minutes as a no_agent cron job and uses
no LLM turns:

  for each pooled Anthropic key that Hermes currently has blocked:
    send a 1-token request on the blocked model (default: the main model)
    and read anthropic-ratelimit-unified-*;
    HTTP 200 / status=allowed  -> `hermes auth reset anthropic <label>`
    anything else              -> leave the block alone

Prints one line per cleared key (delivered to Telegram); prints nothing when
there was nothing to do, so a quiet run is silent.

Keys: Hermes strips ANTHROPIC_* from cron script env, so the token is read
from the gateway process' environment (same uid), falling back to os.environ.
Never prints a token.

Usage: anthropic_pool_watchdog.py [--dry-run] [--verbose]
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zoneinfo

HOME = os.environ.get("HERMES_HOME", "/opt/data")
HERMES = "/opt/hermes/.venv/bin/hermes"
TZ = zoneinfo.ZoneInfo("Europe/Vilnius")
TTL = {429: 3600, 401: 300}  # Hermes defaults when no reset time was recorded
STATE = os.path.join(HOME, "cache", "anthropic_pool_watchdog.json")
BACKOFF = 30 * 60  # don't reset the same key twice within this window (avoids a reset/429 loop)


def vilnius(epoch):
    try:
        return datetime.datetime.fromtimestamp(float(epoch), TZ).strftime("%a %H:%M")
    except (TypeError, ValueError):
        return "?"


def gateway_env(name):
    """Read one env var from the running gateway process (cron scripts get a sanitized env)."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                if b"gateway" not in f.read():
                    continue
            with open(f"/proc/{pid}/environ", "rb") as f:
                for kv in f.read().split(b"\0"):
                    if kv.startswith(name.encode() + b"="):
                        return kv.split(b"=", 1)[1].decode().strip()
        except OSError:
            continue
    return os.environ.get(name, "").strip()


def blocked_until(entry, now):
    """Epoch until which Hermes will skip this entry, or None if it is usable."""
    until = []
    if entry.get("last_status") == "exhausted":
        reset_at = entry.get("last_error_reset_at")
        if isinstance(reset_at, (int, float)):
            until.append(reset_at)
        elif entry.get("last_status_at"):
            until.append(entry["last_status_at"] + TTL.get(entry.get("last_error_code"), 3600))
    for value in (entry.get("model_cooldowns") or {}).values():
        if isinstance(value, (int, float)):
            until.append(value)
    until = [u for u in until if u > now]
    return max(until) if until else None


def main_model():
    try:
        import yaml
        with open(os.path.join(HOME, "config.yaml")) as f:
            m = (yaml.safe_load(f) or {}).get("model") or {}
        return (m.get("default") or m.get("model") or "claude-opus-5-5") if isinstance(m, dict) else str(m)
    except Exception:
        return "claude-opus-5-5"


def probe(token, model):
    """1-token request on the model Hermes would use; returns (http_status, unified headers dict).

    Probing a cheaper model could say "allowed" while a per-model weekly cap still blocks it.
    """
    body = json.dumps({
        "model": model, "max_tokens": 1,
        "system": "You are Claude Code, Anthropic's official CLI for Claude.",
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()
    headers = {"anthropic-version": "2023-06-01", "content-type": "application/json",
               "User-Agent": "claude-cli/2.1.280"}
    if token.startswith("sk-ant-oat"):
        headers.update({"Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20"})
    else:
        headers["x-api-key"] = token
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body,
                                 method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            status, h = r.status, r.headers
    except urllib.error.HTTPError as e:
        status, h = e.code, e.headers
    prefix = "anthropic-ratelimit-unified-"
    return status, {k.lower()[len(prefix):]: v for k, v in h.items() if k.lower().startswith(prefix)}


def summary(u):
    parts = []
    for win, label in (("5h", "5h"), ("7d", "week")):
        util = u.get(f"{win}-utilization")
        if util is not None:
            parts.append(f"{label} {float(util):.0%} (resets {vilnius(u.get(f'{win}-reset'))})")
    return ", ".join(parts) or "no limit headers"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="probe and report, don't reset")
    ap.add_argument("--verbose", action="store_true", help="also report keys left alone")
    args = ap.parse_args()

    try:
        with open(os.path.join(HOME, "auth.json")) as f:
            pool = json.load(f).get("credential_pool", {}).get("anthropic", [])
    except (OSError, ValueError) as exc:
        print(f"KLAIDA: can't read auth.json: {exc}")
        return 1

    try:
        with open(STATE) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    now = time.time()
    default_model = main_model()
    for entry in pool:
        label = entry.get("label") or entry.get("id")
        until = blocked_until(entry, now)
        if until is None:
            if args.verbose:
                print(f"{label}: not blocked")
            continue
        env_name = (entry.get("source") or "").removeprefix("env:")
        token = gateway_env(env_name) if env_name else ""
        if not token:
            if args.verbose:
                print(f"{label}: blocked until {vilnius(until)}, token not readable, skipped")
            continue
        cooled = [m for m, t in (entry.get("model_cooldowns") or {}).items()
                  if isinstance(t, (int, float)) and t > now]
        model = cooled[0] if cooled else default_model
        try:
            status, u = probe(token, model)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if args.verbose:
                print(f"{label}: probe failed ({exc}), left alone")
            continue
        if status == 200 and u.get("status", "allowed") == "allowed":
            last = state.get(label, 0)
            if now - last < BACKOFF:
                if args.verbose:
                    print(f"{label}: probe allowed but reset {int((now - last) / 60)} min ago, backing off")
                continue
            if args.dry_run:
                print(f"♻️ {label}: Hermes blocked it until {vilnius(until)}, but it has room "
                      f"({summary(u)}). Would reset (dry run).")
                continue
            r = subprocess.run([HERMES, "auth", "reset", "anthropic", label],  # noqa: S603 - fixed argv, label from auth.json
                               capture_output=True, text=True, timeout=60, check=False)
            if r.returncode != 0:
                print(f"KLAIDA: {label}: hermes auth reset failed: {(r.stderr or r.stdout).strip()[:300]}")
                continue
            state[label] = now
            print(f"♻️ {label}: cleared a false rate-limit block (was until {vilnius(until)}). "
                  f"Anthropic says {summary(u)}.")
        elif args.verbose:
            print(f"{label}: really limited on {model}, HTTP {status}, {summary(u)}, left alone")
    if not args.dry_run:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w") as f:
            json.dump(state, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
