"""Canonical event kinds and the spellings devices use for them."""

from __future__ import annotations

from enum import StrEnum


class Kind(StrEnum):
    """Canonical event kinds."""

    MOTION = "motion"
    TAMPER = "tamper"
    VIDEO_LOSS = "video_loss"
    SCENE_CHANGE = "scene_change"
    INTRUSION = "intrusion"
    LINE_CROSSING = "line_crossing"
    REGION_ENTRANCE = "region_entrance"
    REGION_EXITING = "region_exiting"
    PIR = "pir"
    IO = "io"
    STORAGE = "storage"
    NETWORK = "network"
    ILLEGAL_ACCESS = "illegal_access"
    FACE = "face"
    ANPR = "anpr"
    AUDIO_EXCEPTION = "audio_exception"
    THERMOMETRY = "thermometry"
    DEFOCUS = "defocus"
    DOORBELL = "doorbell"
    OTHER = "other"


_SPELLINGS: dict[Kind, tuple[str, ...]] = {
    Kind.MOTION: ("vmd", "motiondetection", "motion"),
    Kind.TAMPER: ("tamperdetection", "shelteralarm", "tamper"),
    Kind.VIDEO_LOSS: ("videoloss",),
    Kind.SCENE_CHANGE: ("scenechangedetection", "scenechange"),
    Kind.INTRUSION: ("fielddetection", "intrusion"),
    Kind.LINE_CROSSING: ("linedetection", "linecrossing"),
    Kind.REGION_ENTRANCE: ("regionentrance",),
    Kind.REGION_EXITING: ("regionexiting",),
    Kind.PIR: ("pir", "pira"),
    Kind.IO: ("io", "alarminput"),
    Kind.STORAGE: ("diskfull", "diskerror", "storagedetection", "hdfull", "hderror"),
    Kind.NETWORK: ("nicbroken", "ipconflict"),
    Kind.ILLEGAL_ACCESS: ("illaccess", "illegalaccess"),
    Kind.FACE: ("facedetection", "facesnap", "facecapture", "facecontrast"),
    Kind.ANPR: ("anpr", "vehicledetection"),
    Kind.AUDIO_EXCEPTION: ("audioexception", "audiodetection"),
    Kind.THERMOMETRY: ("thermometry", "temperaturedetection"),
    Kind.DEFOCUS: ("defocus", "defocusdetection"),
    Kind.DOORBELL: ("doorbell", "ring", "ringcall", "bell"),
}

_LOOKUP: dict[str, Kind] = {
    spelling: kind for kind, spellings in _SPELLINGS.items() for spelling in spellings
}

# Kinds that belong to a video channel and can be switched on/off.
SWITCHABLE: tuple[Kind, ...] = (
    Kind.MOTION,
    Kind.TAMPER,
    Kind.INTRUSION,
    Kind.LINE_CROSSING,
    Kind.REGION_ENTRANCE,
    Kind.REGION_EXITING,
    Kind.SCENE_CHANGE,
    Kind.VIDEO_LOSS,
)

# Kinds that produce event entities (each trigger matters, not just on/off).
SMART: frozenset[Kind] = frozenset(
    {
        Kind.MOTION,
        Kind.INTRUSION,
        Kind.LINE_CROSSING,
        Kind.REGION_ENTRANCE,
        Kind.REGION_EXITING,
        Kind.FACE,
        Kind.ANPR,
    }
)

# Kinds that concern the device as a whole rather than one channel.
DEVICE_WIDE: frozenset[Kind] = frozenset(
    {Kind.STORAGE, Kind.NETWORK, Kind.ILLEGAL_ACCESS, Kind.DOORBELL}
)


def canonical(raw: str | None) -> Kind:
    """Map a device spelling to its canonical kind (case-insensitive)."""
    if not raw:
        return Kind.OTHER
    return _LOOKUP.get(raw.strip().lower(), Kind.OTHER)


def kinds_of(raw_names: list[str] | set[str]) -> set[Kind]:
    """Canonical kinds for a list of device spellings, dropping unknown ones."""
    return {canonical(name) for name in raw_names} - {Kind.OTHER}
