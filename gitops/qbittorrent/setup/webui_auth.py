"""Turn on qBittorrent Web UI login before qBittorrent starts (init container).

qBittorrent keeps its settings in /config/qBittorrent/qBittorrent.conf and has no env
switch for the login, so this edits the file in place:

- [WebUI] Username and Password_PBKDF2 from the Doppler login (WEBUI_USERNAME /
  WEBUI_PASSWORD, from the qbittorrent-webui ExternalSecret). The hash uses qBittorrent's own format (PBKDF2-HMAC-SHA512,
  100,000 iterations, 16-byte salt, 64-byte key). It is only rewritten when the stored
  hash does not match the password, so restarts don't churn the file.
- The auth bypass is turned off: no subnet whitelist (it was 0.0.0.0/0, i.e. anyone who
  could reach the pod skipped the login) and localhost has to log in too.

qBittorrent 5.3 moved the Web UI keys from [Preferences] WebUI\\* to their own [WebUI]
section (migrateWebUISettings in src/app/upgrade.cpp, run once) and reads only the new
keys. The login keys are written to [WebUI] and removed from [Preferences], so the old
location can never shadow or be migrated over the new one.

Every other line is kept as is. Prints no secrets. stdlib only.
"""

import base64
import hashlib
import hmac
import os
import secrets
import sys

CONF = os.environ.get("QBT_CONF", "/config/qBittorrent/qBittorrent.conf")
ITERATIONS = 100_000
WEBUI = "[WebUI]"
LEGACY = "[Preferences]"
LEGACY_PREFIX = "WebUI\\"
# Fresh volume: qBittorrent would otherwise start from the image's default file, which
# the entrypoint copies only when no file exists. Same defaults, plus the login below.
DEFAULT_CONF = """[LegalNotice]
Accepted=true

[Preferences]
Connection\\UPnP=false
General\\Locale=en

[WebUI]
Address=*
CSRFProtection=false
HostHeaderValidation=false
ServerDomains=*
UseUPnP=false
"""
LOGIN_KEYS = ("Username", "Password_PBKDF2", "AuthSubnetWhitelistEnabled", "LocalHostAuth", "AuthSubnetWhitelist")


def pbkdf2(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", password.encode(), salt, ITERATIONS, 64)


def new_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = pbkdf2(password, salt)
    return f'"@ByteArray({base64.b64encode(salt).decode()}:{base64.b64encode(key).decode()})"'


def hash_matches(value: str | None, password: str) -> bool:
    if not value:
        return False
    inner = value.strip().strip('"')
    if not (inner.startswith("@ByteArray(") and inner.endswith(")")):
        return False
    try:
        salt_b64, key_b64 = inner[len("@ByteArray("):-1].split(":", 1)
        salt, key = base64.b64decode(salt_b64), base64.b64decode(key_b64)
    except ValueError:
        return False
    return hmac.compare_digest(pbkdf2(password, salt), key)


def section(lines: list[str], name: str, create: bool) -> tuple[int, int] | None:
    """Body bounds [start, end) of an INI section; appended at the end when create is set."""
    if name not in (line.strip() for line in lines):
        if not create:
            return None
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["", name] if lines else [name]
    start = next(i for i, line in enumerate(lines) if line.strip() == name) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("[")), len(lines))
    return start, end


def keys_of(lines: list[str], bounds: tuple[int, int] | None) -> dict[str, str]:
    if not bounds:
        return {}
    return {line.split("=", 1)[0]: line.split("=", 1)[1] for line in lines[bounds[0]:bounds[1]] if "=" in line}


def rewrite(lines: list[str], bounds: tuple[int, int], want: dict[str, str], drop: set[str]) -> None:
    """Set want, remove drop, keep everything else; the body stays sorted like qBittorrent writes it."""
    start, end = bounds
    body = lines[start:end]
    kept = [line for line in body if line.split("=", 1)[0] not in want.keys() | drop]
    while kept and not kept[-1].strip():
        kept.pop()
    new_body = sorted(kept + [f"{k}={v}" for k, v in want.items()], key=lambda s: s.split("=", 1)[0])
    if end < len(lines):
        new_body.append("")
    lines[start:end] = new_body


def main() -> None:
    username = os.environ["WEBUI_USERNAME"].strip()
    password = os.environ["WEBUI_PASSWORD"]
    if not username or len(password) < 12:
        sys.exit("WEBUI_USERNAME is empty or WEBUI_PASSWORD is shorter than 12 characters")

    if os.path.exists(CONF):
        with open(CONF, encoding="utf-8", newline="") as f:
            text = f.read()
        print("config: found")
    else:
        os.makedirs(os.path.dirname(CONF), exist_ok=True)
        text = DEFAULT_CONF
        print("config: missing, starting from the image defaults")
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()

    legacy = section(lines, LEGACY, create=False)
    legacy_keys = keys_of(lines, legacy)
    moved = [k for k in LOGIN_KEYS if LEGACY_PREFIX + k in legacy_keys]
    if legacy:
        rewrite(lines, legacy, {}, {LEGACY_PREFIX + k for k in LOGIN_KEYS})

    webui = section(lines, WEBUI, create=True)
    if webui is None:  # create=True always returns bounds; keeps the type checker honest
        sys.exit(f"could not create {WEBUI} in {CONF}")
    old_hash = keys_of(lines, webui).get("Password_PBKDF2") or legacy_keys.get(LEGACY_PREFIX + "Password_PBKDF2")
    password_value = old_hash if hash_matches(old_hash, password) else new_hash(password)
    want = {
        "Username": username,
        "Password_PBKDF2": password_value,
        "AuthSubnetWhitelistEnabled": "false",
        "LocalHostAuth": "true",
    }
    rewrite(lines, webui, want, {"AuthSubnetWhitelist"})

    out = nl.join(lines) + nl
    changed = out != text
    if changed:
        tmp = CONF + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(out)
        os.replace(tmp, CONF)
    print(f"username: set | password hash: {'unchanged' if password_value == old_hash else 'updated'} | "
          f"subnet bypass: off | localhost bypass: off | old [Preferences] login keys removed: {len(moved)} | "
          f"file {'written' if changed else 'unchanged'}")


if __name__ == "__main__":
    main()
