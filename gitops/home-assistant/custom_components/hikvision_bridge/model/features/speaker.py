"""Speaker and microphone volume of a camera (two-way audio channel 1)."""

from __future__ import annotations

from ...wire.link import IsapiLink
from ...wire.xmlview import XmlNode
from . import patch_and_verify
from .image import ImageCapability, ImageSetting, ImageValue

PATH = "/ISAPI/System/TwoWayAudio/channels/1"

SETTINGS: tuple[ImageSetting, ...] = (
    ImageSetting(
        "speaker_volume",
        "number",
        "",
        "",
        ("speakerVolume",),
        enabled_default=True,
        source="speaker",
    ),
    ImageSetting(
        "microphone_volume",
        "number",
        "",
        "",
        ("microphoneVolume",),
        enabled_default=True,
        source="speaker",
    ),
)


def _span(caps: XmlNode | None, field: str) -> tuple[int, int]:
    """Range from the capabilities; volumes are 0-100 where a device does not say."""
    node = caps.first(field) if caps is not None else None
    try:
        low = int(node.attrs["min"]) if node is not None else 0
        high = int(node.attrs["max"]) if node is not None else 100
    except (KeyError, ValueError):
        return 0, 100
    return (low, high) if high > low else (0, 100)


def parse_capabilities(
    document: XmlNode | None, caps: XmlNode | None
) -> dict[str, ImageCapability]:
    if document is None:
        return {}
    found: dict[str, ImageCapability] = {}
    for setting in SETTINGS:
        field = setting.fields[0]
        if document.text(field) is None:
            continue
        low, high = _span(caps, field)
        found[setting.key] = ImageCapability(setting.key, minimum=low, maximum=high)
    return found


def parse_values(document: XmlNode | None, keys: set[str] | None = None) -> dict[str, ImageValue]:
    if document is None:
        return {}
    values: dict[str, ImageValue] = {}
    for setting in SETTINGS:
        if keys is not None and setting.key not in keys:
            continue
        text = document.text(setting.fields[0])
        if text is not None:
            values[setting.key] = ImageValue(setting.fields[0], text)
    return values


async def read_capabilities(link: IsapiLink, channel_id: str) -> dict[str, ImageCapability]:
    document = await link.read_xml(PATH, optional=True)
    if document is None:
        return {}
    caps = await link.read_xml(f"{PATH}/capabilities", optional=True)
    return parse_capabilities(document, caps)


async def read_values(
    link: IsapiLink, channel_id: str, keys: set[str] | None = None
) -> dict[str, ImageValue] | None:
    document = await link.read_xml(PATH, optional=True)
    return parse_values(document, keys) if document is not None else None


async def write(link: IsapiLink, channel_id: str, key: str, field: str, value: str) -> None:
    await patch_and_verify(link, PATH, {field: value})
