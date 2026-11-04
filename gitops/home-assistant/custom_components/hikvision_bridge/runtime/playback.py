"""Where a channel's recordings come from, and turning downloads into playable files.

Downloads are streamed: recorded files are often hundreds of megabytes. Conversion runs
ffmpeg with the download on stdin; copying the video into MP4 or MPEG-TS costs almost
nothing, converting it to H.264 decodes and encodes every frame. Recordings made with
stream encryption on are decrypted on the way, with the device's verification code.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote, urlsplit
from weakref import WeakKeyDictionary

from aiohttp import ClientResponse
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from ..const import (
    OPT_RTSP_PORT,
    OPT_STREAM_KEY,
    PLAYBACK_H264,
    PLAYBACK_MP4,
    RTSP_PORT_DEFAULT,
    SUBENTRY_CHANNEL_LINK,
)
from ..model.discover import CLOUD_PATH
from ..model.features import recordings, scrambling
from ..model.features.recordings import MediaHead, Recording
from ..model.features.scrambling import ProgramStream
from ..wire.faults import BridgeFault
from ..wire.link import IsapiLink
from ..wire.scrub import LOG_SCRUBBER, scrubbed_logger
from .bridge import DeviceBridge

_LOGGER = scrubbed_logger(__name__)

SOURCE_RECORDER = "recorder"
SOURCE_CARD = "card"
SOURCE_AUTO = "auto"

FORMAT_MP4 = PLAYBACK_MP4
FORMAT_H264 = PLAYBACK_H264
FORMAT_TS = "ts"
FORMAT_ORIGINAL = "original"
FORMATS = (FORMAT_MP4, FORMAT_H264, FORMAT_TS, FORMAT_ORIGINAL)

MIME = {
    FORMAT_MP4: "video/mp4",
    FORMAT_H264: "video/mp4",
    FORMAT_TS: "video/mp2t",
    FORMAT_ORIGINAL: "video/mp2p",
}
EXTENSION = {FORMAT_MP4: "mp4", FORMAT_H264: "mp4", FORMAT_TS: "ts", FORMAT_ORIGINAL: "ps"}
HEAD_BYTES = 256 * 1024
CHUNK = 64 * 1024
H264_MAX_WIDTH = 1920
THUMBNAIL_WIDTH = 320


class RecordingEncrypted(Exception):
    """The recording is encrypted and no known verification code fits."""


class NoRecording(Exception):
    """The download contained no video."""


@dataclass(frozen=True, slots=True)
class PlaybackSource:
    """One place that stores recordings of a channel."""

    key: str
    link: IsapiLink
    track: str
    credentials: dict
    rtsp_port: int
    # Verification code entered in the options, tried before the one the device reports.
    code: str | None = None


def sources(bridge: DeviceBridge, channel_id: str) -> list[PlaybackSource]:
    """Recorder first, then the memory card of the camera itself."""
    found: list[PlaybackSource] = []
    profile = bridge.profile
    entry_data = dict(bridge.entry.data)
    main_port = int(
        bridge.entry.options.get(OPT_RTSP_PORT) or profile.rtsp_port or RTSP_PORT_DEFAULT
    )
    code = bridge.entry.options.get(OPT_STREAM_KEY) or None
    if profile.shape.records:
        found.append(
            PlaybackSource(
                SOURCE_RECORDER,
                bridge.link,
                recordings.track_of(channel_id),
                entry_data,
                main_port,
                code,
            )
        )
        route = bridge.routes.get(channel_id)
        channel = profile.channels.get(channel_id)
        if route is not None and route.healthy and channel is not None and channel.storage:
            sub = bridge.entry.subentries.get(route.subentry_id)
            if sub is not None and sub.subentry_type == SUBENTRY_CHANNEL_LINK:
                found.append(
                    PlaybackSource(
                        SOURCE_CARD,
                        route.link,
                        recordings.track_of(route.remote_channel),
                        dict(sub.data),
                        route.rtsp_port,
                    )
                )
    elif profile.storage:
        found.append(
            PlaybackSource(
                SOURCE_CARD,
                bridge.link,
                recordings.track_of(channel_id),
                entry_data,
                main_port,
                code,
            )
        )
    return found


def pick_source(bridge: DeviceBridge, channel_id: str, key: str) -> PlaybackSource | None:
    available = sources(bridge, channel_id)
    if key == SOURCE_AUTO:
        return available[0] if available else None
    return next((s for s in available if s.key == key), None)


def rtsp_url(source: PlaybackSource, playback_uri: str) -> str:
    """The playback URI with this integration's address, port and credentials."""
    parts = urlsplit(playback_uri)
    user = quote(source.credentials[CONF_USERNAME], safe="")
    password = quote(source.credentials[CONF_PASSWORD], safe="")
    host = source.link.endpoint.base.split("://", 1)[1].rsplit(":", 1)[0]
    query = f"?{parts.query}" if parts.query else ""
    return f"rtsp://{user}:{password}@{host}:{source.rtsp_port}{parts.path}{query}"


def ffmpeg_binary(hass: HomeAssistant) -> str:
    """The ffmpeg configured in Home Assistant, else the one on the search path."""
    manager = hass.data.get("ffmpeg")
    return getattr(manager, "binary", None) or "ffmpeg"


def ffmpeg_args(
    fmt: str,
    codec: str | None,
    *,
    target: str = "pipe:1",
    offset: float | None = None,
    duration: float | None = None,
) -> list[str]:
    """Arguments converting a program stream on stdin to ``fmt`` (not for the original)."""
    args = ["-hide_banner", "-loglevel", "error", "-f", "mpeg", "-i", "pipe:0"]
    if offset:
        args += ["-ss", f"{offset:.3f}"]
    if duration:
        args += ["-t", f"{duration:.3f}"]
    args += ["-map", "0:v:0", "-map", "0:a:0?"]
    if fmt == FORMAT_H264:
        args += [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-vf",
            f"scale='min({H264_MAX_WIDTH},iw)':-2",
        ]
    else:
        args += ["-c:v", "copy"]
        if codec == "hevc" and fmt == FORMAT_MP4:
            args += ["-tag:v", "hvc1"]  # Safari and Apple devices only play hvc1
    args += ["-c:a", "aac", "-b:a", "64k"]
    if fmt == FORMAT_TS:
        args += ["-f", "mpegts"]
    else:
        streaming = target == "pipe:1"
        flags = "frag_keyframe+empty_moov+default_base_moof" if streaming else "+faststart"
        args += ["-movflags", flags, "-f", "mp4"]
    return [*args, "-y", target]


def thumbnail_args() -> list[str]:
    return [
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "mpeg",
        "-i",
        "pipe:0",
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-vf",
        f"scale={THUMBNAIL_WIDTH}:-2",
        "-f",
        "image2",
        "-c:v",
        "mjpeg",
        "pipe:1",
    ]


_codes: WeakKeyDictionary[IsapiLink, str | None] = WeakKeyDictionary()


async def device_code(link: IsapiLink, *, fresh: bool = False) -> str | None:
    """The verification code the device reports; cameras often leave it blank."""
    if not fresh and link in _codes:
        return _codes[link]
    try:
        reply = await link.call("GET", CLOUD_PATH)
        code = (reply.xml().text("verificationCode") or "").strip() or None
    except BridgeFault:
        code = None
    if code:
        LOG_SCRUBBER.remember(code)
    _codes[link] = code
    return code


async def unscrambler(source: PlaybackSource, head: bytes, codec: str | None) -> ProgramStream:
    """A pass-through decrypting with the first verification code that fits the recording.

    The code is checked on the first encrypted parameter set in ``head``; one that cannot be
    checked there is used as it is. Raises RecordingEncrypted if none fits.
    """
    tried: set[str] = set()
    for fresh in (False, True):
        for code in (source.code, await device_code(source.link, fresh=fresh)):
            if not code or code in tried:
                continue
            tried.add(code)
            key = scrambling.cipher_key(code)
            probe = ProgramStream(key, codec)
            probe.feed(head)
            if probe.key_fits is not False:
                return ProgramStream(key, codec)
    raise RecordingEncrypted


async def read_head(response: ClientResponse) -> bytes:
    head = b""
    while len(head) < HEAD_BYTES:
        chunk = await response.content.read(HEAD_BYTES - len(head))
        if not chunk:
            break
        head += chunk
    return head


async def prepare(source: PlaybackSource, head: bytes) -> tuple[MediaHead, ProgramStream]:
    """What a download is, and the pass-through for its bytes (decrypting if needed)."""
    info = recordings.inspect_head(head)
    if info.codec is None and recordings.PACK_START not in head[:4096]:
        raise NoRecording
    if info.scrambled:
        return info, await unscrambler(source, head, info.codec)
    return info, ProgramStream(codec=info.codec)


async def passed(
    stream: ProgramStream, head: bytes, response: ClientResponse
) -> AsyncIterator[bytes]:
    """All bytes of a download through ``stream``."""
    yield stream.feed(head)
    async for chunk in response.content.iter_chunked(CHUNK):
        if data := stream.feed(chunk):
            yield data
    if data := stream.flush():
        yield data


async def headed(chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """``chunks`` from the file header on, where ffmpeg looks for it."""
    first = True
    async for chunk in chunks:
        yield recordings.for_ffmpeg(chunk) if first else chunk
        first = False


@asynccontextmanager
async def open_recording(
    source: PlaybackSource, playback_uri: str
) -> AsyncIterator[tuple[MediaHead, AsyncIterator[bytes]]]:
    """Download one recorded file: what its start looks like, and all of its bytes.

    Raises RecordingEncrypted before any data is handed out if the recording is encrypted
    and no verification code fits.
    """
    async with recordings.open_download(source.link, playback_uri) as response:
        head = await read_head(response)
        info, stream = await prepare(source, head)
        yield info, passed(stream, head, response)


async def run_ffmpeg(
    binary: str,
    args: list[str],
    chunks: AsyncIterator[bytes],
    sink: Callable[[bytes], object] | None = None,
) -> bytes:
    """Feed ``chunks`` (from the file header on) to ffmpeg and pass its output to ``sink``.

    Without a sink the output is collected and returned. The process is killed if the
    caller goes away (for example a closed browser tab).
    """
    process = await asyncio.create_subprocess_exec(
        binary,
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    async def feed() -> None:
        try:
            async for chunk in headed(chunks):
                process.stdin.write(chunk)
                await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # ffmpeg has what it needs (a thumbnail) or gave up
        finally:
            with suppress(BrokenPipeError, ConnectionResetError, OSError):
                process.stdin.close()

    feeder = asyncio.create_task(feed())
    collected = bytearray()
    try:
        while data := await process.stdout.read(CHUNK):
            if sink is None:
                collected += data
            else:
                result = sink(data)
                if asyncio.iscoroutine(result):
                    await result
        errors = await process.stderr.read()
        await process.wait()
        if process.returncode:
            _LOGGER.debug("ffmpeg ended with %s: %s", process.returncode, errors[-500:])
            raise NoRecording
    finally:
        feeder.cancel()
        with suppress(asyncio.CancelledError):
            await feeder
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
    return bytes(collected)


def trim(recording: Recording, start: datetime | None, end: datetime | None) -> tuple:
    """Offset and duration (seconds) of the wanted part within one recorded file."""
    offset = (start - recording.start).total_seconds() if start else 0.0
    offset = max(0.0, offset)
    stop = (end - recording.start).total_seconds() if end else None
    duration = stop - offset if stop is not None else None
    if duration is not None and duration >= recording.duration.total_seconds() - offset:
        duration = None
    return (offset or None), duration
