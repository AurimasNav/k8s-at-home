"""Recordings panel in the sidebar and the dashboard card, served from this folder."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, OPT_PANEL_ADMIN_ONLY, OPT_SIDEBAR_PANEL
from .runtime import sessions

URL_BASE = "/hikvision_bridge_static"
PANEL_PATH = "hikvision-bridge"
MODULE = "hikvision-bridge.js"
FOLDER = Path(__file__).parent / "frontend"
# The panel is one for all entries, so its settings are kept apart from any entry.
STORE_KEY = f"{DOMAIN}.panel"
DATA_PANEL = f"{DOMAIN}_panel"
PANEL_DEFAULTS: dict[str, bool] = {OPT_SIDEBAR_PANEL: True, OPT_PANEL_ADMIN_ONLY: False}


def _fingerprint() -> str:
    return hashlib.sha256((FOLDER / MODULE).read_bytes()).hexdigest()[:12]


class _PanelState:
    def __init__(self, hass: HomeAssistant) -> None:
        self.store: Store[dict[str, Any]] = Store(hass, 1, STORE_KEY)
        self.settings = dict(PANEL_DEFAULTS)
        self.url: str | None = None


def _state(hass: HomeAssistant) -> _PanelState:
    if DATA_PANEL not in hass.data:
        hass.data[DATA_PANEL] = _PanelState(hass)
    return hass.data[DATA_PANEL]


def panel_settings(hass: HomeAssistant) -> dict[str, bool]:
    """Whether the sidebar panel is shown and whether only administrators see it."""
    return dict(_state(hass).settings)


async def async_set_panel_settings(hass: HomeAssistant, settings: dict[str, Any]) -> None:
    """Keep new panel settings and apply them to the sidebar at once."""
    state = _state(hass)
    changed = {key: bool(settings.get(key, value)) for key, value in state.settings.items()}
    if changed == state.settings:
        return
    state.settings = changed
    await state.store.async_save(changed)
    await _async_apply(hass)


async def _async_apply(hass: HomeAssistant) -> None:
    state = _state(hass)
    if state.url is None:
        return
    from homeassistant.components import frontend, panel_custom  # noqa: PLC0415

    frontend.async_remove_panel(hass, PANEL_PATH, warn_if_unknown=False)
    if not state.settings[OPT_SIDEBAR_PANEL]:
        return
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_PATH,
        webcomponent_name="hikvision-bridge-panel",
        sidebar_title="Hikvision",
        sidebar_icon="mdi:cctv",
        module_url=state.url,
        require_admin=state.settings[OPT_PANEL_ADMIN_ONLY],
    )


async def async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the files, add the sidebar panel and make the card available on dashboards."""

    async def _stop_sessions(_event: Event) -> None:
        await sessions.manager(hass).stop_all()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop_sessions)
    state = _state(hass)
    if stored := await state.store.async_load():
        state.settings.update({k: bool(stored[k]) for k in PANEL_DEFAULTS if k in stored})
    if "frontend" not in hass.config.components:
        return
    from homeassistant.components import frontend  # noqa: PLC0415
    from homeassistant.components.http import StaticPathConfig  # noqa: PLC0415

    await hass.http.async_register_static_paths(
        [StaticPathConfig(URL_BASE, str(FOLDER), cache_headers=False)]
    )
    # The fingerprint makes browsers load a changed file after an update.
    state.url = f"{URL_BASE}/{MODULE}?v={await hass.async_add_executor_job(_fingerprint)}"
    frontend.add_extra_js_url(hass, state.url)
    await _async_apply(hass)
