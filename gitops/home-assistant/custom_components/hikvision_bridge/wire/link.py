"""A single authenticated connection to one ISAPI endpoint."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import ipaddress
import json
import time
from typing import Any

import aiohttp
from aiohttp.http_exceptions import BadStatusLine
from yarl import URL

from .faults import (
    AuthHiccup,
    BadCredentials,
    BridgeFault,
    DeviceRejected,
    DeviceStatus,
    Forbidden,
    InsecureAuth,
    LockedOut,
    NameNotResolved,
    NotIsapi,
    TlsUntrusted,
    Unreachable,
    Unsupported,
    classify,
    is_lock,
    read_status,
)
from .scrub import LOG_SCRUBBER, scrubbed_logger
from .signer import DigestSigner, Signer, build_signer, parse_challenges, pick
from .xmlview import XmlError, XmlNode, decode, parse

_LOGGER = scrubbed_logger(__name__)

PROBE_PATH = "/ISAPI/System/deviceInfo"
# One request at a time: digest nonce counts then reach the device in order.
MAX_PARALLEL = 1
# Devices refuse a valid digest now and then, e.g. when another client of the same account
# makes them issue a new nonce. Credentials that worked before therefore get a second chance:
# the first refused call pauses the link, the next refused call blocks it. A real password
# change costs at most three failed logins, below the device's login lock.
HICCUPS_BEFORE_BLOCK = 2
HICCUP_PAUSE = 20.0
# Seconds a lockout holds when the device does not say how long.
LOCK_FALLBACK = 1800.0


def _unparsable(err: aiohttp.ClientResponseError) -> BridgeFault:
    """A reply aiohttp could not parse; only a bad status line is the device refusing.

    Old firmwares answer what they do not support with ``HTTP/1.1 -1 Custom error``. Other
    parse errors (broken headers, ...) say nothing about the request and stay Unreachable.
    aiohttp wraps the parser's error in a copy, so the cause chain is searched.
    """
    cause = err.__cause__
    while cause is not None:
        if isinstance(cause, BadStatusLine):
            return DeviceRejected("bad status line", err.status)
        cause = cause.__cause__
    return Unreachable(f"{type(err).__name__}: {err}")


def _describe_challenges(headers: list[str]) -> str:
    """The authentication a device offers, without nonces or other values."""
    offered = [
        " ".join(
            part
            for part in (
                challenge.scheme,
                challenge.params.get("algorithm"),
                f"qop={challenge.params['qop']}" if "qop" in challenge.params else None,
                "opaque" if "opaque" in challenge.params else None,
                "stale" if challenge.stale else None,
            )
            if part
        )
        for challenge in parse_challenges(headers)
    ]
    return ", ".join(offered) or "no challenge"


def _describe_signer(signer: Signer | None) -> str:
    if isinstance(signer, DigestSigner):
        return f"digest {signer.algorithm or 'without algorithm'}"
    return signer.scheme if signer is not None else "an unsigned request"


def _describe_status(status: DeviceStatus) -> str:
    said = {
        "status": status.status_string,
        "sub status": status.sub_status,
        "lock": status.lock_state,
        "logins left": status.retries_left,
        "unlock in": status.unlock_seconds,
    }
    return ", ".join(f"{key} {value}" for key, value in said.items() if value is not None) or (
        "nothing more"
    )


@dataclass(frozen=True, slots=True)
class Endpoint:
    """Where a device is reached."""

    host: str
    port: int
    scheme: str = "http"

    @property
    def base(self) -> str:
        host = self.host
        try:
            if ipaddress.ip_address(host).version == 6:
                host = f"[{host}]"
        except ValueError:
            pass
        return f"{self.scheme}://{host}:{self.port}"


@dataclass(slots=True)
class Reply:
    status: int
    content_type: str
    body: bytes
    elapsed_ms: float

    @property
    def text(self) -> str:
        return decode(self.body)

    def xml(self) -> XmlNode:
        return parse(self.body)

    def json(self) -> Any:
        return json.loads(self.text)


@dataclass(slots=True)
class LinkStats:
    requests: int = 0
    failures: int = 0
    last_success: float | None = None
    last_error: str | None = None
    latency_ms: dict[str, float] = field(default_factory=dict)


class IsapiLink:
    """Serialises access to one device and owns its authentication state."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        endpoint: Endpoint,
        username: str,
        password: str,
        *,
        request_timeout: float = 15,
        write_timeout: float = 20,
        connect_timeout: float = 10,
        trusted: bool = False,
    ) -> None:
        """``trusted``: the credentials are stored ones that already worked (not a new login)."""
        self._session = session
        self.endpoint = endpoint
        self._username = username
        self._password = password
        self._signer: Signer | None = None
        # Scheme of the first accepted signature; a later challenge may not weaken it.
        self._pinned: str | None = None
        self._auth_checked = False
        self._blocked: BridgeFault | None = None
        # Monotonic time a lockout lifts (None: the block is permanent, like bad credentials).
        self._blocked_until: float | None = None
        self._worked = trusted
        self._hiccups = 0
        self._quiet_until = 0.0
        self._gate = asyncio.Semaphore(MAX_PARALLEL)
        self._write_lock = asyncio.Lock()
        self._establish_lock = asyncio.Lock()
        self._read_timeout = aiohttp.ClientTimeout(total=request_timeout, connect=connect_timeout)
        self._write_timeout = aiohttp.ClientTimeout(total=write_timeout, connect=connect_timeout)
        self._connect_timeout = connect_timeout
        self.unsupported: set[str] = set()
        # Paths that answered a GET once: a later refusal there is a hiccup, not "unsupported".
        self.served: set[str] = set()
        self.stats = LinkStats()
        LOG_SCRUBBER.remember(username, password)

    # ----------------------------------------------------------------- state

    @property
    def blocked(self) -> BridgeFault | None:
        """The fault that stopped all traffic (bad credentials, lockout), if any."""
        return self._blocked

    @property
    def auth_scheme(self) -> str | None:
        return self._signer.scheme if self._signer else None

    def replace_credentials(self, username: str, password: str) -> None:
        """Install new credentials and lift a credential block."""
        LOG_SCRUBBER.remember(username, password)
        self._username = username
        self._password = password
        self._signer = None
        self._pinned = None
        self._auth_checked = False
        self._blocked = None
        self._blocked_until = None
        self._worked = False
        self._hiccups = 0
        self._quiet_until = 0.0

    def _lock(self, fault: LockedOut) -> LockedOut:
        """Block the link until the lockout should have lifted."""
        wait = fault.retry_after if fault.retry_after and fault.retry_after > 0 else LOCK_FALLBACK
        self._blocked = fault
        self._blocked_until = time.monotonic() + wait
        return fault

    def _raise_if_blocked(self) -> None:
        """Raise the standing block; an expired lockout is cleared for one new attempt."""
        if self._blocked is None:
            return
        if self._blocked_until is not None and time.monotonic() >= self._blocked_until:
            self._blocked = None
            self._blocked_until = None
            self._signer = None
            self._auth_checked = False
            return
        raise self._blocked

    def _check_open(self) -> None:
        """Raise instead of sending while the link is blocked or pausing after a refusal."""
        self._raise_if_blocked()
        if time.monotonic() < self._quiet_until:
            raise AuthHiccup("pausing after a refused request")

    # ------------------------------------------------------------ transport

    def _url(self, path: str) -> URL:
        return URL(self.endpoint.base + path, encoded=True)

    async def _raw(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes | None,
        limits: aiohttp.ClientTimeout,
    ) -> tuple[int, str, list[str], bytes]:
        try:
            async with self._session.request(
                method,
                self._url(path),
                headers=headers,
                data=body,
                timeout=limits,
                allow_redirects=False,
            ) as response:
                payload = await response.read()
                return (
                    response.status,
                    response.headers.get("Content-Type", ""),
                    response.headers.getall("WWW-Authenticate", []),
                    payload,
                )
        except aiohttp.ClientConnectorCertificateError as err:
            raise TlsUntrusted(str(err)) from err
        except aiohttp.ClientSSLError as err:
            # A handshake failure without a certificate verdict (e.g. HTTPS sent to an HTTP
            # port) says nothing about trust: the address does not speak TLS.
            raise Unreachable(f"TLS handshake failed: {err}") from err
        except aiohttp.ClientConnectorDNSError as err:
            raise NameNotResolved(self.endpoint.host) from err
        except aiohttp.ClientResponseError as err:
            raise _unparsable(err) from err
        except (aiohttp.ClientError, TimeoutError, OSError) as err:
            raise Unreachable(f"{type(err).__name__}: {err}") from err

    async def _establish(self) -> None:
        """Learn the authentication scheme with one unauthenticated request."""
        async with self._establish_lock:
            if self._auth_checked:
                return
            status, ctype, challenges, payload = await self._raw(
                "GET", PROBE_PATH, {}, None, self._read_timeout
            )
            if status == 200:
                head = payload.lstrip()[:64].lower()
                looks_xml = "xml" in ctype or head.startswith(b"<?xml")
                if "html" in ctype or head.startswith((b"<html", b"<!doctype")) or not looks_xml:
                    raise NotIsapi(f"unexpected content type {ctype}")
                _LOGGER.warning(
                    "%s answers without authentication; consider enabling it on the device",
                    self.endpoint.host,
                )
                self._signer = None
            elif status == 401:
                chosen = pick(parse_challenges(challenges))
                if chosen is None:
                    status_block = read_status(decode(payload))
                    if is_lock(status_block):
                        raise self._lock(
                            LockedOut("device reports a lock", status_block.unlock_seconds)
                        )
                    raise NotIsapi("no usable authentication challenge")
                self._refuse_weak(chosen.scheme)
                self._signer = build_signer(self._username, self._password, chosen)
            elif status in (301, 302, 307, 308):
                raise NotIsapi("redirected; try HTTPS")
            elif status == 404 or "html" in ctype:
                raise NotIsapi(f"HTTP {status}")
            else:
                raise Unreachable(f"HTTP {status} during authentication probe")
            self._auth_checked = True

    def _refuse_weak(self, scheme: str) -> None:
        """Never send the password readable: no basic over HTTP, no downgrade after digest."""
        if self._pinned == "digest" and scheme != "digest":
            _LOGGER.warning(
                "%s now asks for %s although digest worked before; not sending the password",
                self.endpoint.host,
                scheme,
            )
            raise InsecureAuth(f"{scheme} offered after digest worked")
        if scheme == "basic" and self.endpoint.scheme != "https":
            _LOGGER.warning(
                "%s only offers basic authentication over plain HTTP, which would send the "
                "password unencrypted; use HTTPS or set the device's web authentication to digest",
                self.endpoint.host,
            )
            raise InsecureAuth("basic authentication over plain HTTP")

    async def call(
        self,
        method: str,
        path: str,
        *,
        body: bytes | str | None = None,
        content_type: str = "application/xml; charset=UTF-8",
    ) -> Reply:
        """Send one request, handling digest nonce refresh. Raises a BridgeFault on errors."""
        self._raise_if_blocked()
        payload = body.encode("utf-8") if isinstance(body, str) else body
        writing = method != "GET"
        timeout = self._write_timeout if writing else self._read_timeout

        async with self._gate:
            if writing:
                async with self._write_lock:
                    return await self._authorized(method, path, payload, content_type, timeout)
            return await self._authorized(method, path, payload, content_type, timeout)

    async def _authorized(
        self,
        method: str,
        path: str,
        payload: bytes | None,
        content_type: str,
        limits: aiohttp.ClientTimeout,
    ) -> Reply:
        started = time.monotonic()
        self.stats.requests += 1
        for attempt in range(2):
            # Checked inside the request slot: an earlier request may have blocked the link
            # or reset authentication while this one was waiting.
            self._check_open()
            if not self._auth_checked:
                await self._establish()
            headers = {"Accept": "*/*"}
            if payload is not None:
                headers["Content-Type"] = content_type
            signed = self._signed_state()
            if self._signer is not None:
                headers["Authorization"] = self._signer.authorization(method, path)
            try:
                status, ctype, challenges, data = await self._raw(
                    method, path, headers, payload, limits
                )
            except BridgeFault as err:
                self.stats.failures += 1
                self.stats.last_error = type(err).__name__
                raise
            if status != 401:
                self._mark_success(signed)
                elapsed = (time.monotonic() - started) * 1000
                self.stats.last_success = time.monotonic()
                self.stats.latency_ms[path.split("?", maxsplit=1)[0]] = round(elapsed, 1)
                return self._finish(method, path, Reply(status, ctype, data, elapsed))
            fault = self._after_refusal(attempt, challenges, data, signed)
            if fault is not None:
                self.stats.failures += 1
                self.stats.last_error = type(fault).__name__
                raise fault
        raise self._blocked or BadCredentials("rejected")

    def _signed_state(self) -> tuple[int, bool] | None:
        """Nonce generation and whether that nonce had worked, at signing time."""
        signer = self._signer
        if isinstance(signer, DigestSigner):
            return signer.generation, signer.nonce_proven
        return None

    def _mark_success(self, signed: tuple[int, bool] | None) -> None:
        signer = self._signer
        if signer is None:
            return
        self._worked = True
        self._hiccups = 0
        self._pinned = self._pinned or signer.scheme
        # A reply to a request signed with an older nonce says nothing about the current one.
        if (
            isinstance(signer, DigestSigner)
            and signed is not None
            and signed[0] != signer.generation
        ):
            return
        signer.mark_success()

    def _after_refusal(
        self,
        attempt: int,
        challenges: list[str],
        data: bytes,
        signed: tuple[int, bool] | None,
    ) -> BridgeFault | None:
        """Decide after a 401: None means one more attempt is safe, else the fault to raise."""
        status = read_status(decode(data))
        if is_lock(status):
            return self._lock(LockedOut("device reports a lock", status.unlock_seconds))
        fresh = pick(parse_challenges(challenges))
        signer = self._signer
        if attempt == 0 and isinstance(signer, DigestSigner) and signed is not None:
            generation, was_proven = signed
            if generation != signer.generation:
                # Another request already adopted a newer nonce; sign again with it.
                return None
            trusted = self._worked and self._hiccups == 0
            if (
                fresh is not None
                and fresh.scheme == "digest"
                and (fresh.stale or was_proven or trusted)
            ):
                # The nonce expired or was replaced; the credentials are known to work.
                # Exactly one retry with the fresh challenge.
                signer.accept(fresh)
                return None
            if fresh is None and trusted:
                # Device refused without a challenge (e.g. intercom nonce expired);
                # re-probe on attempt 1 to obtain a fresh challenge.
                self._auth_checked = False
                return None
        if self._worked and self._hiccups + 1 < HICCUPS_BEFORE_BLOCK:
            # These credentials worked before: pause, then start over with a fresh challenge
            # instead of giving up on a single refusal.
            self._hiccups += 1
            self._signer = None
            self._auth_checked = False
            self._quiet_until = time.monotonic() + HICCUP_PAUSE
            _LOGGER.warning(
                "%s refused a request although the credentials worked before; trying again later",
                self.endpoint.host,
            )
            return AuthHiccup("refused after a fresh challenge")
        self._blocked = BadCredentials("credentials rejected after a fresh challenge")
        # The details stay in the warning: a failed setup leaves no time to enable debug logs.
        _LOGGER.warning(
            "%s rejected the credentials; all requests stop until they are updated "
            "(sent %s; it offers %s and says %s)",
            self.endpoint.host,
            _describe_signer(signer),
            _describe_challenges(challenges),
            _describe_status(status),
        )
        return self._blocked

    def _finish(self, method: str, path: str, reply: Reply) -> Reply:
        fault = classify(reply.status, reply.text if reply.body else "")
        base = path.split("?", maxsplit=1)[0]
        if fault is None:
            if method == "GET":
                self.served.add(base)
            return reply
        if isinstance(fault, Unsupported):
            if path == base and base not in self.served:
                self.unsupported.add(base)
            elif path != base:
                self.unsupported.add(path)
        if isinstance(fault, LockedOut):
            self._lock(fault)
        raise fault

    # ------------------------------------------------------------ helpers

    def _known_unsupported(self, path: str) -> bool:
        """Whether the path, or its resource without the query, answered unsupported."""
        return path in self.unsupported or path.split("?", maxsplit=1)[0] in self.unsupported

    def _absent(self, path: str, optional: bool) -> bool:
        """Whether an optional read may answer None, i.e. the path never worked."""
        return optional and path.split("?", maxsplit=1)[0] not in self.served

    async def read_xml(self, path: str, *, optional: bool = False) -> XmlNode | None:
        """GET an XML document. With ``optional`` unsupported or forbidden resources return None.

        A path that answered before is not taken for unsupported later: its errors are raised.
        """
        if optional and self._known_unsupported(path):
            return None
        try:
            reply = await self.call("GET", path)
            return reply.xml()
        except Unsupported:
            if self._absent(path, optional):
                return None
            raise
        except Forbidden:
            if self._absent(path, optional):
                self.unsupported.add(path.split("?", maxsplit=1)[0])
                return None
            raise
        except DeviceRejected as err:
            if self._absent(path, optional) and err.status in (400, 500, 501):
                self.unsupported.add(path.split("?", maxsplit=1)[0])
                return None
            raise
        except XmlError as err:
            if optional:
                return None
            raise NotIsapi(f"{path}: {err}") from err

    async def read_text(self, path: str, *, optional: bool = False) -> str | None:
        if optional and self._known_unsupported(path):
            return None
        try:
            return (await self.call("GET", path)).text
        except Unsupported:
            if self._absent(path, optional):
                return None
            raise
        except Forbidden:
            if self._absent(path, optional):
                self.unsupported.add(path.split("?", maxsplit=1)[0])
                return None
            raise

    async def read_json(self, path: str, *, optional: bool = False) -> Any:
        text = await self.read_text(path, optional=optional)
        if text is None:
            return None
        try:
            return json.loads(text)
        except ValueError as err:
            if optional:
                return None
            raise NotIsapi(f"{path}: invalid JSON") from err

    async def read_bytes(self, path: str) -> Reply:
        return await self.call("GET", path)

    async def write_xml(self, path: str, document: str, method: str = "PUT") -> Reply:
        return await self.call(method, path, body=document)

    # ------------------------------------------------------------- streams

    @asynccontextmanager
    async def open_stream(
        self,
        path: str,
        *,
        method: str = "GET",
        body: str | None = None,
        content_type: str = "application/xml; charset=UTF-8",
    ) -> AsyncIterator[aiohttp.ClientResponse]:
        """Open a long-lived authenticated request (event stream, recording download).

        The request slot is held only until the response headers arrive.
        """
        self._raise_if_blocked()
        payload = body.encode("utf-8") if body is not None else None
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=self._connect_timeout)
        for attempt in range(2):
            async with self._gate:
                self._check_open()
                if not self._auth_checked:
                    await self._establish()
                headers = {"Accept": "*/*"}
                if payload is not None:
                    headers["Content-Type"] = content_type
                signed = self._signed_state()
                if self._signer is not None:
                    headers["Authorization"] = self._signer.authorization(method, path)
                try:
                    response = await self._session.request(
                        method,
                        self._url(path),
                        headers=headers,
                        data=payload,
                        timeout=timeout,
                        allow_redirects=False,
                    )
                except aiohttp.ClientConnectorCertificateError as err:
                    raise TlsUntrusted(str(err)) from err
                except aiohttp.ClientResponseError as err:
                    raise _unparsable(err) from err
                except (aiohttp.ClientError, TimeoutError, OSError) as err:
                    raise Unreachable(f"{type(err).__name__}: {err}") from err
                if response.status == 401:
                    challenges = response.headers.getall("WWW-Authenticate", [])
                    data = await response.read()
                    response.release()
                    fault = self._after_refusal(attempt, challenges, data, signed)
                    if fault is None:
                        continue
                    raise fault
                if response.status != 200:
                    data = await response.read()
                    response.release()
                    raise classify(response.status, decode(data)) or DeviceRejected(
                        f"HTTP {response.status}", response.status
                    )
                self._mark_success(signed)
            try:
                yield response
            finally:
                response.close()
            return
        raise self._blocked or BadCredentials("rejected")
