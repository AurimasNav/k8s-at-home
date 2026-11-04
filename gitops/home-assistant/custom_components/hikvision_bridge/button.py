"""Buttons: device restart, capability rescan and PTZ steps."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_ENTRY_KIND, DOMAIN, KIND_CLOUD
from .entity import BridgeEntity, add_entities, drop_entities
from .model.features import intercom, ptz
from .runtime.bridge import DeviceBridge
from .wire.faults import BridgeFault

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    if entry.data.get(CONF_ENTRY_KIND) == KIND_CLOUD:
        from .cloud_entry import async_setup_cloud_buttons  # noqa: PLC0415

        await async_setup_cloud_buttons(hass, entry, async_add_entities)
        return
    bridge: DeviceBridge = entry.runtime_data
    entities: list[BridgeEntity] = [RestartButton(bridge), RescanButton(bridge)]
    entities.extend(
        PtzButton(bridge, channel.channel_id, direction)
        for channel in bridge.profile.channels.values()
        if channel.ptz is not None
        for direction in channel.ptz.directions
    )
    if bridge.profile.intercom and bridge.profile.intercom.doors:
        entities.extend(
            DoorUnlockButton(bridge, door_no) for door_no in bridge.profile.intercom.doors
        )
    add_entities(bridge, async_add_entities, entities)
    # Buttons of earlier versions on channels that turned out to have no such move.
    drop_entities(
        hass,
        bridge,
        "button",
        (
            (channel.channel_id, f"ptz_{direction}")
            for channel in bridge.profile.channels.values()
            if channel.online is not False
            for direction in ptz.DIRECTIONS
            if channel.ptz is None or direction not in channel.ptz.directions
        ),
    )


def _fail(err: BridgeFault) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="write_failed",
        translation_placeholders={"reason": type(err).__name__},
    )


class RestartButton(BridgeEntity, ButtonEntity):
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False
    _attr_translation_key = "restart"

    def __init__(self, bridge: DeviceBridge) -> None:
        super().__init__(bridge, None, "restart")

    async def async_press(self) -> None:
        try:
            await self.bridge.link.write_xml("/ISAPI/System/reboot", "")
        except BridgeFault as err:
            raise _fail(err) from err


class RescanButton(BridgeEntity, ButtonEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "rescan"

    def __init__(self, bridge: DeviceBridge) -> None:
        super().__init__(bridge, None, "rescan")

    async def async_press(self) -> None:
        self.hass.config_entries.async_schedule_reload(self.bridge.entry.entry_id)


class PtzButton(BridgeEntity, ButtonEntity):
    """Moves a PTZ channel a short step in one direction."""

    def __init__(self, bridge: DeviceBridge, channel_id: str, direction: str) -> None:
        super().__init__(bridge, channel_id, f"ptz_{direction}")
        self.direction = direction
        self._attr_translation_key = f"ptz_{direction}"

    async def async_press(self) -> None:
        link, remote = self.bridge.route(self.channel_id)
        try:
            await ptz.step(link, remote, self.direction)
        except BridgeFault as err:
            raise _fail(err) from err


class DoorUnlockButton(BridgeEntity, ButtonEntity):
    _attr_translation_key = "open_door"

    def __init__(self, bridge: DeviceBridge, door_no: int) -> None:
        super().__init__(bridge, None, f"open_door_{door_no}")
        self._door_no = door_no
        if bridge.profile.intercom and len(bridge.profile.intercom.doors) > 1:
            self._attr_translation_key = "open_door_numbered"
            self._attr_translation_placeholders = {"door": str(door_no)}

    async def async_press(self) -> None:
        try:
            await intercom.remote_open_door(self.bridge.link, self._door_no)
        except BridgeFault as err:
            raise _fail(err) from err
