"""Namespace-agnostic XML reading and in-place editing of raw device documents.

Devices mix namespaces between firmwares and even within one event stream, so all lookups
work on local element names. Writes never rebuild a document: the original text is kept
and only the targeted element texts are replaced, so unknown elements, ordering and
namespace declarations survive a read-modify-write cycle.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import re
from xml.etree import ElementTree as ET

try:  # Home Assistant ships defusedxml in most installs; fall back to a guarded parser.
    from defusedxml.ElementTree import fromstring as _defused_fromstring
except ImportError:  # pragma: no cover - depends on installation
    _defused_fromstring = None

MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
_TRUE = {"true", "1", "yes", "on", "enable", "enabled"}


class XmlError(ValueError):
    """Raised when a document cannot be parsed."""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def _safe_fromstring(text: str) -> ET.Element:
    if len(text) > MAX_DOCUMENT_BYTES:
        raise XmlError("document too large")
    if _defused_fromstring is not None:
        return _defused_fromstring(text)
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise XmlError("document type declarations are not accepted")
    return ET.fromstring(text)  # noqa: S314 - DTDs rejected above


@dataclass(slots=True)
class XmlNode:
    """Read-only view on an element, addressed by local names."""

    element: ET.Element

    @property
    def name(self) -> str:
        return _local(self.element.tag)

    @property
    def attrs(self) -> dict[str, str]:
        return {_local(k): v for k, v in self.element.attrib.items()}

    @property
    def value(self) -> str | None:
        text = self.element.text
        return text.strip() if text is not None else None

    def children(self, name: str | None = None) -> Iterator[XmlNode]:
        for child in self.element:
            if not isinstance(child.tag, str):
                continue
            if name is None or _local(child.tag).lower() == name.lower():
                yield XmlNode(child)

    def first(self, path: str) -> XmlNode | None:
        """Follow a slash separated path of local names (case-insensitive)."""
        current: XmlNode | None = self
        for part in path.split("/"):
            if current is None:
                return None
            current = next(current.children(part), None)
        return current

    def all(self, path: str) -> list[XmlNode]:
        """Return every node matching the path; the last segment may repeat."""
        *head, last = path.split("/")
        parents = [self]
        for part in head:
            parents = [c for p in parents for c in p.children(part)]
        return [c for p in parents for c in p.children(last)]

    def walk(self, name: str) -> Iterator[XmlNode]:
        """Yield all descendants with the given local name (any depth)."""
        wanted = name.lower()
        for element in self.element.iter():
            if (
                element is not self.element
                and isinstance(element.tag, str)
                and _local(element.tag).lower() == wanted
            ):
                yield XmlNode(element)

    def text(self, path: str, default: str | None = None) -> str | None:
        node = self.first(path)
        if node is None or node.value in (None, ""):
            return default
        return node.value

    def flag(self, path: str, default: bool | None = None) -> bool | None:
        raw = self.text(path)
        if raw is None:
            return default
        return raw.lower() in _TRUE

    def numeric(self, path: str, default: int | None = None) -> int | None:
        raw = self.text(path)
        if raw is None:
            return default
        try:
            return int(float(raw))
        except ValueError:
            return default

    def options(self, path: str) -> list[str]:
        """Return the comma separated ``opt`` attribute of a capability element."""
        node = self.first(path) if path else self
        if node is None:
            return []
        raw = node.attrs.get("opt", "")
        return [part.strip() for part in raw.split(",") if part.strip()]


def parse(text: str | bytes) -> XmlNode:
    """Parse a device document. Accepts bytes in UTF-8 or with a declared encoding."""
    if isinstance(text, bytes):
        text = decode(text)
    text = text.strip().lstrip("﻿")
    if not text.startswith("<"):
        raise XmlError("not an XML document")
    try:
        return XmlNode(_safe_fromstring(_strip_declaration(text)))
    except ET.ParseError as err:
        raise XmlError(str(err)) from err


def decode(raw: bytes) -> str:
    """Decode a device body tolerantly (UTF-8 first, then GB18030 for odd error texts)."""
    for codec in ("utf-8", "gb18030"):
        try:
            return raw.decode(codec)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _strip_declaration(text: str) -> str:
    # ElementTree rejects str input that carries an encoding declaration.
    if text.startswith("<?xml"):
        end = text.find("?>")
        if end != -1:
            return text[end + 2 :].lstrip()
    return text


def escape(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


_TOKEN = re.compile(
    r"<!--.*?-->|<!\[CDATA\[.*?\]\]>|<\?.*?\?>|<!.*?>"
    r"|<(?P<close>/)?(?P<name>[A-Za-z_][\w.:-]*)(?P<rest>(?:[^>\"']|\"[^\"]*\"|'[^']*')*?)(?P<self>/)?>",
    re.DOTALL,
)


@dataclass(slots=True)
class _Span:
    path: tuple[str, ...]
    open_start: int
    open_end: int
    close_start: int
    close_end: int
    self_closing: bool


class RawDocument:
    """Edits element texts inside the original document text.

    Paths are slash separated local names relative to the root element, e.g.
    ``enabled`` or ``LineItemList/LineItem/enabled``.
    """

    def __init__(self, text: str) -> None:
        self.text = text

    def _spans(self) -> list[_Span]:
        spans: list[_Span] = []
        stack: list[tuple[str, int, int]] = []
        for match in _TOKEN.finditer(self.text):
            name = match.group("name")
            if name is None:
                continue
            local = _local(name).lower()
            if match.group("close"):
                while stack:
                    top_name, open_start, open_end = stack.pop()
                    if top_name == local:
                        path = (*(n for n, _, _ in stack[1:]), local)
                        spans.append(
                            _Span(path, open_start, open_end, match.start(), match.end(), False)
                        )
                        break
            elif match.group("self"):
                path = (*(n for n, _, _ in stack[1:]), local)
                if stack:
                    spans.append(
                        _Span(path, match.start(), match.end(), match.end(), match.end(), True)
                    )
            else:
                stack.append((local, match.start(), match.end()))
        spans.sort(key=lambda s: s.open_start)
        return spans

    def _find(self, path: str, occurrence: int) -> _Span | None:
        wanted = tuple(part.lower() for part in path.split("/"))
        matches = [s for s in self._spans() if s.path == wanted]
        if occurrence >= len(matches):
            return None
        return matches[occurrence]

    def count(self, path: str) -> int:
        wanted = tuple(part.lower() for part in path.split("/"))
        return sum(1 for s in self._spans() if s.path == wanted)

    def get(self, path: str, occurrence: int = 0) -> str | None:
        span = self._find(path, occurrence)
        if span is None:
            return None
        if span.self_closing:
            return ""
        return self.text[span.open_end : span.close_start].strip()

    def set(self, path: str, value: str, occurrence: int = 0) -> bool:
        """Replace the text of one element. Returns False when the element is missing."""
        span = self._find(path, occurrence)
        if span is None:
            return False
        safe = escape(value)
        if span.self_closing:
            tag = self.text[span.open_start + 1 : span.open_end - 2].rstrip()
            name = tag.split()[0]
            replacement = f"<{tag}>{safe}</{name}>"
            self.text = self.text[: span.open_start] + replacement + self.text[span.open_end :]
        else:
            self.text = self.text[: span.open_end] + safe + self.text[span.close_start :]
        return True

    def has(self, path: str) -> bool:
        return self._find(path, 0) is not None
