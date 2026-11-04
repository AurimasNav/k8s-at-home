"""Repair flows. Only additive device changes, only after confirmation."""

from __future__ import annotations

from typing import Any

from homeassistant import data_entry_flow
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant
import voluptuous as vol

from .model.features import linkage
from .wire.faults import BridgeFault


class CenterLinkFlow(RepairsFlow):
    """Adds the 'notify surveillance center' linkage to one trigger."""

    def __init__(self, entry_id: str, trigger_id: str) -> None:
        self._entry_id = entry_id
        self._trigger_id = trigger_id

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        bridge = getattr(entry, "runtime_data", None) if entry else None
        if bridge is None:
            return self.async_abort(reason="not_loaded")
        if user_input is not None:
            try:
                await linkage.ensure_center(bridge.link, self._trigger_id, dry_run=False)
            except BridgeFault as err:
                return self.async_show_form(
                    step_id="confirm",
                    data_schema=vol.Schema({}),
                    errors={"base": "write_failed"},
                    description_placeholders={
                        "trigger": self._trigger_id,
                        "reason": type(err).__name__,
                    },
                )
            self.hass.config_entries.async_schedule_reload(self._entry_id)
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"trigger": self._trigger_id, "reason": ""},
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    if data and data.get("trigger_id") and data.get("entry_id"):
        return CenterLinkFlow(str(data["entry_id"]), str(data["trigger_id"]))
    return ConfirmRepairFlow()
