"""Turns device notifications into entity state, event entities and bus events.

Every router belongs to exactly one config entry; there is no lookup across entries.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import time
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later

from ..const import BUS_EVENT, DOMAIN, GRACE_BEFORE_UNAVAILABLE
from ..model.features import recordings
from ..model.vocabulary import DEVICE_WIDE, Kind, canonical
from ..wire.faults import BridgeFault
from ..wire.feed import FeedFrame

if TYPE_CHECKING:
    from .bridge import DeviceBridge

RECENT_PER_CHANNEL = 10
MAX_UNKNOWN_CHANNELS = 32
DEVICE_SCOPE = "device"


def signal_state(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_state"


def signal_trigger(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_trigger"


@dataclass(frozen=True, slots=True)
class Origin:
    """Where a feed's frames belong: which channel ids map to which entry channels."""

    name: str
    channel_map: dict[str, str] | None = None  # remote channel -> entry channel
    exclusive_channels: frozenset[str] = frozenset()  # entry channels served by other feeds

    def place(self, frame: FeedFrame) -> str | None:
        if frame.channel_id is None:
            return None
        if self.channel_map is not None:
            return self.channel_map.get(frame.channel_id, frame.channel_id)
        return frame.channel_id


@dataclass(slots=True)
class Hit:
    channel: str  # entry channel id or "device"
    kind: Kind
    frame: FeedFrame


class EventRouter:
    def __init__(self, hass: HomeAssistant, bridge: DeviceBridge, hold_seconds: float) -> None:
        self.hass = hass
        self._bridge = bridge
        self._hold = hold_seconds
        self._active: dict[tuple[str, str], float] = {}
        self._timers: dict[tuple[str, str], CALLBACK_TYPE] = {}
        self._targets: dict[tuple[str, str], float] = {}
        self._feeds_up: dict[str, bool] = {}
        self._feeds_down_since: dict[str, float] = {}
        self.recent: dict[str, deque[dict[str, Any]]] = {}
        self.unknown_channels: dict[str, int] = {}
        # Last plate notification per channel (ANPR), for the plate sensor.
        self.last_plate: dict[str, FeedFrame] = {}
        self.latency_ms: float | None = None

    # ---------------------------------------------------------- availability

    @callback
    def feed_state(self, name: str, connected: bool, fault: BridgeFault | None) -> None:
        self._feeds_up[name] = connected
        if connected:
            self._feeds_down_since.pop(name, None)
        else:
            self._feeds_down_since.setdefault(name, time.monotonic())
        async_dispatcher_send(self.hass, signal_state(self._bridge.entry.entry_id), None)
        if fault is not None:
            self._bridge.report_feed_fault(name, fault)

    def feeds_healthy(self) -> bool:
        """True while every feed is up or has been down for less than the grace period."""
        now = time.monotonic()
        grace = GRACE_BEFORE_UNAVAILABLE.total_seconds()
        for name, up in self._feeds_up.items():
            if not up and now - self._feeds_down_since.get(name, now) >= grace:
                return False
        return True

    def feed_connected(self, name: str) -> bool:
        return self._feeds_up.get(name, False)

    # ----------------------------------------------------------------- state

    def is_active(self, channel: str, key: str) -> bool:
        expiry = self._active.get((channel, key))
        return expiry is not None and expiry > time.monotonic()

    # ---------------------------------------------------------------- intake

    @callback
    def make_handler(self, origin: Origin) -> Callable[[FeedFrame], None]:
        def handle(frame: FeedFrame) -> None:
            self._intake(origin, frame)

        return handle

    @callback
    def _intake(self, origin: Origin, frame: FeedFrame) -> None:
        kind = canonical(frame.raw_type)
        known = True
        channel = origin.place(frame)
        if channel is not None and channel in origin.exclusive_channels:
            return  # another feed (direct camera link) owns this channel's events
        if kind == Kind.IO:
            scope = DEVICE_SCOPE
            key = f"io:{frame.port_id or frame.channel_id or '?'}"
        elif kind in DEVICE_WIDE or channel is None:
            scope = DEVICE_SCOPE
            key = kind.value
        else:
            scope = channel
            key = kind.value
            if channel not in self._bridge.profile.channels:
                self._note_unknown(channel)
                known = False

        if frame.device_time is not None:
            self.latency_ms = round((frame.received - frame.device_time).total_seconds() * 1000)
        if known:
            self._remember(scope, kind, frame)
        if scope != DEVICE_SCOPE and origin.channel_map is None:
            # Recordings the device's own events start show which clock it searches on.
            recordings.note_event(self._bridge.link, frame.received)

        if known and kind == Kind.ANPR and frame.plate is not None and frame.is_active:
            self.last_plate[scope] = frame

        if frame.is_active and not known:
            pass  # a device-made channel id gets no state of its own
        elif frame.is_active:
            if kind == Kind.DOORBELL and self.is_active(scope, key):
                return
            self._activate((scope, key))
            for target in frame.targets:
                self._activate((scope, f"target:{target}"))
            async_dispatcher_send(
                self.hass, signal_trigger(self._bridge.entry.entry_id), Hit(scope, kind, frame)
            )
        elif kind == Kind.IO:
            # An input reports its real state; detections end when the hold runs out.
            self._deactivate((scope, key))
        if kind == Kind.STORAGE:
            self._bridge.refresher.request_full()
        self._fire_bus_event(scope, kind, frame)
        async_dispatcher_send(self.hass, signal_state(self._bridge.entry.entry_id), (scope, key))

    def _note_unknown(self, channel: str) -> None:
        """Count an event of a channel the device never listed; the table stays small."""
        counts = self.unknown_channels
        if channel not in counts and len(counts) >= MAX_UNKNOWN_CHANNELS:
            del counts[next(iter(counts))]
        counts[channel] = counts.get(channel, 0) + 1

    def _activate(self, slot: tuple[str, str]) -> None:
        self._active[slot] = time.monotonic() + self._hold
        if (cancel := self._timers.pop(slot, None)) is not None:
            cancel()
        self._timers[slot] = async_call_later(self.hass, self._hold, self._expire_job(slot))

    def _deactivate(self, slot: tuple[str, str]) -> None:
        self._active.pop(slot, None)
        if (cancel := self._timers.pop(slot, None)) is not None:
            cancel()

    def _expire_job(self, slot: tuple[str, str]) -> Callable[[datetime], None]:
        @callback
        def release(_now: datetime) -> None:
            self._timers.pop(slot, None)
            if not self.is_active(*slot):
                self._active.pop(slot, None)
                async_dispatcher_send(self.hass, signal_state(self._bridge.entry.entry_id), slot)

        return release

    def _remember(self, scope: str, kind: Kind, frame: FeedFrame) -> None:
        bucket = self.recent.setdefault(scope, deque(maxlen=RECENT_PER_CHANNEL))
        bucket.append(
            {
                "kind": kind.value,
                "raw_type": frame.raw_type,
                "via": frame.extra.get("via"),
                "state": frame.state,
                "targets": list(frame.targets),
                "region": frame.region_id,
                "plate": frame.plate.number if frame.plate else None,
                "device_time": frame.device_time.isoformat() if frame.device_time else None,
                "received": frame.received.isoformat(),
            }
        )

    def _fire_bus_event(self, scope: str, kind: Kind, frame: FeedFrame) -> None:
        device_id = self._bridge.ha_device_id(None if scope == DEVICE_SCOPE else scope)
        self.hass.bus.async_fire(
            BUS_EVENT,
            {
                "device_id": device_id,
                "entry_id": self._bridge.entry.entry_id,
                "channel": None if scope == DEVICE_SCOPE else scope,
                "kind": kind.value,
                "raw_type": frame.raw_type,
                "state": frame.state,
                "targets": list(frame.targets),
                "region": frame.region_id,
                "port": frame.port_id,
                "anpr": frame.plate.as_dict() if frame.plate else None,
                "device_time": frame.device_time.isoformat() if frame.device_time else None,
                "received": frame.received.isoformat(),
            },
        )

    @callback
    def fire_doorbell(self) -> None:
        if self.is_active(DEVICE_SCOPE, Kind.DOORBELL.value):
            return
        frame = FeedFrame(raw_type="doorbell", state="active")
        self._intake(Origin("intercom"), frame)

    @callback
    def shutdown(self) -> None:
        for cancel in self._timers.values():
            cancel()
        self._timers.clear()
