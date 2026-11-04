"""Device status (uptime, CPU, memory, reboots) and per-channel recording of recorders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ...wire.link import IsapiLink
from ...wire.xmlview import XmlNode

STATUS_PATH = "/ISAPI/System/status"
WORKING_PATH = "/ISAPI/System/workingstatus?format=json"
# Some firmware puts a Unix timestamp into totalRebootCount; no real counter gets this high.
MAX_REBOOTS = 100_000
# Values in this range (2000-01-01 to 2100-01-01) are read as such a timestamp.
EPOCH_RANGE = (946_684_800, 4_102_444_800)


@dataclass(frozen=True, slots=True)
class DeviceHealth:
    uptime_seconds: int | None = None
    cpu_percent: int | None = None
    memory_percent: float | None = None
    reboots: int | None = None
    # A timestamp the device reported in place of the reboot count.
    reboots_timestamp: datetime | None = None


def _reboots(raw: float | None) -> tuple[int | None, datetime | None]:
    """Reboot count, or the timestamp some firmware reports in its place."""
    if raw is None or raw < 0:
        return None, None
    if raw <= MAX_REBOOTS:
        return int(raw), None
    if EPOCH_RANGE[0] <= raw < EPOCH_RANGE[1]:
        return None, datetime.fromtimestamp(int(raw), UTC)
    return None, None


def _number(raw: str | None) -> float | None:
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None


def parse_status(document: XmlNode | None, *, memory_in_mb: bool) -> DeviceHealth | None:
    """Read the status document.

    Recorders report used and available memory in MB, cameras use a different, undocumented
    scale; memory is therefore only given for recorders.
    """
    if document is None:
        return None
    uptime = _number(document.text("deviceUpTime"))
    cpu = _number(document.text("CPUList/CPU/cpuUtilization"))
    reboots, reboots_timestamp = _reboots(_number(document.text("totalRebootCount")))
    memory = None
    if memory_in_mb:
        used = _number(document.text("MemoryList/Memory/memoryUsage"))
        free = _number(document.text("MemoryList/Memory/memoryAvailable"))
        if used is not None and free is not None and used + free > 0:
            memory = round(used / (used + free) * 100, 1)
    if uptime is None and cpu is None and memory is None:
        return None
    return DeviceHealth(
        uptime_seconds=int(uptime) if uptime is not None else None,
        cpu_percent=int(cpu) if cpu is not None else None,
        memory_percent=memory,
        reboots=reboots,
        reboots_timestamp=reboots_timestamp,
    )


async def read_status(link: IsapiLink, *, memory_in_mb: bool) -> DeviceHealth | None:
    document = await link.read_xml(STATUS_PATH, optional=True)
    return parse_status(document, memory_in_mb=memory_in_mb)


def parse_recording(document: Any) -> dict[str, bool]:
    """Whether each recorder channel is recording right now (``record`` 1 = yes)."""
    status = document.get("WorkingStatus") if isinstance(document, dict) else None
    channels = status.get("ChanStatus") if isinstance(status, dict) else None
    recording: dict[str, bool] = {}
    for item in channels if isinstance(channels, list) else []:
        if isinstance(item, dict) and item.get("chanNo") is not None and "record" in item:
            recording[str(item["chanNo"])] = item["record"] == 1
    return recording


async def read_recording(link: IsapiLink) -> dict[str, bool] | None:
    document = await link.read_json(WORKING_PATH, optional=True)
    return parse_recording(document) if document is not None else None
