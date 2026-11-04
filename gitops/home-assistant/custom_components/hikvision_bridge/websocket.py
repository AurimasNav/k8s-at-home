"""Websocket commands of the recordings panel and card: cameras, timeline, playback."""

from __future__ import annotations

from datetime import datetime, timedelta
from functools import partial
from typing import Any

from homeassistant.components import websocket_api
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .const import CONF_ENTRY_KIND, DOMAIN, KIND_CLOUD
from .model.features import recordings
from .runtime import identity, sessions
from .runtime.bridge import DeviceBridge
from .runtime.playback import pick_source, sources
from .views import encode_token
from .wire.faults import BridgeFault, detail
from .wire.scrub import scrubbed_logger

_LOGGER = scrubbed_logger(__name__)

MAX_WINDOW = timedelta(days=2)
# Before the wanted moment: the recording that contains it may have started earlier.
LOOK_BACK = timedelta(hours=3)
LOOK_AHEAD = timedelta(hours=6)

_CHANNEL = {
    vol.Required("entry_id"): str,
    vol.Required("channel_id"): str,
    vol.Required("source"): str,
}


@callback
def async_register_commands(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_cameras)
    websocket_api.async_register_command(hass, ws_timeline)
    websocket_api.async_register_command(hass, ws_days)
    websocket_api.async_register_command(hass, ws_playback_start)
    websocket_api.async_register_command(hass, ws_playback_stop)


def _bridges(hass: HomeAssistant) -> list[DeviceBridge]:
    found = []
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        bridge = getattr(entry, "runtime_data", None)
        if entry.data.get(CONF_ENTRY_KIND) != KIND_CLOUD and isinstance(bridge, DeviceBridge):
            found.append(bridge)
    return found


def _bridge(hass: HomeAssistant, entry_id: str) -> DeviceBridge | None:
    return next((b for b in _bridges(hass) if b.entry.entry_id == entry_id), None)


def _time(value: str) -> datetime:
    moment = dt_util.parse_datetime(value)
    if moment is None:
        raise vol.Invalid("invalid time")
    return dt_util.as_utc(moment)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/cameras"})
@callback
def ws_cameras(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    registry = er.async_get(hass)
    cameras: list[dict[str, Any]] = []
    for bridge in _bridges(hass):
        profile = bridge.profile
        for channel in profile.channels.values():
            live = {}
            for rank, quality in ((1, "high"), (2, "low")):
                uid = identity.entity_uid(profile, channel.channel_id, f"stream_{rank}")
                entity_id = registry.async_get_entity_id(Platform.CAMERA, DOMAIN, uid)
                entry = registry.async_get(entity_id) if entity_id else None
                if entry is not None and entry.disabled_by is None:
                    live[quality] = entity_id
            main = channel.main_stream
            cameras.append(
                {
                    "entry_id": bridge.entry.entry_id,
                    "device": bridge.entry.title,
                    "channel_id": channel.channel_id,
                    "name": channel.name,
                    "online": channel.online is not False,
                    "sources": [s.key for s in sources(bridge, channel.channel_id)],
                    "live": live,
                    "codec": main.codec if main else None,
                    "resolution": f"{main.width}x{main.height}" if main and main.width else None,
                }
            )
    connection.send_result(msg["id"], {"cameras": cameras})


def _event_entities(hass: HomeAssistant, bridge: DeviceBridge, channel_id: str) -> dict[str, str]:
    """Detection binary sensors of a channel: entity id -> kind."""
    registry = er.async_get(hass)
    prefixes = {
        identity.entity_uid(bridge.profile, channel_id, "event_"): "",
        identity.entity_uid(bridge.profile, channel_id, "target_"): "target_",
    }
    found: dict[str, str] = {}
    for entry in er.async_entries_for_config_entry(registry, bridge.entry.entry_id):
        if entry.domain != Platform.BINARY_SENSOR or entry.disabled_by is not None:
            continue
        for prefix, label in prefixes.items():
            if entry.unique_id.startswith(prefix):
                found[entry.entity_id] = label + entry.unique_id[len(prefix) :]
    return found


async def _events(
    hass: HomeAssistant, entities: dict[str, str], start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """On-periods of the detection sensors from the recorder."""
    if not entities or "recorder" not in hass.config.components:
        return []
    from homeassistant.components.recorder import get_instance, history  # noqa: PLC0415

    states = await get_instance(hass).async_add_executor_job(
        partial(
            history.get_significant_states,
            hass,
            start,
            end,
            list(entities),
            significant_changes_only=False,
            minimal_response=False,
            no_attributes=True,
        )
    )
    events: list[dict[str, Any]] = []
    for entity_id, changes in states.items():
        began: datetime | None = None
        for state in changes:
            moment = state.last_changed
            if state.state == "on" and began is None:
                began = max(moment, start)
            elif state.state != "on" and began is not None:
                events.append(_event(entity_id, entities[entity_id], began, moment))
                began = None
        if began is not None:
            events.append(_event(entity_id, entities[entity_id], began, min(end, dt_util.utcnow())))
    return sorted(events, key=lambda e: e["start"])


def _event(entity_id: str, kind: str, start: datetime, end: datetime) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "kind": kind,
        "start": start.isoformat(),
        "end": max(end, start + timedelta(seconds=1)).isoformat(),
    }


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/timeline",
        **_CHANNEL,
        vol.Required("start"): str,
        vol.Required("end"): str,
    }
)
@websocket_api.async_response
async def ws_timeline(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    bridge = _bridge(hass, msg["entry_id"])
    try:
        start, end = _time(msg["start"]), _time(msg["end"])
    except vol.Invalid:
        connection.send_error(msg["id"], "invalid_format", "Invalid time")
        return
    if bridge is None or msg["channel_id"] not in bridge.profile.channels:
        connection.send_error(msg["id"], "not_found", "Unknown camera")
        return
    end = min(end, start + MAX_WINDOW)
    found: list[recordings.Recording] = []
    error = None
    source = pick_source(bridge, msg["channel_id"], msg["source"])
    if source is not None:
        try:
            found = await recordings.search(source.link, source.track, start, end)
        except BridgeFault as err:
            error = type(err).__name__
            _LOGGER.debug("No recordings list from the %s: %s", source.key, detail(err))
    events = await _events(hass, _event_entities(hass, bridge, msg["channel_id"]), start, end)
    connection.send_result(
        msg["id"],
        {
            "source": source.key if source else None,
            "error": error,
            "recordings": [
                {
                    "start": r.start.isoformat(),
                    "end": r.end.isoformat(),
                    "kind": r.kind,
                    "token": encode_token(r.playback_uri),
                }
                for r in found
            ],
            "events": events,
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/days",
        **_CHANNEL,
        vol.Required("year"): vol.All(int, vol.Range(min=2000, max=2100)),
        vol.Required("month"): vol.All(int, vol.Range(min=1, max=12)),
    }
)
@websocket_api.async_response
async def ws_days(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Days of a month with recordings for the date picker; None if the device can't tell."""
    bridge = _bridge(hass, msg["entry_id"])
    if bridge is None or msg["channel_id"] not in bridge.profile.channels:
        connection.send_error(msg["id"], "not_found", "Unknown camera")
        return
    days = None
    source = pick_source(bridge, msg["channel_id"], msg["source"])
    if source is not None:
        try:
            days = await recordings.recorded_days(
                source.link, source.track, msg["year"], msg["month"]
            )
        except BridgeFault as err:
            _LOGGER.debug("No recorded days from the %s: %s", source.key, detail(err))
    connection.send_result(msg["id"], {"days": days})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/playback/start",
        **_CHANNEL,
        vol.Required("time"): str,
        vol.Optional("quality", default=sessions.QUALITY_ORIGINAL): vol.In(sessions.QUALITIES),
        vol.Optional("replace"): str,
    }
)
@websocket_api.async_response
async def ws_playback_start(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    bridge = _bridge(hass, msg["entry_id"])
    channel_id = msg["channel_id"]
    source = pick_source(bridge, channel_id, msg["source"]) if bridge else None
    if bridge is None or source is None:
        connection.send_error(msg["id"], "not_found", "Unknown camera or recording source")
        return
    manager = sessions.manager(hass)
    key = (bridge.entry.entry_id, channel_id, source.key)
    # Claimed before the search: over a slow link an older search may finish later.
    ticket = manager.claim(key)
    try:
        moment = _time(msg["time"])
        found = await recordings.search(
            source.link, source.track, moment - LOOK_BACK, moment + LOOK_AHEAD
        )
    except vol.Invalid:
        connection.send_error(msg["id"], "invalid_format", "Invalid time")
        return
    except BridgeFault as err:
        connection.send_error(msg["id"], "playback_failed", type(err).__name__)
        return
    span = sessions.locate(found, moment)
    if span is None:
        connection.send_error(msg["id"], "no_recording", "No recording from this time on")
        return
    main = bridge.profile.channels[channel_id].main_stream
    try:
        session = await manager.start(
            source,
            key,
            span[0],
            span[1],
            msg["quality"],
            main.codec if main else None,
            msg.get("replace"),
            found,
            ticket,
        )
    except sessions.SessionError as err:
        connection.send_error(msg["id"], err.reason, err.reason)
        return
    except FileNotFoundError:
        connection.send_error(msg["id"], "no_ffmpeg", "ffmpeg is not available")
        return
    connection.send_result(
        msg["id"],
        {
            "session_id": session.session_id,
            "url": session.url,
            "start": session.start.isoformat(),
            "end": session.end.isoformat(),
            "quality": session.quality,
        },
    )


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/playback/stop", vol.Required("session_id"): str}
)
@websocket_api.async_response
async def ws_playback_stop(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    await sessions.manager(hass).stop(msg["session_id"])
    connection.send_result(msg["id"], {})
