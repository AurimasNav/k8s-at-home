"""Per-feature reading and writing on top of the transport layer."""

from __future__ import annotations

import json
from typing import Any

from ...wire.faults import DeviceRejected, Unsupported
from ...wire.link import IsapiLink
from ...wire.xmlview import RawDocument


class VerifyMismatch(DeviceRejected):
    """The device accepted a write but reads back a different value."""

    translation_key = "verify_mismatch"


async def patch_and_verify(
    link: IsapiLink,
    path: str,
    edits: dict[str, str],
    *,
    fallback_path: str | None = None,
) -> RawDocument:
    """Read-modify-write ``path`` changing only ``edits`` (element path -> text).

    The original document text is kept, so unknown elements, ordering and namespace
    declarations survive. If the device refuses the full document as unsupported and a
    ``fallback_path`` exists, the same edit is applied there. The result is read back and
    compared.
    """
    target = path
    try:
        document = await _edit(link, target, edits)
    except Unsupported:
        if fallback_path is None:
            raise
        target = fallback_path
        document = await _edit(link, target, edits)

    check = RawDocument((await link.call("GET", target)).text)
    for element, value in edits.items():
        actual = check.get(element)
        if actual is not None and actual.lower() != value.lower():
            raise VerifyMismatch(
                f"{element} reads back as {actual!r} instead of {value!r}", 200, "verify"
            )
    return document


async def patch_json_and_verify(
    link: IsapiLink,
    path: str,
    root: str,
    edits: dict[str, Any],
    *,
    skip_missing: bool = False,
) -> None:
    """Read-modify-write a JSON resource ``{root: {...}}`` changing only ``edits``.

    With ``skip_missing`` keys the document does not have are left out (alternative names of
    one value across firmware versions).
    """
    document = await link.read_json(path)
    body = document.get(root) if isinstance(document, dict) else None
    if not isinstance(body, dict):
        raise Unsupported(f"{path} has no {root}")
    if skip_missing:
        edits = {key: value for key, value in edits.items() if key in body}
    if not edits:
        raise Unsupported(f"{path} has nothing to change")
    for key, value in edits.items():
        if key not in body:
            raise Unsupported(f"{path} has no {key}")
        body[key] = value
    await link.call("PUT", path, body=json.dumps(document), content_type="application/json")
    check = await link.read_json(path)
    actual = check.get(root, {}) if isinstance(check, dict) else {}
    for key, value in edits.items():
        if str(actual.get(key)) != str(value):
            raise VerifyMismatch(
                f"{key} reads back as {actual.get(key)!r} instead of {value!r}", 200, "verify"
            )


async def _edit(link: IsapiLink, path: str, edits: dict[str, str]) -> RawDocument:
    document = RawDocument((await link.call("GET", path)).text)
    for element, value in edits.items():
        if not document.set(element, value):
            raise Unsupported(f"{path} has no element {element}")
    await link.write_xml(path, document.text)
    return document
