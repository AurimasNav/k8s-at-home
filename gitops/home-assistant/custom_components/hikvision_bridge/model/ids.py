"""Ids the device reports end up in ISAPI paths, so only plain ones are accepted."""

from __future__ import annotations

import re
from urllib.parse import quote

_NUMERIC = re.compile(r"[0-9]{1,12}")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,64}")


def numeric_id(value: str | None) -> str | None:
    """The id when it is only digits (channels, ports, presets), else None."""
    return value if value and _NUMERIC.fullmatch(value) else None


def token_id(value: str | None) -> str | None:
    """The id when it is letters, digits, ``_`` or ``-`` (event trigger ids), else None."""
    return value if value and _TOKEN.fullmatch(value) else None


def seg(value: str) -> str:
    """One percent-encoded path segment."""
    return quote(str(value), safe="")
