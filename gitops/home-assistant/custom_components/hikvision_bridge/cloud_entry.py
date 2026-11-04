"""Config entries of kind 'cloud': a Hik-Connect account and its devices."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import math
import time
from typing import Any

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
)
from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.components.button import ButtonEntity
from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, EntityCategory, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .cloud.account import CaptchaRequired, CloudAccount, CloudAuthFailed, CloudError
from .cloud.actions import fetch_devices, open_door_relay, set_area_defence_mode
from .cloud.catalog import CloudArea, CloudDevice
from .const import CLOUD_INTERVAL, CONF_ACCOUNT, CONF_REGION_HOST, DOMAIN
from .runtime.identity import VENDOR
from .wire.scrub import scrubbed_logger

_LOGGER = scrubbed_logger(__name__)

AREA_MEMBERS_EVERY = timedelta(minutes=10)


class CloudRefresher(DataUpdateCoordinator[dict[str, CloudDevice]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, account: CloudAccount) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} cloud",
            update_interval=CLOUD_INTERVAL,
        )
        self.account = account
        self._members_read = -math.inf

    async def _async_update_data(self) -> dict[str, CloudDevice]:
        # Area modes are read every time; their members (one request per area) rarely change.
        members = time.monotonic() - self._members_read >= AREA_MEMBERS_EVERY.total_seconds()
        try:
            devices = await fetch_devices(self.account, self.data, members=members)
        except (CloudAuthFailed, CaptchaRequired) as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="cloud_auth"
            ) from err
        except CloudError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cloud_failed",
                translation_placeholders={"reason": err.reason},
            ) from err
        if members:
            self._members_read = time.monotonic()
        return devices


@dataclass
class CloudBridge:
    entry: ConfigEntry
    account: CloudAccount
    refresher: CloudRefresher

    def diagnostics(self) -> dict[str, Any]:
        devices = self.refresher.data or {}
        return {
            "region_host": self.account.region_host,
            "requests": self.account.requests,
            "last_error": self.account.last_error,
            "devices": [
                {
                    "model": d.model,
                    "category": d.category,
                    "online": d.online,
                    "channels": d.channels,
                    "stream_encrypted": d.stream_encrypted,
                    "firmware": d.firmware,
                }
                for d in devices.values()
            ],
        }


type CloudConfigEntry = ConfigEntry[CloudBridge]


async def async_setup_cloud_entry(
    hass: HomeAssistant, entry: CloudConfigEntry, platforms: list[Platform]
) -> bool:
    account = CloudAccount(
        async_get_clientsession(hass),
        entry.data[CONF_ACCOUNT],
        entry.data[CONF_PASSWORD],
        entry.data.get(CONF_REGION_HOST),
    )
    refresher = CloudRefresher(hass, entry, account)
    await refresher.async_config_entry_first_refresh()
    entry.runtime_data = CloudBridge(entry, account, refresher)
    await hass.config_entries.async_forward_entry_setups(entry, platforms)
    return True


async def async_unload_cloud_entry(
    hass: HomeAssistant, entry: CloudConfigEntry, platforms: list[Platform]
) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, platforms)


# ------------------------------------------------------------------ entities


CLOUD_PREFIX = "cloud_"


def local_device_id(hass: HomeAssistant, device: CloudDevice) -> str | None:
    """Registry id of the locally configured device the cloud device belongs to, if any.

    The cloud reports a short serial that is contained in the serial a device reports
    locally (the full formats differ). Channel devices and cloud devices are skipped.
    """
    short = device.serial.lower()
    registry = dr.async_get(hass)
    for config_entry in hass.config_entries.async_entries(DOMAIN):
        for entry in dr.async_entries_for_config_entry(registry, config_entry.entry_id):
            for domain, key in entry.identifiers:
                if domain != DOMAIN or "_ch" in key or key.startswith(CLOUD_PREFIX):
                    continue
                if short in key:
                    return entry.id
    return None


class CloudEntity(CoordinatorEntity[CloudRefresher]):
    """Entity of a cloud device; the device sits below the local device when there is one."""

    _attr_has_entity_name = True

    def __init__(self, bridge: CloudBridge, device: CloudDevice, feature: str) -> None:
        super().__init__(bridge.refresher)
        self.bridge = bridge
        self.device_key = device.key
        self._attr_unique_id = f"{CLOUD_PREFIX}{device.key}_{feature}"
        info = DeviceInfo(
            identifiers={(DOMAIN, f"{CLOUD_PREFIX}{device.key}")},
            manufacturer=VENDOR,
            model=device.model or None,
            name=f"{device.name} (Hik-Connect)",
            sw_version=device.firmware,
        )
        if parent := local_device_id(bridge.refresher.hass, device):
            info["via_device_id"] = parent
        self._attr_device_info = info

    @property
    def device(self) -> CloudDevice | None:
        return (self.coordinator.data or {}).get(self.device_key)

    @property
    def available(self) -> bool:
        return super().available and self.device is not None


def _add_for_new_devices(
    bridge: CloudBridge,
    async_add_entities: AddConfigEntryEntitiesCallback,
    build: Any,
) -> None:
    """Create entities for devices that appear on the account later, too."""
    seen: set[str] = set()

    @callback
    def _check() -> None:
        new = [d for k, d in (bridge.refresher.data or {}).items() if k not in seen]
        if not new:
            return
        seen.update(d.key for d in new)
        async_add_entities([entity for device in new for entity in build(device)])

    _check()
    bridge.entry.async_on_unload(bridge.refresher.async_add_listener(_check))


async def async_setup_cloud_binary_sensors(
    hass: HomeAssistant,
    entry: CloudConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    bridge = entry.runtime_data

    def build(device: CloudDevice) -> list[BinarySensorEntity]:
        entities: list[BinarySensorEntity] = [CloudOnline(bridge, device)]
        if device.stream_encrypted is not None:
            entities.append(CloudStreamEncryption(bridge, device))
        return entities

    _add_for_new_devices(bridge, async_add_entities, build)


async def async_setup_cloud_events(
    hass: HomeAssistant,
    entry: CloudConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    bridge = entry.runtime_data
    _add_for_new_devices(bridge, async_add_entities, lambda d: [CloudConnectionEvents(bridge, d)])


async def async_setup_cloud_buttons(
    hass: HomeAssistant,
    entry: CloudConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    bridge = entry.runtime_data
    _add_for_new_devices(
        bridge,
        async_add_entities,
        lambda d: [CloudDoorButton(bridge, d)] if d.is_intercom else [],
    )


async def async_setup_cloud_alarm_control_panels(
    hass: HomeAssistant,
    entry: CloudConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    bridge = entry.runtime_data
    seen: set[tuple[str, int]] = set()

    @callback
    def _check() -> None:
        devices = bridge.refresher.data or {}
        new_entities: list[CloudAreaAlarmPanel] = []
        for device in devices.values():
            for area in device.areas:
                key = (device.key, area.group_id)
                if key not in seen:
                    seen.add(key)
                    new_entities.append(CloudAreaAlarmPanel(bridge, device, area.group_id))
        if new_entities:
            async_add_entities(new_entities)
        # Areas deleted (or recreated by an update) on a device that still reports.
        registry = er.async_get(hass)
        for device_key, group_id in list(seen):
            device = devices.get(device_key)
            if device is None or any(a.group_id == group_id for a in device.areas):
                continue
            seen.discard((device_key, group_id))
            unique_id = f"{CLOUD_PREFIX}{device_key}_area_{group_id}"
            if entity_id := registry.async_get_entity_id(
                Platform.ALARM_CONTROL_PANEL, DOMAIN, unique_id
            ):
                registry.async_remove(entity_id)

    _check()
    bridge.entry.async_on_unload(bridge.refresher.async_add_listener(_check))


class CloudOnline(CloudEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "cloud_online"

    def __init__(self, bridge: CloudBridge, device: CloudDevice) -> None:
        super().__init__(bridge, device, "online")

    @property
    def is_on(self) -> bool | None:
        device = self.device
        return device.online if device else None


class CloudStreamEncryption(CloudEntity, BinarySensorEntity):
    """On while the cloud stream encryption blocks local RTSP decoding."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "cloud_stream_encryption"

    def __init__(self, bridge: CloudBridge, device: CloudDevice) -> None:
        super().__init__(bridge, device, "stream_encryption")

    @property
    def is_on(self) -> bool | None:
        device = self.device
        return device.stream_encrypted if device else None


class CloudConnectionEvents(CloudEntity, EventEntity):
    _attr_translation_key = "cloud_connection"
    _attr_event_types = ["went_online", "went_offline"]

    def __init__(self, bridge: CloudBridge, device: CloudDevice) -> None:
        super().__init__(bridge, device, "connection")
        self._last: bool | None = device.online

    @callback
    def _handle_coordinator_update(self) -> None:
        device = self.device
        if device is not None and self._last is not None and device.online != self._last:
            self._trigger_event("went_online" if device.online else "went_offline")
        if device is not None:
            self._last = device.online
        super()._handle_coordinator_update()


class CloudDoorButton(CloudEntity, ButtonEntity):
    _attr_translation_key = "cloud_unlock"

    def __init__(self, bridge: CloudBridge, device: CloudDevice) -> None:
        super().__init__(bridge, device, "unlock")

    async def async_press(self) -> None:
        device = self.device
        if device is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="cloud_gone")
        try:
            await open_door_relay(self.bridge.account, device)
        except CloudError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="cloud_failed",
                translation_placeholders={"reason": err.reason},
            ) from err


class CloudAreaAlarmPanel(CloudEntity, AlarmControlPanelEntity):
    """Alarm control panel for a Hik-Connect security area (group)."""

    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_AWAY | AlarmControlPanelEntityFeature.ARM_HOME
    )
    _attr_code_arm_required = False

    def __init__(self, bridge: CloudBridge, device: CloudDevice, group_id: int) -> None:
        super().__init__(bridge, device, f"area_{group_id}")
        self.group_id = group_id

    @property
    def area(self) -> CloudArea | None:
        if self.device is None:
            return None
        for a in self.device.areas:
            if a.group_id == self.group_id:
                return a
        return None

    @property
    def name(self) -> str | None:
        return self.area.name if self.area else f"Area {self.group_id}"

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        if self.area is None:
            return None
        if self.area.mode == 1:
            return AlarmControlPanelState.ARMED_AWAY
        if self.area.mode == 2:
            return AlarmControlPanelState.ARMED_HOME
        return AlarmControlPanelState.DISARMED

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.area is None:
            return {"group_id": self.group_id}
        return {
            "group_id": self.area.group_id,
            "device_serial": self.area.device_serial,
            "mode": self.area.mode,
            "camera_ids": list(self.area.camera_ids),
            "camera_count": len(self.area.camera_ids),
        }

    @property
    def available(self) -> bool:
        return super().available and self.area is not None

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        await self._set_mode(0)

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        await self._set_mode(1)

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        await self._set_mode(2)

    async def _set_mode(self, mode: int) -> None:
        device = self.device
        if device is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="cloud_gone")
        try:
            await set_area_defence_mode(self.bridge.account, device.serial, self.group_id, mode)
        except CloudError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="cloud_failed",
                translation_placeholders={"reason": err.reason},
            ) from err
        await self.coordinator.async_request_refresh()
