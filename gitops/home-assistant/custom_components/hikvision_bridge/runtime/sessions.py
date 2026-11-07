"""Playback sessions: recordings from a point in time, made into HLS by ffmpeg.

The device hands out the recordings of a time span as a download, much faster than playing
them; it is decrypted if needed and fed to ffmpeg, which only repackages it. The download is
held back once the prepared video is far enough ahead of the player. Some recorders refuse
the download of a span but hand out their recorded files; a session then covers the file that
holds the wanted time and starts at the last key frame before it. Where the device can, it
leaves out the part before that time itself; otherwise that part is dropped as fast as the
device sends it, without ffmpeg. Devices that refuse both play the span over RTSP instead, in
real time and without decryption. Devices that send recorded files only from their start
play a time further into a file than they reach in READ_SECONDS over RTSP as well, if they can;
a read that its pace shows will not reach the time in READ_SECONDS gives up after PACE_SECONDS.

One session covers one stretch of contiguous recordings, so a position in the video maps to
the recorded time exactly (start of the session plus the playing time). Cameras serve only
one playback at a time; a viewer that seeks therefore replaces its session, and the old
process ends before the new one starts, and a seek still starting is dropped for the newer one.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
import re
import secrets
import shutil
import tempfile
import time
from weakref import WeakKeyDictionary, WeakSet

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from ..model.features import recordings
from ..model.features.scrambling import PTS_WRAP, ProgramStream
from ..wire.faults import DeviceRejected, Unsupported, detail
from ..wire.link import IsapiLink
from ..wire.scrub import scrub_text, scrubbed_logger
from . import playback
from .playback import NoRecording, PlaybackSource, RecordingEncrypted

_LOGGER = scrubbed_logger(__name__)

QUALITY_ORIGINAL = "original"
QUALITY_COMPATIBLE = "compatible"
QUALITIES = (QUALITY_ORIGINAL, QUALITY_COMPATIBLE)
SEGMENT_SECONDS = 2
FIRST_SEGMENT_TIMEOUT = 25
# Longest wait for the part of a recorded file before the wanted time; slower devices start
# playing from the latest key frame they reached.
SKIP_SECONDS = 30
# Time a read of a recorded file runs before its pace tells whether it reaches the wanted time.
PACE_SECONDS = 5
# Longest read up to the wanted time while RTSP can play it instead: RTSP starts at the time
# in a few seconds, however far into the file it lies.
READ_SECONDS = 10
# Download kept while no key frame was found yet; past it the file plays from its start.
SKIP_SEARCH_BYTES = 8 * 1024 * 1024
# From this far into a recorded file the device is asked to start it at the wanted time.
LATER_START_SECONDS = 20
# Bytes compared to tell whether the device sent a recorded file from its start anyway.
COMPARE_BYTES = 64 * 1024
# ffmpeg gives up on an RTSP connection that sends nothing for this long (microseconds).
RTSP_TIMEOUT = 10_000_000
# Pause before asking a device again that is still releasing the previous playback.
BUSY_PAUSE = 2.0
IDLE_SECONDS = 45
MAX_SESSIONS = 4
# Video prepared beyond the last segment the player fetched (players buffer ahead too).
READ_AHEAD = 60
# Recordings closer than this count as one stretch.
JOIN_GAP = timedelta(seconds=10)
MAX_STRETCH = timedelta(hours=3)
PLAYLIST = "index.m3u8"
SEGMENT = "seg{:05d}.m4s"
LOG = "ffmpeg.log"
PIPE = "pipe:0"
FILE_NAME = re.compile(r"^[\w.-]+\.(m3u8|m4s|mp4|ts)$")
_SEGMENT_NUMBER = re.compile(r"^seg(\d+)\.m4s$")
DATA_KEY = "hikvision_bridge_sessions"


class SessionError(Exception):
    """Playback could not start; ``reason`` is a translation key."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _UseRtsp(Exception):
    """No download from the device; ``lasting`` if none will come, ``refused`` if it said no.

    ``far`` if the time lies further into a recorded file than the device reaches in time.
    """

    def __init__(self, lasting: bool, refused: bool = False, far: bool = False) -> None:
        super().__init__(lasting)
        self.lasting = lasting
        self.refused = refused
        self.far = far


class _TooSlow(Exception):
    """A recorded file is sent too slowly to reach the wanted time; ``reach`` is how far it gets.

    ``reach`` is the playing time into the file reached in READ_SECONDS at the pace so far.
    """

    def __init__(self, reach: float) -> None:
        super().__init__(reach)
        self.reach = reach


@dataclass(slots=True)
class Stretch:
    """Contiguous recordings: where playback starts and where it ends."""

    start: datetime
    end: datetime
    kinds: set[str] = field(default_factory=set)


def stretches(found: list[recordings.Recording]) -> list[Stretch]:
    joined: list[Stretch] = []
    for item in sorted(found, key=lambda r: r.start):
        last = joined[-1] if joined else None
        if last and item.start - last.end <= JOIN_GAP and item.end - last.start <= MAX_STRETCH:
            last.end = max(last.end, item.end)
            last.kinds.add(item.kind)
        else:
            joined.append(Stretch(item.start, item.end, {item.kind}))
    return joined


def locate(found: list[recordings.Recording], moment: datetime) -> tuple[datetime, datetime] | None:
    """Start and end of playback for ``moment``; in a gap, the next recording."""
    for stretch in stretches(found):
        if stretch.end <= moment:
            continue
        return max(moment, stretch.start), stretch.end
    return None


def covering(found: list[recordings.Recording], moment: datetime) -> recordings.Recording | None:
    """The recorded file holding ``moment``; in a gap, the next one."""
    ordered = sorted(found, key=lambda r: r.start)
    return next((r for r in ordered if r.start <= moment < r.end), None) or next(
        (r for r in ordered if r.start > moment), None
    )


def playback_uri(
    source: PlaybackSource, start: datetime, end: datetime, wall: timedelta = timedelta(0)
) -> str:
    """The span on the device's wall clock (``wall`` ahead of UTC)."""
    return (
        f"rtsp://device/Streaming/tracks/{source.track}/"
        f"?starttime={recordings.compact(start, wall)}&endtime={recordings.compact(end, wall)}"
    )


def hls_args(url: str, quality: str, codec: str | None, target: Path) -> list[str]:
    """ffmpeg arguments; ``url`` is an RTSP address or PIPE for a program stream on stdin."""
    args = ["-hide_banner", "-loglevel", "error"]
    if url == PIPE:
        args += ["-f", "mpeg"]
    else:
        args += ["-rtsp_transport", "tcp", "-timeout", str(RTSP_TIMEOUT)]
    # The streams are known after the first key frame; the default waits five seconds.
    args += ["-analyzeduration", "2000000", "-i", url, "-map", "0:v:0", "-map", "0:a:0?"]
    if quality == QUALITY_COMPATIBLE:
        args += [
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-crf",
            "26",
            "-pix_fmt",
            "yuv420p",
            "-vf",
            f"scale='min({playback.H264_MAX_WIDTH},iw)':-2",
            "-g",
            str(SEGMENT_SECONDS * 25),
        ]
    else:
        args += ["-c:v", "copy"]
        if codec and ("265" in codec or codec == "hevc"):
            args += ["-tag:v", "hvc1"]
    args += ["-c:a", "aac", "-b:a", "64k"]
    args += [
        "-f",
        "hls",
        "-hls_time",
        str(SEGMENT_SECONDS),
        "-hls_list_size",
        "0",
        "-hls_segment_type",
        "fmp4",
        "-hls_playlist_type",
        "event",
        "-hls_flags",
        "independent_segments",
        "-hls_segment_filename",
        str(target / SEGMENT.replace("{:05d}", "%05d")),
        str(target / PLAYLIST),
    ]
    return args


def played_seconds(playlist: str, fetched: int) -> float:
    """Video length up to and including segment number ``fetched``."""
    total = 0.0
    duration = 0.0
    for line in playlist.splitlines():
        if line.startswith("#EXTINF:"):
            with suppress(ValueError):
                duration = float(line[8:].split(",", 1)[0])
        elif match := _SEGMENT_NUMBER.match(line.strip()):
            if int(match.group(1)) > fetched:
                break
            total += duration
    return total


async def skipped(
    stream: ProgramStream, chunks: AsyncIterator[bytes], skip: float, give_up: bool = False
) -> tuple[bytes, float]:
    """Read ``chunks`` up to ``skip`` seconds and drop what lies before the last key frame.

    Returns the bytes from that key frame on and its playing time. Stops at the latest key
    frame reached after SKIP_SECONDS; without any key frame the file plays from its start.
    With ``give_up``, a read whose pace shows it cannot reach ``skip`` in READ_SECONDS raises
    _TooSlow once PACE_SECONDS have passed, instead of waiting out SKIP_SECONDS.
    """
    began = time.monotonic()
    stream.key_frames = []
    kept = bytearray()
    kept_at = 0  # stream offset of kept[0]
    cut: float | None = None
    async for data in chunks:
        kept += data
        frames, stream.key_frames = stream.key_frames, []
        for at, seconds in frames:
            if cut is None or seconds <= skip:
                drop = min(at - kept_at, len(kept))
                del kept[:drop]
                kept_at += drop
                cut = seconds
        if cut is None:
            if len(kept) > SKIP_SEARCH_BYTES:
                break
        elif stream.seconds >= skip or time.monotonic() - began > SKIP_SECONDS:
            break
        if give_up and stream.seconds > 0:
            elapsed = time.monotonic() - began
            if elapsed >= PACE_SECONDS:
                pace = stream.seconds / elapsed
                if elapsed + (skip - stream.seconds) / pace > READ_SECONDS:
                    stream.key_frames = None
                    raise _TooSlow(pace * READ_SECONDS)
    stream.key_frames = None
    if cut is None:
        _LOGGER.debug("No key frame in the recorded file, playing it from its start")
        return recordings.strip_header(bytes(kept)), 0.0
    _LOGGER.debug(
        "Recorded file plays from %.0f s (wanted %.0f s), reached in %.1f s",
        cut,
        skip,
        time.monotonic() - began,
    )
    return bytes(kept), cut


def left_out(whole: bytes, later: bytes, wanted: float) -> float | None:
    """Seconds of a recorded file the device left out at the start of ``later``.

    ``whole`` is the start of the file itself. None if both start alike: the device sent the
    file from its start. Without time stamps the device is taken to start where it was asked.
    """
    whole, later = recordings.strip_header(whole), recordings.strip_header(later)
    size = min(len(whole), len(later), COMPARE_BYTES)
    if whole[:size] == later[:size]:
        return None
    first, then = recordings.first_pts(whole), recordings.first_pts(later)
    if first is None or then is None:
        return wanted
    seconds = ((then - first) % PTS_WRAP) / 90000
    return seconds if 0 < seconds <= wanted + 60 else wanted


async def _joined(first: bytes, chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    yield first
    async for data in chunks:
        yield data


def _reason(errors: str, scrambled: bool = False) -> str:
    """Why ffmpeg gave up; ``scrambled`` if the video was known to be encrypted.

    Unreadable video is blamed on the verification code only for an encrypted recording:
    over a weak link (Wi-Fi) a plain recording breaks off the same way.
    """
    text = errors.lower()
    if "dimensions not set" in text or "invalid data" in text:
        return "playback_encrypted" if scrambled else "playback_unreadable"
    # A device still releasing the previous playback refuses the next one for a moment.
    if "453" in text or "not enough bandwidth" in text or "500 (internal server error)" in text:
        return "playback_busy"
    if "401" in text or "unauthorized" in text:
        return "playback_auth"
    return "playback_failed"


@dataclass(slots=True)
class Session:
    session_id: str
    source_key: tuple[str, str, str]
    start: datetime
    end: datetime
    quality: str
    folder: Path
    process: asyncio.subprocess.Process | None = None
    pump: asyncio.Task | None = None
    last_access: float = field(default_factory=time.monotonic)
    fetched: int = -1  # highest segment number the player asked for
    scrambled: bool = False  # the download was encrypted

    @property
    def url(self) -> str:
        return f"/api/hikvision_bridge/session/{self.session_id}/{PLAYLIST}"

    def touch(self, name: str | None = None) -> None:
        self.last_access = time.monotonic()
        if name and (match := _SEGMENT_NUMBER.match(name)):
            self.fetched = max(self.fetched, int(match.group(1)))


class SessionManager:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self._unsub: CALLBACK_TYPE | None = None
        self._rtsp_only: WeakSet[IsapiLink] = WeakSet()
        # Devices that refuse the download of a span but hand out recorded files.
        self._by_file: WeakSet[IsapiLink] = WeakSet()
        # Devices that send recorded files only from their start.
        self._whole_files: WeakSet[IsapiLink] = WeakSet()
        # Seconds into a recorded file a device reaches in READ_SECONDS (SKIP_SECONDS once it
        # read on for lack of RTSP), if it fell short.
        self._reach: WeakKeyDictionary[IsapiLink, float] = WeakKeyDictionary()
        # Devices whose RTSP playback failed for a time beyond that reach.
        self._no_far_rtsp: WeakSet[IsapiLink] = WeakSet()
        # Starts still running, by camera; a newer one for the camera cancels them.
        self._starting: dict[tuple[str, str, str], asyncio.Task[Session]] = {}
        # Latest start asked for, by camera.
        self._claims: dict[tuple[str, str, str], int] = {}

    def claim(self, key: tuple[str, str, str]) -> int:
        """Note a start for the camera as it comes in; it drops the starts asked for before.

        Pass the ticket to ``start``: a start whose ticket is no longer the latest is dropped,
        however late it gets there.
        """
        ticket = self._claims.get(key, 0) + 1
        self._claims[key] = ticket
        if (pending := self._starting.get(key)) is not None:
            pending.cancel()
        return ticket

    def get(self, session_id: str, name: str | None = None) -> Session | None:
        session = self.sessions.get(session_id)
        if session is not None:
            session.touch(name)
        return session

    async def start(
        self,
        source: PlaybackSource,
        key: tuple[str, str, str],
        start: datetime,
        end: datetime,
        quality: str,
        codec: str | None,
        replace: str | None = None,
        found: list[recordings.Recording] | None = None,
        ticket: int | None = None,
    ) -> Session:
        """Play from ``start``; ``found`` are the recorded files around it.

        Raises SessionError("playback_replaced") if a newer start for the camera came in;
        ``ticket`` from ``claim`` dates this one to when it was asked for.
        """
        if ticket is not None and ticket != self._claims.get(key):
            raise SessionError("playback_replaced")
        if (pending := self._starting.get(key)) is not None:
            pending.cancel()
        task = asyncio.ensure_future(
            self._start(source, key, start, end, quality, codec, replace, found)
        )
        self._starting[key] = task
        try:
            return await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if task.cancelled() and (current is None or not current.cancelling()):
                raise SessionError("playback_replaced") from None
            raise
        finally:
            if self._starting.get(key) is task:
                del self._starting[key]

    async def _start(
        self,
        source: PlaybackSource,
        key: tuple[str, str, str],
        start: datetime,
        end: datetime,
        quality: str,
        codec: str | None,
        replace: str | None,
        found: list[recordings.Recording] | None,
    ) -> Session:
        async with self._lock:
            if replace:
                await self._stop(replace)
            # A camera plays one recording at a time; a new view of it ends the old one.
            for other in [s for s in self.sessions.values() if s.source_key == key]:
                await self._stop(other.session_id)
            while len(self.sessions) >= MAX_SESSIONS:
                oldest = min(self.sessions.values(), key=lambda s: s.last_access)
                await self._stop(oldest.session_id)
            uri = playback_uri(source, start, end, await recordings.wall_offset(source.link))
            folder = Path(
                await self.hass.async_add_executor_job(tempfile.mkdtemp, "", "hikvision_")
            )
            try:
                far = False
                if source.link not in self._rtsp_only:
                    try:
                        session = await self._download(
                            source, key, start, end, uri, quality, folder, found or []
                        )
                    except _UseRtsp as err:
                        if err.lasting:
                            self._rtsp_only.add(source.link)
                        far = err.far
                    else:
                        self.sessions[session.session_id] = session
                        self._watch()
                        return session
                try:
                    session = await self._from_rtsp(
                        source, key, start, end, uri, quality, codec, folder
                    )
                except SessionError as err:
                    if not far:
                        raise
                    # Read the recorded file up to the time after all. A camera still busy
                    # with the previous playback says nothing about its RTSP playback.
                    if err.reason != "playback_busy":
                        self._no_far_rtsp.add(source.link)
                    try:
                        session = await self._download(
                            source, key, start, end, uri, quality, folder, found or [], far=True
                        )
                    except _UseRtsp:
                        raise err from None
            except BaseException:
                await self.hass.async_add_executor_job(shutil.rmtree, folder, True)
                raise
            self.sessions[session.session_id] = session
            self._watch()
            return session

    # ------------------------------------------------------------- download

    async def _download(
        self,
        source: PlaybackSource,
        key: tuple[str, str, str],
        start: datetime,
        end: datetime,
        uri: str,
        quality: str,
        folder: Path,
        found: list[recordings.Recording],
        far: bool = False,
    ) -> Session:
        """A session from the download of the span, else of the recorded file at ``start``.

        ``far`` reads the file up to ``start`` even beyond the device's reach.
        """
        recording = covering(found, start)
        if source.link not in self._by_file:
            try:
                return await self._from_download(
                    source, key, start, end, uri, quality, folder, "the time span"
                )
            except _UseRtsp as err:
                if not err.refused or recording is None:
                    raise
                self._by_file.add(source.link)
        if recording is None:
            raise _UseRtsp(False)
        start = max(start, recording.start)
        end = min(end, recording.end)
        skip = (start - recording.start).total_seconds()
        # Search results of the same file: devices may list one file in several parts.
        parts = [r for r in found if recording.name and r.name == recording.name]
        _LOGGER.debug(
            "Recorded file %s to %s, %.0f s in; %d search results of this file from %s",
            recording.start.isoformat(),
            recording.end.isoformat(),
            skip,
            len(parts),
            min(r.start for r in parts).isoformat() if parts else "-",
        )
        if (
            not far
            and source.link in self._whole_files
            and source.link not in self._no_far_rtsp
            and skip > self._reach.get(source.link, skip)
        ):
            _LOGGER.debug(
                "Recorded file reaches %.0f s in time, not %.0f s: playing over RTSP",
                self._reach[source.link],
                skip,
            )
            raise _UseRtsp(False, far=True)
        # A read that is too slow to reach the time gives up for RTSP, unless RTSP was tried
        # already for this device (then reading on is the only way left).
        give_up = not far and source.link not in self._no_far_rtsp
        if skip >= LATER_START_SECONDS and source.link not in self._whole_files:
            wall = await recordings.wall_offset(source.link)
            if later := recordings.starting_at(recording.playback_uri, start, wall):
                try:
                    return await self._from_download(
                        source,
                        key,
                        start,
                        end,
                        later,
                        quality,
                        folder,
                        "the recorded file from the time",
                        skip,
                        recording.playback_uri,
                        give_up,
                    )
                except _UseRtsp as err:
                    if err.far:
                        raise
                    if err.refused:
                        self._whole_files.add(source.link)
        return await self._from_download(
            source,
            key,
            start,
            end,
            recording.playback_uri,
            quality,
            folder,
            "the recorded file",
            skip,
            give_up=give_up,
        )

    async def _from_download(
        self,
        source: PlaybackSource,
        key: tuple[str, str, str],
        start: datetime,
        end: datetime,
        uri: str,
        quality: str,
        folder: Path,
        what: str,
        skip: float = 0.0,
        whole: str | None = None,
        give_up: bool = False,
    ) -> Session:
        wait = FIRST_SEGMENT_TIMEOUT + (SKIP_SECONDS if skip > 0 else 0)
        for attempt in range(2):
            session = Session(secrets.token_urlsafe(16), key, start, end, quality, folder)
            ready: asyncio.Future[None] = self.hass.loop.create_future()
            session.pump = self.hass.async_create_background_task(
                self._pump(session, source, uri, ready, skip, whole, give_up),
                "hikvision_bridge playback download",
            )
            try:
                await asyncio.wait_for(asyncio.shield(ready), wait)
            except BaseException as err:
                # Ends ffmpeg too, should the download have started it.
                await self._end(session)
                if isinstance(err, _TooSlow):
                    raise _UseRtsp(False, far=True) from err
                if isinstance(err, RecordingEncrypted):
                    raise SessionError("playback_encrypted") from err
                if isinstance(err, TimeoutError):
                    _LOGGER.debug("Playback of %s did not start: no data in %s s", what, wait)
                    raise SessionError("playback_failed") from err
                if isinstance(err, FileNotFoundError) or not isinstance(err, Exception):
                    raise
                # A device still releasing the previous playback answers with an error 500.
                busy = isinstance(err, DeviceRejected) and err.status == 500
                if busy and not attempt:
                    _LOGGER.debug("No download of %s yet: %s", what, detail(err))
                    await asyncio.sleep(BUSY_PAUSE)
                    continue
                _LOGGER.debug("No download of %s: %s", what, detail(err))
                # Only a refusal is lasting; a dropped connection or busy device may work
                # next time.
                raise _UseRtsp(
                    isinstance(err, (DeviceRejected, Unsupported, NoRecording)) and not busy,
                    isinstance(err, DeviceRejected),
                ) from err
            break
        reason = await self._first_segment(session, what)
        if reason is not None:
            await self._end(session)
            raise SessionError(reason)
        _LOGGER.debug("Playing %s", what)
        return session

    async def _pump(
        self,
        session: Session,
        source: PlaybackSource,
        uri: str,
        ready: asyncio.Future[None],
        skip: float = 0.0,
        whole: str | None = None,
        give_up: bool = False,
    ) -> None:
        """Download, decrypt and hand the span to ffmpeg, keeping READ_AHEAD in front.

        With ``skip`` the video starts at the last key frame before that many seconds, and
        the session start moves back to that frame. ``uri`` may ask the device to leave
        out the part before; ``whole`` is then the file from its start, to compare with.
        ``give_up`` allows the read to stop early, as _TooSlow, when its pace is too slow.
        """
        stdin = None
        try:
            reference = await self._head(source, whole) if whole else b""
            async with recordings.open_download(source.link, uri) as response:
                head = await playback.read_head(response)
                device = 0.0  # seconds the device left out
                if whole:
                    device = left_out(reference, head, skip) or 0.0
                    if device:
                        _LOGGER.debug("Recorder starts the file at %.0f s", device)
                    else:
                        _LOGGER.debug("Recorder sends recorded files only from their start")
                        self._whole_files.add(source.link)
                info, stream = await playback.prepare(source, head)
                session.scrambled = info.scrambled
                chunks = playback.passed(stream, head, response)
                rest = skip - device
                cut = 0.0
                if rest > 0:
                    began = time.monotonic()
                    try:
                        first, cut = await skipped(stream, chunks, rest, give_up)
                    except _TooSlow as slow:
                        self._reach[source.link] = device + slow.reach
                        _LOGGER.debug(
                            "Recorded file is sent too slowly to reach %.0f s in %s s: "
                            "playing it over RTSP if it can",
                            rest,
                            READ_SECONDS,
                        )
                        raise
                    if cut < rest - 1 and time.monotonic() - began >= SKIP_SECONDS:
                        # A read for lack of RTSP gets further than READ_SECONDS would.
                        reached = device + stream.seconds
                        self._reach[source.link] = min(
                            self._reach.get(source.link, reached), reached
                        )
                    chunks = _joined(recordings.file_header(head) + first, chunks)
                else:
                    chunks = playback.headed(chunks)
                session.start -= timedelta(seconds=rest - cut)
                session.process = await self._spawn(
                    session, hls_args(PIPE, session.quality, info.codec, session.folder), True
                )
                stdin = session.process.stdin
                assert stdin is not None
                ready.set_result(None)
                played = (-1, 0.0)
                async for data in chunks:
                    stdin.write(data)
                    await stdin.drain()
                    while stream.seconds - cut > played[1] + READ_AHEAD:
                        if session.fetched != played[0]:
                            text = await self.hass.async_add_executor_job(
                                _read, session.folder / PLAYLIST
                            )
                            played = (session.fetched, played_seconds(text or "", session.fetched))
                            continue
                        await asyncio.sleep(1)
        except asyncio.CancelledError:
            if not ready.done():
                ready.cancel()
            raise
        except Exception as err:  # noqa: BLE001 - handed to the waiting start
            if not ready.done():
                ready.set_exception(err)
            elif not isinstance(err, (BrokenPipeError, ConnectionResetError)):
                # The playlist ends here; the player starts over from that point.
                _LOGGER.debug("Playback download ended early: %s", type(err).__name__)
        finally:
            if stdin is not None:
                with suppress(BrokenPipeError, ConnectionResetError, OSError):
                    stdin.close()

    async def _head(self, source: PlaybackSource, uri: str) -> bytes:
        async with recordings.open_download(source.link, uri) as response:
            return await playback.read_head(response)

    # ------------------------------------------------------------- RTSP

    async def _from_rtsp(
        self,
        source: PlaybackSource,
        key: tuple[str, str, str],
        start: datetime,
        end: datetime,
        uri: str,
        quality: str,
        codec: str | None,
        folder: Path,
    ) -> Session:
        url = playback.rtsp_url(source, uri)
        reason = "playback_failed"
        for attempt in range(2):
            session = Session(secrets.token_urlsafe(16), key, start, end, quality, folder)
            try:
                await self._spawn(session, hls_args(url, quality, codec, folder), False)
            except asyncio.CancelledError:
                # A newer start: the camera serves one playback, this one must not linger.
                await self._end(session)
                raise
            reason = await self._first_segment(session, "RTSP")
            if reason is None:
                _LOGGER.debug("Playing over RTSP")
                return session
            if reason not in ("playback_busy", "playback_unreadable") or attempt:
                break
            # The device releases the previous playback late; a weak link may just drop data.
            await asyncio.sleep(1.5)
        raise SessionError(reason)

    # ------------------------------------------------------------- common

    async def _spawn(
        self, session: Session, args: list[str], piped: bool
    ) -> asyncio.subprocess.Process:
        """Start ffmpeg as ``session.process``, set before anything can cancel the caller."""
        # Errors go to a file: a pipe nobody reads would stall a long session.
        log = await self.hass.async_add_executor_job((session.folder / LOG).open, "wb")
        try:
            session.process = await asyncio.create_subprocess_exec(
                playback.ffmpeg_binary(self.hass),
                *args,
                stdin=asyncio.subprocess.PIPE if piped else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=log,
            )
        finally:
            await asyncio.shield(self.hass.async_add_executor_job(log.close))
        return session.process

    async def _first_segment(self, session: Session, what: str) -> str | None:
        """None once the first segment is written, else why ffmpeg gave up."""
        assert session.process is not None
        playlist = session.folder / PLAYLIST
        deadline = time.monotonic() + FIRST_SEGMENT_TIMEOUT
        try:
            while time.monotonic() < deadline:
                text = await self.hass.async_add_executor_job(_read, playlist)
                if text and "#EXTINF" in text:
                    return None
                if session.process.returncode is not None:
                    break
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            await self._end(session)
            raise
        running = session.process.returncode is None
        await self._end(session)
        errors = await self.hass.async_add_executor_job(_read, session.folder / LOG) or ""
        _LOGGER.debug(
            "Playback of %s did not start: %s%s",
            what,
            f"no video in {FIRST_SEGMENT_TIMEOUT} s"
            if running
            else f"ffmpeg ended with {session.process.returncode}",
            f"; ffmpeg: {scrub_text(errors[-400:])}" if errors else "",
        )
        return _reason(errors, session.scrambled) if errors else "playback_failed"

    async def _end(self, session: Session) -> None:
        await _cancel(session.pump)
        if session.process is not None:
            await _end(session.process)

    async def stop(self, session_id: str) -> None:
        async with self._lock:
            await self._stop(session_id)

    async def _stop(self, session_id: str) -> None:
        session = self.sessions.pop(session_id, None)
        if session is None:
            return
        await self._end(session)
        await self.hass.async_add_executor_job(shutil.rmtree, session.folder, True)

    async def stop_all(self) -> None:
        async with self._lock:
            for session_id in list(self.sessions):
                await self._stop(session_id)
        if self._unsub:
            self._unsub()
            self._unsub = None

    def _watch(self) -> None:
        if self._unsub is None:
            self._unsub = async_track_time_interval(self.hass, self._reap, timedelta(seconds=10))

    @callback
    def _reap(self, _now: datetime) -> None:
        idle = [
            s.session_id
            for s in self.sessions.values()
            if time.monotonic() - s.last_access > IDLE_SECONDS
        ]
        for session_id in idle:
            self.hass.async_create_task(self.stop(session_id))


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


async def _cancel(task: asyncio.Task | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _end(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), 5)
    except TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()


def manager(hass: HomeAssistant) -> SessionManager:
    if DATA_KEY not in hass.data:
        hass.data[DATA_KEY] = SessionManager(hass)
    return hass.data[DATA_KEY]
