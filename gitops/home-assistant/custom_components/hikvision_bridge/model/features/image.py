"""Image settings of a channel (day/night, supplement light, colour, WDR, ...).

The device describes what it supports in ``/ISAPI/Image/channels/<ch>/capabilities``: ``opt``
lists for choices and ``min``/``max`` for levels. Current values come from the channel's image
document, which only contains the settings of the active modes; a setting whose element is
missing there is not adjustable right now. Writes go to the sub-resource of one section, so
only that section is sent back to the device.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

from ...wire.link import IsapiLink
from ...wire.xmlview import XmlNode
from . import patch_and_verify

Platform = Literal["select", "number", "switch"]

CAPS_PATH = "/ISAPI/Image/channels/{ch}/capabilities"
IMAGE_PATH = "/ISAPI/Image/channels/{ch}"


@dataclass(frozen=True, slots=True)
class ImageSetting:
    key: str
    platform: Platform
    section: str
    resource: str
    # Element paths below the section; the first one present is used (they differ per mode).
    fields: tuple[str, ...]
    enabled_default: bool = False
    seconds: bool = False
    # Which resource family serves the setting (see ``adjust.SOURCES``).
    source: str = "image"


def _s(
    key: str, platform: Platform, section: str, resource: str, *fields: str, **kw
) -> ImageSetting:
    return ImageSetting(key, platform, section, resource, fields, **kw)


SETTINGS: tuple[ImageSetting, ...] = (
    _s(
        "day_night", "select", "IrcutFilter", "IrcutFilter", "IrcutFilterType", enabled_default=True
    ),
    _s("day_night_sensitivity", "number", "IrcutFilter", "IrcutFilter", "nightToDayFilterLevel"),
    _s(
        "day_night_delay",
        "number",
        "IrcutFilter",
        "IrcutFilter",
        "nightToDayFilterTime",
        seconds=True,
    ),
    _s(
        "supplement_light",
        "select",
        "SupplementLight",
        "supplementLight",
        "supplementLightMode",
        enabled_default=True,
    ),
    _s(
        "light_brightness_control",
        "select",
        "SupplementLight",
        "supplementLight",
        "EventIntelligenceModeCfg/brightnessRegulatMode",
        "mixedLightBrightnessRegulatMode",
    ),
    _s(
        "white_light_brightness",
        "number",
        "SupplementLight",
        "supplementLight",
        "EventIntelligenceModeCfg/whiteLightBrightness",
        "whiteLightBrightness",
        enabled_default=True,
    ),
    _s(
        "ir_light_brightness",
        "number",
        "SupplementLight",
        "supplementLight",
        "EventIntelligenceModeCfg/irLightBrightness",
        "irLightBrightness",
        enabled_default=True,
    ),
    _s(
        "white_light_duration",
        "number",
        "SupplementLight",
        "supplementLight",
        "EventIntelligenceModeCfg/whiteLightTime",
        seconds=True,
    ),
    _s("brightness", "number", "Color", "color", "brightnessLevel", enabled_default=True),
    _s("contrast", "number", "Color", "color", "contrastLevel", enabled_default=True),
    _s("saturation", "number", "Color", "color", "saturationLevel", enabled_default=True),
    _s("sharpness", "number", "Sharpness", "sharpness", "SharpnessLevel", enabled_default=True),
    _s("wdr", "select", "WDR", "WDR", "mode", enabled_default=True),
    _s("wdr_level", "number", "WDR", "WDR", "WDRLevel"),
    _s("blc", "switch", "BLC", "BLC", "enabled"),
    _s("blc_area", "select", "BLC", "BLC", "BLCMode"),
    _s("hlc", "switch", "HLC", "HLC", "enabled"),
    _s("hlc_level", "number", "HLC", "HLC", "HLCLevel"),
    _s("noise_reduction", "select", "NoiseReduce", "noiseReduce", "mode"),
    _s("noise_reduction_level", "number", "NoiseReduce", "noiseReduce", "GeneralMode/generalLevel"),
    _s("defog", "select", "Dehaze", "dehaze", "DehazeMode"),
    _s("defog_level", "number", "Dehaze", "dehaze", "DehazeLevel"),
    _s("white_balance", "select", "WhiteBalance", "whiteBalance", "WhiteBalanceStyle"),
    _s("white_balance_red", "number", "WhiteBalance", "whiteBalance", "WhiteBalanceRed"),
    _s("white_balance_blue", "number", "WhiteBalance", "whiteBalance", "WhiteBalanceBlue"),
    _s("exposure_mode", "select", "Exposure", "exposure", "ExposureType"),
    _s("overexposure_suppression", "switch", "Exposure", "exposure", "OverexposeSuppress/enabled"),
    _s("shutter", "select", "Shutter", "shutter", "ShutterLevel"),
    _s("gain_limit", "number", "Gain", "gain", "GainLimit"),
    _s(
        "power_line_frequency",
        "select",
        "powerLineFrequency",
        "powerLineFrequency",
        "powerLineFrequencyMode",
    ),
    _s("image_flip", "switch", "ImageFlip", "imageFlip", "enabled"),
    _s("flip_direction", "select", "ImageFlip", "imageFlip", "ImageFlipStyle"),
    _s("stabilization", "switch", "EIS", "EIS", "enabled"),
    _s(
        "lens_correction",
        "switch",
        "LensDistortionCorrection",
        "lensDistortionCorrection",
        "enabled",
    ),
)
BY_KEY = {setting.key: setting for setting in SETTINGS}


@dataclass(frozen=True, slots=True)
class ImageCapability:
    """What a channel supports for one setting."""

    key: str
    options: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    # Display names for ``options`` when the device values are ids (e.g. alarm sounds).
    labels: tuple[str, ...] = ()

    @property
    def setting(self) -> ImageSetting:
        from .adjust import BY_KEY as ALL  # noqa: PLC0415 - the registry imports this module

        return ALL[self.key]


@dataclass(frozen=True, slots=True)
class ImageValue:
    """Current value of one setting and the element it was read from."""

    field: str
    value: str


def option_key(option: str) -> str:
    """Translatable form of a device option (``colorVuWhiteLight`` -> ``colorvuwhitelight``).

    Values that cannot be translation keys (shutter times like ``1/25``) stay as they are.
    """
    return option.lower() if re.fullmatch(r"[A-Za-z0-9_]+", option) else option


def _integer(raw: str | None) -> int | None:
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


def _capability(setting: ImageSetting, node: XmlNode) -> ImageCapability | None:
    options = tuple(node.options(""))
    if setting.platform == "switch":
        lowered = {o.lower() for o in options}
        return ImageCapability(setting.key) if {"true", "false"} <= lowered else None
    if setting.platform == "select":
        return ImageCapability(setting.key, options=options) if len(options) > 1 else None
    low, high = _integer(node.attrs.get("min")), _integer(node.attrs.get("max"))
    if low is None and options:
        numbers = [n for n in (_integer(o) for o in options) if n is not None]
        if numbers:
            low, high = min(numbers), max(numbers)
    if low is None or high is None or high <= low:
        return None
    return ImageCapability(setting.key, minimum=low, maximum=high)


def parse_capabilities(document: XmlNode | None) -> dict[str, ImageCapability]:
    if document is None:
        return {}
    found: dict[str, ImageCapability] = {}
    for setting in SETTINGS:
        for field in setting.fields:
            node = document.first(f"{setting.section}/{field}")
            if node is None:
                continue
            capability = _capability(setting, node)
            if capability is not None:
                found[setting.key] = capability
            break
    return found


def parse_values(document: XmlNode | None, keys: set[str] | None = None) -> dict[str, ImageValue]:
    if document is None:
        return {}
    values: dict[str, ImageValue] = {}
    for setting in SETTINGS:
        if keys is not None and setting.key not in keys:
            continue
        for field in setting.fields:
            text = document.text(f"{setting.section}/{field}")
            if text is not None:
                values[setting.key] = ImageValue(field, text)
                break
    return values


async def read_capabilities(link: IsapiLink, channel_id: str) -> dict[str, ImageCapability]:
    return parse_capabilities(await link.read_xml(CAPS_PATH.format(ch=channel_id), optional=True))


async def read_values(
    link: IsapiLink, channel_id: str, keys: set[str] | None = None
) -> dict[str, ImageValue] | None:
    document = await link.read_xml(IMAGE_PATH.format(ch=channel_id), optional=True)
    return parse_values(document, keys) if document is not None else None


async def write(link: IsapiLink, channel_id: str, key: str, field: str, value: str) -> None:
    """Change one element in the sub-resource of its section and read it back."""
    setting = BY_KEY[key]
    await patch_and_verify(
        link,
        f"{IMAGE_PATH.format(ch=channel_id)}/{setting.resource}",
        {field: value},
    )
