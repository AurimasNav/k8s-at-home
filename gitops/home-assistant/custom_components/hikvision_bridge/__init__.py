"""Hikvision Bridge: local ISAPI integration for Hikvision cameras and recorders."""

from __future__ import annotations

from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    ConfigEntry,
    ConfigEntryChange,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_ENTRY_KIND,
    CONF_SERIAL,
    DOMAIN,
    KIND_CLOUD,
    OPT_RECORDING_CLOCK,
    RECORDING_CLOCK_DEVICE,
    RECORDING_CLOCK_UTC,
)
from .model.discover import discover
from .model.features import recordings
from .panel import async_register_frontend
from .runtime import sessions
from .runtime.bridge import DeviceBridge, build_link
from .runtime.hookin import async_register_webhook, async_unregister_webhook
from .services import async_register_services
from .views import async_register_views
from .websocket import async_register_commands
from .wire.faults import (
    AuthHiccup,
    BadCredentials,
    BridgeFault,
    LockedOut,
    NotIsapi,
    Unreachable,
)
from .wire.scrub import LOG_SCRUBBER, scrubbed_logger

_LOGGER = scrubbed_logger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.EVENT,
    Platform.IMAGE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]
CLOUD_PLATFORMS: list[Platform] = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.EVENT,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type BridgeConfigEntry = ConfigEntry[DeviceBridge]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_register_services(hass)
    async_register_views(hass)
    async_register_commands(hass)
    await async_register_frontend(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.data.get(CONF_ENTRY_KIND) == KIND_CLOUD:
        from .cloud_entry import async_setup_cloud_entry  # noqa: PLC0415

        return await async_setup_cloud_entry(hass, entry, CLOUD_PLATFORMS)
    return await _async_setup_device(hass, entry)


async def _async_setup_device(hass: HomeAssistant, entry: BridgeConfigEntry) -> bool:
    link = build_link(hass, dict(entry.data))
    # Recording times on the device's clock or in real UTC, if set; learned otherwise.
    recordings.fix_basis(
        link,
        {RECORDING_CLOCK_DEVICE: False, RECORDING_CLOCK_UTC: True}.get(
            entry.options.get(OPT_RECORDING_CLOCK)
        ),
    )
    try:
        profile = await discover(link)
    except (BadCredentials, AuthHiccup) as err:
        # A refusal during setup is not retried: setup retries would each cost logins.
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="bad_credentials"
        ) from err
    except LockedOut as err:
        raise ConfigEntryError(translation_domain=DOMAIN, translation_key="locked_out") from err
    except NotIsapi as err:
        raise ConfigEntryError(translation_domain=DOMAIN, translation_key="not_isapi") from err
    except Unreachable as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="unreachable",
            translation_placeholders={"host": link.endpoint.host},
        ) from err
    except BridgeFault as err:
        raise ConfigEntryNotReady(str(err)) from err

    expected = entry.data.get(CONF_SERIAL)
    if expected and profile.identity.serial and profile.identity.serial != expected:
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="other_device",
            translation_placeholders={"host": link.endpoint.host},
        )

    bridge = DeviceBridge(hass, entry, link, profile)
    await bridge.async_prepare_routes()
    try:
        await bridge.async_probe_extras()
    except (BadCredentials, AuthHiccup) as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="bad_credentials"
        ) from err
    except LockedOut as err:
        raise ConfigEntryError(translation_domain=DOMAIN, translation_key="locked_out") from err
    except BridgeFault as err:
        raise ConfigEntryNotReady(str(err)) from err
    bridge.register_devices()
    try:
        await bridge.refresher.async_config_entry_first_refresh()
    except BaseException:
        await bridge.async_shutdown()
        raise
    # Only a fully set up bridge is published: HA keeps runtime_data after a failed setup.
    entry.runtime_data = bridge

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    bridge.start_feeds()
    async_register_webhook(hass, entry)
    bridge.schedule_audit()
    bridge.schedule_intercom_poll()
    _reload_on_subentry_change(hass, entry)
    return True


def _reload_on_subentry_change(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when a channel link is added, changed or removed.

    Options changes reload through OptionsFlowWithReload; an update listener is not
    used, because Home Assistant no longer accepts one next to its own reload.
    """
    seen = dict(entry.subentries)

    @callback
    def _changed(change: ConfigEntryChange, changed: ConfigEntry) -> None:
        if (
            change is ConfigEntryChange.UPDATED
            and changed.entry_id == entry.entry_id
            and dict(changed.subentries) != seen
        ):
            hass.config_entries.async_schedule_reload(entry.entry_id)

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, _changed))


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.data.get(CONF_ENTRY_KIND) == KIND_CLOUD:
        from .cloud_entry import async_unload_cloud_entry  # noqa: PLC0415

        return await async_unload_cloud_entry(hass, entry, CLOUD_PLATFORMS)
    bridge: DeviceBridge = entry.runtime_data
    async_unregister_webhook(hass, entry)
    await sessions.manager(hass).stop_entry(entry.entry_id)
    await bridge.async_shutdown()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


def _entry_strings(entry: ConfigEntry) -> set[str]:
    """String values of an entry and its channel links (credentials among them)."""
    values = [*entry.data.values()]
    for subentry in entry.subentries.values():
        values.extend(subentry.data.values())
    return {v for v in values if isinstance(v, str)}


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    # A literal another entry still uses (shared password) must stay masked.
    in_use: set[str] = set()
    for other in hass.config_entries.async_entries(DOMAIN):
        if other.entry_id != entry.entry_id:
            in_use |= _entry_strings(other)
    LOG_SCRUBBER.forget(*(_entry_strings(entry) - in_use))


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow removing channel devices that no longer exist on the recorder."""
    bridge = getattr(entry, "runtime_data", None)
    if not isinstance(bridge, DeviceBridge):
        return True
    return device.id not in bridge.device_ids.values()
