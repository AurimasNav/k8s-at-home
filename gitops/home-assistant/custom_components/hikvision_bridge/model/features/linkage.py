"""Device-side event triggers and the "notify surveillance center" linkage."""

from __future__ import annotations

from ...wire.link import IsapiLink
from ...wire.xmlview import RawDocument, XmlNode, parse
from ..inventory import TriggerLink
from ..vocabulary import canonical
from . import VerifyMismatch

LIST_PATH = "/ISAPI/Event/triggers"
_CENTER_ENTRY = (
    "<EventTriggerNotification><id>center</id>"
    "<notificationMethod>center</notificationMethod>"
    "<notificationRecurrence>beginning</notificationRecurrence>"
    "</EventTriggerNotification>"
)


def parse_triggers(document: XmlNode | None) -> list[TriggerLink]:
    if document is None:
        return []
    links: list[TriggerLink] = []
    for node in document.walk("EventTrigger"):
        raw_type = node.text("eventType")
        trigger_id = node.text("id")
        if not raw_type or not trigger_id:
            continue
        channel = (
            node.text("videoInputChannelID")
            or node.text("dynVideoInputChannelID")
            or node.text("inputIOPortID")
            or node.text("dynInputIOPortID")
        )
        methods = {(m.value or "").lower() for m in node.walk("notificationMethod") if m.value}
        links.append(TriggerLink(trigger_id, canonical(raw_type), raw_type, channel, methods))
    return links


def add_center(document_text: str) -> str | None:
    """Return the trigger document with a center notification added, or None if present."""
    if parse(document_text).first("EventTriggerNotificationList") is None:
        return None
    if any(
        (m.value or "").lower() == "center" for m in parse(document_text).walk("notificationMethod")
    ):
        return None
    raw = RawDocument(document_text)
    lower = raw.text.lower()
    close = lower.rfind("</eventtriggernotificationlist>")
    if close != -1:
        return raw.text[:close] + _CENTER_ENTRY + raw.text[close:]
    empty = lower.rfind("<eventtriggernotificationlist/>")
    if empty == -1:
        return None
    end = empty + len("<EventTriggerNotificationList/>")
    return (
        raw.text[:empty]
        + "<EventTriggerNotificationList>"
        + _CENTER_ENTRY
        + "</EventTriggerNotificationList>"
        + raw.text[end:]
    )


async def ensure_center(link: IsapiLink, trigger_id: str, *, dry_run: bool) -> bool:
    """Add the center linkage to one trigger. Returns True when a change is (or would be) made."""
    path = f"{LIST_PATH}/{trigger_id}"
    current = (await link.call("GET", path)).text
    updated = add_center(current)
    if updated is None:
        return False
    if dry_run:
        return True
    await link.write_xml(path, updated)
    check = await link.read_xml(path)
    if check is None or not any(
        (m.value or "").lower() == "center" for m in check.walk("notificationMethod")
    ):
        raise VerifyMismatch(f"trigger {trigger_id} still lacks the center linkage", 200)
    return True
