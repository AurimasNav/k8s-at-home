"""Event entities: one per channel, firing on every smart detection trigger."""

from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_ENTRY_KIND, KIND_CLOUD
from .entity import BridgeEntity, add_entities
from .model.vocabulary import SMART, Kind
from .runtime.bridge import DeviceBridge
from .runtime.router import Hit, signal_trigger

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    if entry.data.get(CONF_ENTRY_KIND) == KIND_CLOUD:
        from .cloud_entry import async_setup_cloud_events  # noqa: PLC0415

        await async_setup_cloud_events(hass, entry, async_add_entities)
        return
    bridge: DeviceBridge = entry.runtime_data
    entities: list[EventEntity] = [
        DetectionEvents(bridge, channel.channel_id, sorted(channel.kinds & SMART))
        for channel in bridge.profile.channels.values()
        if channel.kinds & SMART
    ]
    if bridge.profile.intercom and bridge.profile.intercom.has_call_status:
        entities.append(DoorbellEvent(bridge))
    add_entities(bridge, async_add_entities, entities)


class DetectionEvents(BridgeEntity, EventEntity):
    _attr_translation_key = "detection"
    _attr_device_class = EventDeviceClass.MOTION
    _attr_should_poll = False

    def __init__(self, bridge: DeviceBridge, channel_id: str, kinds: list[Kind]) -> None:
        super().__init__(bridge, channel_id, "detection_events")
        self._attr_event_types = [kind.value for kind in kinds]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_trigger(self.bridge.entry.entry_id), self._on_hit
            )
        )

    @callback
    def _on_hit(self, hit: Hit) -> None:
        if hit.channel != self.channel_id or hit.kind.value not in self.event_types:
            return
        attributes = {
            "targets": list(hit.frame.targets),
            "region": hit.frame.region_id,
            "raw_type": hit.frame.raw_type,
        }
        if hit.frame.plate is not None:
            attributes |= hit.frame.plate.as_dict()
        self._trigger_event(hit.kind.value, attributes)
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self.bridge.router.feeds_healthy()


class DoorbellEvent(BridgeEntity, EventEntity):
    _attr_translation_key = "doorbell"
    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = ["ring"]
    _attr_should_poll = False

    def __init__(self, bridge: DeviceBridge) -> None:
        super().__init__(bridge, None, "doorbell")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_trigger(self.bridge.entry.entry_id), self._on_hit
            )
        )

    @callback
    def _on_hit(self, hit: Hit) -> None:
        if hit.kind == Kind.DOORBELL:
            self._trigger_event("ring", {"raw_type": hit.frame.raw_type})
            self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self.bridge.router.feeds_healthy()
