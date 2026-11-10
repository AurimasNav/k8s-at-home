"""Recorded files as seekable video on demand (HLS), made by playback sessions.

The playlist lists the whole file in PART_SECONDS parts, so players know its length and seek
in it themselves. A part comes from the playback session of the file if that reaches it soon,
else from a session started at the part (a seek), as the panel does. Session video carries the
time stamps of the file and is cut at every key frame; a part is made of the pieces that start
in it, so parts of different sessions line up and every part starts with a key frame.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import datetime, time as time_of_day, timedelta
import math
import secrets
import struct
import time
from urllib.parse import parse_qsl, urlsplit

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..model.features import recordings
from ..wire.faults import BridgeFault, detail
from ..wire.scrub import scrubbed_logger
from . import sessions
from .playback import PlaybackSource
from .sessions import Session, SessionError

# Above the key frame interval of recorders (often 1 to 2 s); a part without a key frame of
# its own repeats the piece before it.
PART_SECONDS = 4
INIT = "init.mp4"
MAX_VODS = 16
SEARCH_BACK_DAYS = 7
# Longest wait for the next piece while its session still runs.
WAIT_SECONDS = sessions.FIRST_SEGMENT_TIMEOUT
POLL_SECONDS = 0.25
URL = "/api/hikvision_bridge/vod/{token}/{name}"
DATA_KEY = "hikvision_bridge_vod"

_LOGGER = scrubbed_logger(__name__)


class VodsFull(Exception):
    """MAX_VODS files are played at the moment; none is idle to make room."""


@dataclass(slots=True)
class Vod:
    """One recorded file played on demand; ``token`` in its URLs is the access token."""

    token: str
    key: tuple[str, str, str]  # entry, channel, recording source
    owner: str
    recording: recordings.Recording  # the file as listed: its part in the day searched
    file_start: datetime  # where the file really starts
    # Last use; a file paused for long keeps playing on, and only idle ones make room.
    used: float = field(default_factory=time.monotonic)
    session_id: str | None = None
    first: int = 0  # part the session started at
    starting: asyncio.Task[None] | None = None
    starting_at: int = 0
    restarts: int = 0
    init: bytes | None = None

    @property
    def parts(self) -> int:
        return max(1, math.ceil(self.recording.duration.total_seconds() / PART_SECONDS))

    def playlist(self) -> str:
        total = self.recording.duration.total_seconds()
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:7",
            f"#EXT-X-TARGETDURATION:{PART_SECONDS}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            "#EXT-X-INDEPENDENT-SEGMENTS",
            f'#EXT-X-MAP:URI="{URL.format(token=self.token, name=INIT)}"',
        ]
        for number in range(self.parts):
            length = min(PART_SECONDS, total - number * PART_SECONDS)
            lines += [f"#EXTINF:{length:.3f},", URL.format(token=self.token, name=f"{number}.m4s")]
        lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------- media

    async def init_section(
        self, hass: HomeAssistant, source: PlaybackSource, codec: str | None
    ) -> bytes | None:
        """The init section; the same for all sessions of the file, so it is kept."""
        if self.init is None:
            session = await self._session(hass, source, codec, self.first)
            if session is not None:
                self.init = await hass.async_add_executor_job(_read_bytes, session, INIT)
        return self.init

    async def part(
        self, hass: HomeAssistant, source: PlaybackSource, codec: str | None, number: int
    ) -> AsyncIterator[bytes]:
        """The pieces of part ``number`` as their session writes them.

        Raises SessionError if the part ends short (its session stopped or stalled): a player
        must not take it for the whole part.
        """
        session = await self._session(hass, source, codec, number)
        if session is None:
            return
        manager = sessions.manager(hass)
        low, high = number * PART_SECONDS, (number + 1) * PART_SECONDS
        # The part a session started at also takes the video before it, from the key frame.
        if number == self.first:
            low -= PART_SECONDS
        sent: set[int] = set()
        last = time.monotonic()
        pieces: list[tuple[int, float, float]] = []
        done = False
        while self.session_id == session.session_id and session.session_id in manager.sessions:
            ended = session.process is None or session.process.returncode is not None
            pieces, done = await hass.async_add_executor_job(_pieces, session)
            for piece, start, _ in pieces:
                if piece not in sent and low <= start < high:
                    sent.add(piece)
                    last = time.monotonic()
                    manager.get(session.session_id, sessions.SEGMENT.format(piece))
                    if data := await hass.async_add_executor_job(_read_bytes, session, piece):
                        yield _media(data)
            if done or ended or (pieces and pieces[-1][2] >= high):
                break
            if time.monotonic() - last > WAIT_SECONDS:
                break
            manager.get(session.session_id)
            await asyncio.sleep(POLL_SECONDS)
        reached = done or bool(pieces and pieces[-1][2] >= high)
        if not sent:
            # A key frame interval longer than a part: the piece over its start stands in.
            for piece, start, end in pieces:
                if start < low < end and (
                    data := await hass.async_add_executor_job(_read_bytes, session, piece)
                ):
                    yield _media(data)
        if not reached:
            raise SessionError("playback_failed")

    async def _session(
        self, hass: HomeAssistant, source: PlaybackSource, codec: str | None, number: int
    ) -> Session | None:
        """The session that has or soon reaches part ``number``, else one started there.

        None once a later request started elsewhere: the player has moved on (seeked).
        """
        manager = sessions.manager(hass)
        seen = self.restarts
        while True:
            session = manager.get(self.session_id) if self.session_id else None
            if (
                session is not None
                and number >= self.first
                and await self._reaches(hass, session, number)
            ):
                return session
            task = self.starting
            # Starts at nearly the same part come in together (init section and first part).
            if task is not None and not task.done() and 0 <= number - self.starting_at <= 2:
                with suppress(SessionError):
                    await asyncio.shield(task)
                continue
            if seen != self.restarts:
                return None
            self.restarts += 1
            self.starting_at = number
            self.starting = task = hass.async_create_task(
                self._start(hass, source, codec, number), "hikvision_bridge vod seek"
            )
            await asyncio.shield(task)
            return manager.get(self.session_id) if self.session_id else None

    async def _start(
        self, hass: HomeAssistant, source: PlaybackSource, codec: str | None, number: int
    ) -> None:
        manager = sessions.manager(hass)
        ticket = manager.claim(self.key)
        session = await manager.start(
            source,
            self.key,
            self.recording.start + timedelta(seconds=number * PART_SECONDS),
            self.recording.end,
            sessions.QUALITY_ORIGINAL,
            codec,
            found=[replace(self.recording, start=self.file_start)],
            ticket=ticket,
            owner=self.owner,
            origin=self.recording.start,
        )
        self.session_id, self.first = session.session_id, number

    async def _reaches(self, hass: HomeAssistant, session: Session, number: int) -> bool:
        """Whether ``session`` has part ``number`` or gets there soon."""
        pieces, done = await hass.async_add_executor_job(_pieces, session)
        end = pieces[-1][2] if pieces else session.offset or 0.0
        if number * PART_SECONDS < end:
            return True
        if done or (session.process is not None and session.process.returncode is not None):
            # Nothing comes after the end of the file; anywhere else a new session goes on.
            return end >= self.recording.duration.total_seconds() - PART_SECONDS
        # A download runs far ahead of the player; RTSP plays in real time.
        ahead = sessions.READ_AHEAD if session.pump is not None else PART_SECONDS
        return number * PART_SECONDS <= end + ahead


def _pieces(session: Session) -> tuple[list[tuple[int, float, float]], bool]:
    """Pieces a session wrote (number, start and end in the file) and whether that is all."""
    text = sessions._read(session.folder / sessions.PLAYLIST) or ""
    found: list[tuple[int, float, float]] = []
    at = session.offset or 0.0
    duration = 0.0
    for line in text.splitlines():
        if line.startswith("#EXTINF:"):
            with suppress(ValueError):
                duration = float(line[8:].split(",", 1)[0])
        elif match := sessions._SEGMENT_NUMBER.match(line.strip()):
            found.append((int(match.group(1)), at, at + duration))
            at += duration
    return found, "#EXT-X-ENDLIST" in text


def _read_bytes(session: Session, piece: int | str) -> bytes | None:
    """A piece or the init section; None if the session ended meanwhile."""
    name = piece if isinstance(piece, str) else sessions.SEGMENT.format(piece)
    try:
        return (session.folder / name).read_bytes()
    except OSError:
        return None


def _media(data: bytes) -> bytes:
    """The fragments (moof and mdat) of a piece, without its styp and sidx boxes.

    Those belong at the start of a segment; a part holds several pieces.
    """
    kept = bytearray()
    position = 0
    while position + 8 <= len(data):
        size, kind = struct.unpack_from(">I4s", data, position)
        if size < 8:
            break
        if kind not in (b"styp", b"sidx"):
            kept += data[position : position + size]
        position += size
    return bytes(kept)


async def create(
    hass: HomeAssistant,
    key: tuple[str, str, str],
    source: PlaybackSource,
    playback_uri: str,
    owner: str,
) -> Vod | None:
    """The video on demand of a recorded file for ``owner``; None without its times.

    Raises VodsFull if no room can be made, BridgeFault if the device does not answer.
    """
    query = dict(parse_qsl(urlsplit(playback_uri).query))
    wall = await recordings.wall_offset(source.link)
    start = recordings.parse_time(query.get("starttime"), wall)
    end = recordings.parse_time(query.get("endtime"), wall)
    if start is None or end is None:
        return None
    # A file still recording is listed up to the end of the search.
    end = min(end, dt_util.utcnow())
    if end <= start:
        return None
    vods: dict[str, Vod] = hass.data.setdefault(DATA_KEY, {})
    now = time.monotonic()
    for token, vod in list(vods.items()):
        if now - vod.used > sessions.MAX_LIFETIME:
            del vods[token]
        elif vod.key == key and vod.owner == owner and vod.recording.playback_uri == playback_uri:
            vod.used = now
            return vod
    if len(vods) >= MAX_VODS:
        # Make room from the owner's own files first; one played lately by anyone stays.
        idle = [v for v in vods.values() if now - v.used > sessions.IDLE_SECONDS]
        if not idle:
            raise VodsFull
        oldest = min(idle, key=lambda v: (v.owner != owner, v.used))
        del vods[oldest.token]
    recording = recordings.Recording(source.track, start, end, "", playback_uri, query.get("name"))
    vod = Vod(
        secrets.token_urlsafe(16), key, owner, recording, await _file_start(source, recording)
    )
    vods[vod.token] = vod
    return vod


async def _file_start(source: PlaybackSource, recording: recordings.Recording) -> datetime:
    """Where a file listed from midnight on really starts.

    The media browser searches a day; devices list a file running into it from midnight on,
    and send it from its real start. Searched back a day at a time, up to SEARCH_BACK_DAYS.
    """
    start = recording.start
    if not recording.name or dt_util.as_local(start).time() != time_of_day(0):
        return start
    for _ in range(SEARCH_BACK_DAYS):
        before = start - timedelta(days=1)
        try:
            found = await recordings.search(source.link, source.track, before, start)
        except BridgeFault as err:
            # Played from where it is listed then; a download skips to the wrong place at worst.
            _LOGGER.debug("Real start of a recorded file not found: %s", detail(err))
            break
        start = min((r.start for r in found if r.name == recording.name), default=start)
        if start > before:
            break
    return start


def get(hass: HomeAssistant, token: str) -> Vod | None:
    vod = hass.data.get(DATA_KEY, {}).get(token)
    if vod is None:
        return None
    now = time.monotonic()
    if now - vod.used > sessions.MAX_LIFETIME:
        del hass.data[DATA_KEY][token]
        return None
    vod.used = now
    return vod


def drop(hass: HomeAssistant, token: str) -> None:
    hass.data.get(DATA_KEY, {}).pop(token, None)


def drop_entry(hass: HomeAssistant, entry_id: str) -> None:
    """Forget the files of a config entry (it is unloading)."""
    vods: dict[str, Vod] = hass.data.get(DATA_KEY, {})
    for token in [t for t, v in vods.items() if v.key[0] == entry_id]:
        del vods[token]
