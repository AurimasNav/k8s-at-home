"""Remove secrets and identifying values from text before it is logged or exported."""

from __future__ import annotations

from collections.abc import Iterable
import logging
import re

MASK = "**REDACTED**"

# Element / key names whose values never leave the integration unmasked.
SENSITIVE_NAMES: frozenset[str] = frozenset(
    {
        "password",
        "username",
        "user",
        "serialnumber",
        "subserialnumber",
        "hddserialnumber",
        "macaddress",
        "verificationcode",
        "encryptkey",
        "secretkey",
        "deviceid",
        "uuid",
        "token",
        "sessionid",
        "accesstoken",
        "refreshtoken",
        "ipv6address",
        "challenge",
        "salt",
        "encryptpwd",
        "devicesserial",
        "deviceserial",
        "fullserial",
        "wanip",
        "netip",
        "casip",
        "rfsessionid",
        "refreshsessionid",
    }
)

_URL_USERINFO = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s\"'<>]+@")
_XML_ELEMENT = re.compile(r"<(?P<tag>[A-Za-z_][\w.-]*)(?P<attrs>[^<>]*?)>(?P<val>[^<]+)</(?P=tag)>")
_JSON_PAIR = re.compile(r'"(?P<key>[A-Za-z_][\w-]*)"\s*:\s*"(?P<val>[^"]*)"')
_REALM_SERIAL = re.compile(r'(realm="[^"(]*\()[^)"]*(\))')
_MAC = re.compile(r"\b[0-9a-fA-F]{2}(?:[:-][0-9a-fA-F]{2}){5}\b")


def scrub_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Return ``text`` with credentials, serials and identifiers masked."""
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, MASK)
    text = _URL_USERINFO.sub(lambda m: f"{m['scheme']}{MASK}@", text)
    text = _XML_ELEMENT.sub(_mask_xml, text)
    text = _JSON_PAIR.sub(_mask_json, text)
    text = _REALM_SERIAL.sub(rf"\g<1>{MASK}\g<2>", text)
    return _MAC.sub(MASK, text)


def _mask_xml(match: re.Match[str]) -> str:
    if match["tag"].lower() in SENSITIVE_NAMES:
        return f"<{match['tag']}{match['attrs']}>{MASK}</{match['tag']}>"
    return match[0]


def _mask_json(match: re.Match[str]) -> str:
    if match["key"].lower() in SENSITIVE_NAMES and match["val"]:
        return f'"{match["key"]}": "{MASK}"'
    return match[0]


def scrub_url(url: str) -> str:
    """Mask only the userinfo part of a URL."""
    return _URL_USERINFO.sub(lambda m: f"{m['scheme']}{MASK}@", url)


class ScrubbingFilter(logging.Filter):
    """Logging filter that masks secrets in every record of the integration's loggers."""

    def __init__(self) -> None:
        super().__init__()
        self._secrets: set[str] = set()

    def remember(self, *secrets: str) -> None:
        """Register literal secrets (passwords, usernames) to be masked."""
        self._secrets.update(s for s in secrets if s and len(s) >= 3)

    def forget(self, *secrets: str) -> None:
        """Stop masking the given literals (after an entry was removed)."""
        self._secrets.difference_update(secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never break logging
            return True
        cleaned = scrub_text(message, self._secrets)
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            cleaned_exc = scrub_text(str(exc), self._secrets)
            if cleaned_exc != str(exc):
                # Drop the traceback when it would leak; keep the masked text.
                cleaned = f"{cleaned} ({type(exc).__name__}: {cleaned_exc})"
                record.exc_info = None
                record.exc_text = None
        record.msg = cleaned
        record.args = None
        return True


LOG_SCRUBBER = ScrubbingFilter()


def scrubbed_logger(name: str) -> logging.Logger:
    """Return a module logger that masks secrets.

    Logger filters do not apply to records of child loggers, so every module of the
    integration obtains its logger through this function.
    """
    logger = logging.getLogger(name)
    if LOG_SCRUBBER not in logger.filters:
        logger.addFilter(LOG_SCRUBBER)
    return logger
