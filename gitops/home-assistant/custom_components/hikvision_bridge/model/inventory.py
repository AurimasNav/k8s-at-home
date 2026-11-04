"""Plain data describing a discovered device."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from .vocabulary import Kind

if TYPE_CHECKING:
    from .features.image import ImageCapability
    from .features.intercom import IntercomCapabilities
    from .features.ptz import PtzCapability


class DeviceShape(StrEnum):
    CAMERA = "camera"
    MULTI_CAMERA = "multi_camera"
    RECORDER = "recorder"
    HYBRID_RECORDER = "hybrid_recorder"
    INTERCOM = "intercom"
    ALARM_PANEL = "alarm_panel"
    UNKNOWN = "unknown"

    @property
    def records(self) -> bool:
        return self in (DeviceShape.RECORDER, DeviceShape.HYBRID_RECORDER)


class ChannelSource(StrEnum):
    LOCAL = "local"  # the device's own sensor or analog input
    PROXY = "proxy"  # an IP camera attached to a recorder


@dataclass(slots=True)
class Identity:
    serial: str
    model: str
    name: str
    firmware: str
    firmware_build: str | None = None
    mac: str | None = None
    device_type: str | None = None
    hardware: str | None = None

    @property
    def stable_id(self) -> str:
        """Serial without separators, used for device and entity identifiers."""
        return "".join(ch for ch in (self.serial or self.mac or "") if ch.isalnum()).lower()


@dataclass(slots=True)
class StreamSpec:
    stream_id: str
    codec: str | None = None
    width: int | None = None
    height: int | None = None

    @property
    def rank(self) -> int:
        """1 = main, 2 = sub, 3+ = additional streams."""
        return int(self.stream_id[-2:]) if self.stream_id[-2:].isdigit() else 9


@dataclass(slots=True)
class ChannelProfile:
    channel_id: str
    name: str
    source: ChannelSource
    online: bool | None = None
    camera_address: str | None = None
    camera_model: str | None = None
    camera_serial: str | None = None
    camera_firmware: str | None = None
    kinds: set[Kind] = field(default_factory=set)
    raw_kinds: set[str] = field(default_factory=set)
    streams: list[StreamSpec] = field(default_factory=list)
    switchable: set[Kind] = field(default_factory=set)
    # Adjustable settings (image, strobe, siren, speaker) by key.
    image: dict[str, ImageCapability] = field(default_factory=dict)
    ptz: PtzCapability | None = None
    # Read over a direct camera connection: memory card, status, armable inputs.
    storage: list[StorageUnit] = field(default_factory=list)
    health: bool = False
    armable_inputs: tuple[str, ...] = ()

    @property
    def main_stream(self) -> StreamSpec | None:
        return min(self.streams, key=lambda s: s.rank, default=None)


@dataclass(slots=True)
class IoPort:
    port_id: str
    output: bool
    name: str | None = None
    channel_id: str | None = None
    proxied: bool = False


@dataclass(slots=True)
class StorageUnit:
    unit_id: str
    name: str
    kind: str
    status: str
    capacity_mb: int | None
    free_mb: int | None
    network: bool = False


@dataclass(slots=True)
class CloudLinkState:
    enabled: bool
    registered: bool
    stream_encrypted: bool


@dataclass(slots=True)
class TriggerLink:
    """Which notification methods a device-side trigger uses."""

    trigger_id: str
    kind: Kind
    raw_type: str
    channel_id: str | None
    methods: set[str]

    @property
    def notifies_center(self) -> bool:
        return "center" in self.methods


@dataclass(slots=True)
class DeviceProfile:
    identity: Identity
    shape: DeviceShape
    channels: dict[str, ChannelProfile] = field(default_factory=dict)
    inputs: dict[str, IoPort] = field(default_factory=dict)
    outputs: dict[str, IoPort] = field(default_factory=dict)
    storage: list[StorageUnit] = field(default_factory=list)
    triggers: list[TriggerLink] = field(default_factory=list)
    cloud: CloudLinkState | None = None
    rtsp_port: int | None = None
    https_enabled: bool | None = None
    default_password: bool = False
    risky_password: bool = False
    has_alert_stream: bool = True
    health: bool = False
    recording: bool = False
    armable_inputs: tuple[str, ...] = ()
    intercom: IntercomCapabilities | None = None

    def trigger_for(self, kind: Kind, channel_id: str | None) -> TriggerLink | None:
        for trigger in self.triggers:
            if trigger.kind == kind and trigger.channel_id == channel_id:
                return trigger
        return None
