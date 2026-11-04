"""Recordings of recorders and memory cards in the media browser.

Identifiers: ``entry/channel/source/day/kind`` for folders (later parts optional) and
``entry/channel/source/clip/token`` for one recorded file.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from homeassistant.components.media_player import BrowseError, MediaClass
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ENTRY_KIND,
    DOMAIN,
    KIND_CLOUD,
    OPT_PLAYBACK,
    OPT_RTSP_TRANSPORT,
    PLAYBACK_STREAM,
    RTSP_TCP,
)
from .model.features import recordings
from .runtime import identity
from .runtime.bridge import DeviceBridge
from .runtime.playback import (
    FORMAT_MP4,
    MIME,
    PlaybackSource,
    pick_source,
    rtsp_url,
    sources,
)
from .views import decode_token, encode_token, recording_url, thumbnail_url
from .wire.faults import BridgeFault
from .wire.scrub import scrubbed_logger

_LOGGER = scrubbed_logger(__name__)

DAYS_WITHOUT_CALENDAR = 14
MAX_DAYS = 62
ALL = "all"
# Thumbnails cost one download each; continuous recording days have too many files.
MAX_THUMBNAILS = 60


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    return BridgeMediaSource(hass)


def _folder(identifier: str, title: str, **kwargs) -> BrowseMediaSource:
    return BrowseMediaSource(
        domain=DOMAIN,
        identifier=identifier,
        media_class=MediaClass.DIRECTORY,
        media_content_type="",
        title=title,
        can_play=False,
        can_expand=True,
        children_media_class=kwargs.pop("children_media_class", MediaClass.DIRECTORY),
        **kwargs,
    )


class BridgeMediaSource(MediaSource):
    name = "Hikvision Bridge"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    # ------------------------------------------------------------- helpers

    def _bridges(self) -> list[DeviceBridge]:
        found = []
        for entry in self.hass.config_entries.async_loaded_entries(DOMAIN):
            if entry.data.get(CONF_ENTRY_KIND) == KIND_CLOUD:
                continue
            bridge = getattr(entry, "runtime_data", None)
            if isinstance(bridge, DeviceBridge):
                found.append(bridge)
        return found

    def _bridge(self, entry_id: str) -> DeviceBridge:
        for bridge in self._bridges():
            if bridge.entry.entry_id == entry_id:
                return bridge
        raise BrowseError("Device not loaded")

    async def _labels(self) -> dict[str, str]:
        texts = await async_get_translations(
            self.hass, self.hass.config.language, "selector", [DOMAIN]
        )
        prefix = f"component.{DOMAIN}.selector."
        return {
            key.removeprefix(prefix): value
            for key, value in texts.items()
            if key.startswith(prefix)
        }

    def _camera_thumbnail(self, bridge: DeviceBridge, channel_id: str) -> str | None:
        uid = identity.entity_uid(bridge.profile, channel_id, "stream_1")
        entity_id = er.async_get(self.hass).async_get_entity_id(Platform.CAMERA, DOMAIN, uid)
        return f"/api/camera_proxy/{entity_id}" if entity_id else None

    @staticmethod
    def _channels(bridge: DeviceBridge) -> list[str]:
        return [cid for cid in bridge.profile.channels if sources(bridge, cid)]

    # -------------------------------------------------------------- browse

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        parts = item.identifier.split("/") if item.identifier else []
        labels = await self._labels()
        try:
            if not parts:
                return self._root()
            bridge = self._bridge(parts[0])
            if len(parts) == 1:
                return self._device(bridge)
            channel_id = parts[1]
            if channel_id not in bridge.profile.channels:
                raise BrowseError("Unknown channel")
            if len(parts) == 2:
                return await self._channel(bridge, channel_id, labels)
            source = pick_source(bridge, channel_id, parts[2])
            if source is None:
                raise BrowseError("Unknown recording source")
            if len(parts) == 3:
                return await self._days(bridge, channel_id, source, labels)
            day = date.fromisoformat(parts[3])
            kind = parts[4] if len(parts) > 4 else None
            return await self._day(bridge, channel_id, source, day, kind, labels)
        except BridgeFault as err:
            raise BrowseError(f"Device not reachable ({type(err).__name__})") from err
        except ValueError as err:
            raise BrowseError("Unknown media item") from err

    def _root(self) -> BrowseMediaSource:
        base = _folder("", self.name)
        base.identifier = None
        base.children = [
            _folder(bridge.entry.entry_id, bridge.entry.title)
            for bridge in self._bridges()
            if self._channels(bridge)
        ]
        return base

    def _device(self, bridge: DeviceBridge) -> BrowseMediaSource:
        base = _folder(bridge.entry.entry_id, bridge.entry.title)
        base.children = [
            _folder(
                f"{bridge.entry.entry_id}/{cid}",
                bridge.profile.channels[cid].name,
                thumbnail=self._camera_thumbnail(bridge, cid),
            )
            for cid in self._channels(bridge)
        ]
        return base

    async def _channel(
        self, bridge: DeviceBridge, channel_id: str, labels: dict[str, str]
    ) -> BrowseMediaSource:
        available = sources(bridge, channel_id)
        if len(available) == 1:
            return await self._days(bridge, channel_id, available[0], labels)
        base = _folder(
            f"{bridge.entry.entry_id}/{channel_id}", bridge.profile.channels[channel_id].name
        )
        base.children = [
            _folder(
                f"{bridge.entry.entry_id}/{channel_id}/{source.key}",
                labels.get(f"recording_source.options.{source.key}", source.key),
            )
            for source in available
        ]
        return base

    async def _days(
        self,
        bridge: DeviceBridge,
        channel_id: str,
        source: PlaybackSource,
        labels: dict[str, str],
    ) -> BrowseMediaSource:
        prefix = f"{bridge.entry.entry_id}/{channel_id}/{source.key}"
        title = bridge.profile.channels[channel_id].name
        if len(sources(bridge, channel_id)) > 1:
            label = labels.get(f"recording_source.options.{source.key}", source.key)
            title = f"{title} - {label}"
        base = _folder(prefix, title)
        today = dt_util.now().date()
        days: list[date] = []
        months = {(today.year, today.month)}
        earlier = today.replace(day=1) - timedelta(days=1)
        months.add((earlier.year, earlier.month))
        calendar = True
        for year, month in sorted(months, reverse=True):
            found = await recordings.recorded_days(source.link, source.track, year, month)
            if found is None:
                calendar = False
                break
            days.extend(date(year, month, d) for d in found if date(year, month, d) <= today)
        if not calendar:
            days = [today - timedelta(days=n) for n in range(DAYS_WITHOUT_CALENDAR)]
        base.children = [
            _folder(f"{prefix}/{day.isoformat()}", day.isoformat())
            for day in sorted(days, reverse=True)[:MAX_DAYS]
        ]
        return base

    async def _day(
        self,
        bridge: DeviceBridge,
        channel_id: str,
        source: PlaybackSource,
        day: date,
        kind: str | None,
        labels: dict[str, str],
    ) -> BrowseMediaSource:
        prefix = f"{bridge.entry.entry_id}/{channel_id}/{source.key}/{day.isoformat()}"
        start = _midnight(day)
        found = await recordings.search(source.link, source.track, start, start + timedelta(days=1))
        kinds = sorted({r.kind for r in found}, key=recordings.KINDS.index)
        title = day.isoformat()
        if kind is None and len(kinds) > 1:
            base = _folder(prefix, title)
            base.children = [
                _folder(
                    f"{prefix}/{key}",
                    labels.get(f"recording_kind.options.{key}", key),
                    children_media_class=MediaClass.VIDEO,
                )
                for key in (ALL, *kinds)
            ]
            return base
        if kind not in (None, ALL):
            found = [r for r in found if r.kind == kind]
            title = f"{title} - {labels.get(f'recording_kind.options.{kind}', kind)}"
        base = _folder(f"{prefix}/{kind}" if kind else prefix, title)
        base.children_media_class = MediaClass.VIDEO
        thumbnails = len(found) <= MAX_THUMBNAILS
        clip_prefix = f"{bridge.entry.entry_id}/{channel_id}/{source.key}/clip"
        base.children = [
            BrowseMediaSource(
                domain=DOMAIN,
                identifier=f"{clip_prefix}/{encode_token(r.playback_uri)}",
                media_class=MediaClass.VIDEO,
                media_content_type=MIME[FORMAT_MP4],
                title=_clip_title(r, labels, show_kind=kind == ALL),
                can_play=True,
                can_expand=False,
                thumbnail=thumbnail_url(
                    bridge.entry.entry_id, channel_id, source.key, r.playback_uri
                )
                if thumbnails
                else None,
            )
            for r in sorted(found, key=lambda r: r.start, reverse=True)
        ]
        return base

    # ------------------------------------------------------------- resolve

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        parts = (item.identifier or "").split("/")
        if len(parts) != 5 or parts[3] != "clip":
            raise Unresolvable("Not a recording")
        entry_id, channel_id, source_key, _, token = parts
        uri = decode_token(token)
        try:
            bridge = self._bridge(entry_id)
        except BrowseError as err:
            raise Unresolvable(str(err)) from err
        source = pick_source(bridge, channel_id, source_key)
        if uri is None or source is None:
            raise Unresolvable("Unknown recording")
        mode = bridge.entry.options.get(OPT_PLAYBACK, FORMAT_MP4)
        if mode == PLAYBACK_STREAM:
            try:
                return await self._stream(bridge, source, uri)
            except (HomeAssistantError, ImportError) as err:
                _LOGGER.debug("Stream playback unavailable, converting instead: %s", err)
                mode = FORMAT_MP4
        url = recording_url(entry_id, channel_id, source.key, mode, uri)
        return PlayMedia(url, MIME[mode])

    async def _stream(self, bridge: DeviceBridge, source: PlaybackSource, uri: str) -> PlayMedia:
        """Let the device play the recording over RTSP; Home Assistant serves it as HLS."""
        from homeassistant.components.stream import (  # noqa: PLC0415 - optional
            HLS_PROVIDER,
            create_stream,
        )

        try:
            # Home Assistant 2026.10 moved it from stream to camera.
            from homeassistant.components.camera import (  # noqa: PLC0415 - optional
                DynamicStreamSettings,
            )
        except ImportError:
            from homeassistant.components.stream import (  # noqa: PLC0415 - optional
                DynamicStreamSettings,
            )

        stream = create_stream(
            self.hass,
            rtsp_url(source, uri),
            {"rtsp_transport": bridge.entry.options.get(OPT_RTSP_TRANSPORT, RTSP_TCP)},
            DynamicStreamSettings(),
            "recording",
        )
        stream.add_provider(HLS_PROVIDER)
        await stream.start()
        return PlayMedia(stream.endpoint_url(HLS_PROVIDER), "application/vnd.apple.mpegurl")


def _midnight(day: date) -> datetime:
    return dt_util.start_of_local_day(day)


def _clip_title(recording: recordings.Recording, labels: dict[str, str], *, show_kind: bool) -> str:
    start = dt_util.as_local(recording.start)
    end = dt_util.as_local(recording.end)
    title = f"{start:%H:%M:%S} - {end:%H:%M:%S}"
    if show_kind:
        title += f" · {labels.get(f'recording_kind.options.{recording.kind}', recording.kind)}"
    return title
