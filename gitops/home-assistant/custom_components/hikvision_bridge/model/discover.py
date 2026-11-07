"""Read-only discovery of a device: identity, shape, channels, capabilities."""

from __future__ import annotations

import asyncio
from collections import defaultdict

from ..wire.faults import NotIsapi
from ..wire.link import IsapiLink
from ..wire.scrub import scrubbed_logger
from ..wire.xmlview import XmlNode
from .features import detection, intercom, io, linkage, storage
from .ids import numeric_id
from .inventory import (
    ChannelProfile,
    ChannelSource,
    CloudLinkState,
    DeviceProfile,
    DeviceShape,
    Identity,
    StreamSpec,
)
from .vocabulary import SWITCHABLE, Kind, canonical

_LOGGER = scrubbed_logger(__name__)

_RECORDER_TYPES = {"nvr", "dvr", "hvr", "dvs", "hybridnvr", "netvideorecorder"}


def read_identity(info: XmlNode) -> Identity:
    firmware = info.text("firmwareVersion") or "unknown"
    serial = info.text("serialNumber") or ""
    if not serial and not info.text("macAddress"):
        raise NotIsapi("device reports neither serial number nor MAC address")
    return Identity(
        serial=serial,
        model=info.text("model") or "Hikvision device",
        name=info.text("deviceName") or info.text("model") or "Hikvision",
        firmware=firmware,
        firmware_build=info.text("firmwareReleasedDate"),
        mac=info.text("macAddress"),
        device_type=info.text("deviceType"),
        hardware=info.text("hardwareVersion"),
    )


async def read_identity_only(link: IsapiLink) -> Identity:
    info = await link.read_xml("/ISAPI/System/deviceInfo")
    if info is None:
        raise NotIsapi("no device information")
    return read_identity(info)


def _proxy_channels(listing: XmlNode | None, status: XmlNode | None) -> dict[str, ChannelProfile]:
    channels: dict[str, ChannelProfile] = {}
    if listing is None:
        return channels
    for node in listing.children("InputProxyChannel"):
        channel_id = numeric_id(node.text("id"))
        if not channel_id:
            continue
        source = node.first("sourceInputPortDescriptor")
        channels[channel_id] = ChannelProfile(
            channel_id=channel_id,
            name=node.text("name") or f"Channel {channel_id}",
            source=ChannelSource.PROXY,
            camera_address=source.text("ipAddress") or source.text("hostName") if source else None,
            camera_model=source.text("model") if source else None,
            camera_serial=source.text("serialNumber") if source else None,
            camera_firmware=source.text("firmwareVersion") if source else None,
        )
    if status is not None:
        for node in status.children("InputProxyChannelStatus"):
            channel = channels.get(node.text("id") or "")
            if channel is not None:
                channel.online = node.flag("online")
    return channels


def _local_channels(listing: XmlNode | None) -> dict[str, ChannelProfile]:
    channels: dict[str, ChannelProfile] = {}
    if listing is None:
        return channels
    for node in listing.children("VideoInputChannel"):
        channel_id = numeric_id(node.text("id"))
        if channel_id:
            channels[channel_id] = ChannelProfile(
                channel_id=channel_id,
                name=node.text("name") or f"Channel {channel_id}",
                source=ChannelSource.LOCAL,
                online=True,
            )
    return channels


def _streams(listing: XmlNode | None) -> dict[str, list[StreamSpec]]:
    by_channel: dict[str, list[StreamSpec]] = defaultdict(list)
    if listing is None:
        return by_channel
    nodes = [listing] if listing.name == "StreamingChannel" else listing.all("StreamingChannel")
    for node in nodes:
        stream_id = numeric_id(node.text("id"))
        if not stream_id or len(stream_id) < 3:
            continue
        if node.flag("enabled") is False:
            continue
        video = node.first("Video")
        by_channel[str(int(stream_id) // 100)].append(
            StreamSpec(
                stream_id=stream_id,
                codec=video.text("videoCodecType") if video else None,
                width=video.numeric("videoResolutionWidth") if video else None,
                height=video.numeric("videoResolutionHeight") if video else None,
            )
        )
    return by_channel


def _channel_kinds(document: XmlNode | None) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    if document is None:
        return result
    for node in document.children("ChannelEventCap"):
        channel_id = numeric_id(node.text("channelID"))
        if channel_id:
            result[channel_id] = set(node.options("eventType"))
    return result


def decide_shape(identity: Identity, proxy: int, local: int) -> DeviceShape:
    device_type = (identity.device_type or "").replace(" ", "").lower()
    if proxy and local:
        return DeviceShape.HYBRID_RECORDER
    if proxy or device_type in _RECORDER_TYPES:
        return DeviceShape.RECORDER
    if "intercom" in device_type or device_type in {"vis", "villa", "doorstation"}:
        return DeviceShape.INTERCOM
    if "securitycp" in device_type or "axpro" in device_type:
        return DeviceShape.ALARM_PANEL
    if local > 1:
        return DeviceShape.MULTI_CAMERA
    if local == 1 or device_type in {"ipcamera", "ipdome", "ipzoom", "ptz"}:
        return DeviceShape.CAMERA
    return DeviceShape.UNKNOWN


CLOUD_PATH = "/ISAPI/System/Network/EZVIZ"


def read_cloud_state(cloud: XmlNode | None) -> CloudLinkState | None:
    """Hik-Connect platform access settings, including stream encryption."""
    if cloud is None:
        return None
    # Older DVRs omit both flags; absence does not mean encryption is off.
    flags = (cloud.flag("streamEncrypteEnabled"), cloud.flag("streamEncryptEnabled"))
    encrypted = any(flags) if any(value is not None for value in flags) else None
    return CloudLinkState(
        enabled=bool(cloud.flag("enabled", False)),
        registered=bool(cloud.flag("registerStatus", False)),
        stream_encrypted=encrypted,
    )


async def discover(link: IsapiLink, *, probe_switches: bool = True) -> DeviceProfile:
    """Collect everything needed to build entities. Only GET requests are sent."""
    identity = await read_identity_only(link)

    (
        proxy_list,
        proxy_status,
        video_inputs,
        stream_list,
        proxy_streams,
        event_caps,
        trigger_list,
        inputs,
        outputs,
        cloud,
        accesses,
        user_check,
    ) = await asyncio.gather(
        link.read_xml("/ISAPI/ContentMgmt/InputProxy/channels", optional=True),
        link.read_xml("/ISAPI/ContentMgmt/InputProxy/channels/status", optional=True),
        link.read_xml("/ISAPI/System/Video/inputs/channels", optional=True),
        link.read_xml("/ISAPI/Streaming/channels", optional=True),
        link.read_xml("/ISAPI/ContentMgmt/StreamingProxy/channels", optional=True),
        link.read_xml("/ISAPI/Event/channels/capabilities", optional=True),
        link.read_xml(linkage.LIST_PATH, optional=True),
        link.read_xml("/ISAPI/System/IO/inputs", optional=True),
        link.read_xml("/ISAPI/System/IO/outputs", optional=True),
        link.read_xml(CLOUD_PATH, optional=True),
        link.read_xml("/ISAPI/Security/adminAccesses", optional=True),
        link.read_xml("/ISAPI/Security/userCheck", optional=True),
    )

    channels = _proxy_channels(proxy_list, proxy_status)
    local = _local_channels(video_inputs)
    streams = _streams(stream_list)
    # Older recorder firmware lists the streams of IP channels only through the proxy.
    for channel_id, proxied in _streams(proxy_streams).items():
        known = {s.stream_id for s in streams.get(channel_id, [])}
        if len(known) < 2:
            streams[channel_id] = [
                *streams.get(channel_id, []),
                *(s for s in proxied if s.stream_id not in known),
            ]
    if not channels and not local and not streams:
        # Some cameras only answer for individual streams.
        single = await link.read_xml("/ISAPI/Streaming/channels/101", optional=True)
        streams = _streams(single)
        if not streams:
            streams = {"1": []}
    if not channels and not local:
        # Cameras that do not list their inputs still stream: derive channels from streams.
        for channel_id in sorted(streams, key=lambda c: (len(c), c)):
            local[channel_id] = ChannelProfile(
                channel_id=channel_id,
                name=identity.name,
                source=ChannelSource.LOCAL,
                online=True,
            )
    shape = decide_shape(identity, len(channels), len(local))
    for channel_id, channel in local.items():
        channels.setdefault(channel_id, channel)

    triggers = linkage.parse_triggers(trigger_list)
    kinds_by_channel = _channel_kinds(event_caps)
    for channel_id, channel in channels.items():
        channel.streams = sorted(streams.get(channel_id, []), key=lambda s: s.rank)
        raw = kinds_by_channel.get(channel_id)
        if raw is None:
            raw = {t.raw_type for t in triggers if t.channel_id == channel_id}
        channel.raw_kinds = raw
        channel.kinds = {canonical(r) for r in raw} - {Kind.OTHER}
        if shape == DeviceShape.CAMERA and len(channels) == 1:
            channel.name = identity.name

    profile = DeviceProfile(
        identity=identity,
        shape=shape,
        channels=channels,
        inputs=io.parse_ports(inputs, output=False),
        outputs=io.parse_ports(outputs, output=True),
        triggers=triggers,
        storage=await storage.read(link),
    )
    profile.cloud = read_cloud_state(cloud)
    if accesses is not None:
        for node in accesses.children("AdminAccessProtocol"):
            protocol = (node.text("protocol") or "").upper()
            if protocol == "RTSP" and node.flag("enabled", True):
                profile.rtsp_port = node.numeric("portNo")
            elif protocol == "HTTPS":
                profile.https_enabled = node.flag("enabled")
    if user_check is not None:
        profile.default_password = bool(user_check.flag("isDefaultPassword", False))
        profile.risky_password = bool(user_check.flag("isRiskPassword", False))
    if shape == DeviceShape.INTERCOM or (identity.device_type or "").lower() == "vis":
        profile.intercom = await intercom.read_capabilities(link)

    if probe_switches:
        await probe_switchable(link, profile)
    return profile


async def probe_switchable(link: IsapiLink, profile: DeviceProfile) -> None:
    """Find which detections are readable (and therefore switchable) per channel."""

    async def check(channel: ChannelProfile, kind: Kind) -> None:
        state = await detection.read(link, kind, channel.channel_id)
        if state is not None:
            channel.switchable.add(kind)

    jobs = []
    for channel in profile.channels.values():
        if channel.online is False:
            continue
        wanted = [k for k in SWITCHABLE if k in channel.kinds] or [Kind.MOTION]
        jobs.extend(check(channel, kind) for kind in wanted)
    await asyncio.gather(*jobs)
