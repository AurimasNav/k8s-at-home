"""Image entities: the pictures an ANPR camera sends with each plate it reads."""

from __future__ import annotations

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import BridgeEntity, add_entities
from .model.vocabulary import Kind
from .runtime.bridge import DeviceBridge
from .runtime.router import Hit, signal_trigger

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    bridge: DeviceBridge = entry.runtime_data
    add_entities(
        bridge,
        async_add_entities,
        (
            PlatePicture(hass, bridge, channel.channel_id, slot)
            for channel in bridge.profile.channels.values()
            if Kind.ANPR in channel.kinds
            for slot in ("scene", "plate")
        ),
    )


class PlatePicture(BridgeEntity, ImageEntity):
    """Last vehicle (scene) or plate close-up picture of a plate notification."""

    _attr_should_poll = False
    _attr_content_type = "image/jpeg"

    def __init__(
        self, hass: HomeAssistant, bridge: DeviceBridge, channel_id: str, slot: str
    ) -> None:
        ImageEntity.__init__(self, hass)
        BridgeEntity.__init__(self, bridge, channel_id, f"anpr_{slot}_picture")
        self._slot = slot
        self._attr_translation_key = f"anpr_{slot}_picture"
        # The close-up is a crop of the scene picture; most automations want the scene.
        self._attr_entity_registry_enabled_default = slot == "scene"
        self._picture: bytes | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_trigger(self.bridge.entry.entry_id), self._on_hit
            )
        )

    @callback
    def _on_hit(self, hit: Hit) -> None:
        if hit.channel != self.channel_id or hit.kind is not Kind.ANPR:
            return
        picture = hit.frame.pictures.get(self._slot)  # an unnamed picture counts as scene
        if picture is None:
            return
        self._picture = picture
        self._attr_image_last_updated = hit.frame.received
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        return self._picture
