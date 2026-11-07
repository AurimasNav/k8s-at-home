"""Loopback RTSP relay for Hikvision-encrypted H.264 video over TCP.

Only marked NAL units are decrypted. RTP timestamps and sequence numbers are kept;
Home Assistant's normal stream worker handles playback, without transcoding here.
Protocol references (MIT): dsameendra/sentinel-eye tools/NOTES.md and
matthewdeaves/homebridge-hikvision-dvr lib/hikcrypt.js. No vendor SDK is loaded.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
import re
import secrets
from urllib.parse import urlsplit, urlunsplit

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..model.features.scrambling import cipher_key
from ..wire.scrub import LOG_SCRUBBER, scrubbed_logger
from ..wire.signer import Signer, build_signer, parse_challenges, pick

_LOGGER = scrubbed_logger(__name__)
TIMEOUT = 20
MAX_NAL = 4 * 1024 * 1024
MAX_BODY = 64 * 1024
_STREAM_URL = re.compile(r'(?:rtsp://[^\s/;,"<>]+)?/Streaming/[^\s;,"<>]*', re.IGNORECASE)


class LiveError(Exception):
    """A protocol error with a message containing no device-supplied text."""


class LiveRejected(LiveError):
    """The code, the login or its scheme cannot work; retrying would only risk a lockout."""


@dataclass
class Message:
    line: str
    headers: list[tuple[str, str]]
    body: bytes = b""

    def get(self, name: str) -> str:
        return next((v for k, v in self.headers if k.lower() == name.lower()), "")

    def encode(self) -> bytes:
        headers = [(k, v) for k, v in self.headers if k.lower() != "content-length"]
        headers.append(("Content-Length", str(len(self.body))))
        return (
            self.line + "\r\n" + "\r\n".join(f"{k}: {v}" for k, v in headers) + "\r\n\r\n"
        ).encode() + self.body


async def read_message(
    reader: asyncio.StreamReader, idle_seconds: int = TIMEOUT
) -> Message | bytes:
    """One bounded RTSP message or one complete interleaved RTP/RTCP frame."""
    async with asyncio.timeout(idle_seconds):
        first = await reader.readexactly(1)
        if first == b"$":
            head = await reader.readexactly(3)
            return first + head + await reader.readexactly(int.from_bytes(head[1:], "big"))
        head = first + await reader.readuntil(b"\r\n\r\n")
        lines = head.decode("utf-8").split("\r\n")
        headers = [tuple(line.split(":", 1)) for line in lines[1:] if line]
        headers = [(k.strip(), v.strip()) for k, v in headers]
        msg = Message(lines[0], headers)
        size = int(msg.get("Content-Length") or 0)
        if not 0 <= size <= MAX_BODY:
            raise LiveError("RTSP body exceeds limit")
        msg.body = await reader.readexactly(size)
        return msg


class H264Decryptor:
    """Reassemble encrypted FU-A NALs, decrypt, then retain their RTP packetization."""

    def __init__(self, code: str) -> None:
        # ECB is the device's existing wire format, not a choice of encryption for new data.
        self.cipher = Cipher(algorithms.AES(cipher_key(code)), modes.ECB())  # noqa: S305
        self.parts: list[tuple[bytes, int, int]] = []
        self.size = 0
        self.identity: bytes | None = None
        self.sequence = 0
        self.header = 0
        self.encrypted = False

    def nal(self, data: bytes) -> bytes:
        # H.264 encrypted wrapper: copied header + AES(original NAL) + clear tail.
        count = min(4096, (len(data) - 1) // 16 * 16)
        if count < 16:
            raise LiveError("Encrypted H.264 unit is too short")
        plain = self.cipher.decryptor().update(data[1 : 1 + count])
        if plain[0] != data[0]:
            raise LiveRejected("Live verification code does not fit the encrypted stream")
        return plain + data[1 + count :]

    def feed(self, frame: bytes) -> list[bytes]:
        packet = frame[4:]
        if len(packet) < 12 or packet[0] >> 6 != 2:
            raise LiveError("Invalid RTP packet")
        offset = 12 + (packet[0] & 15) * 4
        flagged = False
        if packet[0] & 16:
            if offset + 4 > len(packet):
                raise LiveError("Invalid RTP extension")
            flagged = packet[offset : offset + 2] == b"\x40\x00"
            offset += 4 + int.from_bytes(packet[offset + 2 : offset + 4], "big") * 4
        end = len(packet)
        if packet[0] & 32:
            padding = packet[-1]
            if not padding or padding > end - offset:
                raise LiveError("Invalid RTP padding")
            end -= padding
        if offset > end:
            raise LiveError("Invalid RTP payload offset")
        if offset == end:
            return [frame]  # Some DVRs send empty RTP packets during recorded playback.
        kind = packet[offset] & 31
        if kind != 28:
            self.parts.clear()
            if not flagged:
                return [frame]
            payload = packet[offset:end]
            if 1 <= kind <= 23:
                payload = self.nal(payload)
            elif kind == 24:  # STAP-A: each NAL has its own length and wrapper.
                result = bytearray(payload[:1])
                at = 1
                while at < len(payload):
                    if at + 2 > len(payload):
                        raise LiveError("Truncated H.264 aggregation")
                    size = int.from_bytes(payload[at : at + 2], "big")
                    at += 2
                    if not size or at + size > len(payload):
                        raise LiveError("Invalid H.264 aggregation")
                    nal = self.nal(payload[at : at + size])
                    result += len(nal).to_bytes(2, "big") + nal
                    at += size
                payload = bytes(result)
            else:
                raise LiveError("Unsupported encrypted H.264 packetization")
            return [self._frame(frame, offset, end, payload)]
        if end - offset < 3:
            raise LiveError("Truncated H.264 fragment")
        start, finish = packet[offset + 1] & 128, packet[offset + 1] & 64
        sequence = int.from_bytes(packet[2:4], "big")
        identity = packet[4:12]  # timestamp + SSRC
        if start:
            self.parts.clear()
            self.size = 1
            self.header = (packet[offset] & 224) | (packet[offset + 1] & 31)
            self.encrypted = flagged
            self.identity = identity
        elif not self.parts or identity != self.identity or sequence != (self.sequence + 1) % 65536:
            self.parts.clear()
            return []  # Drop incomplete NAL, resume at the next start fragment.
        self.sequence = sequence
        self.parts.append((frame, offset, end))
        self.size += end - offset - 2
        if self.size > MAX_NAL or len(self.parts) > 8192:
            self.parts.clear()
            raise LiveError("Live H.264 unit exceeds limit")
        if not finish:
            return []
        parts, self.parts = self.parts, []
        if not self.encrypted:
            return [part[0] for part in parts]
        data = bytes([self.header]) + b"".join(f[4 + at + 2 : 4 + end] for f, at, end in parts)
        # FU headers already contain the reconstructed NAL header.
        data = self.nal(data)[1:]
        result = []
        at = 0
        for f, offset, end in parts:
            count = min(end - offset - 2, len(data) - at)
            if count <= 0:
                raise LiveError("Invalid encrypted H.264 fragmentation")
            payload = f[4 + offset : 4 + offset + 2] + data[at : at + count]
            result.append(self._frame(f, offset, end, payload))
            at += count
        return result

    @staticmethod
    def _frame(frame: bytes, offset: int, end: int, payload: bytes) -> bytes:
        packet = frame[4 : 4 + offset] + payload + frame[4 + end :]
        return frame[:2] + len(packet).to_bytes(2, "big") + packet


class LiveRelay:
    """One fixed upstream stream, reachable only on loopback with a random path token."""

    def __init__(
        self,
        host: str,
        port: int,
        stream_id: str,
        username: str,
        password: str,
        code: str,
        *,
        resource_path: str | None = None,
        on_failure: Callable[[], None] | None = None,
    ) -> None:
        self.host, self.port = host, port
        address = f"[{host}]" if ":" in host else host
        resource = (
            resource_path if resource_path is not None else f"/Streaming/Channels/{stream_id}"
        )
        self.upstream = f"rtsp://{address}:{port}{resource}"
        self.username, self.password, self.code = username, password, code
        self.token = secrets.token_urlsafe(32)
        LOG_SCRUBBER.remember(code, self.token)
        self.server: asyncio.Server | None = None
        self.tasks: set[asyncio.Task] = set()
        self.url = ""
        self.failure: str | None = None
        self.on_failure = on_failure
        self.lock = asyncio.Lock()
        self.closing = False

    async def start(self) -> str:
        async with self.lock:
            if self.closing:
                raise LiveError("Live relay has been unloaded")
            if self.server is None:
                self.server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
                port = self.server.sockets[0].getsockname()[1]
                self.url = f"rtsp://127.0.0.1:{port}/{self.token}"
        return self.url

    async def close(self) -> None:
        self.closing = True
        server, self.server = self.server, None
        if server:
            server.close()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if server:
            await server.wait_closed()

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if self.closing or self.failure or len(self.tasks) >= 2:
            writer.close()
            return
        self.tasks.add(task)
        remote: asyncio.StreamWriter | None = None
        pumps: list[asyncio.Task] = []
        try:
            # Authenticate the local path before opening any DVR connection.
            first = await read_message(reader)
            if not isinstance(first, Message) or not self._valid_uri(first.line.split()[1]):
                raise LiveError("Invalid local live-stream URL")
            async with asyncio.timeout(TIMEOUT):
                upstream_reader, remote = await asyncio.open_connection(self.host, self.port)
            state = _Session(self, writer, remote)
            await state.request(first)
            pumps = [
                asyncio.create_task(state.requests(reader)),
                asyncio.create_task(state.responses(upstream_reader)),
            ]
            done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for finished in done:
                finished.result()
        except LiveRejected as err:
            # Terminal: refuse further connections until the entry reloads, so a wrong
            # login is not retried into a lockout. The callback raises a repair issue.
            first_failure = self.failure is None
            self.failure = str(err)
            _LOGGER.warning("Encrypted live stream stopped: %s", err)
            if first_failure and self.on_failure is not None:
                self.on_failure()
        except LiveError as err:
            _LOGGER.warning("Encrypted live stream stopped: %s", err)
        except (
            OSError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            UnicodeError,
            ValueError,
            IndexError,
        ):
            _LOGGER.debug("Encrypted live stream connection closed")
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            for connection in (remote, writer):
                if connection:
                    connection.close()
                    with suppress(OSError):
                        await connection.wait_closed()
            self.tasks.discard(task)

    def rewrite_urls(self, text: str) -> str:
        """Map only this stream's path, independent of the device's advertised authority."""
        scope = urlsplit(self.upstream).path.rstrip("/")

        def local(match: re.Match[str]) -> str:
            parts = urlsplit(match[0])
            if parts.path.lower() != scope.lower() and not parts.path.lower().startswith(
                scope.lower() + "/"
            ):
                return match[0]
            suffix = parts.path[len(scope) :]
            return self.url + suffix + (f"?{parts.query}" if parts.query else "")

        return _STREAM_URL.sub(local, text)

    def target(self, uri: str) -> str:
        """Keep configured host/port and append track paths before the time query."""
        upstream, local = urlsplit(self.upstream), urlsplit(uri)
        suffix = local.path[len(urlsplit(self.url).path) :]
        path = upstream.path.rstrip("/") + suffix if suffix else upstream.path
        return urlunsplit(
            (upstream.scheme, upstream.netloc, path, local.query or upstream.query, "")
        )

    def _valid_uri(self, uri: str) -> bool:
        return uri == self.url or uri.startswith((self.url + "/", self.url + "?"))


class _Session:
    def __init__(
        self,
        relay: LiveRelay,
        local: asyncio.StreamWriter,
        remote: asyncio.StreamWriter,
    ) -> None:
        self.relay, self.local, self.remote = relay, local, remote
        self.signer: Signer | None = None
        self.pending: dict[str, tuple[Message, bool, bool]] = {}
        self.video_channel: int | None = None
        self.decryptor: H264Decryptor | None = None

    async def request(self, msg: Message, *, stale_retry: bool = False) -> None:
        method, uri, version = msg.line.split()
        if version != "RTSP/1.0" or not self.relay._valid_uri(uri):
            raise LiveError("Invalid local RTSP request")
        if method not in {
            "OPTIONS",
            "DESCRIBE",
            "SETUP",
            "PLAY",
            "PAUSE",
            "GET_PARAMETER",
            "TEARDOWN",
        }:
            raise LiveError("Unsupported RTSP method")
        if method == "SETUP" and "RTP/AVP/TCP" not in msg.get("Transport").upper():
            raise LiveError("Encrypted live streaming requires RTSP over TCP")
        target = self.relay.target(uri)
        headers = [(k, v) for k, v in msg.headers if k.lower() not in {"authorization", "host"}]
        if self.signer:
            headers.append(("Authorization", self.signer.authorization(method, target)))
        self.pending[msg.get("CSeq")] = (msg, self.signer is not None, stale_retry)
        if len(self.pending) > 8:
            raise LiveError("Too many pending RTSP requests")
        self.remote.write(Message(f"{method} {target} RTSP/1.0", headers, msg.body).encode())
        await self.remote.drain()

    async def requests(self, reader: asyncio.StreamReader) -> None:
        while True:
            msg = await read_message(reader, idle_seconds=120)
            if isinstance(msg, bytes):
                self.remote.write(msg)  # Receiver reports / RTCP.
                await self.remote.drain()
            else:
                await self.request(msg)

    async def responses(self, reader: asyncio.StreamReader) -> None:
        while True:
            msg = await read_message(reader)
            if isinstance(msg, bytes):
                frames = (
                    self.decryptor.feed(msg)
                    if self.decryptor is not None and msg[1] == self.video_channel
                    else [msg]
                )
                for frame in frames:
                    self.local.write(frame)
                await self.local.drain()
                continue
            request, signed, stale_retry = self.pending.pop(msg.get("CSeq"), (None, False, False))
            status = msg.line.split()[1]
            if status == "401":
                challenge = pick(
                    parse_challenges([v for k, v in msg.headers if k.lower() == "www-authenticate"])
                )
                if signed and not (
                    challenge
                    and challenge.scheme == "digest"
                    and challenge.stale
                    and not stale_retry
                ):
                    raise LiveRejected(
                        "DVR rejected live-stream credentials; reload after correcting them"
                    )
                if challenge is None or request is None:
                    raise LiveError("Unsupported RTSP authentication challenge")
                if challenge.scheme != "digest":
                    # RTSP is plain text: Basic would send the password readable on the network.
                    raise LiveRejected("DVR offered only Basic authentication over plain RTSP")
                self.signer = build_signer(self.relay.username, self.relay.password, challenge)
                await self.request(request, stale_retry=signed)
                continue
            if status.startswith("3"):
                raise LiveError("RTSP redirects are not supported")
            if request and request.line.startswith("DESCRIBE ") and status == "200":
                sdp = msg.body.decode()
                # Only H.264 is decrypted. Other codecs retain their original RTP/SDP,
                # so clear H.265 still plays if metadata described the codec incorrectly.
                session, *tracks = sdp.split("m=")
                video = next((t for t in tracks if t.startswith("video ")), None)
                if video is None:
                    raise LiveError("No live video track")
                if "H264/90000" in video.upper():
                    self.decryptor = H264Decryptor(self.relay.code)
                    # Encrypted audio is outside this implementation.
                    msg.body = (session + "m=" + video).encode()
                else:
                    self.decryptor = None
            if request and request.line.startswith("SETUP ") and status == "200":
                match = re.search(r"interleaved=(\d+)-(\d+)", msg.get("Transport"))
                if not match:
                    raise LiveError("DVR did not accept interleaved TCP transport")
                self.video_channel = int(match[1])
            # Absolute track URLs, Content-Base and RTP-Info must point back at this relay.
            msg.headers = [(k, self.relay.rewrite_urls(v)) for k, v in msg.headers]
            msg.body = self.relay.rewrite_urls(msg.body.decode()).encode()
            self.local.write(msg.encode())
            await self.local.drain()
