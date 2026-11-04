"""Strobe light and audible alarm of cameras with a deterrence light and speaker.

Both are JSON resources. The strobe light is per channel and also answered by recorders for
their channels; the audible alarm is a camera setting and needs a direct connection. The
capability documents give ranges, the strobe frequencies and the list of built-in sounds.
"""

from __future__ import annotations

from typing import Any

from ...wire.link import IsapiLink
from . import patch_json_and_verify
from .image import ImageCapability, ImageSetting, ImageValue

STROBE_PATH = "/ISAPI/Event/triggers/notifications/channels/{ch}/whiteLightAlarm"
SIREN_PATH = "/ISAPI/Event/triggers/notifications/AudioAlarm"
JSON = "?format=json"

STROBE: tuple[ImageSetting, ...] = (
    ImageSetting(
        "strobe_duration",
        "number",
        "WhiteLightAlarm",
        "",
        ("durationTime",),
        enabled_default=True,
        seconds=True,
        source="strobe",
    ),
    ImageSetting(
        "strobe_frequency",
        "select",
        "WhiteLightAlarm",
        "",
        ("frequency",),
        enabled_default=True,
        source="strobe",
    ),
)
SIREN: tuple[ImageSetting, ...] = (
    # Newer firmware keeps the built-in sound in alertAudioID, older in audioID.
    ImageSetting(
        "siren_sound",
        "select",
        "AudioAlarm",
        "",
        ("alertAudioID", "audioID"),
        enabled_default=True,
        source="siren",
    ),
    ImageSetting(
        "siren_volume",
        "number",
        "AudioAlarm",
        "",
        ("audioVolume",),
        enabled_default=True,
        source="siren",
    ),
    ImageSetting("siren_repeats", "number", "AudioAlarm", "", ("alarmTimes",), source="siren"),
)


def _range(node: Any) -> tuple[int, int] | None:
    if not isinstance(node, dict):
        return None
    try:
        low, high = int(node["@min"]), int(node["@max"])
    except (KeyError, TypeError, ValueError):
        return None
    return (low, high) if high > low else None


def _options(node: Any) -> tuple[str, ...]:
    raw = node.get("@opt", "") if isinstance(node, dict) else ""
    return tuple(part.strip() for part in str(raw).split(",") if part.strip())


def parse_strobe_capabilities(document: Any) -> dict[str, ImageCapability]:
    caps = document.get("WhiteLightAlarmCap") if isinstance(document, dict) else None
    if not isinstance(caps, dict):
        return {}
    found: dict[str, ImageCapability] = {}
    if span := _range(caps.get("durationTime")):
        found["strobe_duration"] = ImageCapability(
            "strobe_duration", minimum=span[0], maximum=span[1]
        )
    if len(options := _options(caps.get("frequency"))) > 1:
        found["strobe_frequency"] = ImageCapability("strobe_frequency", options=options)
    return found


def parse_siren_capabilities(document: Any) -> dict[str, ImageCapability]:
    caps = document.get("AudioAlarmCap") if isinstance(document, dict) else None
    if not isinstance(caps, dict):
        return {}
    found: dict[str, ImageCapability] = {}
    sounds: list[tuple[str, str]] = []
    for list_key, id_key, text_key in (
        ("AlertAudioTypeListCap", "alertAudioID", "alertAudioDescription"),
        ("audioTypeListCap", "audioID", "audioDescription"),
    ):
        for item in caps.get(list_key) or []:
            if isinstance(item, dict) and item.get(id_key) is not None:
                label = str(item.get(text_key) or item[id_key])
                sounds.append((str(item[id_key]), label))
        if sounds:
            break
    labels = [label for _, label in sounds]
    if len(sounds) > 1 and len(set(labels)) == len(labels):
        found["siren_sound"] = ImageCapability(
            "siren_sound",
            options=tuple(sound_id for sound_id, _ in sounds),
            labels=tuple(labels),
        )
    for key, field in (("siren_volume", "audioVolume"), ("siren_repeats", "alarmTimes")):
        if span := _range(caps.get(field)):
            found[key] = ImageCapability(key, minimum=span[0], maximum=span[1])
    return found


def parse_values(
    document: Any, root: str, settings: tuple[ImageSetting, ...], keys: set[str] | None
) -> dict[str, ImageValue]:
    body = document.get(root) if isinstance(document, dict) else None
    if not isinstance(body, dict):
        return {}
    values: dict[str, ImageValue] = {}
    for setting in settings:
        if keys is not None and setting.key not in keys:
            continue
        for field in setting.fields:
            if body.get(field) is not None:
                values[setting.key] = ImageValue(field, str(body[field]))
                break
    return values


def _caps_path(path: str) -> str:
    return f"{path}/capabilities{JSON}"


async def read_strobe_capabilities(link: IsapiLink, channel_id: str) -> dict[str, ImageCapability]:
    document = await link.read_json(_caps_path(STROBE_PATH.format(ch=channel_id)), optional=True)
    return parse_strobe_capabilities(document)


async def read_siren_capabilities(link: IsapiLink, channel_id: str) -> dict[str, ImageCapability]:
    return parse_siren_capabilities(await link.read_json(_caps_path(SIREN_PATH), optional=True))


async def read_strobe_values(
    link: IsapiLink, channel_id: str, keys: set[str] | None = None
) -> dict[str, ImageValue] | None:
    document = await link.read_json(STROBE_PATH.format(ch=channel_id) + JSON, optional=True)
    return parse_values(document, "WhiteLightAlarm", STROBE, keys) if document else None


async def read_siren_values(
    link: IsapiLink, channel_id: str, keys: set[str] | None = None
) -> dict[str, ImageValue] | None:
    document = await link.read_json(SIREN_PATH + JSON, optional=True)
    return parse_values(document, "AudioAlarm", SIREN, keys) if document else None


def _typed(key: str, value: str) -> Any:
    return value if key == "strobe_frequency" else int(value)


async def write_strobe(link: IsapiLink, channel_id: str, key: str, field: str, value: str) -> None:
    await patch_json_and_verify(
        link,
        STROBE_PATH.format(ch=channel_id) + JSON,
        "WhiteLightAlarm",
        {field: _typed(key, value)},
    )


async def write_siren(link: IsapiLink, channel_id: str, key: str, field: str, value: str) -> None:
    fields = ("alertAudioID", "audioID") if key == "siren_sound" else (field,)
    await patch_json_and_verify(
        link,
        SIREN_PATH + JSON,
        "AudioAlarm",
        {f: _typed(key, value) for f in fields},
        skip_missing=True,
    )
