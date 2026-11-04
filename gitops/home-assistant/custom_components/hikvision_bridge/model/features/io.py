"""Alarm inputs and outputs."""

from __future__ import annotations

from ...wire.link import IsapiLink
from ...wire.xmlview import XmlNode
from ..inventory import IoPort
from . import patch_and_verify

_OUTPUT_COMMAND = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<IOPortData version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
    "<outputState>{state}</outputState></IOPortData>"
)


def parse_ports(document: XmlNode | None, *, output: bool) -> dict[str, IoPort]:
    if document is None:
        return {}
    element = "IOOutputPort" if output else "IOInputPort"
    ports: dict[str, IoPort] = {}
    for node in document.children(element):
        port_id = node.text("id")
        if not port_id:
            continue
        proxied = node.first("IODescriptor") is not None or (node.text("IOType") or "").lower() == (
            "digitalchannel"
        )
        channel = None
        if proxied and port_id.isdigit() and len(port_id) >= 3:
            channel = str(int(port_id) // 100)
        ports[port_id] = IoPort(
            port_id=port_id,
            output=output,
            name=node.text("name"),
            channel_id=channel,
            proxied=proxied,
        )
    return ports


def _status_path(port: IoPort) -> str:
    direction = "outputs" if port.output else "inputs"
    return f"/ISAPI/System/IO/{direction}/{port.port_id}/status"


async def read_state(link: IsapiLink, port: IoPort) -> bool | None:
    document = await link.read_xml(_status_path(port), optional=True)
    if document is None:
        return None
    state = (document.text("ioState") or document.text("IOState") or "").lower()
    if state in ("active", "high"):
        return True
    if state in ("inactive", "low"):
        return False
    return None


INPUTS_PATH = "/ISAPI/System/IO/inputs"


def parse_armed(document: XmlNode | None) -> dict[str, bool]:
    """Which alarm inputs are armed (``enabled``), by port id."""
    if document is None:
        return {}
    armed: dict[str, bool] = {}
    for node in document.children("IOInputPort"):
        port_id = node.text("id")
        enabled = node.flag("enabled")
        if port_id and enabled is not None:
            armed[port_id] = enabled
    return armed


async def read_armed(link: IsapiLink) -> dict[str, bool] | None:
    document = await link.read_xml(INPUTS_PATH, optional=True)
    return parse_armed(document) if document is not None else None


async def set_armed(link: IsapiLink, port_id: str, on: bool) -> None:
    """Arm or disarm one input; only its ``enabled`` flag changes."""
    await patch_and_verify(link, f"{INPUTS_PATH}/{port_id}", {"enabled": "true" if on else "false"})


async def set_output(link: IsapiLink, port: IoPort, on: bool) -> None:
    await link.write_xml(
        f"/ISAPI/System/IO/outputs/{port.port_id}/trigger",
        _OUTPUT_COMMAND.format(state="high" if on else "low"),
    )
