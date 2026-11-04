"""Integration actions (services)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.util import dt as dt_util, slugify
import voluptuous as vol
from yarl import URL

from .const import DOMAIN, OPT_WEBHOOK_ID
from .model.features import linkage, patch_and_verify, ptz, recordings
from .model.vocabulary import Kind
from .wire.faults import BridgeFault
from .wire.scrub import scrub_text

FIELD_DRY_RUN = "dry_run"
FIELD_KINDS = "kinds"
FIELD_METHOD = "method"
FIELD_PATH = "path"
FIELD_BODY = "body"
FIELD_CONFIRM = "confirm_write"
FIELD_PAN = "pan"
FIELD_TILT = "tilt"
FIELD_ZOOM = "zoom"
FIELD_DURATION = "duration"
FIELD_PRESET = "preset"
FIELD_SLOT = "slot"
FIELD_START = "start"
FIELD_END = "end"
FIELD_SOURCE = "source"
FIELD_FORMAT = "format"
FIELD_TRIM = "trim"
FIELD_GROUP_ID = "group_id"
FIELD_GROUP_NAME = "group_name"
FIELD_RESOURCE_IDS = "resource_ids"
FIELD_SERIAL = "device_serial"

MAX_PERIOD = timedelta(days=1)
MAX_FILES = 50
MEDIA_FOLDER = DOMAIN

MAX_RESPONSE = 65536

_DEVICE = vol.Schema({vol.Required(ATTR_DEVICE_ID): cv.string}, extra=vol.ALLOW_EXTRA)


def _target_of(hass: HomeAssistant, device_id: str) -> tuple[Any, str | None]:
    """Return (bridge, channel id or None) for a device of this integration."""
    from .runtime.bridge import DeviceBridge  # noqa: PLC0415 - avoid import cycle at load

    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="unknown_device")
    entry = hass.config_entries.async_get_entry(device.config_entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="unknown_device")
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="device_not_ready")
    bridge = getattr(entry, "runtime_data", None)
    if not isinstance(bridge, DeviceBridge):
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="unknown_device")
    for channel_id, registered in bridge.device_ids.items():
        if registered == device_id and channel_id is not None:
            if registered == bridge.device_ids.get(None):
                return bridge, next(iter(bridge.profile.channels), None)
            return bridge, channel_id
    return bridge, None


def _fail(err: BridgeFault, key: str = "write_failed") -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders={"reason": type(err).__name__},
    )


async def _add_center_links(call: ServiceCall) -> ServiceResponse:
    bridge, channel = _target_of(call.hass, call.data[ATTR_DEVICE_ID])
    kinds = {Kind(k) for k in call.data.get(FIELD_KINDS, [])}
    dry_run = call.data[FIELD_DRY_RUN]
    changed: list[str] = []
    for trigger in bridge.profile.triggers:
        if trigger.notifies_center or trigger.kind is Kind.OTHER:
            continue
        if channel is not None and trigger.channel_id != channel:
            continue
        if kinds and trigger.kind not in kinds:
            continue
        try:
            if await linkage.ensure_center(bridge.link, trigger.trigger_id, dry_run=dry_run):
                changed.append(trigger.trigger_id)
        except BridgeFault as err:
            raise _fail(err) from err
    if not dry_run and changed:
        call.hass.config_entries.async_schedule_reload(bridge.entry.entry_id)
    return {"dry_run": dry_run, "triggers": changed}


async def _isapi_request(call: ServiceCall) -> ServiceResponse:
    bridge, _ = _target_of(call.hass, call.data[ATTR_DEVICE_ID])
    method = call.data[FIELD_METHOD]
    path = call.data[FIELD_PATH]
    if not path.startswith("/ISAPI/"):
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="bad_path")
    if method != "GET" and not call.data.get(FIELD_CONFIRM):
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="confirm_needed")
    try:
        reply = await bridge.link.call(method, path, body=call.data.get(FIELD_BODY))
    except BridgeFault as err:
        return {"ok": False, "error": type(err).__name__, "detail": scrub_text(str(err))}
    text = reply.text if not reply.content_type.startswith("image/") else "<binary>"
    return {
        "ok": True,
        "status": reply.status,
        "content_type": reply.content_type,
        "body": scrub_text(text[:MAX_RESPONSE]),
    }


async def _ptz_move(call: ServiceCall) -> None:
    bridge, channel = _target_of(call.hass, call.data[ATTR_DEVICE_ID])
    if channel is None:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="channel_needed")
    link, remote = bridge.route(channel)
    try:
        await ptz.move(
            link,
            remote,
            call.data[FIELD_PAN],
            call.data[FIELD_TILT],
            call.data[FIELD_ZOOM],
            call.data[FIELD_DURATION],
        )
    except BridgeFault as err:
        raise _fail(err) from err


async def _ptz_preset(call: ServiceCall) -> None:
    bridge, channel = _target_of(call.hass, call.data[ATTR_DEVICE_ID])
    if channel is None:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="channel_needed")
    link, remote = bridge.route(channel)
    try:
        await ptz.goto_preset(link, remote, str(call.data[FIELD_PRESET]))
    except BridgeFault as err:
        raise _fail(err) from err


async def _set_notification_host(call: ServiceCall) -> ServiceResponse:
    bridge, _ = _target_of(call.hass, call.data[ATTR_DEVICE_ID])
    if not call.data.get(FIELD_CONFIRM):
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="confirm_needed")
    webhook_id = bridge.entry.options.get(OPT_WEBHOOK_ID)
    if not webhook_id:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="webhook_off")
    try:
        base = get_url(call.hass, prefer_external=False, allow_cloud=False)
    except NoURLAvailableError as err:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_url") from err
    url = URL(base)
    slot = call.data[FIELD_SLOT]
    path = f"/ISAPI/Event/notification/httpHosts/{slot}"
    edits = {
        "url": f"/api/webhook/{webhook_id}",
        "protocolType": url.scheme.upper(),
        "addressingFormatType": "ipaddress"
        if (url.host or "").replace(".", "").isdigit()
        else "hostname",
        "portNo": str(url.port),
    }
    edits["ipAddress" if edits["addressingFormatType"] == "ipaddress" else "hostName"] = (
        url.host or ""
    )
    try:
        await patch_and_verify(bridge.link, path, edits)
    except BridgeFault as err:
        raise _fail(err) from err
    return {"slot": slot, "target": f"{url.scheme}://{url.host}:{url.port}/api/webhook/***"}


def _period(call: ServiceCall) -> tuple[datetime, datetime]:
    start = dt_util.as_utc(call.data[FIELD_START])
    end = dt_util.as_utc(call.data[FIELD_END])
    if end <= start or end - start > MAX_PERIOD:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="bad_period")
    return start, end


async def _find_recordings(call: ServiceCall) -> tuple[Any, str, Any, list]:
    """Bridge, channel, source and recordings of the requested period.

    With the automatic source, the camera's memory card is searched when the recorder has
    nothing for the period.
    """
    from .runtime import playback  # noqa: PLC0415 - avoid import cycle at load

    bridge, channel = _target_of(call.hass, call.data[ATTR_DEVICE_ID])
    if channel is None:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="channel_needed")
    start, end = _period(call)
    wanted = call.data[FIELD_SOURCE]
    candidates = [
        s for s in playback.sources(bridge, channel) if wanted in (playback.SOURCE_AUTO, s.key)
    ]
    if not candidates:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_recordings")
    for source in candidates:
        try:
            found = await recordings.search(source.link, source.track, start, end)
        except BridgeFault as err:
            raise _fail(err, "read_failed") from err
        if found:
            return bridge, channel, source, found
    return bridge, channel, candidates[0], []


async def _search_recordings(call: ServiceCall) -> ServiceResponse:
    _, _, source, found = await _find_recordings(call)
    return {
        "source": source.key,
        "recordings": [
            {
                "start": r.start.isoformat(),
                "end": r.end.isoformat(),
                "kind": r.kind,
                "size": r.size,
                "locked": r.locked,
            }
            for r in found
        ],
    }


async def _download_recording(call: ServiceCall) -> ServiceResponse:
    from .runtime import playback  # noqa: PLC0415 - avoid import cycle at load

    hass = call.hass
    bridge, channel, source, found = await _find_recordings(call)
    if not found:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_recordings")
    if len(found) > MAX_FILES:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="too_many_recordings",
            translation_placeholders={"count": str(len(found)), "limit": str(MAX_FILES)},
        )
    fmt = call.data[FIELD_FORMAT]
    start, end = _period(call)
    root = Path(hass.config.media_dirs.get("local") or hass.config.path("media"))
    folder = root / MEDIA_FOLDER / slugify(bridge.entry.title)
    if not hass.config.is_allowed_path(str(folder)):
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="media_not_allowed")
    await hass.async_add_executor_job(lambda: folder.mkdir(parents=True, exist_ok=True))
    name = slugify(bridge.profile.channels[channel].name) or f"channel_{channel}"
    saved: list[str] = []
    for recording in found:
        stamp = dt_util.as_local(recording.start).strftime("%Y%m%d_%H%M%S")
        target = folder / f"{name}_{stamp}.{playback.EXTENSION[fmt]}"
        partial = target.with_name(f".{target.name}.part")
        offset, duration = (
            playback.trim(recording, start, end) if call.data[FIELD_TRIM] else (None, None)
        )
        try:
            async with playback.open_recording(source, recording.playback_uri) as (
                head,
                chunks,
            ):
                if fmt == playback.FORMAT_ORIGINAL:
                    await _save_chunks(hass, chunks, partial)
                else:
                    args = playback.ffmpeg_args(
                        fmt, head.codec, target=str(partial), offset=offset, duration=duration
                    )
                    await playback.run_ffmpeg(playback.ffmpeg_binary(hass), args, chunks)
        except BaseException as err:
            await hass.async_add_executor_job(_remove, partial)
            raise _download_fault(err) from err
        await hass.async_add_executor_job(partial.replace, target)
        saved.append(str(target))
    return {"source": source.key, "files": saved}


def _remove(path: Path) -> None:
    path.unlink(missing_ok=True)


def _download_fault(err: BaseException) -> BaseException:
    from .runtime import playback  # noqa: PLC0415 - avoid import cycle at load

    keys = (
        (playback.RecordingEncrypted, "recording_encrypted"),
        (playback.NoRecording, "conversion_failed"),
        (FileNotFoundError, "no_ffmpeg"),
    )
    for kind, key in keys:
        if isinstance(err, kind):
            return HomeAssistantError(translation_domain=DOMAIN, translation_key=key)
    if isinstance(err, BridgeFault):
        return _fail(err, "read_failed")
    return err


async def _save_chunks(hass: HomeAssistant, chunks: Any, path: Path) -> None:
    handle = await hass.async_add_executor_job(path.open, "wb")
    try:
        async for chunk in chunks:
            await hass.async_add_executor_job(handle.write, chunk)
    finally:
        await hass.async_add_executor_job(handle.close)


def _cloud_target_of(
    hass: HomeAssistant, device_id: str | None = None, serial: str | None = None
) -> tuple[Any, Any]:
    """(CloudBridge, CloudDevice) of a cloud service call, by device id or serial."""
    from .cloud_entry import CLOUD_PREFIX, CloudBridge  # noqa: PLC0415

    if device_id and (device_entry := dr.async_get(hass).async_get(device_id)) is not None:
        entry = hass.config_entries.async_get_entry(device_entry.config_entry_id)
        if entry is not None and isinstance(
            bridge := getattr(entry, "runtime_data", None), CloudBridge
        ):
            for domain, ident in device_entry.identifiers:
                if domain == DOMAIN and ident.startswith(CLOUD_PREFIX):
                    key = ident.removeprefix(CLOUD_PREFIX)
                    if device := (bridge.refresher.data or {}).get(key):
                        return bridge, device

    if serial:
        for entry in hass.config_entries.async_entries(DOMAIN):
            bridge = getattr(entry, "runtime_data", None)
            if entry.state is ConfigEntryState.LOADED and isinstance(bridge, CloudBridge):
                for device in (bridge.refresher.data or {}).values():
                    if device.serial.lower() == serial.lower():
                        return bridge, device

    raise ServiceValidationError(translation_domain=DOMAIN, translation_key="unknown_device")


def _cloud_failed(err: Exception) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="cloud_failed",
        translation_placeholders={"reason": getattr(err, "reason", str(err))},
    )


async def _cloud_create_area(call: ServiceCall) -> ServiceResponse:
    from .cloud.account import CloudError  # noqa: PLC0415
    from .cloud.actions import create_area  # noqa: PLC0415

    bridge, device = _cloud_target_of(
        call.hass, call.data.get(ATTR_DEVICE_ID), call.data.get(FIELD_SERIAL)
    )
    group_name = call.data[FIELD_GROUP_NAME]
    resource_ids = [str(r) for r in call.data[FIELD_RESOURCE_IDS]]
    try:
        new_id = await create_area(bridge.account, device.serial, group_name, resource_ids)
    except CloudError as err:
        raise _cloud_failed(err) from err
    await bridge.refresher.async_request_refresh()
    return {"group_id": new_id}


async def _cloud_update_area(call: ServiceCall) -> ServiceResponse:
    from .cloud.account import CloudError  # noqa: PLC0415
    from .cloud.actions import update_area  # noqa: PLC0415

    bridge, device = _cloud_target_of(
        call.hass, call.data.get(ATTR_DEVICE_ID), call.data.get(FIELD_SERIAL)
    )
    group_id = int(call.data[FIELD_GROUP_ID])
    group_name = call.data[FIELD_GROUP_NAME]
    resource_ids = [str(r) for r in call.data[FIELD_RESOURCE_IDS]]
    previous = next((a for a in device.areas if a.group_id == group_id), None)
    try:
        new_id = await update_area(
            bridge.account, device.serial, group_id, group_name, resource_ids, previous
        )
    except CloudError as err:
        raise _cloud_failed(err) from err
    await bridge.refresher.async_request_refresh()
    return {"group_id": new_id}


async def _cloud_delete_area(call: ServiceCall) -> None:
    from .cloud.account import CloudError  # noqa: PLC0415
    from .cloud.actions import delete_area  # noqa: PLC0415

    bridge, device = _cloud_target_of(
        call.hass, call.data.get(ATTR_DEVICE_ID), call.data.get(FIELD_SERIAL)
    )
    group_id = int(call.data[FIELD_GROUP_ID])
    try:
        await delete_area(bridge.account, device.serial, group_id)
    except CloudError as err:
        raise _cloud_failed(err) from err
    await bridge.refresher.async_request_refresh()


def async_register_services(hass: HomeAssistant) -> None:
    hass.services.async_register(
        DOMAIN,
        "add_center_links",
        _add_center_links,
        schema=_DEVICE.extend(
            {
                vol.Optional(FIELD_DRY_RUN, default=True): cv.boolean,
                vol.Optional(FIELD_KINDS, default=[]): vol.All(
                    cv.ensure_list, [vol.In([k.value for k in Kind])]
                ),
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "isapi_request",
        _isapi_request,
        schema=_DEVICE.extend(
            {
                vol.Optional(FIELD_METHOD, default="GET"): vol.In(["GET", "PUT", "POST", "DELETE"]),
                vol.Required(FIELD_PATH): cv.string,
                vol.Optional(FIELD_BODY): cv.string,
                vol.Optional(FIELD_CONFIRM, default=False): cv.boolean,
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        "ptz_move",
        _ptz_move,
        schema=_DEVICE.extend(
            {
                vol.Optional(FIELD_PAN, default=0): vol.All(vol.Coerce(int), vol.Range(-100, 100)),
                vol.Optional(FIELD_TILT, default=0): vol.All(vol.Coerce(int), vol.Range(-100, 100)),
                vol.Optional(FIELD_ZOOM, default=0): vol.All(vol.Coerce(int), vol.Range(-100, 100)),
                vol.Optional(FIELD_DURATION, default=0.5): vol.All(
                    vol.Coerce(float), vol.Range(0.1, 10)
                ),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "ptz_preset",
        _ptz_preset,
        schema=_DEVICE.extend(
            {vol.Required(FIELD_PRESET): vol.All(vol.Coerce(int), vol.Range(1, 300))}
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "set_notification_host",
        _set_notification_host,
        schema=_DEVICE.extend(
            {
                vol.Optional(FIELD_SLOT, default=1): vol.All(vol.Coerce(int), vol.Range(1, 8)),
                vol.Required(FIELD_CONFIRM): cv.boolean,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    period = _DEVICE.extend(
        {
            vol.Required(FIELD_START): cv.datetime,
            vol.Required(FIELD_END): cv.datetime,
            vol.Optional(FIELD_SOURCE, default="auto"): vol.In(["auto", "recorder", "card"]),
        }
    )
    hass.services.async_register(
        DOMAIN,
        "search_recordings",
        _search_recordings,
        schema=period,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        "download_recording",
        _download_recording,
        schema=period.extend(
            {
                vol.Optional(FIELD_FORMAT, default="mp4"): vol.In(
                    ["mp4", "h264", "ts", "original"]
                ),
                vol.Optional(FIELD_TRIM, default=True): cv.boolean,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    cloud_area_base = {
        vol.Optional(ATTR_DEVICE_ID): cv.string,
        vol.Optional(FIELD_SERIAL): cv.string,
    }
    hass.services.async_register(
        DOMAIN,
        "cloud_create_area",
        _cloud_create_area,
        schema=vol.Schema(
            {
                **cloud_area_base,
                vol.Required(FIELD_GROUP_NAME): cv.string,
                vol.Required(FIELD_RESOURCE_IDS): vol.All(cv.ensure_list, [cv.string]),
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "cloud_update_area",
        _cloud_update_area,
        schema=vol.Schema(
            {
                **cloud_area_base,
                vol.Required(FIELD_GROUP_ID): vol.Coerce(int),
                vol.Required(FIELD_GROUP_NAME): cv.string,
                vol.Required(FIELD_RESOURCE_IDS): vol.All(cv.ensure_list, [cv.string]),
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "cloud_delete_area",
        _cloud_delete_area,
        schema=vol.Schema(
            {
                **cloud_area_base,
                vol.Required(FIELD_GROUP_ID): vol.Coerce(int),
            }
        ),
    )
