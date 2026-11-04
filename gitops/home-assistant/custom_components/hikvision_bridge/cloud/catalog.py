"""Device list of a Hik-Connect account, reduced to what the integration shows.

Connection details (local/WAN addresses, ports) and the stream key are deliberately
never copied out of the raw response.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PAGE_PATH = "/v3/userdevices/v1/devices/pagelist"
PAGE_SIZE = 50
PAGE_FILTER = "STATUS,CHANNEL,FEATURE"
MAX_PAGES = 10

# Model prefixes of door stations / indoor stations. Intercom detection is heuristic.
_INTERCOM_PREFIXES = ("DS-KV", "DS-KD", "DS-KB", "DS-KH", "DS-KIS")
_INTERCOM_WORDS = ("vis", "intercom", "doorbell")


@dataclass(slots=True, frozen=True)
class CloudArea:
    group_id: int
    device_serial: str
    name: str
    mode: int  # 0=disarmed, 1=armed, 2=armed-silent
    camera_ids: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class CloudDevice:
    serial: str
    full_serial: str
    name: str
    model: str
    firmware: str | None
    category: str
    online: bool
    channels: int
    stream_encrypted: bool | None
    areas: tuple[CloudArea, ...] = ()

    @property
    def key(self) -> str:
        """Same normalisation as local device keys, so both land on one registry device."""
        source = self.full_serial or self.serial
        return "".join(ch for ch in source if ch.isalnum()).lower()

    @property
    def is_intercom(self) -> bool:
        model = self.model.upper()
        if model.startswith(_INTERCOM_PREFIXES):
            return True
        category = self.category.lower()
        return any(word in category for word in _INTERCOM_WORDS)

    def with_areas(self, areas: tuple[CloudArea, ...]) -> CloudDevice:
        return CloudDevice(
            serial=self.serial,
            full_serial=self.full_serial,
            name=self.name,
            model=self.model,
            firmware=self.firmware,
            category=self.category,
            online=self.online,
            channels=self.channels,
            stream_encrypted=self.stream_encrypted,
            areas=areas,
        )


def parse_areas(payload: dict[str, Any], serial: str) -> list[CloudArea]:
    raw_list = payload.get("list")
    if not isinstance(raw_list, list):
        return []
    areas: list[CloudArea] = []
    for item in raw_list:
        if not isinstance(item, dict) or "groupId" not in item:
            continue
        try:
            gid = int(item["groupId"])
            mode = int(item.get("mode", 0))
        except (TypeError, ValueError):
            continue
        name = str(item.get("groupName") or f"Area {gid}")
        dev_serial = str(item.get("groupDevSerial") or serial)
        areas.append(
            CloudArea(
                group_id=gid,
                device_serial=dev_serial,
                name=name,
                mode=mode,
            )
        )
    return areas


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _flag(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    return _int(value, -1) == 1


def parse_page(payload: dict[str, Any]) -> tuple[list[CloudDevice], bool]:
    """Devices of one page and whether more pages follow."""
    raw_devices = payload.get("deviceInfos")
    if not isinstance(raw_devices, list):
        raw_devices = []
    statuses = payload.get("statusInfos")
    if not isinstance(statuses, dict):
        statuses = {}
    devices: list[CloudDevice] = []
    for raw in raw_devices:
        if not isinstance(raw, dict):
            continue
        serial = str(raw.get("deviceSerial") or "").strip()
        if not serial:
            continue
        status = statuses.get(serial)
        if not isinstance(status, dict):
            status = {}
        devices.append(
            CloudDevice(
                serial=serial,
                full_serial=str(raw.get("fullSerial") or "").strip(),
                name=str(raw.get("name") or serial),
                model=str(raw.get("deviceType") or raw.get("fullModel") or ""),
                firmware=str(raw["version"]) if raw.get("version") else None,
                category=str(raw.get("deviceCategory") or ""),
                online=_int(raw.get("status")) == 1,
                channels=_int(raw.get("channelNumber")),
                stream_encrypted=_flag(status.get("isEncrypt")),
            )
        )
    page = payload.get("page")
    if not isinstance(page, dict):
        return devices, False
    if "hasNext" in page:
        return devices, bool(page["hasNext"])
    total = _int(page.get("totalCount"), -1)
    offset = _int(page.get("offset"))
    return devices, total >= 0 and offset + len(raw_devices) < total


def page_params(offset: int) -> dict[str, str]:
    return {
        "groupId": "-1",
        "limit": str(PAGE_SIZE),
        "offset": str(offset),
        "filter": PAGE_FILTER,
    }
