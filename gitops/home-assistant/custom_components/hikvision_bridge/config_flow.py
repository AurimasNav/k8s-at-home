"""Config, reauth, reconfigure, options and channel-link flows."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import ipaddress
import secrets
from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryFlow,
    OptionsFlow,
    OptionsFlowWithReload,
    SubentryFlowResult,
)
from homeassistant.const import CONF_HOST, CONF_MAC, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol
from yarl import URL

from .const import (
    CONF_ACCOUNT,
    CONF_CAMERA_SERIAL,
    CONF_CHANNEL_ID,
    CONF_CONNECTION,
    CONF_ENTRY_KIND,
    CONF_EVENT_SOURCE,
    CONF_REGION_HOST,
    CONF_RTSP_PORT,
    CONF_SCHEME,
    CONF_SERIAL,
    CONF_STREAM_SOURCE,
    CONF_VERIFY_TLS,
    CONNECTION_AUTO,
    DEFAULT_HOLD_SECONDS,
    DEFAULT_PORTS,
    DOMAIN,
    EVENT_PATH_BOTH,
    EVENT_PATH_STREAM,
    EVENT_PATH_WEBHOOK,
    EVENT_SOURCE_CAMERA,
    EVENT_SOURCE_RECORDER,
    KIND_CLOUD,
    KIND_DEVICE,
    OPT_EVENT_PATH,
    OPT_HOLD_SECONDS,
    OPT_PANEL_ADMIN_ONLY,
    OPT_PLAYBACK,
    OPT_RECORDING_CLOCK,
    OPT_RTSP_PORT,
    OPT_RTSP_TRANSPORT,
    OPT_SIDEBAR_PANEL,
    OPT_STREAM_KEY,
    OPT_TARGET_SENSORS,
    OPT_TRY_ENCRYPTED_STREAM,
    OPT_WEBHOOK_ID,
    PLAYBACK_H264,
    PLAYBACK_MP4,
    PLAYBACK_STREAM,
    RECORDING_CLOCK_AUTO,
    RECORDING_CLOCK_DEVICE,
    RECORDING_CLOCK_UTC,
    RTSP_PORT_DEFAULT,
    RTSP_TCP,
    RTSP_UDP,
    SCHEME_HTTP,
    SCHEME_HTTPS,
    STREAM_SOURCE_CAMERA,
    STREAM_SOURCE_RECORDER,
    SUBENTRY_CHANNEL_LINK,
)
from .model.discover import read_identity_only
from .model.inventory import Identity
from .panel import PANEL_DEFAULTS, async_set_panel_settings, panel_settings
from .wire import faults
from .wire.link import Endpoint, IsapiLink

CONF_REUSE = "reuse_recorder_credentials"
CONF_CAPTCHA = "captcha"


@dataclass(frozen=True, slots=True)
class Address:
    """What a host field holds: a bare host or a pasted URL such as an RTSP link."""

    host: str
    port: int | None = None
    scheme: str | None = None
    rtsp_port: int | None = None


def split_address(text: str) -> Address:
    """Host, and what a pasted URL says about ports and scheme; its credentials are dropped."""
    text = text.strip()
    try:
        ipaddress.ip_address(text.strip("[]"))
    except ValueError:
        pass
    else:
        return Address(text.strip("[]"))
    if "://" not in text:
        text = f"//{text}"
    try:
        url = URL(text)
        port = url.explicit_port
    except ValueError:
        return Address(text.lstrip("/"))
    host = url.raw_host or text.lstrip("/")
    scheme = url.scheme.lower()
    if scheme in ("rtsp", "rtsps"):
        return Address(host, rtsp_port=port)
    if scheme in (SCHEME_HTTP, SCHEME_HTTPS):
        return Address(host, port, scheme)
    return Address(host, port)


def with_address(data: dict[str, Any]) -> tuple[dict[str, Any], int | None]:
    """Form input with the host field reduced to the host, and an RTSP port it named.

    Fields filled in by the user take precedence over what the URL says.
    """
    address = split_address(data[CONF_HOST])
    resolved = {**data, CONF_HOST: address.host}
    if address.port and not data.get(CONF_PORT):
        resolved[CONF_PORT] = address.port
    if address.scheme and data.get(CONF_CONNECTION, CONNECTION_AUTO) == CONNECTION_AUTO:
        resolved[CONF_CONNECTION] = address.scheme
    return resolved, address.rtsp_port


def _rtsp_option(port: int | None) -> dict[str, int]:
    """Options for an RTSP port taken from a pasted URL; the default port needs none."""
    return {OPT_RTSP_PORT: port} if port and port != RTSP_PORT_DEFAULT else {}


def _error_key(err: faults.BridgeFault) -> str:
    return {
        faults.BadCredentials: "invalid_auth",
        faults.LockedOut: "locked_out",
        faults.NameNotResolved: "name_not_resolved",
        faults.TlsUntrusted: "tls_untrusted",
        faults.InsecureAuth: "insecure_auth",
        faults.Unreachable: "cannot_connect",
        faults.NotIsapi: "not_isapi",
        faults.Forbidden: "forbidden",
    }.get(type(err), "unknown")


PORT_RANGE = (1, 65535)
HOLD_RANGE = (1, 300)


def field_errors(
    data: Mapping[str, Any],
    required: tuple[str, ...] = (),
    ranges: Mapping[str, tuple[tuple[int, int], str]] | None = None,
) -> dict[str, str]:
    """Errors shown under the fields themselves, instead of the browser's own form checks.

    Text fields are optional in the schema, so the browser does not block the form with
    "Please fill out this field"; an empty one is reported here, beside the field.
    """
    errors = {key: "required" for key in required if not str(data.get(key) or "").strip()}
    for key, ((low, high), reason) in (ranges or {}).items():
        value = data.get(key)
        if value not in (None, "") and not low <= int(value) <= high:
            errors[key] = reason
    return errors


def _port(key: str) -> dict[str, tuple[tuple[int, int], str]]:
    return {key: (PORT_RANGE, "invalid_port")}


def _required(key: str, defaults: Mapping[str, Any] | None = None) -> vol.Optional:
    """A field that must be filled, checked by field_errors rather than by the browser."""
    value = (defaults or {}).get(key)
    if value in (None, ""):
        return vol.Optional(key)
    return vol.Optional(key, description={"suggested_value": value})


async def probe_device(
    hass: HomeAssistant, data: dict[str, Any]
) -> tuple[dict[str, Any], Identity]:
    """Connect once per candidate scheme and read the identity.

    With "auto" HTTPS is tried first; HTTP is only tried when HTTPS cannot connect at all.
    An untrusted certificate ends the attempt: it may come from an impostor.
    An authentication failure ends the attempt immediately (lockout protection).
    """
    connection = data.get(CONF_CONNECTION, CONNECTION_AUTO)
    schemes = [SCHEME_HTTPS, SCHEME_HTTP] if connection == CONNECTION_AUTO else [connection]
    last: faults.BridgeFault | None = None
    for scheme in schemes:
        port = data.get(CONF_PORT) or DEFAULT_PORTS[scheme]
        verify = bool(data.get(CONF_VERIFY_TLS, True)) if scheme == SCHEME_HTTPS else True
        session = async_get_clientsession(hass, verify_ssl=verify)
        link = IsapiLink(
            session,
            Endpoint(data[CONF_HOST].strip(), int(port), scheme),
            data[CONF_USERNAME],
            data[CONF_PASSWORD],
        )
        try:
            identity = await read_identity_only(link)
        except faults.TlsUntrusted:
            # An untrusted certificate may be an impostor: never retry it over plain HTTP.
            raise
        except (faults.Unreachable, faults.NotIsapi) as err:
            last = err
            continue
        resolved = {
            CONF_HOST: data[CONF_HOST].strip(),
            CONF_PORT: int(port),
            CONF_SCHEME: scheme,
            CONF_VERIFY_TLS: verify,
            CONF_USERNAME: data[CONF_USERNAME],
            CONF_PASSWORD: data[CONF_PASSWORD],
        }
        return resolved, identity
    assert last is not None
    raise last


def _connection_schema(defaults: Mapping[str, Any], *, with_host: bool = True) -> vol.Schema:
    fields: dict[Any, Any] = {}
    if with_host:
        fields[_required(CONF_HOST, defaults)] = str
        fields[
            vol.Optional(CONF_PORT, description={"suggested_value": defaults.get(CONF_PORT)})
        ] = vol.Coerce(int)
        fields[
            vol.Required(CONF_CONNECTION, default=defaults.get(CONF_CONNECTION, CONNECTION_AUTO))
        ] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[CONNECTION_AUTO, SCHEME_HTTPS, SCHEME_HTTP],
                translation_key="connection",
            )
        )
        fields[vol.Required(CONF_VERIFY_TLS, default=defaults.get(CONF_VERIFY_TLS, True))] = bool
    fields[_required(CONF_USERNAME, defaults)] = str
    fields[_required(CONF_PASSWORD)] = selector.TextSelector(
        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
    )
    return vol.Schema(fields)


_CONNECTION_FIELDS = (CONF_HOST, CONF_USERNAME, CONF_PASSWORD)
_CREDENTIAL_FIELDS = (CONF_USERNAME, CONF_PASSWORD)


def _recorder_entries_with_camera(
    hass: HomeAssistant, serial: str
) -> list[tuple[ConfigEntry, str]]:
    """Recorder entries that already show the camera with this serial as a channel."""
    found = []
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        bridge = getattr(entry, "runtime_data", None)
        profile = getattr(bridge, "profile", None)
        if profile is None:
            continue
        for channel in profile.channels.values():
            if channel.camera_serial and channel.camera_serial == serial:
                found.append((entry, channel.channel_id))
    return found


def _masked_account(account: str) -> str:
    """Short form of the account for titles: enough to tell accounts apart, not the address."""
    name, at, domain = account.partition("@")
    if at:
        return f"{name[:1]}***@{domain}"
    return f"***{account[-3:]}" if len(account) > 3 else "***"


class BridgeConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._pending: dict[str, Any] = {}
        self._identity: Identity | None = None
        self._recorder_match: tuple[ConfigEntry, str] | None = None
        self._rtsp_port: int | None = None
        self._cloud: Any = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return BridgeOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        if config_entry.data.get(CONF_ENTRY_KIND) == KIND_CLOUD:
            return {}
        return {SUBENTRY_CHANNEL_LINK: ChannelLinkFlow}

    # ----------------------------------------------------------------- user

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["device", "cloud"])

    async def async_step_device(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = field_errors(user_input, _CONNECTION_FIELDS, _port(CONF_PORT))
        if user_input is not None and not errors:
            candidate, self._rtsp_port = with_address(user_input)
            try:
                resolved, identity = await probe_device(self.hass, candidate)
            except faults.BridgeFault as err:
                errors["base"] = _error_key(err)
            else:
                await self.async_set_unique_id(identity.stable_id)
                self._abort_if_unique_id_configured()
                self._pending = resolved
                self._identity = identity
                matches = _recorder_entries_with_camera(self.hass, identity.serial)
                if matches:
                    self._recorder_match = matches[0]
                    return await self.async_step_duplicate()
                return self._create_device_entry()
        return self.async_show_form(
            step_id="device",
            data_schema=self.add_suggested_values_to_schema(
                _connection_schema(user_input or {}), user_input or {}
            ),
            errors=errors,
        )

    async def async_step_duplicate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._recorder_match is not None
        entry, channel_id = self._recorder_match
        return self.async_show_menu(
            step_id="duplicate",
            menu_options=["link_to_channel", "keep_separate"],
            description_placeholders={"recorder": entry.title, "channel": channel_id},
        )

    async def async_step_link_to_channel(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._recorder_match is not None
        assert self._identity is not None
        entry, channel_id = self._recorder_match
        if any(
            s.unique_id == channel_id
            for s in entry.subentries.values()
            if s.subentry_type == SUBENTRY_CHANNEL_LINK
        ):
            return self.async_abort(reason="channel_already_linked")
        self.hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                data=MappingProxyType(
                    {
                        **self._pending,
                        CONF_CHANNEL_ID: channel_id,
                        CONF_CAMERA_SERIAL: self._identity.serial,
                        CONF_EVENT_SOURCE: EVENT_SOURCE_RECORDER,
                    }
                ),
                subentry_type=SUBENTRY_CHANNEL_LINK,
                title=f"{self._identity.name} ({channel_id})",
                unique_id=channel_id,
            ),
        )
        return self.async_abort(reason="linked_to_channel")

    async def async_step_keep_separate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self._create_device_entry()

    def _create_device_entry(self) -> ConfigFlowResult:
        assert self._identity is not None
        return self.async_create_entry(
            title=self._identity.name,
            data={
                CONF_ENTRY_KIND: KIND_DEVICE,
                **self._pending,
                CONF_SERIAL: self._identity.serial,
                CONF_MAC: self._identity.mac,
            },
            options=_rtsp_option(self._rtsp_port),
        )

    # --------------------------------------------------------------- reauth

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        if entry_data.get(CONF_ENTRY_KIND) == KIND_CLOUD:
            return await self.async_step_cloud_reauth()
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = field_errors(user_input, _CREDENTIAL_FIELDS)
        if user_input is not None and not errors:
            candidate = {
                **entry.data,
                CONF_CONNECTION: entry.data[CONF_SCHEME],
                CONF_USERNAME: user_input[CONF_USERNAME],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            }
            try:
                resolved, identity = await probe_device(self.hass, candidate)
            except faults.BridgeFault as err:
                errors["base"] = _error_key(err)
            else:
                await self.async_set_unique_id(identity.stable_id)
                self._abort_if_unique_id_mismatch(reason="other_device")
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_USERNAME: resolved[CONF_USERNAME],
                        CONF_PASSWORD: resolved[CONF_PASSWORD],
                    },
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_connection_schema(
                {CONF_USERNAME: (user_input or entry.data).get(CONF_USERNAME)}, with_host=False
            ),
            description_placeholders={"device": entry.title, "host": entry.data[CONF_HOST]},
            errors=errors,
        )

    # ---------------------------------------------------------- reconfigure

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        if entry.data.get(CONF_ENTRY_KIND) == KIND_CLOUD:
            return await self.async_step_cloud_reauth()
        errors: dict[str, str] = {}
        defaults = {**entry.data, CONF_CONNECTION: entry.data.get(CONF_SCHEME, CONNECTION_AUTO)}
        if user_input is not None:
            errors = field_errors(user_input, _CONNECTION_FIELDS, _port(CONF_PORT))
        if user_input is not None and not errors:
            candidate, rtsp_port = with_address(user_input)
            try:
                resolved, identity = await probe_device(self.hass, candidate)
            except faults.BridgeFault as err:
                errors["base"] = _error_key(err)
            else:
                await self.async_set_unique_id(identity.stable_id)
                self._abort_if_unique_id_mismatch(reason="other_device")
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates=resolved,
                    options={**entry.options, **_rtsp_option(rtsp_port)},
                )
        if user_input is not None:
            defaults = {**defaults, **user_input}
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_connection_schema(defaults),
            errors=errors,
        )

    # ---------------------------------------------------------------- cloud

    async def async_step_cloud(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        from .cloud.account import CaptchaRequired, CloudAccount, CloudError  # noqa: PLC0415

        errors: dict[str, str] = {}
        if user_input is not None:
            errors = field_errors(user_input, (CONF_ACCOUNT, CONF_PASSWORD))
        if user_input is not None and not errors:
            account = CloudAccount(
                async_get_clientsession(self.hass),
                user_input[CONF_ACCOUNT],
                user_input[CONF_PASSWORD],
            )
            try:
                await account.login(captcha=user_input.get(CONF_CAPTCHA))
            except CaptchaRequired as err:
                self._cloud = (user_input, err.image)
                return await self.async_step_cloud_captcha()
            except CloudError as err:
                errors["base"] = err.reason
            else:
                return await self._finish_cloud(account, user_input)
        return self.async_show_form(
            step_id="cloud",
            data_schema=vol.Schema(
                {
                    _required(CONF_ACCOUNT, user_input): str,
                    _required(CONF_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_cloud_captcha(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        stored, image = self._cloud
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = field_errors(user_input, (CONF_CAPTCHA,))
        if user_input is not None and not errors:
            return await self.async_step_cloud({**stored, CONF_CAPTCHA: user_input[CONF_CAPTCHA]})
        return self.async_show_form(
            step_id="cloud_captcha",
            data_schema=vol.Schema({_required(CONF_CAPTCHA): str}),
            description_placeholders={"image": image or ""},
            errors=errors,
        )

    async def async_step_cloud_reauth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        from .cloud.account import CaptchaRequired, CloudAccount, CloudError  # noqa: PLC0415

        entry = (
            self._get_reauth_entry() if self.source == "reauth" else self._get_reconfigure_entry()
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = field_errors(user_input, (CONF_PASSWORD,))
        if user_input is not None and not errors:
            account = CloudAccount(
                async_get_clientsession(self.hass),
                entry.data[CONF_ACCOUNT],
                user_input[CONF_PASSWORD],
            )
            try:
                await account.login(captcha=user_input.get(CONF_CAPTCHA))
            except CaptchaRequired:
                errors["base"] = "captcha_required"
            except CloudError as err:
                errors["base"] = err.reason
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_REGION_HOST: account.region_host,
                    },
                )
        return self.async_show_form(
            step_id="cloud_reauth",
            data_schema=vol.Schema(
                {
                    _required(CONF_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                    vol.Optional(CONF_CAPTCHA): str,
                }
            ),
            description_placeholders={"account": entry.data[CONF_ACCOUNT]},
            errors=errors,
        )

    async def _finish_cloud(self, account: Any, user_input: dict[str, Any]) -> ConfigFlowResult:
        await self.async_set_unique_id(f"cloud_{account.account_id}")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"Hik-Connect ({_masked_account(user_input[CONF_ACCOUNT])})",
            data={
                CONF_ENTRY_KIND: KIND_CLOUD,
                CONF_ACCOUNT: user_input[CONF_ACCOUNT],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
                CONF_REGION_HOST: account.region_host,
            },
        )


def _panel_fields(hass: HomeAssistant) -> dict[Any, Any]:
    """The sidebar panel serves all entries; every entry's options show the same settings."""
    current = panel_settings(hass)
    return {
        vol.Required(OPT_SIDEBAR_PANEL, default=current[OPT_SIDEBAR_PANEL]): bool,
        vol.Required(OPT_PANEL_ADMIN_ONLY, default=current[OPT_PANEL_ADMIN_ONLY]): bool,
    }


class BridgeOptionsFlow(OptionsFlowWithReload):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = dict(self.config_entry.options)
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = field_errors(
                user_input,
                ranges={
                    OPT_HOLD_SECONDS: (HOLD_RANGE, "invalid_hold_seconds"),
                    **_port(OPT_RTSP_PORT),
                },
            )
        if user_input is not None and not errors:
            user_input = dict(user_input)
            await async_set_panel_settings(
                self.hass,
                {key: user_input.pop(key) for key in PANEL_DEFAULTS if key in user_input},
            )
            options.update(user_input)
            if not user_input.get(OPT_STREAM_KEY):
                options.pop(OPT_STREAM_KEY, None)
            if options.get(OPT_EVENT_PATH) in (EVENT_PATH_WEBHOOK, EVENT_PATH_BOTH):
                options.setdefault(OPT_WEBHOOK_ID, secrets.token_hex(16))
                self._options = options
                return await self.async_step_webhook()
            return self.async_create_entry(data=options)
        if self.config_entry.data.get(CONF_ENTRY_KIND) == KIND_CLOUD:
            return self.async_show_form(
                step_id="init", data_schema=vol.Schema(_panel_fields(self.hass))
            )
        schema = vol.Schema(
            {
                vol.Required(
                    OPT_EVENT_PATH, default=options.get(OPT_EVENT_PATH, EVENT_PATH_STREAM)
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[EVENT_PATH_STREAM, EVENT_PATH_WEBHOOK, EVENT_PATH_BOTH],
                        translation_key="event_path",
                    )
                ),
                vol.Required(
                    OPT_HOLD_SECONDS, default=options.get(OPT_HOLD_SECONDS, DEFAULT_HOLD_SECONDS)
                ): vol.Coerce(int),
                vol.Optional(
                    OPT_RTSP_PORT, description={"suggested_value": options.get(OPT_RTSP_PORT)}
                ): vol.Coerce(int),
                vol.Required(
                    OPT_RTSP_TRANSPORT, default=options.get(OPT_RTSP_TRANSPORT, RTSP_TCP)
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[RTSP_TCP, RTSP_UDP], translation_key="rtsp_transport"
                    )
                ),
                vol.Required(
                    OPT_TARGET_SENSORS, default=options.get(OPT_TARGET_SENSORS, False)
                ): bool,
                vol.Required(
                    OPT_PLAYBACK, default=options.get(OPT_PLAYBACK, PLAYBACK_MP4)
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[PLAYBACK_MP4, PLAYBACK_H264, PLAYBACK_STREAM],
                        translation_key="playback",
                    )
                ),
                vol.Required(
                    OPT_RECORDING_CLOCK,
                    default=options.get(OPT_RECORDING_CLOCK, RECORDING_CLOCK_AUTO),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[RECORDING_CLOCK_AUTO, RECORDING_CLOCK_DEVICE, RECORDING_CLOCK_UTC],
                        translation_key="recording_clock",
                    )
                ),
                vol.Optional(
                    OPT_STREAM_KEY, description={"suggested_value": options.get(OPT_STREAM_KEY)}
                ): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Required(
                    OPT_TRY_ENCRYPTED_STREAM,
                    default=options.get(OPT_TRY_ENCRYPTED_STREAM, False),
                ): bool,
                **_panel_fields(self.hass),
            }
        )
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)

    async def async_step_webhook(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=self._options)
        return self.async_show_form(
            step_id="webhook",
            data_schema=vol.Schema({}),
            description_placeholders={"path": f"/api/webhook/{self._options[OPT_WEBHOOK_ID]}"},
        )


def _recorder_vouches_for(host: str, channel: Any, recorder_host: str) -> bool:
    """Whether the recorder itself names this address for the channel.

    The recorder's credentials are only sent to the camera address the recorder reports, or
    to the recorder's own address (port forwarding to the camera); never to an address
    typed in for a device that has not proven its identity yet.
    """
    wanted = host.strip().lower()
    return wanted in {a.strip().lower() for a in (channel.camera_address, recorder_host) if a}


class ChannelLinkFlow(ConfigSubentryFlow):
    """Attach a direct connection (own address and credentials) to a recorder channel."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return await self._form(user_input, None)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return await self._form(user_input, self._get_reconfigure_subentry())

    async def _form(self, user_input: dict[str, Any] | None, existing: Any) -> SubentryFlowResult:
        entry = self._get_entry()
        bridge = getattr(entry, "runtime_data", None)
        profile = getattr(bridge, "profile", None)
        if profile is None:
            return self.async_abort(reason="not_loaded")
        if not profile.shape.records:
            return self.async_abort(reason="not_a_recorder")
        taken = {
            s.unique_id
            for s in entry.subentries.values()
            if s.subentry_type == SUBENTRY_CHANNEL_LINK and s is not existing
        }
        choices = [c for c in profile.channels.values() if c.channel_id not in taken]
        if not choices:
            return self.async_abort(reason="no_free_channel")
        errors: dict[str, str] = {}
        defaults: dict[str, Any] = dict(existing.data) if existing else {}
        if user_input is not None:
            errors = field_errors(
                user_input,
                (CONF_HOST,) if user_input.get(CONF_REUSE) else _CONNECTION_FIELDS,
                {**_port(CONF_PORT), **_port(CONF_RTSP_PORT)},
            )
        if user_input is not None and not errors:
            channel = profile.channels[user_input[CONF_CHANNEL_ID]]
            data, rtsp_port = with_address(user_input)
            if data.pop(CONF_REUSE, False):
                if not _recorder_vouches_for(data[CONF_HOST], channel, entry.data[CONF_HOST]):
                    errors["base"] = "reuse_unknown_address"
                data[CONF_USERNAME] = entry.data[CONF_USERNAME]
                data[CONF_PASSWORD] = entry.data[CONF_PASSWORD]
        if user_input is not None and not errors:
            try:
                resolved, identity = await probe_device(self.hass, data)
            except faults.BridgeFault as err:
                errors["base"] = _error_key(err)
            else:
                # A camera that reports no serial cannot prove it is the channel's camera.
                if channel.camera_serial and identity.serial != channel.camera_serial:
                    errors["base"] = "wrong_camera"
                else:
                    payload = {
                        **resolved,
                        CONF_CHANNEL_ID: channel.channel_id,
                        CONF_CAMERA_SERIAL: identity.serial,
                        CONF_EVENT_SOURCE: user_input[CONF_EVENT_SOURCE],
                        CONF_STREAM_SOURCE: user_input[CONF_STREAM_SOURCE],
                    }
                    rtsp_port = user_input.get(CONF_RTSP_PORT) or rtsp_port
                    if rtsp_port:
                        payload[CONF_RTSP_PORT] = int(rtsp_port)
                    title = f"{channel.name} ({channel.channel_id})"
                    if existing is not None:
                        return self.async_update_and_abort(
                            entry, existing, data=payload, title=title, unique_id=channel.channel_id
                        )
                    return self.async_create_entry(
                        title=title, data=payload, unique_id=channel.channel_id
                    )
        if user_input is not None:
            defaults = {**defaults, **user_input}
        first = choices[0]
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_CHANNEL_ID, default=defaults.get(CONF_CHANNEL_ID, first.channel_id)
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(
                                value=c.channel_id, label=f"{c.channel_id}: {c.name}"
                            )
                            for c in choices
                        ]
                    )
                ),
                _required(
                    CONF_HOST, {CONF_HOST: defaults.get(CONF_HOST, first.camera_address)}
                ): str,
                vol.Optional(
                    CONF_PORT, description={"suggested_value": defaults.get(CONF_PORT)}
                ): vol.Coerce(int),
                vol.Optional(
                    CONF_RTSP_PORT, description={"suggested_value": defaults.get(CONF_RTSP_PORT)}
                ): vol.Coerce(int),
                vol.Required(
                    CONF_CONNECTION, default=defaults.get(CONF_SCHEME, CONNECTION_AUTO)
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[CONNECTION_AUTO, SCHEME_HTTPS, SCHEME_HTTP],
                        translation_key="connection",
                    )
                ),
                vol.Required(CONF_VERIFY_TLS, default=defaults.get(CONF_VERIFY_TLS, True)): bool,
                vol.Required(CONF_REUSE, default=False): bool,
                vol.Optional(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")): str,
                vol.Optional(CONF_PASSWORD, default=""): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Required(
                    CONF_EVENT_SOURCE,
                    default=defaults.get(CONF_EVENT_SOURCE, EVENT_SOURCE_RECORDER),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[EVENT_SOURCE_RECORDER, EVENT_SOURCE_CAMERA],
                        translation_key="event_source",
                    )
                ),
                vol.Required(
                    CONF_STREAM_SOURCE,
                    default=defaults.get(CONF_STREAM_SOURCE, STREAM_SOURCE_CAMERA),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[STREAM_SOURCE_CAMERA, STREAM_SOURCE_RECORDER],
                        translation_key="stream_source",
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="reconfigure" if existing else "user", data_schema=schema, errors=errors
        )
