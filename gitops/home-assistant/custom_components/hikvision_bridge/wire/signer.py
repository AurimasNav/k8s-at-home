"""HTTP authentication for ISAPI devices (RFC 7616 digest, basic as last resort).

Lockout protection lives here: the signer is never asked to guess. A challenge is always
obtained without credentials first, digest is used whenever offered, and the caller stops
all traffic after one rejection of a fresh challenge.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import re
import secrets
import time

_PARAM = re.compile(
    r'(?P<key>[A-Za-z][\w-]*)\s*=\s*(?:"(?P<quoted>(?:[^"\\]|\\.)*)"|(?P<token>[^\s,]+))'
)


@dataclass(frozen=True, slots=True)
class Challenge:
    """One parsed ``WWW-Authenticate`` challenge."""

    scheme: str
    params: dict[str, str] = field(default_factory=dict)

    @property
    def stale(self) -> bool:
        return self.params.get("stale", "").lower() == "true"


def parse_challenges(headers: list[str]) -> list[Challenge]:
    """Parse all challenges from the given ``WWW-Authenticate`` header values."""
    found: list[Challenge] = []
    for header in headers:
        # A header may carry several challenges; split on scheme names at token starts.
        for chunk in re.split(r",\s*(?=(?:Digest|Basic|Bearer)\s)", header.strip()):
            scheme, _, rest = chunk.strip().partition(" ")
            if not scheme:
                continue
            params = {
                m["key"].lower(): (m["quoted"] if m["quoted"] is not None else m["token"])
                for m in _PARAM.finditer(rest)
            }
            found.append(Challenge(scheme.lower(), params))
    return found


def pick(challenges: list[Challenge]) -> Challenge | None:
    """Prefer digest (strongest algorithm first); basic only if nothing else is offered."""
    digests = [c for c in challenges if c.scheme == "digest"]
    if digests:
        return max(
            digests, key=lambda c: c.params.get("algorithm", "MD5").upper().startswith("SHA-256")
        )
    basics = [c for c in challenges if c.scheme == "basic"]
    return basics[0] if basics else None


class Signer:
    """Produces ``Authorization`` header values."""

    scheme = ""

    def authorization(self, method: str, uri: str) -> str:
        raise NotImplementedError

    def accept(self, challenge: Challenge) -> None:
        """Take over a new challenge (nonce refresh)."""

    @property
    def proven(self) -> bool:
        """True once the current credentials were accepted at least once."""
        return False

    def mark_success(self) -> None:
        """Record that the last header was accepted."""


class BasicSigner(Signer):
    scheme = "basic"

    def __init__(self, username: str, password: str) -> None:
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._value = f"Basic {token}"
        self._proven = False

    def authorization(self, method: str, uri: str) -> str:
        return self._value

    @property
    def proven(self) -> bool:
        return self._proven

    def mark_success(self) -> None:
        self._proven = True


class DigestSigner(Signer):
    scheme = "digest"

    def __init__(self, username: str, password: str, challenge: Challenge) -> None:
        self._username = username
        self._password = password
        self._challenge = challenge
        self._count = 0
        self._nonce_proven = False
        self._ever_proven = False
        self.generation = 0
        self.adopted_at = time.monotonic()

    @property
    def proven(self) -> bool:
        return self._ever_proven

    @property
    def algorithm(self) -> str | None:
        return self._challenge.params.get("algorithm")

    @property
    def nonce_proven(self) -> bool:
        return self._nonce_proven

    def accept(self, challenge: Challenge) -> None:
        self._challenge = challenge
        self._count = 0
        self._nonce_proven = False
        self.generation += 1
        self.adopted_at = time.monotonic()

    def mark_success(self) -> None:
        self._nonce_proven = True
        self._ever_proven = True

    def _hash(self, data: str) -> str:
        algorithm = self._challenge.params.get("algorithm", "MD5").upper()
        if algorithm.startswith("SHA-256"):
            return hashlib.sha256(data.encode()).hexdigest()
        return hashlib.md5(data.encode(), usedforsecurity=False).hexdigest()

    def authorization(self, method: str, uri: str) -> str:
        params = self._challenge.params
        realm = params.get("realm", "")
        nonce = params.get("nonce", "")
        algorithm = params.get("algorithm")
        qop_offered = [q.strip() for q in params.get("qop", "").split(",") if q.strip()]
        qop = "auth" if "auth" in qop_offered else None

        self._count += 1
        nc = f"{self._count:08x}"
        cnonce = secrets.token_hex(8)

        ha1 = self._hash(f"{self._username}:{realm}:{self._password}")
        if algorithm and algorithm.upper().endswith("-SESS"):
            ha1 = self._hash(f"{ha1}:{nonce}:{cnonce}")
        ha2 = self._hash(f"{method}:{uri}")
        if qop:
            response = self._hash(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
        else:
            response = self._hash(f"{ha1}:{nonce}:{ha2}")

        parts = [
            f'username="{self._username}"',
            f'realm="{realm}"',
            f'nonce="{nonce}"',
            f'uri="{uri}"',
            f'response="{response}"',
        ]
        if algorithm:
            parts.append(f"algorithm={algorithm}")
        if "opaque" in params:
            parts.append(f'opaque="{params["opaque"]}"')
        if qop:
            parts.extend((f"qop={qop}", f"nc={nc}", f'cnonce="{cnonce}"'))
        return "Digest " + ", ".join(parts)


def build_signer(username: str, password: str, challenge: Challenge) -> Signer:
    if challenge.scheme == "digest":
        return DigestSigner(username, password, challenge)
    return BasicSigner(username, password)
