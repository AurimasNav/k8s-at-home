"""Binary sensors: detections, alarm inputs, channel connectivity, event connection."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_ENTRY_KIND, KIND_CLOUD, OPT_TARGET_SENSORS
from .entity import PolledEntity, PushEntity, add_entities
from .model.features.storage import HEALTHY
from .model.inventory import DeviceShape, IoPort
from .model.vocabulary import Kind
from .runtime.bridge import MAIN_FEED, DeviceBridge
from .runtime.router import DEVICE_SCOPE, signal_state

PARALLEL_UPDATES = 0

_CHANNEL_KINDS: dict[Kind, BinarySensorDeviceClass | None] = {
    Kind.MOTION: BinarySensorDeviceClass.MOTION,
    Kind.INTRUSION: BinarySensorDeviceClass.MOTION,
    Kind.LINE_CROSSING: BinarySensorDeviceClass.MOTION,
    Kind.REGION_ENTRANCE: BinarySensorDeviceClass.MOTION,
    Kind.REGION_EXITING: BinarySensorDeviceClass.MOTION,
    Kind.PIR: BinarySensorDeviceClass.MOTION,
    Kind.TAMPER: BinarySensorDeviceClass.TAMPER,
    Kind.VIDEO_LOSS: BinarySensorDeviceClass.PROBLEM,
    Kind.SCENE_CHANGE: BinarySensorDeviceClass.TAMPER,
    Kind.DEFOCUS: BinarySensorDeviceClass.PROBLEM,
    Kind.AUDIO_EXCEPTION: BinarySensorDeviceClass.SOUND,
}
_TARGETS = ("human", "vehicle")


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    if entry.data.get(CONF_ENTRY_KIND) == KIND_CLOUD:
        from .cloud_entry import async_setup_cloud_binary_sensors  # noqa: PLC0415

        await async_setup_cloud_binary_sensors(hass, entry, async_add_entities)
        return
    bridge: DeviceBridge = entry.runtime_data
    profile = bridge.profile
    entities: list[BinarySensorEntity] = []
    for channel in profile.channels.values():
        for kind in sorted(channel.kinds & _CHANNEL_KINDS.keys()):
            entities.append(DetectionSensor(bridge, channel.channel_id, kind))
        if entry.options.get(OPT_TARGET_SENSORS) and channel.kinds & {
            Kind.INTRUSION,
            Kind.LINE_CROSSING,
            Kind.REGION_ENTRANCE,
            Kind.REGION_EXITING,
            Kind.MOTION,
        }:
            entities.extend(TargetSensor(bridge, channel.channel_id, t) for t in _TARGETS)
        if profile.shape.records:
            entities.append(ChannelOnlineSensor(bridge, channel.channel_id))
        if profile.recording:
            entities.append(RecordingSensor(bridge, channel.channel_id))
    entities.extend(AlarmInputSensor(bridge, port) for port in profile.inputs.values())
    if profile.storage:
        entities.append(StorageProblemSensor(bridge))
    if profile.cloud is not None:
        entities.append(StreamEncryptionSensor(bridge))
    if profile.intercom and profile.intercom.has_call_status:
        entities.append(DoorbellRingingSensor(bridge))
    if profile.shape != DeviceShape.INTERCOM:
        entities.append(EventLinkSensor(bridge))
    add_entities(bridge, async_add_entities, entities)


class DetectionSensor(PushEntity, BinarySensorEntity):
    def __init__(self, bridge: DeviceBridge, channel_id: str, kind: Kind) -> None:
        super().__init__(bridge, channel_id, f"event_{kind.value}")
        self._kind = kind
        self._attr_translation_key = kind.value
        self._attr_device_class = _CHANNEL_KINDS[kind]
        if kind in (Kind.DEFOCUS, Kind.AUDIO_EXCEPTION, Kind.SCENE_CHANGE):
            self._attr_entity_registry_enabled_default = False

    @property
    def is_on(self) -> bool:
        return self.bridge.router.is_active(self.channel_id, self._kind.value)


class TargetSensor(PushEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    def __init__(self, bridge: DeviceBridge, channel_id: str, target: str) -> None:
        super().__init__(bridge, channel_id, f"target_{target}")
        self._target = target
        self._attr_translation_key = f"target_{target}"

    @property
    def is_on(self) -> bool:
        return self.bridge.router.is_active(self.channel_id, f"target:{self._target}")


class AlarmInputSensor(PolledEntity, BinarySensorEntity):
    """Polled state, sped up by push notifications."""

    _attr_translation_key = "alarm_input"

    def __init__(self, bridge: DeviceBridge, port: IoPort) -> None:
        super().__init__(bridge, port.channel_id, f"input_{port.port_id}")
        self._port = port
        self._attr_translation_placeholders = {"port": port.name or port.port_id}

    @property
    def is_on(self) -> bool | None:
        if self.bridge.router.is_active(DEVICE_SCOPE, f"io:{self._port.port_id}"):
            return True
        return self.coordinator.data.inputs.get(self._port.port_id)


class ChannelOnlineSensor(PolledEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "channel_online"

    def __init__(self, bridge: DeviceBridge, channel_id: str) -> None:
        super().__init__(bridge, channel_id, "online")

    @property
    def is_on(self) -> bool | None:
        online = self.coordinator.data.channels_online.get(self.channel_id)
        if online is None:
            return self.bridge.profile.channels[self.channel_id].online
        return online


class StorageProblemSensor(PolledEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "storage_problem"

    def __init__(self, bridge: DeviceBridge) -> None:
        super().__init__(bridge, None, "storage_problem")

    @property
    def is_on(self) -> bool:
        if self.bridge.router.is_active(DEVICE_SCOPE, Kind.STORAGE.value):
            return True
        return any(unit.status not in HEALTHY for unit in self.coordinator.data.storage)


class StreamEncryptionSensor(PolledEntity, BinarySensorEntity):
    """On while Hik-Connect stream encryption keeps Home Assistant from playing the video."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "stream_encryption"

    def __init__(self, bridge: DeviceBridge) -> None:
        super().__init__(bridge, None, "stream_encryption")

    @property
    def is_on(self) -> bool:
        cloud = self.bridge.profile.cloud
        return bool(cloud and cloud.stream_encrypted)


class EventLinkSensor(PushEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "event_link"

    def __init__(self, bridge: DeviceBridge) -> None:
        super().__init__(bridge, None, "event_link")

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.bridge.router.feed_connected(MAIN_FEED) or self.bridge.router.feed_connected(
            "webhook"
        )


class RecordingSensor(PolledEntity, BinarySensorEntity):
    """Whether the recorder is writing this channel to disk right now."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_translation_key = "recording"

    def __init__(self, bridge: DeviceBridge, channel_id: str) -> None:
        super().__init__(bridge, channel_id, "recording")

    @property
    def available(self) -> bool:
        return super().available and self.channel_id in self.coordinator.data.recording

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.recording.get(self.channel_id)


class DoorbellRingingSensor(PolledEntity, BinarySensorEntity):
    """On while the doorbell is ringing (polled or pushed)."""

    _attr_translation_key = "doorbell_ringing"

    def __init__(self, bridge: DeviceBridge) -> None:
        super().__init__(bridge, None, "doorbell_ringing")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_state(self.bridge.entry.entry_id), self._on_push
            )
        )

    @callback
    def _on_push(self, slot: object) -> None:
        if slot == (DEVICE_SCOPE, Kind.DOORBELL.value):
            self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        if self.bridge.router.is_active(DEVICE_SCOPE, Kind.DOORBELL.value):
            return True
        return self.coordinator.data is not None and self.coordinator.data.call_status == "ringing"
