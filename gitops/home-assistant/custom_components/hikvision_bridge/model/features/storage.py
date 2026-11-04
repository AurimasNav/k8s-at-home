"""Disks, memory cards and network storage."""

from __future__ import annotations

from ...wire.link import IsapiLink
from ...wire.xmlview import XmlNode
from ..inventory import StorageUnit

PATH = "/ISAPI/ContentMgmt/Storage"
HEALTHY = {"ok", "normal", "idle", "sleeping"}


def parse_units(document: XmlNode | None) -> list[StorageUnit]:
    if document is None:
        return []
    units: list[StorageUnit] = []
    for node in document.all("hddList/hdd"):
        units.append(
            StorageUnit(
                unit_id=node.text("id", "?") or "?",
                name=node.text("hddName") or f"hdd{node.text('id')}",
                kind=(node.text("hddType") or "disk").lower(),
                status=(node.text("status") or "unknown").lower(),
                capacity_mb=node.numeric("capacity"),
                free_mb=node.numeric("freeSpace"),
            )
        )
    for node in document.all("nasList/nas"):
        units.append(
            StorageUnit(
                unit_id=f"nas{node.text('id', '?')}",
                name=node.text("path") or f"nas{node.text('id')}",
                kind="nas",
                status=(node.text("status") or "unknown").lower(),
                capacity_mb=node.numeric("capacity"),
                free_mb=node.numeric("freeSpace"),
                network=True,
            )
        )
    return units


async def read(link: IsapiLink) -> list[StorageUnit]:
    return parse_units(await link.read_xml(PATH, optional=True))
