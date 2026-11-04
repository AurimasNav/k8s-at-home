"""Optional push path: the device posts notifications to a per-entry webhook."""

from __future__ import annotations

from aiohttp import web
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from ..const import DOMAIN, EVENT_PATH_BOTH, EVENT_PATH_WEBHOOK, OPT_EVENT_PATH, OPT_WEBHOOK_ID
from ..wire.feed import MultipartSplitter, Part, boundary_of, frame_from_part
from ..wire.scrub import scrubbed_logger
from .router import Origin

_LOGGER = scrubbed_logger(__name__)
WEBHOOK_FEED = "webhook"
MAX_BODY = 4 * 1024 * 1024


def webhook_wanted(entry: ConfigEntry) -> bool:
    return entry.options.get(OPT_EVENT_PATH) in (EVENT_PATH_WEBHOOK, EVENT_PATH_BOTH) and bool(
        entry.options.get(OPT_WEBHOOK_ID)
    )


@callback
def async_register_webhook(hass: HomeAssistant, entry: ConfigEntry) -> None:
    if not webhook_wanted(entry):
        return
    bridge = entry.runtime_data
    handler = bridge.router.make_handler(Origin(WEBHOOK_FEED))

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
            if item is None or isinstance(item, bytes) or item.heartbeat:
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
