"""Enable/disable state of per-channel detections and whether an area is drawn."""

from __future__ import annotations

from dataclasses import dataclass

from ...wire.link import IsapiLink
from ...wire.xmlview import XmlNode, parse
from ..vocabulary import Kind
from . import patch_and_verify

# Where each switchable detection is configured, relative to /ISAPI, per channel.
_PATHS: dict[Kind, str] = {
    Kind.MOTION: "/ISAPI/System/Video/inputs/channels/{ch}/motionDetection",
    Kind.TAMPER: "/ISAPI/System/Video/inputs/channels/{ch}/tamperDetection",
    Kind.INTRUSION: "/ISAPI/Smart/FieldDetection/{ch}",
    Kind.LINE_CROSSING: "/ISAPI/Smart/LineDetection/{ch}",
    Kind.REGION_ENTRANCE: "/ISAPI/Smart/regionEntrance/{ch}",
    Kind.REGION_EXITING: "/ISAPI/Smart/regionExiting/{ch}",
    Kind.SCENE_CHANGE: "/ISAPI/Smart/SceneChangeDetection/{ch}",
    Kind.VIDEO_LOSS: "/ISAPI/System/Video/inputs/channels/{ch}/videoLoss",
}
# Watched by the recorder itself, not by the camera: always configured on the recorder.
RECORDER_ONLY: frozenset[Kind] = frozenset({Kind.VIDEO_LOSS})


@dataclass(frozen=True, slots=True)
class DetectionState:
    enabled: bool
    area_defined: bool | None
    target_filter: tuple[str, ...] = ()


def config_path(kind: Kind, channel_id: str) -> str | None:
    template = _PATHS.get(kind)
    return template.format(ch=channel_id) if template else None


def _has_points(nodes: list[XmlNode]) -> bool:
    for node in nodes:
        x = node.numeric("positionX", 0) or 0
        y = node.numeric("positionY", 0) or 0
        if x or y:
            return True
    return False


def interpret(kind: Kind, document: XmlNode) -> DetectionState:
    enabled = bool(document.flag("enabled", False))
    area: bool | None
    if kind == Kind.LINE_CROSSING:
        area = _has_points(list(document.walk("Coordinates")))
    elif kind in (Kind.INTRUSION, Kind.REGION_ENTRANCE, Kind.REGION_EXITING):
        area = _has_points(list(document.walk("RegionCoordinates")))
    elif kind == Kind.MOTION:
        area = None  # grid layouts are not decoded; the device default covers the frame
    else:
        area = None
    targets = document.text("MotionDetectionLayout/targetType") or ""
    return DetectionState(
        enabled=enabled,
        area_defined=area,
        target_filter=tuple(t.strip() for t in targets.split(",") if t.strip()),
    )


async def read(link: IsapiLink, kind: Kind, channel_id: str) -> DetectionState | None:
    path = config_path(kind, channel_id)
    if path is None:
        return None
    document = await link.read_xml(path, optional=True)
    return interpret(kind, document) if document is not None else None


async def switch(link: IsapiLink, kind: Kind, channel_id: str, on: bool) -> DetectionState:
    path = config_path(kind, channel_id)
    if path is None:
        raise ValueError(f"{kind} cannot be switched")
    document = await patch_and_verify(link, path, {"enabled": "true" if on else "false"})
    return interpret(kind, parse(document.text))
