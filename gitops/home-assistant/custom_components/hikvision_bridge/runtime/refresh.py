"""Polling of switch states, alarm I/O, channel status and storage, with availability hysteresis."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass, field
import time
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from ..const import (
    DOMAIN,
    FAILURES_BEFORE_UNAVAILABLE,
    FAST_INTERVAL,
    GRACE_BEFORE_UNAVAILABLE,
)
from ..model.discover import CLOUD_PATH, read_cloud_state
from ..model.features import adjust, detection, health, io, storage
from ..model.features.detection import DetectionState
from ..model.features.health import DeviceHealth
from ..model.features.image import ImageValue
from ..model.inventory import StorageUnit
from ..model.vocabulary import Kind
from ..wire.faults import BadCredentials, BridgeFault, LockedOut, Unreachable
from ..wire.scrub import scrubbed_logger

if TYPE_CHECKING:
    from .bridge import DeviceBridge

_LOGGER = scrubbed_logger(__name__)

# Multiples of the base interval (30 s).
DETECTION_EVERY = 2  # 60 s
CHANNELS_EVERY = 4  # 120 s
STORAGE_EVERY = 20  # 10 min


@dataclass(slots=True)
class Snapshot:
    detections: dict[tuple[str, Kind], DetectionState] = field(default_factory=dict)
    inputs: dict[str, bool | None] = field(default_factory=dict)
    outputs: dict[str, bool | None] = field(default_factory=dict)
    channels_online: dict[str, bool | None] = field(default_factory=dict)
    storage: list[StorageUnit] = field(default_factory=list)
    stream_encrypted: bool | None = None
    image: dict[str, dict[str, ImageValue]] = field(default_factory=dict)
    # Keyed by channel id; None is the device of the entry itself.
    health: dict[str | None, DeviceHealth] = field(default_factory=dict)
    recording: dict[str, bool] = field(default_factory=dict)
    # (channel id or None for the entry's device, input port id) -> armed
    armed: dict[tuple[str | None, str], bool] = field(default_factory=dict)
    channel_storage: dict[str, list[StorageUnit]] = field(default_factory=dict)
    call_status: str | None = None


def _put[K, V](target: dict[K, V], key: K, value: V | None) -> None:
    """Store a read value, or forget the key when the resource could not be read."""
    if value is None:
        target.pop(key, None)
    else:
        target[key] = value


class StatusRefresher(DataUpdateCoordinator[Snapshot]):
    """One coordinator per device entry. Different resources refresh at different cadences."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, bridge: DeviceBridge) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=FAST_INTERVAL,
            always_update=False,
        )
        self._bridge = bridge
        self._tick = 0
        self._failures = 0
        self._last_success = time.monotonic()
        self._force_full = True
        # Failed reads in a row per (resource, key); values outlive a few of them.
        self._misses: dict[tuple[str, object], int] = {}

    def request_full(self) -> None:
        """Make the next refresh read every resource (after writes or events)."""
        self._force_full = True

    async def _async_update_data(self) -> Snapshot:
        previous = self.data
        full = self._force_full or previous is None
        self._force_full = False
        due = lambda every: full or self._tick % every == 0  # noqa: E731
        self._tick += 1
        snapshot = (
            Snapshot()
            if previous is None
            else Snapshot(
                detections=dict(previous.detections),
                inputs=dict(previous.inputs),
                outputs=dict(previous.outputs),
                channels_online=dict(previous.channels_online),
                storage=list(previous.storage),
                stream_encrypted=previous.stream_encrypted,
                image=dict(previous.image),
                health=dict(previous.health),
                recording=dict(previous.recording),
                armed=dict(previous.armed),
                channel_storage=dict(previous.channel_storage),
                call_status=previous.call_status,
            )
        )
        try:
            await self._read_io(snapshot)
            if due(DETECTION_EVERY):
                await self._read_detections(snapshot)
            if due(CHANNELS_EVERY):
                await self._read_channels(snapshot)
                await self._read_cloud(snapshot)
                await self._read_image(snapshot)
                await self._read_recording(snapshot)
            if due(STORAGE_EVERY):
                await self._read_storage(snapshot)
                await self._read_channel_storage(snapshot)
                await self._read_health(snapshot)
                await self._read_armed(snapshot)
        except BadCredentials as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except LockedOut as err:
            self._bridge.report_lockout(err)
            raise UpdateFailed("device locked the account") from err
        except BridgeFault as err:
            return self._tolerate(err, previous)
        self._failures = 0
        self._last_success = time.monotonic()
        return snapshot

    def _tolerate(self, err: BridgeFault, previous: Snapshot | None) -> Snapshot:
        """Keep the last data through short outages (3 failures or 90 s)."""
        self._failures += 1
        quiet_for = time.monotonic() - self._last_success
        if (
            previous is not None
            and self._failures < FAILURES_BEFORE_UNAVAILABLE
            and quiet_for < GRACE_BEFORE_UNAVAILABLE.total_seconds()
        ):
            _LOGGER.debug(
                "Ignoring transient error %s (%d in a row)", type(err).__name__, self._failures
            )
            return previous
        raise UpdateFailed(f"{type(err).__name__}: {err}") from err

    async def _read_io(self, snapshot: Snapshot) -> None:
        profile = self._bridge.profile
        link = self._bridge.link
        ports = [*profile.inputs.values(), *profile.outputs.values()]
        results = await asyncio.gather(
            *(io.read_state(link, port) for port in ports), return_exceptions=True
        )
        for port, result in zip(ports, results, strict=True):
            target = snapshot.outputs if port.output else snapshot.inputs
            if isinstance(result, (BadCredentials, LockedOut)):
                raise result
            if isinstance(result, Unreachable) or (
                isinstance(result, BaseException) and not isinstance(result, BridgeFault)
            ):
                raise result
            key = ("io", port.port_id)
            if not isinstance(result, BaseException):
                self._misses.pop(key, None)
                target[port.port_id] = result
            elif self._missed(key):
                target[port.port_id] = None

    async def _read_detections(self, snapshot: Snapshot) -> None:
        jobs = []
        keys = []
        for channel in self._bridge.profile.channels.values():
            for kind in channel.switchable:
                link, remote = self._bridge.detection_route(channel.channel_id, kind)
                keys.append((channel.channel_id, kind))
                jobs.append(detection.read(link, kind, remote))
        results = await asyncio.gather(*jobs, return_exceptions=True)
        failures = 0
        for key, result in zip(keys, results, strict=True):
            if isinstance(result, (BadCredentials, LockedOut)):
                if self._bridge.is_main_link_fault(key[0]):
                    raise result
                self._bridge.report_channel_fault(key[0], result)
                snapshot.detections.pop(key, None)
            elif isinstance(result, BridgeFault):
                failures += 1
                if self._missed(("detection", key)):
                    snapshot.detections.pop(key, None)
            elif isinstance(result, BaseException):
                raise result
            else:
                self._settle("detection", snapshot.detections, key, True, result)
        if keys and failures == len(keys):
            raise next(r for r in results if isinstance(r, BridgeFault))

    async def _read_cloud(self, snapshot: Snapshot) -> None:
        """Follow the Hik-Connect settings, so that switching stream encryption takes effect."""
        if self._bridge.profile.cloud is None:
            return
        ok, document = await self._try_read(self._bridge.link.read_xml(CLOUD_PATH, optional=True))
        state = read_cloud_state(document) if ok else None
        if state is not None:
            self._bridge.update_cloud_state(state)
            snapshot.stream_encrypted = state.stream_encrypted

    async def _try_read[T](
        self, read: Awaitable[T], channel_id: str | None = None
    ) -> tuple[bool, T | None]:
        """Run one read; returns whether it worked and the value.

        Refused logins on the main link stop the refresh, as does an unreachable device for
        reads of the entry's device itself. Any other failure only fails this read.
        """
        try:
            return True, await read
        except (BadCredentials, LockedOut) as err:
            if channel_id is None or self._bridge.is_main_link_fault(channel_id):
                raise
            self._bridge.report_channel_fault(channel_id, err)
        except Unreachable:
            if channel_id is None:
                raise
            _LOGGER.debug("Channel %s unreachable", channel_id)
        except BridgeFault as err:
            _LOGGER.debug("Resource unreadable: %s", type(err).__name__)
        return False, None

    def _missed(self, key: tuple[str, object]) -> bool:
        """Count a failed read; True once the last value should be given up."""
        self._misses[key] = self._misses.get(key, 0) + 1
        return self._misses[key] >= FAILURES_BEFORE_UNAVAILABLE

    def _settle[K, V](
        self, resource: str, target: dict[K, V], key: K, ok: bool, value: V | None
    ) -> None:
        """Store a read value; through a few failed reads in a row keep the last one."""
        if ok:
            self._misses.pop((resource, key), None)
            _put(target, key, value)
        elif self._missed((resource, key)):
            target.pop(key, None)

    async def _read_image(self, snapshot: Snapshot) -> None:
        """Adjustable settings, one document per source and channel that offers any."""
        for channel in self._bridge.profile.channels.values():
            if not channel.image:
                continue
            link, remote = self._bridge.route(channel.channel_id)
            keys = set(channel.image)
            previous = snapshot.image.get(channel.channel_id, {})
            values: dict[str, ImageValue] = {}
            readable = failed = False
            for source in adjust.SOURCES.values():
                wanted = keys & {s.key for s in source.settings}
                if not wanted:
                    continue
                ok, found = await self._try_read(
                    source.read_values(link, remote, wanted), channel.channel_id
                )
                if not ok:
                    # Keep this source's last values while the other sources still answer.
                    failed = True
                    values.update({k: v for k, v in previous.items() if k in wanted})
                elif found is not None:
                    readable = True
                    values.update(found)
            if readable or not failed:
                self._settle("image", snapshot.image, channel.channel_id, True, values or None)
            else:
                self._settle("image", snapshot.image, channel.channel_id, False, None)

    async def _read_health(self, snapshot: Snapshot) -> None:
        bridge = self._bridge
        profile = bridge.profile
        if profile.health:
            ok, status = await self._try_read(
                health.read_status(bridge.link, memory_in_mb=profile.shape.records)
            )
            self._settle("health", snapshot.health, None, ok, status)
        for channel in profile.channels.values():
            link, _ = bridge.route(channel.channel_id)
            if not channel.health or link is bridge.link:
                continue
            ok, status = await self._try_read(
                health.read_status(link, memory_in_mb=False), channel.channel_id
            )
            self._settle("health", snapshot.health, channel.channel_id, ok, status)

    async def _read_recording(self, snapshot: Snapshot) -> None:
        if self._bridge.profile.recording:
            _, recording = await self._try_read(health.read_recording(self._bridge.link))
            if recording is not None:
                snapshot.recording = recording

    async def _read_armed(self, snapshot: Snapshot) -> None:
        bridge = self._bridge
        if bridge.profile.armable_inputs:
            ok, armed = await self._try_read(io.read_armed(bridge.link))
            for port_id in bridge.profile.armable_inputs:
                value = (armed or {}).get(port_id)
                self._settle("armed", snapshot.armed, (None, port_id), ok, value)
        for channel in bridge.profile.channels.values():
            link, _ = bridge.route(channel.channel_id)
            if not channel.armable_inputs or link is bridge.link:
                continue
            ok, armed = await self._try_read(io.read_armed(link), channel.channel_id)
            for port_id in channel.armable_inputs:
                value = (armed or {}).get(port_id)
                self._settle("armed", snapshot.armed, (channel.channel_id, port_id), ok, value)

    async def _read_storage(self, snapshot: Snapshot) -> None:
        ok, units = await self._try_read(storage.read(self._bridge.link))
        if ok:
            self._misses.pop(("storage", None), None)
            snapshot.storage = units or []
        elif self._missed(("storage", None)):
            snapshot.storage = []

    async def _read_channel_storage(self, snapshot: Snapshot) -> None:
        bridge = self._bridge
        for channel in bridge.profile.channels.values():
            link, _ = bridge.route(channel.channel_id)
            if not channel.storage or link is bridge.link:
                continue
            ok, units = await self._try_read(storage.read(link), channel.channel_id)
            self._settle("storage", snapshot.channel_storage, channel.channel_id, ok, units)

    async def _read_channels(self, snapshot: Snapshot) -> None:
        profile = self._bridge.profile
        if not profile.shape.records:
            return
        _, status = await self._try_read(
            self._bridge.link.read_xml(
                "/ISAPI/ContentMgmt/InputProxy/channels/status", optional=True
            )
        )
        if status is None:
            return
        for node in status.children("InputProxyChannelStatus"):
            channel_id = node.text("id")
            if channel_id:
                snapshot.channels_online[channel_id] = node.flag("online")
