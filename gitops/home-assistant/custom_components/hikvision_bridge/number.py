"""Numbers: image setting levels per channel (brightness, light intensity, WDR level, ...)."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import ImageSettingEntity, add_entities, drop_entities, image_entities
from .model.features.image import ImageCapability
from .runtime.bridge import DeviceBridge

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    bridge: DeviceBridge = entry.runtime_data
    add_entities(bridge, async_add_entities, image_entities(bridge, "number", ImageNumber))
    # The park action number of earlier versions; it is a select now.
    drop_entities(
        hass,
        bridge,
        "number",
        ((channel_id, "image_park_action_number") for channel_id in bridge.profile.channels),
    )


class ImageNumber(ImageSettingEntity, NumberEntity):
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, bridge: DeviceBridge, channel_id: str, capability: ImageCapability) -> None:
        super().__init__(bridge, channel_id, capability)
        self._attr_native_min_value = capability.minimum or 0
        self._attr_native_max_value = capability.maximum or 100
        if capability.setting.seconds:
            self._attr_native_unit_of_measurement = UnitOfTime.SECONDS
            self._attr_mode = NumberMode.BOX
        elif (capability.minimum, capability.maximum) == (0, 100):
            self._attr_native_unit_of_measurement = PERCENTAGE

    @property
    def native_value(self) -> float | None:
        current = self.current
        if current is None:
            return None
        try:
            return float(current.value)
        except ValueError:
            return None

    async def async_set_native_value(self, value: float) -> None:
        await self.async_write_value(str(round(value)))
