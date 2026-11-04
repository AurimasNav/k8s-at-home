"""Selects: image setting modes per channel (day/night, light, WDR, ...) and PTZ presets."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .entity import BridgeEntity, ImageSettingEntity, add_entities, image_entities
from .model.features import park, ptz
from .model.features.image import ImageCapability, option_key
from .runtime.bridge import DeviceBridge
from .wire.faults import BridgeFault

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    bridge: DeviceBridge = entry.runtime_data
    entities: list[BridgeEntity] = list(image_entities(bridge, "select", _image_select))
    entities.extend(
        PtzPresetSelect(bridge, channel.channel_id, channel.ptz.presets)
        for channel in bridge.profile.channels.values()
        if channel.ptz is not None and channel.ptz.presets
    )
    add_entities(bridge, async_add_entities, entities)


def _image_select(
    bridge: DeviceBridge, channel_id: str, capability: ImageCapability
) -> ImageSettingEntity:
    if capability.key == "park_action_number":
        return ParkNumberSelect(bridge, channel_id, capability)
    return ImageSelect(bridge, channel_id, capability)


class ImageSelect(ImageSettingEntity, SelectEntity):
    def __init__(self, bridge: DeviceBridge, channel_id: str, capability: ImageCapability) -> None:
        super().__init__(bridge, channel_id, capability)
        if capability.labels:
            # Device-named choices (alarm sounds): show the device's names.
            self._device_option = dict(zip(capability.labels, capability.options, strict=True))
        else:
            # Options use a translatable form and map back to the device spelling.
            self._device_option = {option_key(o): o for o in capability.options}
        self._from_device = {device: shown for shown, device in self._device_option.items()}
        self._attr_options = list(self._device_option)

    @property
    def current_option(self) -> str | None:
        current = self.current
        if current is None:
            return None
        if current.value in self._from_device:
            return self._from_device[current.value]
        key = option_key(current.value)
        return key if key in self._device_option else None

    async def async_select_option(self, option: str) -> None:
        await self.async_write_value(self._device_option[option])


class ParkNumberSelect(ImageSettingEntity, SelectEntity):
    """Number of the park action: a stored preset, a patrol or a pattern.

    The choices follow the current action type; scans take no number, so the select is
    unavailable while one is chosen.
    """

    def _numbers(self) -> dict[str, str]:
        values = self.coordinator.data.image.get(self.channel_id, {})
        kind = values.get("park_action_type")
        channel = self.bridge.profile.channels.get(self.channel_id)
        presets = channel.ptz.presets if channel is not None and channel.ptz else {}
        span = (
            self.capability.minimum or park.DEFAULT_NUMBER[0],
            self.capability.maximum or park.DEFAULT_NUMBER[1],
        )
        return park.action_numbers(kind.value if kind else None, span, presets)

    def _choices(self) -> dict[str, str]:
        """Shown name -> number, with a current number outside the list kept as it is."""
        choices = {name: number for number, name in self._numbers().items()}
        current = self.current
        if choices and current is not None and current.value not in choices.values():
            choices.setdefault(current.value, current.value)
        return choices

    @property
    def available(self) -> bool:
        return super().available and bool(self._numbers())

    @property
    def options(self) -> list[str]:
        return list(self._choices())

    @property
    def current_option(self) -> str | None:
        current = self.current
        if current is None:
            return None
        return next((n for n, v in self._choices().items() if v == current.value), None)

    async def async_select_option(self, option: str) -> None:
        await self.async_write_value(self._choices()[option])


class PtzPresetSelect(BridgeEntity, SelectEntity):
    """Moves a PTZ channel to a stored preset.

    The device does not report whether it still stands at a preset, so no option is shown
    as current and the same preset can be chosen again.
    """

    _attr_translation_key = "ptz_preset"
    _attr_current_option = None

    def __init__(self, bridge: DeviceBridge, channel_id: str, presets: dict[str, str]) -> None:
        super().__init__(bridge, channel_id, "ptz_preset")
        self._preset_id = {name: preset_id for preset_id, name in presets.items()}
        self._attr_options = list(self._preset_id)

    async def async_select_option(self, option: str) -> None:
        link, remote = self.bridge.route(self.channel_id)
        try:
            await ptz.goto_preset(link, remote, self._preset_id[option])
        except BridgeFault as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="write_failed",
                translation_placeholders={"reason": type(err).__name__},
            ) from err
