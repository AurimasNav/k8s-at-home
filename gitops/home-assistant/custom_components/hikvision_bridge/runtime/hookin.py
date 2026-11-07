"""Optional push path: the device posts notifications to a per-entry webhook."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aiohttp import web
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from ..const import DOMAIN, EVENT_PATH_BOTH, EVENT_PATH_WEBHOOK, OPT_EVENT_PATH, OPT_WEBHOOK_ID
from ..wire.feed import (
    FeedFrame,
    MultipartSplitter,
    Part,
    boundary_of,
    frame_from_part,
)
from ..wire.scrub import scrubbed_logger
from .router import Origin

_LOGGER = scrubbed_logger(__name__)
WEBHOOK_FEED = "webhook"
MAX_BODY = 4 * 1024 * 1024


def webhook_wanted(entry: ConfigEntry) -> bool:
    return entry.options.get(OPT_EVENT_PATH) in (EVENT_PATH_WEBHOOK, EVENT_PATH_BOTH) and bool(
        entry.options.get(OPT_WEBHOOK_ID)
    )


# Pushed notifications rarely carry heartbeats, so the device clock offset is often unknown.
# A device clock that is off (no NTP, wrong time zone) must not lose live events after a
# restart; only frames far older than the registration count as replays.
WEBHOOK_TOLERANCE = timedelta(days=1)


class FreshnessGate:
    """Drop replays of old notifications.

    A frame stamped (on the device clock, corrected by the offset heartbeats reveal) more
    than WEBHOOK_TOLERANCE before the webhook was registered is a replay and is dropped.
    Frames without a device time pass, as they do on the stream.
    """

    def __init__(self, since: datetime | None = None) -> None:
        self.since = since or datetime.now(UTC)
        self.offset = timedelta(0)
        self.dropped = 0

    def admit(self, frame: FeedFrame) -> bool:
        if frame.device_time is None:
            return True
        if frame.heartbeat:
            self.offset = frame.device_time - frame.received
            return True
        if frame.device_time - self.offset < self.since - WEBHOOK_TOLERANCE:
            self.dropped += 1
            return False
        return True


@callback
def async_register_webhook(hass: HomeAssistant, entry: ConfigEntry) -> None:
    if not webhook_wanted(entry):
        return
    bridge = entry.runtime_data
    handler = bridge.router.make_handler(Origin(WEBHOOK_FEED))
    gate = FreshnessGate()

    async def receive(hass: HomeAssistant, webhook_id: str, request: web.Request) -> web.Response:
        body = await request.content.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            return web.Response(status=413)
        ctype = request.headers.get("Content-Type", "")
        parts: list[Part]
        if ctype.lower().startswith("multipart/"):
            parts = list(MultipartSplitter(boundary_of(ctype)).push(body + b"\r\n--"))
        else:
            parts = [Part({"content-type": ctype}, body)]
        delivered = 0
        for part in parts:
            item = frame_from_part(part)
            if item is None or isinstance(item, bytes):
                continue
            if not gate.admit(item):
                _LOGGER.debug("Webhook dropped a stale notification (%d so far)", gate.dropped)
                continue
            if item.heartbeat:
                continue
            handler(item)
            delivered += 1
        _LOGGER.debug("Webhook delivered %d notification(s)", delivered)
        return web.Response(status=200)

    webhook.async_register(
        hass,
        DOMAIN,
        f"{entry.title} notifications",
        entry.options[OPT_WEBHOOK_ID],
        receive,
        local_only=True,
        allowed_methods=["POST", "PUT"],
    )
    bridge.router.feed_state(WEBHOOK_FEED, True, None)


@callback
def async_unregister_webhook(hass: HomeAssistant, entry: ConfigEntry) -> None:
    if webhook_wanted(entry):
        webhook.async_unregister(hass, entry.options[OPT_WEBHOOK_ID])
