"""Stable identifiers and the device tree."""

from __future__ import annotations

import re

from homeassistant.helpers.device_registry import DeviceInfo

from ..const import DOMAIN
from ..model.inventory import ChannelProfile, DeviceProfile

VENDOR = "Hikvision"
_UNSAFE = re.compile(r"[^a-z0-9]+")


def safe_key(value: str) -> str:
    return _UNSAFE.sub("_", value.lower()).strip("_")


def device_key(profile: DeviceProfile) -> str:
    """Key of the entry device: its serial number (MAC as fallback)."""
    return profile.identity.stable_id


def uses_channel_devices(profile: DeviceProfile) -> bool:
    """Recorders and multi-sensor cameras get one HA device per channel."""
    return profile.shape.records or len(profile.channels) > 1


def channel_key(profile: DeviceProfile, channel_id: str) -> str:
    """Channel devices are bound to the slot, never to the attached camera's serial.

    This keeps them distinct from a separately configured camera with the same serial and
    stable when a camera is replaced.
    """
    return f"{device_key(profile)}_ch{safe_key(channel_id)}"


def entity_uid(profile: DeviceProfile, channel_id: str | None, feature: str) -> str:
    scope = f"ch{safe_key(channel_id)}" if channel_id is not None else "device"
    return f"{device_key(profile)}_{scope}_{safe_key(feature)}"


def main_device_info(profile: DeviceProfile, base_url: str) -> DeviceInfo:
    identity = profile.identity
    info = DeviceInfo(
        identifiers={(DOMAIN, device_key(profile))},
        manufacturer=VENDOR,
        model=identity.model,
        name=identity.name,
        serial_number=identity.serial or None,
        sw_version=" ".join(p for p in (identity.firmware, identity.firmware_build) if p),
        hw_version=identity.hardware,
        configuration_url=base_url,
    )
    if identity.mac:
        info["connections"] = {("mac", identity.mac.lower())}
    return info


def channel_device_info(
    profile: DeviceProfile, channel: ChannelProfile, parent_device_id: str
) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, channel_key(profile, channel.channel_id))},
        manufacturer=VENDOR,
        model=channel.camera_model or "Video channel",
        name=channel.name,
        sw_version=channel.camera_firmware,
        via_device_id=parent_device_id,
    )


def entity_device_identifier(profile: DeviceProfile, channel_id: str | None) -> tuple[str, str]:
    """The registry identifier an entity of this channel attaches to."""
    if channel_id is not None and uses_channel_devices(profile):
        return (DOMAIN, channel_key(profile, channel_id))
    return (DOMAIN, device_key(profile))
