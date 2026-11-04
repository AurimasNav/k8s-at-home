"""Shared entity base classes."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable

from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .model.features import adjust
from .model.features.image import ImageCapability, ImageValue
from .runtime import identity
from .runtime.bridge import DeviceBridge
from .runtime.refresh import StatusRefresher
from .runtime.router import signal_state
from .wire.faults import BridgeFault


class BridgeEntity(Entity):
    """Entity bound to one device entry; never sets an entity_id."""

    _attr_has_entity_name = True

    def __init__(self, bridge: DeviceBridge, channel_id: str | None, feature: str) -> None:
        self.bridge = bridge
        self.channel_id = channel_id
        self._attr_unique_id = identity.entity_uid(bridge.profile, channel_id, feature)
        self._attr_device_info = DeviceInfo(
            identifiers={identity.entity_device_identifier(bridge.profile, channel_id)}
        )


class PushEntity(BridgeEntity):
    """Entity driven by device notifications."""

    _attr_should_poll = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_state(self.bridge.entry.entry_id), self._on_state
            )
        )

    @callback
    def _on_state(self, _slot: object) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self.bridge.router.feeds_healthy()


class PolledEntity(CoordinatorEntity[StatusRefresher], BridgeEntity):
    """Entity backed by the status refresher."""

    def __init__(self, bridge: DeviceBridge, channel_id: str | None, feature: str) -> None:
        CoordinatorEntity.__init__(self, bridge.refresher)
        BridgeEntity.__init__(self, bridge, channel_id, feature)


class ImageSettingEntity(PolledEntity):
    """One image setting of a channel; adjustable only while the active mode contains it."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, bridge: DeviceBridge, channel_id: str, capability: ImageCapability) -> None:
        super().__init__(bridge, channel_id, f"image_{capability.key}")
        self.capability = capability
        self._attr_translation_key = capability.key
        self._attr_entity_registry_enabled_default = capability.setting.enabled_default

    @property
    def current(self) -> ImageValue | None:
        return self.coordinator.data.image.get(self.channel_id, {}).get(self.capability.key)

    @property
    def available(self) -> bool:
        return super().available and self.current is not None

    async def async_write_value(self, value: str) -> None:
        current = self.current
        if current is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="setting_inactive")
        link, remote = self.bridge.route(self.channel_id)
        try:
            await adjust.write(link, remote, self.capability.key, current.field, value)
        except BridgeFault as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="write_failed",
                translation_placeholders={"reason": type(err).__name__},
            ) from err
        values = dict(self.coordinator.data.image.get(self.channel_id, {}))
        values[self.capability.key] = ImageValue(current.field, value)
        self.coordinator.data.image[self.channel_id] = values
        # Settings that follow this one (the park action number) show the change at once.
        self.coordinator.async_update_listeners()
        # Other settings depend on modes (e.g. light brightness); read them all again.
        self.bridge.refresher.request_full()
        await self.bridge.refresher.async_request_refresh()


def add_entities(
    bridge: DeviceBridge,
    async_add_entities: AddConfigEntryEntitiesCallback,
    entities: Iterable[BridgeEntity],
) -> None:
    """Add entities under the sub-entry their device belongs to (direct camera links)."""
    groups: dict[str | None, list[BridgeEntity]] = defaultdict(list)
    for entity in entities:
        groups[bridge.subentry_of(entity.channel_id)].append(entity)
    for subentry_id, group in groups.items():
        async_add_entities(group, config_subentry_id=subentry_id)


@callback
def drop_entities(
    hass: HomeAssistant, bridge: DeviceBridge, platform: str, features: Iterable[tuple[str, str]]
) -> None:
    """Remove entities of features a channel turned out not to have (channel, feature)."""
    registry = er.async_get(hass)
    for channel_id, feature in features:
        uid = identity.entity_uid(bridge.profile, channel_id, feature)
        if entity_id := registry.async_get_entity_id(platform, DOMAIN, uid):
            registry.async_remove(entity_id)


def image_entities[T](
    bridge: DeviceBridge,
    platform: str,
    factory: Callable[[DeviceBridge, str, ImageCapability], T],
) -> list[T]:
    return [
        factory(bridge, channel.channel_id, capability)
        for channel in bridge.profile.channels.values()
        for capability in channel.image.values()
        if capability.setting.platform == platform
    ]
