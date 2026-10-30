"""Apply gitops/cleanuparr/setup/desired.json to Cleanuparr through its REST API.

Runs as an Argo CD PostSync Job after every sync. Idempotent: the account is
created only on first run (409 = already done), instances/clients/rules are
matched by name and updated in place, module settings are read, merged with the
desired values and written back. Things added in the UI but not listed in
desired.json are left alone (reported as "unmanaged").

Order matters: Dry Run is applied before any instance is added, so a fresh
install never acts on the queue before the first review.

Secrets come from env (Doppler via ExternalSecret) and are never printed.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

BASE = os.environ.get("CLEANUPARR_URL", "http://cleanuparr.cleanuparr").rstrip("/")
DESIRED_PATH = os.environ.get("DESIRED_PATH", "/setup/desired.json")
PASSWORD = os.environ["CLEANUPARR_PASSWORD"]
_token = None


class ApiError(Exception):
    pass


def call(method, path, body=None, ok=(200, 201, 204)) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if _token:
        req.add_header("Authorization", f"Bearer {_token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as err:
        status, raw = err.code, err.read()
    if status not in ok:
        raise ApiError(f"{method} {path} -> HTTP {status}: {raw[:300].decode(errors='replace')}")
    try:
        return status, (json.loads(raw) if raw.strip() else None)
    except ValueError:
        return status, raw.decode(errors="replace")


def wait_ready(timeout=300):
    deadline = time.monotonic() + timeout
    while True:
        try:
            call("GET", "/health/ready")
            return
        except (ApiError, OSError) as err:
            if time.monotonic() > deadline:
                sys.exit(f"Cleanuparr not ready after {timeout}s: {err}")
            time.sleep(5)


def merged(current, desired):
    out = dict(current)
    for key, value in desired.items():
        if isinstance(value, dict) and isinstance(current.get(key), dict):
            out[key] = merged(current[key], value)
        else:
            out[key] = value
    return out


def changed_keys(before, after, prefix=""):
    keys = []
    for key, value in after.items():
        old = before.get(key)
        if isinstance(value, dict) and isinstance(old, dict):
            keys += changed_keys(old, value, f"{prefix}{key}.")
        elif old != value:
            keys.append(prefix + key)
    return keys


def apply_settings(label, path, desired):
    _, current = call("GET", path)
    want = merged(current, desired)
    diff = changed_keys(current, want)
    if diff:
        call("PUT", path, want)
    print(f"{label}: {'updated ' + ', '.join(diff) if diff else 'unchanged'}")


def bootstrap(username):
    _, status = call("GET", "/api/auth/status")
    if not status.get("setupCompleted"):
        code, _ = call("POST", "/api/auth/setup/account",
                       {"username": username, "password": PASSWORD}, ok=(200, 201, 409))
        call("POST", "/api/auth/setup/complete", ok=(200, 201, 204, 409))
        print("account:", "created" if code != 409 else "already existed", "and setup completed")
    else:
        print("account: setup already completed")


def login(username):
    global _token
    try:
        _, res = call("POST", "/api/auth/login", {"username": username, "password": PASSWORD})
    except ApiError as err:
        sys.exit(f"login failed ({err}). CLEANUPARR_PASSWORD in Doppler must match the Cleanuparr account.")
    if res.get("requiresTwoFactor"):
        sys.exit("login needs 2FA. This job logs in with a password, so keep Cleanuparr's own 2FA off "
                 "(oauth2-proxy in front is the login gate).")
    _token = res["tokens"]["accessToken"]


def sync_arr(kind, wanted):
    _, cfg = call("GET", f"/api/configuration/{kind}")
    existing = {i["name"]: i for i in cfg.get("instances", [])}
    for want in wanted:
        body = {
            "name": want["name"],
            "url": want["url"],
            "apiKey": os.environ[want["apiKeyEnv"]],
            "version": want["version"],
            "enabled": want.get("enabled", True),
        }
        if want["name"] in existing:
            call("PUT", f"/api/configuration/{kind}/instances/{existing[want['name']]['id']}", body)
            print(f"{kind} instance {want['name']}: synced ({want['url']})")
        else:
            call("POST", f"/api/configuration/{kind}/instances", body)
            print(f"{kind} instance {want['name']}: created ({want['url']})")
    for name in sorted(set(existing) - {w["name"] for w in wanted}):
        print(f"{kind} instance {name}: unmanaged, left as is")


def sync_download_clients(wanted):
    _, cfg = call("GET", "/api/configuration/download_client")
    existing = {c["name"]: c for c in cfg.get("clients", [])}
    for want in wanted:
        body = {"enabled": True, "username": None, "password": None, **want}
        if want["name"] in existing:
            call("PUT", f"/api/configuration/download_client/{existing[want['name']]['id']}", body)
            print(f"download client {want['name']}: synced ({want['host']})")
        else:
            call("POST", "/api/configuration/download_client", body)
            print(f"download client {want['name']}: created ({want['host']})")
    for name in sorted(set(existing) - {w["name"] for w in wanted}):
        print(f"download client {name}: unmanaged, left as is")


def sync_rules(kind, wanted):
    _, rules = call("GET", f"/api/queue-rules/{kind}")
    existing = {r["name"]: r for r in rules}
    for want in wanted:
        if want["name"] in existing:
            current = existing[want["name"]]
            body = merged(current, want)
            if changed_keys(current, body):
                call("PUT", f"/api/queue-rules/{kind}/{current['id']}", body)
                print(f"{kind} rule {want['name']}: updated")
            else:
                print(f"{kind} rule {want['name']}: unchanged")
        else:
            call("POST", f"/api/queue-rules/{kind}", want)
            print(f"{kind} rule {want['name']}: created")
    for name in sorted(set(existing) - {w["name"] for w in wanted}):
        print(f"{kind} rule {name}: unmanaged, left as is")


def sync_seeker(desired):
    _, current = call("GET", "/api/configuration/seeker")
    wanted_instances = desired.get("instances", {})
    instances, seen = [], set()
    for inst in current.get("instances", []):
        key = f"{inst['instanceType']}/{inst['instanceName']}"
        seen.add(key)
        instances.append({**inst, **wanted_instances.get(key, {})})
    for key in sorted(set(wanted_instances) - seen):
        print(f"seeker: no {key} instance in Cleanuparr, skipped")
    top = {k: v for k, v in desired.items() if k != "instances"}
    body = {**merged(current, top), "instances": instances}
    diff = changed_keys(current, body) + [
        f"instances.{i['instanceType']}/{i['instanceName']}"
        for i, old in zip(instances, current.get("instances", []), strict=True)
        if changed_keys(old, i)
    ]
    diff = [d for d in diff if d != "instances"]
    if diff:
        call("PUT", "/api/configuration/seeker", body)
    print(f"seeker: {'updated ' + ', '.join(diff) if diff else 'unchanged'}")


def main():
    with open(DESIRED_PATH) as f:
        desired = json.load(f)
    username = desired["account"]["username"]

    wait_ready()
    bootstrap(username)
    login(username)

    # Dry Run and the auth hardening go first, before Cleanuparr knows any instance.
    apply_settings("general", "/api/configuration/general", desired["general"])

    sync_arr("radarr", desired.get("radarr", []))
    sync_arr("sonarr", desired.get("sonarr", []))
    sync_download_clients(desired.get("downloadClients", []))

    apply_settings("queue cleaner", "/api/configuration/queue_cleaner", desired["queueCleaner"])
    sync_rules("stall", desired.get("stallRules", []))
    sync_rules("slow", desired.get("slowRules", []))
    apply_settings("malware blocker", "/api/configuration/malware_blocker", desired["malwareBlocker"])
    sync_seeker(desired["seeker"])

    _, general = call("GET", "/api/configuration/general")
    if general["dryRun"] != desired["general"]["dryRun"]:
        sys.exit("dryRun did not stick")
    print(f"done: dryRun={general['dryRun']}")


if __name__ == "__main__":
    main()
