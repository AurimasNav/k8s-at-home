"""Turn on qBittorrent Web UI login before qBittorrent starts (init container).

qBittorrent keeps its settings in /config/qBittorrent/qBittorrent.conf and has no env
switch for the login, so this edits the [Preferences] section in place:

- WebUI\\Username and WebUI\\Password_PBKDF2 from the Doppler login (WEBUI_USERNAME /
  WEBUI_PASSWORD, from the qbittorrent-webui ExternalSecret). The hash uses qBittorrent's own format (PBKDF2-HMAC-SHA512,
  100,000 iterations, 16-byte salt, 64-byte key). It is only rewritten when the stored
  hash does not match the password, so restarts don't churn the file.
- The auth bypass is turned off: no subnet whitelist (it was 0.0.0.0/0, i.e. anyone who
  could reach the pod skipped the login) and localhost has to log in too.

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
SECTION = "[Preferences]"
# Fresh volume: qBittorrent would otherwise start from the image's default file, which
# the entrypoint copies only when no file exists. Same defaults, plus the login below.
DEFAULT_CONF = """[LegalNotice]
Accepted=true

[Preferences]
Connection\\UPnP=false
General\\Locale=en
WebUI\\Address=*
WebUI\\CSRFProtection=false
WebUI\\HostHeaderValidation=false
WebUI\\ServerDomains=*
WebUI\\UseUPnP=false
"""


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

    if SECTION not in (line.strip() for line in lines):
        lines += ["", SECTION]
    start = next(i for i, line in enumerate(lines) if line.strip() == SECTION) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("[")), len(lines))
    body = lines[start:end]
    keys = {line.split("=", 1)[0]: line.split("=", 1)[1] for line in body if "=" in line}

    old_hash = keys.get("WebUI\\Password_PBKDF2")
    password_value = old_hash if hash_matches(old_hash, password) else new_hash(password)
    want = {
        "WebUI\\Username": username,
        "WebUI\\Password_PBKDF2": password_value,
        "WebUI\\AuthSubnetWhitelistEnabled": "false",
        "WebUI\\LocalHostAuth": "true",
    }
    drop = {"WebUI\\AuthSubnetWhitelist"}

    kept = [line for line in body if line.split("=", 1)[0] not in want.keys() | drop]
    while kept and not kept[-1].strip():
        kept.pop()
    new_body = sorted(kept + [f"{k}={v}" for k, v in want.items()], key=lambda s: s.split("=", 1)[0])
    if end < len(lines):
        new_body.append("")
    lines[start:end] = new_body

    out = nl.join(lines) + nl
    changed = out != text
    if changed:
        tmp = CONF + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(out)
        os.replace(tmp, CONF)
    print(f"username: set | password hash: {'unchanged' if password_value == old_hash else 'updated'} | "
          f"subnet bypass: off | localhost bypass: off | file {'written' if changed else 'unchanged'}")


if __name__ == "__main__":
    main()
