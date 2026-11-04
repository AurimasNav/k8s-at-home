"""Pan, tilt, zoom and presets of a PTZ channel."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

from ...wire.faults import BridgeFault
from ...wire.link import IsapiLink
from ...wire.xmlview import XmlNode

BASE = "/ISAPI/PTZCtrl/channels/{}"
MOVE = "<PTZData><pan>{p}</pan><tilt>{t}</tilt><zoom>{z}</zoom></PTZData>"

# Direction -> (pan, tilt, zoom) sign; moved at STEP_SPEED for STEP_SECONDS.
DIRECTIONS: dict[str, tuple[int, int, int]] = {
    "pan_left": (-1, 0, 0),
    "pan_right": (1, 0, 0),
    "tilt_up": (0, 1, 0),
    "tilt_down": (0, -1, 0),
    "zoom_in": (0, 0, 1),
    "zoom_out": (0, 0, -1),
}
STEP_SPEED = 50
STEP_SECONDS = 0.5


@dataclass(frozen=True, slots=True)
class PtzCapability:
    pan_tilt: bool = False
    zoom: bool = False
    # Preset id -> shown name (unique).
    presets: dict[str, str] = field(default_factory=dict)

    @property
    def directions(self) -> list[str]:
        return [
            key
            for key, (pan, tilt, zoom) in DIRECTIONS.items()
            if (self.pan_tilt and (pan or tilt)) or (self.zoom and zoom)
        ]


def parse_presets(document: XmlNode | None) -> dict[str, str]:
    """Stored presets; unnamed ones get a numbered name, duplicates get their id."""
    presets: dict[str, str] = {}
    if document is None:
        return presets
    for node in document.children("PTZPreset"):
        preset_id = node.text("id")
        if not preset_id or node.flag("enabled", True) is False:
            continue
        name = node.text("presetName") or f"Preset {preset_id}"
        if name in presets.values():
            name = f"{name} ({preset_id})"
        presets[preset_id] = name
    return presets


def parse_capabilities(
    caps: XmlNode | None, presets: XmlNode | None, settings: XmlNode | None = None
) -> PtzCapability | None:
    """None for channels without PTZ (fixed cameras usually refuse the capabilities).

    Some fixed cameras describe a serial (RS-485) PTZ anyway: switched off (NVRs), or with
    pan and tilt marked unsupported in the channel settings. Their "zoom" is then the
    digital zoom of the picture, which serial PTZ commands don't reach.
    """
    if caps is None or caps.flag("enabled") is False:
        return None
    if settings is not None and settings.flag("enabled") is False:
        return None
    can_pan = settings.flag("panSupport") if settings is not None else None
    can_tilt = settings.flag("tiltSupport") if settings is not None else None
    can_zoom = settings.flag("zoomSupport") if settings is not None else None
    protocol = (settings.text("controlProtocol") or "") if settings is not None else ""
    if can_pan is False and can_tilt is False and protocol.upper() not in ("", "HIKVISION"):
        can_zoom = False
    found = PtzCapability(
        pan_tilt=caps.first("ContinuousPanTiltSpace") is not None
        and not (can_pan is False and can_tilt is False),
        zoom=caps.first("ContinuousZoomSpace") is not None and can_zoom is not False,
        presets=parse_presets(presets),
    )
    if not found.pan_tilt and not found.zoom and not found.presets:
        return None
    return found


async def read_capabilities(link: IsapiLink, channel_id: str) -> PtzCapability | None:
    base = BASE.format(channel_id)
    caps = await link.read_xml(f"{base}/capabilities", optional=True)
    if caps is None:
        return None
    settings = await link.read_xml(base, optional=True)
    presets = await link.read_xml(f"{base}/presets", optional=True)
    return parse_capabilities(caps, presets, settings)


async def move(
    link: IsapiLink, channel_id: str, pan: int, tilt: int, zoom: int, seconds: float
) -> None:
    """Move continuously for ``seconds``, then stop (also when the move fails)."""
    path = f"{BASE.format(channel_id)}/continuous"
    try:
        await link.write_xml(path, MOVE.format(p=pan, t=tilt, z=zoom))
        await asyncio.sleep(seconds)
    finally:
        with contextlib.suppress(BridgeFault):
            await link.write_xml(path, MOVE.format(p=0, t=0, z=0))


async def step(link: IsapiLink, channel_id: str, direction: str) -> None:
    pan, tilt, zoom = DIRECTIONS[direction]
    await move(
        link,
        channel_id,
        pan * STEP_SPEED,
        tilt * STEP_SPEED,
        zoom * STEP_SPEED,
        STEP_SECONDS,
    )


async def goto_preset(link: IsapiLink, channel_id: str, preset_id: str) -> None:
    await link.write_xml(f"{BASE.format(channel_id)}/presets/{preset_id}/goto", "")
