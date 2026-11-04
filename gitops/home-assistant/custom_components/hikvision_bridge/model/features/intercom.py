"""Hikvision Video Intercom and Access Control."""

from __future__ import annotations

from dataclasses import dataclass
import json

from ...wire.link import IsapiLink
from ...wire.xmlview import XmlNode

_DOOR_CONTROL_CMD = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<RemoteControlDoor version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
    "<cmd>{cmd}</cmd></RemoteControlDoor>"
)


@dataclass(slots=True)
class IntercomCapabilities:
    has_call_status: bool = False
    doors: tuple[int, ...] = ()


def parse_capabilities(
    intercom_doc: XmlNode | None, door_doc: XmlNode | None
) -> IntercomCapabilities | None:
    if intercom_doc is None and door_doc is None:
        return None
    has_call_status = bool(intercom_doc and intercom_doc.flag("isSupportCallStatus", False))
    doors: list[int] = []
    if door_doc is not None:
        door_node = door_doc.first("doorNo")
        cmds = [c.lower() for c in door_doc.options("cmd")]
        if not cmds or "open" in cmds:
            if door_node is not None:
                min_door = door_node.attrs.get("min")
                max_door = door_node.attrs.get("max")
                if min_door and max_door and min_door.isdigit() and max_door.isdigit():
                    doors.extend(range(int(min_door), int(max_door) + 1))
            if not doors:
                doors.append(1)
    elif intercom_doc and intercom_doc.flag("isSupportRemoteOpenDoor", False):
        doors.append(1)
    return IntercomCapabilities(has_call_status=has_call_status, doors=tuple(doors))


async def read_capabilities(link: IsapiLink) -> IntercomCapabilities | None:
    intercom_doc = await link.read_xml("/ISAPI/VideoIntercom/capabilities", optional=True)
    door_doc = await link.read_xml(
        "/ISAPI/AccessControl/RemoteControl/door/capabilities", optional=True
    )
    return parse_capabilities(intercom_doc, door_doc)


def canonical_call_status(raw: str | None) -> str:
    if not raw:
        return "idle"
    clean = raw.strip().lower()
    if clean in ("ring", "ringing"):
        return "ringing"
    if clean in ("oncall", "on_call"):
        return "on_call"
    return "idle"


async def read_call_status(link: IsapiLink) -> str | None:
    reply = await link.read_text("/ISAPI/VideoIntercom/callStatus?format=json", optional=True)
    if reply is None:
        return None
    try:
        data = json.loads(reply)
        status = data.get("CallStatus", {}).get("status")
        return canonical_call_status(status)
    except (json.JSONDecodeError, KeyError, AttributeError):
        return None


async def remote_open_door(link: IsapiLink, door_no: int = 1) -> None:
    await link.write_xml(
        f"/ISAPI/AccessControl/RemoteControl/door/{door_no}",
        _DOOR_CONTROL_CMD.format(cmd="open"),
    )
