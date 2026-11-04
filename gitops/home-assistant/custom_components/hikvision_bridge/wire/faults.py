"""Error taxonomy for device communication and the classifier that maps responses to it."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re


class BridgeFault(Exception):
    """Base for every error raised by the transport layer."""

    translation_key = "unknown"


class Unreachable(BridgeFault):
    """No TCP/TLS connection or no answer in time."""

    translation_key = "unreachable"


class NameNotResolved(Unreachable):
    """Host name could not be resolved."""

    translation_key = "name_not_resolved"


class TlsUntrusted(Unreachable):
    """TLS handshake failed because the certificate is not trusted."""

    translation_key = "tls_untrusted"


class AuthHiccup(Unreachable):
    """A request was refused although these credentials worked before; tried again later."""


class NotIsapi(BridgeFault):
    """Something answered, but it is not an ISAPI device."""

    translation_key = "not_isapi"


class BadCredentials(BridgeFault):
    """The device rejected the credentials after a fresh challenge."""

    translation_key = "bad_credentials"


class LockedOut(BridgeFault):
    """The device locked the account or our IP address."""

    translation_key = "locked_out"

    def __init__(self, message: str = "", retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class Forbidden(BridgeFault):
    """The account lacks a privilege for this resource."""

    translation_key = "forbidden"


class Unsupported(BridgeFault):
    """The resource does not exist on this device or firmware."""

    translation_key = "unsupported"


class DeviceRejected(BridgeFault):
    """The device understood the request but refused it (bad parameters, busy, conflict)."""

    translation_key = "device_rejected"

    def __init__(self, message: str, status: int, sub_status: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.sub_status = sub_status


def detail(err: BaseException) -> str:
    """Kind of an error with what the device said, for debug logs."""
    if isinstance(err, DeviceRejected):
        said = " ".join(
            part for part in (str(err), err.sub_status) if part and part != f"HTTP {err.status}"
        )
        return f"{type(err).__name__} (HTTP {err.status}{': ' + said if said else ''})"
    return f"{type(err).__name__} ({err})" if str(err) else type(err).__name__


@dataclass(frozen=True, slots=True)
class DeviceStatus:
    """The device's own status block contained in error responses."""

    status_code: int | None = None
    status_string: str | None = None
    sub_status: str | None = None
    error_message: str | None = None
    lock_state: str | None = None
    unlock_seconds: int | None = None
    retries_left: int | None = None


_TAG = r"<(?:\w+:)?{name}\b[^>]*>([^<]*)<"
_UNSUPPORTED_SUB = {"notsupport", "invalidoperation", "notsupported", "methodnotallowed"}
_PRIVILEGE_SUB = {"lowprivilege", "nopermission", "privilegeerror", "permissiondenied"}
_LOCK_SUB = {"userlocked", "iplocked", "userlock", "locked"}


def _xml_value(text: str, name: str) -> str | None:
    match = re.search(_TAG.format(name=name), text, flags=re.IGNORECASE)
    return match.group(1).strip() if match else None


def _as_int(value: str | int | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def read_status(body: str) -> DeviceStatus:
    """Extract the device status block from an XML or JSON body (tolerant, never raises)."""
    stripped = body.lstrip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except ValueError:
            data = {}
        if isinstance(data, dict):
            return DeviceStatus(
                status_code=_as_int(data.get("statusCode")),
                status_string=data.get("statusString"),
                sub_status=data.get("subStatusCode"),
                error_message=data.get("errorMsg"),
                lock_state=data.get("lockStatus"),
                unlock_seconds=_as_int(data.get("unlockTime")),
                retries_left=_as_int(data.get("retryLoginTime")),
            )
        return DeviceStatus()
    return DeviceStatus(
        status_code=_as_int(_xml_value(body, "statusCode")),
        status_string=_xml_value(body, "statusString"),
        sub_status=_xml_value(body, "subStatusCode"),
        error_message=_xml_value(body, "errorMsg"),
        lock_state=_xml_value(body, "lockStatus"),
        unlock_seconds=_as_int(_xml_value(body, "unlockTime")),
        retries_left=_as_int(_xml_value(body, "retryLoginTime")),
    )


def is_lock(status: DeviceStatus) -> bool:
    """Return True when the status block reports a locked account or address."""
    if status.lock_state and status.lock_state.lower() in {"lock", "locked"}:
        return True
    if status.sub_status and status.sub_status.lower() in _LOCK_SUB:
        return True
    return status.retries_left == 0 and status.unlock_seconds not in (None, 0)


def classify(http_status: int, body: str) -> BridgeFault | None:
    """Map a non-success response (other than 401) to a fault, or None for success."""
    if 200 <= http_status < 300:
        status = read_status(body) if body else DeviceStatus()
        # Some firmwares answer 200 with an embedded failure.
        if status.status_code not in (None, 1) and status.sub_status:
            return _from_sub_status(http_status, status)
        return None
    status = read_status(body)
    if http_status == 404:
        return Unsupported(f"HTTP 404 {status.sub_status or ''}".strip())
    if http_status == 405:
        return Unsupported("HTTP 405")
    return _from_sub_status(http_status, status)


def _from_sub_status(http_status: int, status: DeviceStatus) -> BridgeFault:
    sub = (status.sub_status or "").lower()
    if is_lock(status):
        return LockedOut(sub or "locked", status.unlock_seconds)
    if sub in _UNSUPPORTED_SUB:
        return Unsupported(status.sub_status or "notSupport")
    if sub in _PRIVILEGE_SUB:
        return Forbidden(status.sub_status or "lowPrivilege")
    if http_status == 403 and not sub:
        return Forbidden("HTTP 403")
    if http_status == 503 and sub in {"serviceunavailable", ""}:
        return Unsupported("serviceUnavailable")
    message = status.error_message or status.status_string or f"HTTP {http_status}"
    return DeviceRejected(message, http_status, status.sub_status)
