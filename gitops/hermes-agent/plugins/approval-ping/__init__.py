"""approval-ping: make pending Hermes prompts hard to miss on Telegram.

When an exec approval or a clarify question is waiting in a Telegram chat, post a short message
that @-mentions the approvers (TELEGRAM_ALLOWED_USERS, or APPROVAL_PING_USER_IDS), remind
again later, and delete those pings once the prompt is answered or expires.

Observer only: it never blocks, answers or delays a prompt, and every error is swallowed, so a
broken ping can't break approvals or the Telegram connection.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"
_KEY_RE = re.compile(r"(?:^|:)telegram:([a-z_]+):(-?\d+)(?::(\d+))?")
_FIRST_DELAY = float(os.getenv("APPROVAL_PING_FIRST_DELAY", "3"))
_REMINDERS = sorted(
    int(x)
    for x in os.getenv("APPROVAL_PING_REMINDERS", "300,1200").split(",")
    if x.strip().isdigit()
)

_lock = threading.Lock()
_pending: dict[tuple, list[_Ping]] = {}
_names: dict[tuple, str] = {}


def _api(method: str, **params) -> dict | None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return None
    data = urllib.parse.urlencode(
        {k: v for k, v in params.items() if v is not None}
    ).encode()
    try:
        with urllib.request.urlopen(
            _API.format(token=token, method=method), data=data, timeout=15
        ) as r:
            body = json.load(r)
        return body.get("result") if body.get("ok") else None
    except Exception as exc:  # never raise into the approval flow
        log.debug("approval-ping %s failed: %s", method, type(exc).__name__)
        return None


def _user_ids() -> list[str]:
    raw = os.getenv("APPROVAL_PING_USER_IDS") or os.getenv("TELEGRAM_ALLOWED_USERS", "")
    return [x.strip() for x in raw.replace(";", ",").split(",") if x.strip().isdigit()]


def _mentions(chat_id: str) -> str:
    out = []
    for uid in _user_ids():
        name = _names.get((chat_id, uid))
        if name is None:
            member = _api("getChatMember", chat_id=chat_id, user_id=uid) or {}
            name = (member.get("user") or {}).get("first_name") or "approver"
            _names[(chat_id, uid)] = name
        out.append(f'<a href="tg://user?id={uid}">{html.escape(name)}</a>')
    return " ".join(out)


def _minutes(seconds: float) -> str:
    m = max(1, round(seconds / 60))
    return f"{m} min"


class _Ping:
    def __init__(self, chat_id: str, thread_id: str | None, what: str, timeout: int):
        self.chat_id, self.thread_id, self.what, self.timeout = (
            chat_id,
            thread_id,
            what,
            timeout,
        )
        self.message_ids: list[int] = []
        self.timers: list[threading.Timer] = []
        self.done = False

    def start(self) -> None:
        self._schedule(_FIRST_DELAY, first=True)
        for at in _REMINDERS:
            if at < self.timeout:
                self._schedule(at, first=False)

    def _schedule(self, delay: float, first: bool) -> None:
        t = threading.Timer(delay, self._send, kwargs={"first": first, "delay": delay})
        t.daemon = True
        self.timers.append(t)
        t.start()

    def _send(self, first: bool, delay: float) -> None:
        if self.done:
            return
        who = _mentions(self.chat_id)
        if first:
            text = f"🔔 {who} — {self.what} waiting above ⬆️ (expires in {_minutes(self.timeout)})"
        else:
            text = f"⏰ {who} — still waiting: {self.what} above ⬆️ ({_minutes(self.timeout - delay)} left)"
        msg = _api(
            "sendMessage",
            chat_id=self.chat_id,
            message_thread_id=self.thread_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview="true",
        )
        if not msg:
            return
        with _lock:
            late = self.done
            if not late:
                self.message_ids.append(msg["message_id"])
        if late:  # answered while we were sending
            _api("deleteMessage", chat_id=self.chat_id, message_id=msg["message_id"])

    def finish(self) -> None:
        with _lock:
            self.done = True
            ids, self.message_ids = self.message_ids, []
        for t in self.timers:
            t.cancel()
        for mid in ids:
            _api("deleteMessage", chat_id=self.chat_id, message_id=mid)


def _session_target() -> tuple[str, str | None] | None:
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return None
    if get_session_env("HERMES_SESSION_PLATFORM", "") != "telegram":
        return None
    chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
    return (
        (chat_id, get_session_env("HERMES_SESSION_THREAD_ID", "") or None)
        if chat_id
        else None
    )


def _target(session_key: str) -> tuple[str, str | None] | None:
    m = _KEY_RE.search(session_key or "")
    if m:
        return m.group(2), m.group(3)
    return _session_target()


def _approval_timeout() -> int:
    try:
        from tools.approval_context import _get_approval_timeout

        return int(_get_approval_timeout())
    except Exception:
        return 300


def _clarify_timeout() -> int:
    try:
        from tools.clarify_gateway import get_clarify_timeout

        return int(get_clarify_timeout())
    except Exception:
        return 3600


def _open(key: tuple, target: tuple[str, str | None], what: str, timeout: int) -> None:
    ping = _Ping(target[0], target[1], what, timeout)
    with _lock:
        _pending.setdefault(key, []).append(ping)
    ping.start()


def _close(key: tuple) -> None:
    with _lock:
        stack = _pending.get(key) or []
        ping = stack.pop(0) if stack else None
        if not stack:
            _pending.pop(key, None)
    if ping is not None:
        # off the agent thread: deleteMessage is network I/O
        threading.Thread(
            target=ping.finish, name="approval-ping-finish", daemon=True
        ).start()


def _on_pre_approval_request(
    session_key: str = "",
    command: str = "",
    surface: str = "",
    coalesced: bool = False,
    **_,
) -> None:
    try:
        if surface != "gateway" or coalesced:
            return
        target = _target(session_key)
        if target:
            _open(
                ("approval", session_key, command),
                target,
                "approval",
                _approval_timeout(),
            )
    except Exception:
        log.debug("approval-ping pre_approval_request failed", exc_info=True)


def _on_post_approval_response(
    session_key: str = "",
    command: str = "",
    surface: str = "",
    coalesced: bool = False,
    **_,
) -> None:
    try:
        if surface == "gateway" and not coalesced:
            _close(("approval", session_key, command))
    except Exception:
        log.debug("approval-ping post_approval_response failed", exc_info=True)


def _on_pre_tool_call(
    tool_name: str = "", tool_call_id: str = "", session_id: str = "", **_
) -> None:
    try:
        if tool_name != "clarify":
            return None
        target = _session_target()
        if target:
            _open(
                ("clarify", session_id, tool_call_id),
                target,
                "question",
                _clarify_timeout(),
            )
    except Exception:
        log.debug("approval-ping clarify start failed", exc_info=True)
    return None


def _on_post_tool_call(
    tool_name: str = "", tool_call_id: str = "", session_id: str = "", **_
) -> None:
    try:
        if tool_name == "clarify":
            _close(("clarify", session_id, tool_call_id))
    except Exception:
        log.debug("approval-ping clarify end failed", exc_info=True)


def register(ctx) -> None:
    ctx.register_hook("pre_approval_request", _on_pre_approval_request)
    ctx.register_hook("post_approval_response", _on_post_approval_response)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
