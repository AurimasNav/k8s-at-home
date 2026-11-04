"""Diagnostics download with masking of credentials and identifiers."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_MAC, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ACCOUNT,
    CONF_CAMERA_SERIAL,
    CONF_ENTRY_KIND,
    CONF_SERIAL,
    KIND_CLOUD,
    OPT_STREAM_KEY,
    OPT_WEBHOOK_ID,
)
from .wire.scrub import MASK, scrub_text

TO_REDACT = {
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_HOST,
    CONF_SERIAL,
    CONF_MAC,
    CONF_CAMERA_SERIAL,
    CONF_ACCOUNT,
    OPT_WEBHOOK_ID,
    OPT_STREAM_KEY,
    "serial",
    "mac",
    "camera_serial",
    "camera_address",
    "verification_code",
    "token",
    "session",
    "plate",
}


def _deep_scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _deep_scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_deep_scrub(v) for v in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
            "subentries": [
                {
                    "type": s.subentry_type,
                    "data": async_redact_data(dict(s.data), TO_REDACT),
                }
                for s in entry.subentries.values()
            ],
        }
    }
    if entry.data.get(CONF_ENTRY_KIND) == KIND_CLOUD:
        runtime = getattr(entry, "runtime_data", None)
        if runtime is not None:
            result["cloud"] = runtime.diagnostics()
        return _deep_scrub(async_redact_data(result, TO_REDACT))

    bridge = getattr(entry, "runtime_data", None)
    if bridge is None:
        return result
    profile = asdict(bridge.profile)
    identity = profile["identity"]
    identity["serial"] = MASK
    identity["mac"] = MASK
    result["profile"] = profile
    result["link"] = {
        "auth": bridge.link.auth_scheme,
        "scheme": bridge.link.endpoint.scheme,
        "unsupported_paths": sorted(bridge.link.unsupported),
        "stats": asdict(bridge.link.stats),
    }
    result["routes"] = {
        channel: {"healthy": route.healthy, "events_from_camera": route.events_from_camera}
        for channel, route in bridge.routes.items()
    }
    result["feeds"] = {name: asdict(feed.stats) for name, feed in bridge.feeds.items()}
    result["events"] = {
        "recent": {scope: list(items) for scope, items in bridge.router.recent.items()},
        "unknown_channels": bridge.router.unknown_channels,
        "latency_ms": bridge.router.latency_ms,
    }
    result["audit"] = (
        [asdict(item) for item in bridge.report.items] if bridge.report is not None else None
    )
    data = bridge.refresher.data
    result["state"] = (
        {
            "detections": {f"{c}/{k}": asdict(v) for (c, k), v in data.detections.items()},
            "inputs": data.inputs,
            "outputs": data.outputs,
            "channels_online": data.channels_online,
            "storage": [asdict(u) for u in data.storage],
        }
        if data is not None
        else None
    )
    return _deep_scrub(async_redact_data(result, TO_REDACT))
