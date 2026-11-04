"""All adjustable channel settings behind one interface: image, strobe, siren, speaker, park."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ...wire.link import IsapiLink
from . import image, park, signals, speaker
from .image import ImageCapability, ImageSetting, ImageValue


@dataclass(frozen=True, slots=True)
class Source:
    settings: tuple[ImageSetting, ...]
    # Camera settings that a recorder does not pass through: only read over a direct
    # connection (or from a camera configured on its own).
    direct_only: bool
    read_capabilities: Callable[[IsapiLink, str], Awaitable[dict[str, ImageCapability]]]
    read_values: Callable[
        [IsapiLink, str, set[str] | None], Awaitable[dict[str, ImageValue] | None]
    ]
    write: Callable[[IsapiLink, str, str, str, str], Awaitable[None]]


SOURCES: dict[str, Source] = {
    "image": Source(image.SETTINGS, False, image.read_capabilities, image.read_values, image.write),
    "strobe": Source(
        signals.STROBE,
        False,
        signals.read_strobe_capabilities,
        signals.read_strobe_values,
        signals.write_strobe,
    ),
    "siren": Source(
        signals.SIREN,
        True,
        signals.read_siren_capabilities,
        signals.read_siren_values,
        signals.write_siren,
    ),
    "speaker": Source(
        speaker.SETTINGS, True, speaker.read_capabilities, speaker.read_values, speaker.write
    ),
    "park": Source(park.SETTINGS, False, park.read_capabilities, park.read_values, park.write),
}
BY_KEY: dict[str, ImageSetting] = {
    setting.key: setting for source in SOURCES.values() for setting in source.settings
}


async def write(link: IsapiLink, channel_id: str, key: str, field: str, value: str) -> None:
    await SOURCES[BY_KEY[key].source].write(link, channel_id, key, field, value)
