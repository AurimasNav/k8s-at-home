"""Hik-Connect account session: login with region redirect, renewal and captcha detection."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import secrets
import time
from typing import Any

import aiohttp

from ..wire.scrub import LOG_SCRUBBER, scrubbed_logger

_LOGGER = scrubbed_logger(__name__)

DEFAULT_HOST = "api.hik-connect.com"
CLIENT_NAME = "Home Assistant"
SESSION_LIFETIME = 25 * 60  # renew well before the server-side expiry


class CloudError(Exception):
    """Cloud request failed; ``reason`` is a translation key."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


class CaptchaRequired(CloudError):
    def __init__(self, image: str | None = None) -> None:
        super().__init__("captcha_required")
        self.image = image


class CloudAuthFailed(CloudError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("invalid_auth", detail)


@dataclass(slots=True)
class CloudSession:
    session_id: str
    refresh_id: str | None
    issued: float = field(default_factory=time.monotonic)

    @property
    def stale(self) -> bool:
        return time.monotonic() - self.issued > SESSION_LIFETIME


def _code(payload: dict[str, Any]) -> int | None:
    meta = payload.get("meta") if isinstance(payload, dict) else None
    if isinstance(meta, dict):
        try:
            return int(meta.get("code"))
        except (TypeError, ValueError):
            return None
    try:
        return int(payload.get("resultCode"))  # older response style
    except (TypeError, ValueError, AttributeError):
        return None


def _message(payload: dict[str, Any]) -> str:
    meta = payload.get("meta") if isinstance(payload, dict) else None
    if isinstance(meta, dict):
        return str(meta.get("message") or "")
    return str(payload.get("resultDes") or "") if isinstance(payload, dict) else ""


_OK = {200, 0}
_REDIRECT = {1100}
_BAD_CREDENTIALS = {1013, 1014, 1001}
_CAPTCHA = {1015, 1016, 1017, 6002}
_LOCKED = {1026, 1027}


class CloudAccount:
    """One logged-in Hik-Connect account."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        account: str,
        password: str,
        region_host: str | None = None,
    ) -> None:
        self._http = session
        self._account = account
        self._password = password
        self.region_host = region_host or DEFAULT_HOST
        self._feature = secrets.token_hex(16)
        self._session: CloudSession | None = None
        self.account_id: str = hashlib.sha256(account.strip().lower().encode()).hexdigest()[:16]
        self.requests = 0
        self.last_error: str | None = None
        LOG_SCRUBBER.remember(account, password)

    # ----------------------------------------------------------------- http

    def _headers(self) -> dict[str, str]:
        headers = {
            "clientType": "55",
            "lang": "en-US",
            "featureCode": self._feature,
            "Accept": "application/json",
        }
        if self._session is not None:
            headers["sessionId"] = self._session.session_id
        return headers

    async def _exchange(
        self,
        method: str,
        path: str,
        *,
        form: dict[str, str] | None = None,
        json_data: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.requests += 1
        url = f"https://{self.region_host}{path}"
        try:
            async with self._http.request(
                method,
                url,
                data=form,
                json=json_data,
                params=params,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                if response.status >= 500:
                    raise CloudError("cloud_unavailable", f"HTTP {response.status}")
                try:
                    payload = await response.json(content_type=None)
                except ValueError as err:
                    raise CloudError("cloud_changed", "response is not JSON") from err
        except aiohttp.ClientError as err:
            raise CloudError("cannot_connect", type(err).__name__) from err
        except TimeoutError as err:
            raise CloudError("cannot_connect", "timeout") from err
        if not isinstance(payload, dict):
            raise CloudError("cloud_changed", "unexpected response shape")
        return payload

    # ---------------------------------------------------------------- login

    async def login(self, *, captcha: str | None = None) -> None:
        """Log in once; follows a single region redirect. Never retries a rejection."""
        form = {
            "account": self._account,
            "password": hashlib.md5(self._password.encode(), usedforsecurity=False).hexdigest(),
            "featureCode": self._feature,
            "cuName": base64.b64encode(CLIENT_NAME.encode()).decode(),
        }
        if captcha:
            form["imageCode"] = captcha
        for _hop in range(2):
            self._session = None
            payload = await self._exchange("POST", "/v3/users/login/v2", form=form)
            code = _code(payload)
            area = payload.get("loginArea") or {}
            domain = area.get("apiDomain") if isinstance(area, dict) else None
            if code in _REDIRECT and domain and domain != self.region_host:
                self.region_host = str(domain)
                continue
            if code in _OK:
                session = payload.get("loginSession") or {}
                if not isinstance(session, dict) or not session.get("sessionId"):
                    raise CloudError("cloud_changed", "no session in login response")
                if domain:
                    self.region_host = str(domain)
                self._session = CloudSession(session["sessionId"], session.get("rfSessionId"))
                LOG_SCRUBBER.remember(session["sessionId"], session.get("rfSessionId") or "")
                user = payload.get("loginUser") or {}
                if isinstance(user, dict) and user.get("userId"):
                    self.account_id = hashlib.sha256(str(user["userId"]).encode()).hexdigest()[:16]
                return
            self._raise_for(code, payload)
        raise CloudError("cloud_changed", "redirect loop")

    def _raise_for(self, code: int | None, payload: dict[str, Any]) -> None:
        self.last_error = f"{code}"
        if code in _CAPTCHA:
            image = payload.get("captchaUrl") or payload.get("imageUrl")
            raise CaptchaRequired(str(image) if image else None)
        if code in _BAD_CREDENTIALS:
            raise CloudAuthFailed(_message(payload))
        if code in _LOCKED:
            raise CloudError("locked_out", _message(payload))
        if code == 70002:
            raise CloudError("device_offline", _message(payload))
        raise CloudError("cloud_rejected", f"code {code}: {_message(payload)}")

    async def renew(self) -> None:
        """Renew the session with the refresh id; log in again if that fails."""
        if self._session is None or not self._session.refresh_id:
            await self.login()
            return
        payload = await self._exchange(
            "PUT",
            "/v3/apigateway/login",
            form={"refreshSessionId": self._session.refresh_id, "featureCode": self._feature},
        )
        session = payload.get("sessionInfo") or payload.get("loginSession") or {}
        if _code(payload) in _OK and isinstance(session, dict) and session.get("sessionId"):
            self._session = CloudSession(
                session["sessionId"], session.get("refreshSessionId") or session.get("rfSessionId")
            )
            LOG_SCRUBBER.remember(session["sessionId"])
            return
        await self.login()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Authenticated request with transparent session renewal (once)."""
        if self._session is None:
            await self.login()
        elif self._session.stale:
            await self.renew()
        payload = await self._exchange(method, path, params=params, json_data=json_data)
        if _code(payload) in {401, 1003, 1004, 1005}:
            await self.renew()
            payload = await self._exchange(method, path, params=params, json_data=json_data)
        code = _code(payload)
        if code not in _OK:
            self._raise_for(code, payload)
        return payload
