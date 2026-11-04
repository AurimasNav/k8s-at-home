"""Sensors: storage units, device status, network address, event latency and plates."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfInformation, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .entity import PolledEntity, PushEntity, add_entities
from .model.features.health import DeviceHealth
from .model.inventory import StorageUnit
from .model.vocabulary import Kind
from .runtime import identity
from .runtime.bridge import DeviceBridge

PARALLEL_UPDATES = 0

STORAGE_STATES = ["ok", "unformatted", "error", "idle", "sleeping", "abnormal", "full", "unknown"]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    bridge: DeviceBridge = entry.runtime_data
    entities: list[SensorEntity] = []
    profile = bridge.profile
    owners: list[tuple[str | None, list[StorageUnit]]] = [(None, profile.storage)]
    owners.extend((c.channel_id, c.storage) for c in profile.channels.values())
    for channel_id, units in owners:
        for unit in units:
            entities.append(StorageStatus(bridge, channel_id, unit))
            entities.append(StorageFree(bridge, channel_id, unit))
            entities.append(StorageCapacity(bridge, channel_id, unit))
    health_owners = [None] if profile.health else []
    health_owners.extend(c.channel_id for c in profile.channels.values() if c.health)
    for channel_id in health_owners:
        entities.append(BootTime(bridge, channel_id))
        entities.append(CpuLoad(bridge, channel_id))
        entities.append(Reboots(bridge, channel_id))
        if channel_id is None and profile.shape.records:
            entities.append(MemoryUse(bridge, channel_id))
    entities.append(NetworkAddress(bridge, None))
    if identity.uses_channel_devices(profile):
        entities.extend(
            NetworkAddress(bridge, channel_id)
            for channel_id in profile.channels
            if bridge.channel_address(channel_id)
        )
    entities.append(EventLatency(bridge))
    entities.extend(
        LicensePlate(bridge, channel.channel_id)
        for channel in profile.channels.values()
        if Kind.ANPR in channel.kinds
    )
    if profile.intercom and profile.intercom.has_call_status:
        entities.append(IntercomCallStatus(bridge))
    add_entities(bridge, async_add_entities, entities)


class _StorageBase(PolledEntity, SensorEntity):
    def __init__(
        self, bridge: DeviceBridge, channel_id: str | None, unit: StorageUnit, feature: str
    ) -> None:
        super().__init__(bridge, channel_id, f"storage_{unit.unit_id}_{feature}")
        self._unit_id = unit.unit_id
        self._attr_translation_placeholders = {"unit": unit.name}

    @property
    def _unit(self) -> StorageUnit | None:
        data = self.coordinator.data
        units = (
            data.storage
            if self.channel_id is None
            else data.channel_storage.get(self.channel_id, [])
        )
        return next((u for u in units if u.unit_id == self._unit_id), None)

    @property
    def available(self) -> bool:
        return super().available and self._unit is not None


class StorageStatus(_StorageBase):
    _attr_translation_key = "storage_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_options = STORAGE_STATES

    def __init__(self, bridge: DeviceBridge, channel_id: str | None, unit: StorageUnit) -> None:
        super().__init__(bridge, channel_id, unit, "status")

    @property
    def native_value(self) -> str | None:
        unit = self._unit
        if unit is None:
            return None
        return unit.status if unit.status in STORAGE_STATES else "unknown"


class StorageFree(_StorageBase):
    _attr_translation_key = "storage_free"
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_native_unit_of_measurement = UnitOfInformation.MEBIBYTES
    _attr_suggested_unit_of_measurement = UnitOfInformation.GIBIBYTES
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, bridge: DeviceBridge, channel_id: str | None, unit: StorageUnit) -> None:
        super().__init__(bridge, channel_id, unit, "free")

    @property
    def native_value(self) -> int | None:
        unit = self._unit
        return unit.free_mb if unit else None


class StorageCapacity(_StorageBase):
    _attr_translation_key = "storage_capacity"
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_native_unit_of_measurement = UnitOfInformation.MEBIBYTES
    _attr_suggested_unit_of_measurement = UnitOfInformation.GIBIBYTES
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, bridge: DeviceBridge, channel_id: str | None, unit: StorageUnit) -> None:
        super().__init__(bridge, channel_id, unit, "capacity")

    @property
    def native_value(self) -> int | None:
        unit = self._unit
        return unit.capacity_mb if unit else None


class EventLatency(PushEntity, SensorEntity):
    _attr_translation_key = "event_latency"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MILLISECONDS
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, bridge: DeviceBridge) -> None:
        super().__init__(bridge, None, "event_latency")

    @property
    def native_value(self) -> float | None:
        return self.bridge.router.latency_ms


class LicensePlate(PushEntity, SensorEntity):
    """The plate an ANPR camera read last; details as attributes."""

    _attr_translation_key = "license_plate"

    def __init__(self, bridge: DeviceBridge, channel_id: str) -> None:
        super().__init__(bridge, channel_id, "license_plate")

    @property
    def native_value(self) -> str | None:
        frame = self.bridge.router.last_plate.get(self.channel_id)
        return frame.plate.number if frame and frame.plate else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        frame = self.bridge.router.last_plate.get(self.channel_id)
        if frame is None or frame.plate is None:
            return None
        details = frame.plate.as_dict()
        details.pop("plate")
        details["time"] = (frame.device_time or frame.received).isoformat()
        return details


class _HealthBase(PolledEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, bridge: DeviceBridge, channel_id: str | None, feature: str) -> None:
        super().__init__(bridge, channel_id, feature)
        self._attr_translation_key = feature

    @property
    def _health(self) -> DeviceHealth | None:
        return self.coordinator.data.health.get(self.channel_id)

    @property
    def available(self) -> bool:
        return super().available and self._health is not None


class BootTime(_HealthBase):
    """When the device last started, derived from its uptime."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, bridge: DeviceBridge, channel_id: str | None) -> None:
        super().__init__(bridge, channel_id, "boot_time")
        self._boot: datetime | None = None

    @property
    def native_value(self) -> datetime | None:
        health = self._health
        if health is None or health.uptime_seconds is None:
            return None
        boot = (dt_util.utcnow() - timedelta(seconds=health.uptime_seconds)).replace(microsecond=0)
        # Polling delay shifts the computed moment slightly; keep it steady.
        if self._boot is None or abs(boot - self._boot) > timedelta(minutes=2):
            self._boot = boot
        return self._boot


class CpuLoad(_HealthBase):
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, bridge: DeviceBridge, channel_id: str | None) -> None:
        super().__init__(bridge, channel_id, "cpu_load")

    @property
    def native_value(self) -> int | None:
        health = self._health
        return health.cpu_percent if health else None


class MemoryUse(_HealthBase):
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, bridge: DeviceBridge, channel_id: str | None) -> None:
        super().__init__(bridge, channel_id, "memory_use")

    @property
    def native_value(self) -> float | None:
        health = self._health
        return health.memory_percent if health else None


class Reboots(_HealthBase):
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, bridge: DeviceBridge, channel_id: str | None) -> None:
        super().__init__(bridge, channel_id, "reboots")

    @property
    def native_value(self) -> int | None:
        health = self._health
        return health.reboots if health else None

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        # Firmware that reports a timestamp instead of a count: show it, leave the count empty.
        health = self._health
        if health is None or health.reboots_timestamp is None:
            return None
        return {"reported_timestamp": health.reboots_timestamp.isoformat()}


class NetworkAddress(PolledEntity, SensorEntity):
    """The address Home Assistant reaches the device (or the channel's camera) at."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "network_address"

    def __init__(self, bridge: DeviceBridge, channel_id: str | None) -> None:
        super().__init__(bridge, channel_id, "network_address")

    @property
    def native_value(self) -> str | None:
        return self.bridge.channel_address(self.channel_id)


class IntercomCallStatus(PolledEntity, SensorEntity):
    _attr_translation_key = "call_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["idle", "ringing", "on_call"]

    def __init__(self, bridge: DeviceBridge) -> None:
        super().__init__(bridge, None, "call_status")

    @property
    def native_value(self) -> str | None:
        return self.coordinator.data.call_status
