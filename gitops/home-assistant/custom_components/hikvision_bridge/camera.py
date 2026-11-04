"""Camera entities: RTSP stream source and ISAPI snapshots per channel stream."""

from __future__ import annotations

from urllib.parse import quote

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    OPT_RTSP_PORT,
    OPT_RTSP_TRANSPORT,
    OPT_TRY_ENCRYPTED_STREAM,
    RTSP_PORT_DEFAULT,
    RTSP_TCP,
    SUBENTRY_CHANNEL_LINK,
)
from .entity import BridgeEntity, add_entities
from .model.inventory import StreamSpec
from .runtime.bridge import DeviceBridge
from .wire.faults import BridgeFault, DeviceRejected, Unsupported, detail
from .wire.link import IsapiLink
from .wire.scrub import scrubbed_logger

_LOGGER = scrubbed_logger(__name__)
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    bridge: DeviceBridge = entry.runtime_data
    entities = []
    for channel in bridge.profile.channels.values():
        streams = channel.streams
        if not streams and channel.channel_id.isdigit():
            streams = [StreamSpec(f"{int(channel.channel_id) * 100 + 1}")]
        for stream in streams:
            if stream.rank > 3:
                continue
            entities.append(ChannelCamera(bridge, channel.channel_id, stream))
    add_entities(bridge, async_add_entities, entities)


class ChannelCamera(BridgeEntity, Camera):
    def __init__(self, bridge: DeviceBridge, channel_id: str, stream: StreamSpec) -> None:
        BridgeEntity.__init__(self, bridge, channel_id, f"stream_{stream.rank}")
        Camera.__init__(self)
        self._stream = stream
        self._attr_translation_key = {1: "main_stream", 2: "sub_stream"}.get(
            stream.rank, "third_stream"
        )
        # Main and sub stream are both useful (full view, light live view); the third is rare.
        self._attr_entity_registry_enabled_default = stream.rank <= 2
        transport = bridge.entry.options.get(OPT_RTSP_TRANSPORT, RTSP_TCP)
        self.stream_options["rtsp_transport"] = transport
        self._was_playable = self._playable

    @property
    def _blocked_by_encryption(self) -> bool:
        """Hik-Connect stream encryption is on and no direct camera link avoids it.

        A direct link that leaves the live stream on the recorder, or the option to try the
        live stream anyway, is an explicit choice to try the recorder's stream.
        """
        if self.bridge.entry.options.get(OPT_TRY_ENCRYPTED_STREAM, False):
            return False
        cloud = self.bridge.profile.cloud
        route = self.bridge.routes.get(self.channel_id)
        direct = route is not None and route.healthy
        return bool(cloud and cloud.stream_encrypted) and not direct

    @property
    def _playable(self) -> bool:
        return not self._blocked_by_encryption

    @property
    def supported_features(self) -> CameraEntityFeature:
        return CameraEntityFeature.STREAM if self._playable else CameraEntityFeature(0)

    @property
    def extra_state_attributes(self) -> dict:
        stream = self._stream
        cloud = self.bridge.profile.cloud
        return {
            "codec": stream.codec,
            "resolution": f"{stream.width}x{stream.height}" if stream.width else None,
            "stream_encrypted": bool(cloud and cloud.stream_encrypted),
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # The encryption setting is re-read while running; follow it without a reload.
        self.async_on_remove(self.bridge.refresher.async_add_listener(self._on_refresh))

    @callback
    def _on_refresh(self) -> None:
        if self._playable == self._was_playable:
            return
        self._was_playable = self._playable
        self.hass.async_create_task(self._async_playable_changed())

    async def _async_playable_changed(self) -> None:
        await self.async_refresh_providers(write_state=False)
        self.async_write_ha_state()

    def _source(self) -> tuple[IsapiLink, str, dict]:
        """Link, stream id and credentials to use (direct camera link if configured)."""
        route = self.bridge.routes.get(self.channel_id)
        if route is not None and route.healthy:
            sub = next(
                s
                for s in self.bridge.entry.subentries.values()
                if s.subentry_type == SUBENTRY_CHANNEL_LINK and s.subentry_id == route.subentry_id
            )
            remote_stream = f"{int(route.remote_channel) * 100 + self._stream.rank}"
            return route.link, remote_stream, dict(sub.data)
        return self.bridge.link, self._stream.stream_id, dict(self.bridge.entry.data)

    def _live_source(self) -> tuple[IsapiLink, str, dict]:
        """Like ``_source``, but a direct link may leave the live stream on the recorder."""
        route = self.bridge.routes.get(self.channel_id)
        if route is not None and route.stream_from_recorder:
            return self.bridge.link, self._stream.stream_id, dict(self.bridge.entry.data)
        return self._source()

    async def stream_source(self) -> str | None:
        if not self._playable:
            return None
        link, stream_id, data = self._live_source()
        if link is self.bridge.link:
            port = (
                self.bridge.entry.options.get(OPT_RTSP_PORT)
                or self.bridge.profile.rtsp_port
                or RTSP_PORT_DEFAULT
            )
        else:
            port = self.bridge.routes[self.channel_id].rtsp_port
        user = quote(data[CONF_USERNAME], safe="")
        password = quote(data[CONF_PASSWORD], safe="")
        host = link.endpoint.base.split("://", 1)[1].rsplit(":", 1)[0]
        return f"rtsp://{user}:{password}@{host}:{port}/Streaming/Channels/{stream_id}"

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        link, stream_id, _ = self._source()
        query = (
            f"?videoResolutionWidth={self._stream.width}&videoResolutionHeight={self._stream.height}"
            if self._stream.width and self._stream.height
            else ""
        )
        paths = []
        if link is self.bridge.link and self.bridge.profile.shape.records:
            if query:
                paths.append(
                    f"/ISAPI/ContentMgmt/StreamingProxy/channels/{stream_id}/picture{query}"
                )
            paths.append(f"/ISAPI/ContentMgmt/StreamingProxy/channels/{stream_id}/picture")
        if query:
            paths.append(f"/ISAPI/Streaming/channels/{stream_id}/picture{query}")
        paths.append(f"/ISAPI/Streaming/channels/{stream_id}/picture")
        refused: DeviceRejected | None = None
        for path in paths:
            if path in link.unsupported:
                continue
            try:
                reply = await link.read_bytes(path)
            except Unsupported:
                continue
            except DeviceRejected as err:
                refused = err
                if "?" in path:
                    # The device refuses the size parameters: use its default size from now on.
                    link.unsupported.add(path)
                continue
            except BridgeFault as err:
                _LOGGER.debug("Snapshot of channel %s failed: %s", self.channel_id, detail(err))
                return None
            if reply.content_type.startswith("image/"):
                return reply.body
        if refused is not None:
            _LOGGER.debug(
                "Snapshot of channel %s (stream %s) failed: %s",
                self.channel_id,
                stream_id,
                detail(refused),
            )
        return None
