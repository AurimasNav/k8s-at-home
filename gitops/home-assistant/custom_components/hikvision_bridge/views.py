"""HTTP views streaming recordings and their thumbnails from the device to the browser."""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections import OrderedDict
import re

from aiohttp import web
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .runtime import playback as playback_module, sessions
from .runtime.bridge import DeviceBridge
from .runtime.playback import (
    EXTENSION,
    FORMAT_ORIGINAL,
    FORMATS,
    MIME,
    NoRecording,
    PlaybackSource,
    RecordingEncrypted,
    ffmpeg_args,
    open_recording,
    pick_source,
    run_ffmpeg,
    thumbnail_args,
)
from .wire.faults import BridgeFault
from .wire.scrub import scrubbed_logger

_LOGGER = scrubbed_logger(__name__)

RECORDING_URL = "/api/hikvision_bridge/recording/{entry_id}/{channel_id}/{source}/{fmt}/{token}"
THUMBNAIL_URL = "/api/hikvision_bridge/thumbnail/{entry_id}/{channel_id}/{source}/{token}"
_PLAYBACK_URI = re.compile(r"^rtsp://[^/\s]+/Streaming/tracks/\d+/?\?[^\s<>]*$", re.IGNORECASE)
THUMBNAIL_CACHE = 256
# Each thumbnail opens a download; recorders serve only a few at a time.
THUMBNAIL_PARALLEL = 2


def encode_token(playback_uri: str) -> str:
    return base64.urlsafe_b64encode(playback_uri.encode()).decode().rstrip("=")


def decode_token(token: str) -> str | None:
    """The playback URI of a token, or None unless it is one of a device's recordings."""
    try:
        uri = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    return uri if _PLAYBACK_URI.match(uri) else None


def recording_url(
    entry_id: str,
    channel_id: str,
    source: str,
    fmt: str,
    playback_uri: str,
) -> str:
    return RECORDING_URL.format(
        entry_id=entry_id,
        channel_id=channel_id,
        source=source,
        fmt=fmt,
        token=encode_token(playback_uri),
    )


def thumbnail_url(entry_id: str, channel_id: str, source: str, playback_uri: str) -> str:
    return THUMBNAIL_URL.format(
        entry_id=entry_id,
        channel_id=channel_id,
        source=source,
        token=encode_token(playback_uri),
    )


def _find_source(
    hass: HomeAssistant, entry_id: str, channel_id: str, source: str
) -> tuple[DeviceBridge, PlaybackSource] | None:
    entry = hass.config_entries.async_get_entry(entry_id)
    bridge = getattr(entry, "runtime_data", None) if entry and entry.domain == DOMAIN else None
    if not isinstance(bridge, DeviceBridge) or channel_id not in bridge.profile.channels:
        return None
    found = pick_source(bridge, channel_id, source)
    return (bridge, found) if found else None


def _seconds(value: str | None) -> float | None:
    try:
        number = float(value) if value else None
    except ValueError:
        return None
    return number if number and number > 0 else None


class RecordingView(HomeAssistantView):
    """One recorded file, converted while it downloads."""

    url = RECORDING_URL
    name = "api:hikvision_bridge:recording"
    requires_auth = True

    async def get(
        self,
        request: web.Request,
        entry_id: str,
        channel_id: str,
        source: str,
        fmt: str,
        token: str,
    ) -> web.StreamResponse:
        hass = request.app[KEY_HASS]
        uri = decode_token(token)
        found = _find_source(hass, entry_id, channel_id, source)
        if uri is None or found is None or fmt not in FORMATS:
            return web.Response(status=404)
        _, playback = found
        offset = _seconds(request.query.get("offset"))
        duration = _seconds(request.query.get("duration"))
        response = web.StreamResponse(headers={"Content-Type": MIME[fmt]})
        if request.query.get("download"):
            name = f"recording_{channel_id}.{EXTENSION[fmt]}"
            response.headers["Content-Disposition"] = f'attachment; filename="{name}"'

        async def send(data: bytes) -> None:
            # Started with the first data, so that earlier failures still get a status.
            if not response.prepared:
                await response.prepare(request)
            await response.write(data)

        try:
            async with open_recording(playback, uri) as (head, chunks):
                if fmt == FORMAT_ORIGINAL:
                    async for chunk in chunks:
                        await send(chunk)
                else:
                    args = ffmpeg_args(fmt, head.codec, offset=offset, duration=duration)
                    await run_ffmpeg(playback_module.ffmpeg_binary(hass), args, chunks, send)
            if not response.prepared:
                await response.prepare(request)
            await response.write_eof()
        except RecordingEncrypted:
            return web.Response(status=409, text="Encrypted recording; no verification code fits.")
        except NoRecording:
            if not response.prepared:
                return web.Response(status=502, text="No video in this recording.")
        except BridgeFault as err:
            _LOGGER.debug("Recording download failed: %s", type(err).__name__)
            if not response.prepared:
                return web.Response(status=502, text=type(err).__name__)
        except FileNotFoundError:
            return web.Response(status=503, text="ffmpeg is not available.")
        except ConnectionResetError:
            # The player went away (closed, or seeking with a new request).
            pass
        return response


class ThumbnailView(HomeAssistantView):
    """First picture of a recorded file."""

    url = THUMBNAIL_URL
    name = "api:hikvision_bridge:thumbnail"
    requires_auth = True

    def __init__(self) -> None:
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._gate = asyncio.Semaphore(THUMBNAIL_PARALLEL)

    async def get(
        self, request: web.Request, entry_id: str, channel_id: str, source: str, token: str
    ) -> web.Response:
        hass = request.app[KEY_HASS]
        key = f"{entry_id}/{channel_id}/{source}/{token}"
        if (cached := self._cache.get(key)) is not None:
            self._cache.move_to_end(key)
            return web.Response(body=cached, content_type="image/jpeg")
        uri = decode_token(token)
        found = _find_source(hass, entry_id, channel_id, source)
        if uri is None or found is None:
            return web.Response(status=404)
        _, playback = found
        try:
            async with self._gate, open_recording(playback, uri) as (_, chunks):
                image = await run_ffmpeg(
                    playback_module.ffmpeg_binary(hass), thumbnail_args(), chunks
                )
        except (RecordingEncrypted, NoRecording, BridgeFault, FileNotFoundError) as err:
            _LOGGER.debug("No thumbnail: %s", type(err).__name__)
            return web.Response(status=404)
        if not image:
            return web.Response(status=404)
        self._cache[key] = image
        while len(self._cache) > THUMBNAIL_CACHE:
            self._cache.popitem(last=False)
        return web.Response(
            body=image, content_type="image/jpeg", headers={"Cache-Control": "max-age=86400"}
        )


class SessionView(HomeAssistantView):
    """Playlist and segments of a playback session.

    Players fetch these without the login header; the random session id is the access
    token, like Home Assistant's own HLS streams.
    """

    url = "/api/hikvision_bridge/session/{session_id}/{name}"
    name = "api:hikvision_bridge:session"
    requires_auth = False

    async def get(self, request: web.Request, session_id: str, name: str) -> web.StreamResponse:
        if not sessions.FILE_NAME.match(name):
            return web.Response(status=404)
        session = sessions.manager(request.app[KEY_HASS]).get(session_id, name)
        if session is None:
            return web.Response(status=404)
        path = session.folder / name
        if not await request.app[KEY_HASS].async_add_executor_job(path.is_file):
            return web.Response(status=404)
        content_type = {
            "m3u8": "application/vnd.apple.mpegurl",
            "m4s": "video/iso.segment",
            "mp4": "video/mp4",
            "ts": "video/mp2t",
        }[name.rsplit(".", 1)[1]]
        return web.FileResponse(
            path, headers={"Content-Type": content_type, "Cache-Control": "no-cache"}
        )


def async_register_views(hass: HomeAssistant) -> None:
    hass.http.register_view(RecordingView())
    hass.http.register_view(ThumbnailView())
    hass.http.register_view(SessionView())
