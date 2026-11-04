"""Runtime object of one device entry (stored in ``entry.runtime_data``)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from ..const import (
    AUDIT_INTERVAL,
    CONF_CAMERA_SERIAL,
    CONF_CHANNEL_ID,
    CONF_EVENT_SOURCE,
    CONF_RTSP_PORT,
    CONF_SCHEME,
    CONF_STREAM_SOURCE,
    CONF_VERIFY_TLS,
    DEFAULT_HOLD_SECONDS,
    DOMAIN,
    EVENT_PATH_STREAM,
    EVENT_PATH_WEBHOOK,
    EVENT_SOURCE_CAMERA,
    INTERCOM_INTERVAL,
    OPT_EVENT_PATH,
    OPT_HOLD_SECONDS,
    RTSP_PORT_DEFAULT,
    SCHEME_HTTPS,
    STREAM_SOURCE_RECORDER,
    SUBENTRY_CHANNEL_LINK,
)
from ..model import audit
from ..model.discover import read_identity_only
from ..model.features import adjust, detection, health, intercom, io, ptz, storage
from ..model.inventory import CloudLinkState, DeviceProfile, DeviceShape
from ..model.vocabulary import Kind
from ..wire.faults import BadCredentials, BridgeFault, LockedOut
from ..wire.feed import AlarmFeed
from ..wire.link import Endpoint, IsapiLink
from ..wire.scrub import scrubbed_logger
from . import identity
from .refresh import StatusRefresher
from .router import EventRouter, Origin

_LOGGER = scrubbed_logger(__name__)
MAIN_FEED = "main"


def build_link(hass: HomeAssistant, data: dict) -> IsapiLink:
    scheme = data.get(CONF_SCHEME, "http")
    verify = bool(data.get(CONF_VERIFY_TLS, True)) if scheme == SCHEME_HTTPS else True
    session = async_get_clientsession(hass, verify_ssl=verify)
    return IsapiLink(
        session,
        Endpoint(data[CONF_HOST], int(data[CONF_PORT]), scheme),
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
        trusted=True,
    )


def direct_rtsp_port(data: dict, recorder_host: str) -> int:
    """RTSP port of a direct camera connection.

    A camera at the recorder's own address is reached through the recorder's port forwarding,
    which carries RTSP on the same port as HTTP.
    """
    if data.get(CONF_RTSP_PORT):
        return int(data[CONF_RTSP_PORT])
    if data[CONF_HOST].strip().lower() == recorder_host.strip().lower():
        return int(data[CONF_PORT])
    return RTSP_PORT_DEFAULT


@dataclass(slots=True)
class ChannelRoute:
    """A direct connection to the camera behind a recorder channel."""

    subentry_id: str
    channel_id: str
    link: IsapiLink
    remote_channel: str = "1"
    rtsp_port: int = RTSP_PORT_DEFAULT
    events_from_camera: bool = False
    stream_from_recorder: bool = False
    healthy: bool = True


@dataclass(slots=True)
class DeviceBridge:
    hass: HomeAssistant
    entry: ConfigEntry
    link: IsapiLink
    profile: DeviceProfile
    refresher: StatusRefresher = field(init=False)
    router: EventRouter = field(init=False)
    routes: dict[str, ChannelRoute] = field(default_factory=dict)
    feeds: dict[str, AlarmFeed] = field(default_factory=dict)
    report: audit.AuditReport | None = None
    device_ids: dict[str | None, str] = field(default_factory=dict)
    _unsubs: list[CALLBACK_TYPE] = field(default_factory=list)
    _last_audit: datetime | None = None
    _intercom_busy: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.refresher = StatusRefresher(self.hass, self.entry, self)
        hold = float(self.entry.options.get(OPT_HOLD_SECONDS, DEFAULT_HOLD_SECONDS))
        self.router = EventRouter(self.hass, self, hold)

    # ------------------------------------------------------------- routing

    def route(self, channel_id: str) -> tuple[IsapiLink, str]:
        """Which link and channel id to use for configuration of an entry channel."""
        direct = self.routes.get(channel_id)
        if direct is not None and direct.healthy:
            return direct.link, direct.remote_channel
        return self.link, channel_id

    def detection_route(self, channel_id: str, kind: Kind) -> tuple[IsapiLink, str]:
        """Like ``route``, but detections the recorder watches itself stay on the recorder."""
        if kind in detection.RECORDER_ONLY:
            return self.link, channel_id
        return self.route(channel_id)

    def reaches_camera(self, channel_id: str) -> bool:
        """Whether settings of the camera itself are reachable for this channel."""
        direct = self.routes.get(channel_id)
        return (direct is not None and direct.healthy) or not self.profile.shape.records

    def channel_address(self, channel_id: str | None) -> str | None:
        """Network address of the camera of a channel (or of the device itself)."""
        if channel_id is None:
            return self.link.endpoint.host
        direct = self.routes.get(channel_id)
        if direct is not None:
            return direct.link.endpoint.host
        channel = self.profile.channels.get(channel_id)
        if channel is not None and channel.camera_address:
            return channel.camera_address
        return None if identity.uses_channel_devices(self.profile) else self.link.endpoint.host

    def is_main_link_fault(self, channel_id: str) -> bool:
        direct = self.routes.get(channel_id)
        return direct is None or not direct.healthy

    def ha_device_id(self, channel_id: str | None) -> str | None:
        if channel_id is not None and channel_id in self.device_ids:
            return self.device_ids[channel_id]
        return self.device_ids.get(None)

    # ----------------------------------------------------------- lifecycle

    async def async_prepare_routes(self) -> None:
        for subentry in self.entry.subentries.values():
            if subentry.subentry_type != SUBENTRY_CHANNEL_LINK:
                continue
            data = dict(subentry.data)
            channel_id = data[CONF_CHANNEL_ID]
            route = ChannelRoute(
                subentry_id=subentry.subentry_id,
                channel_id=channel_id,
                link=build_link(self.hass, data),
                rtsp_port=direct_rtsp_port(data, self.link.endpoint.host),
                events_from_camera=data.get(CONF_EVENT_SOURCE) == EVENT_SOURCE_CAMERA,
                stream_from_recorder=data.get(CONF_STREAM_SOURCE) == STREAM_SOURCE_RECORDER,
            )
            self.routes[channel_id] = route
            try:
                found = await read_identity_only(route.link)
            except BridgeFault as err:
                self.report_channel_fault(channel_id, err)
                continue
            expected = data.get(CONF_CAMERA_SERIAL)
            if expected and found.serial and found.serial != expected:
                route.healthy = False
                self._issue(
                    f"channel_mismatch_{channel_id}",
                    "channel_link_mismatch",
                    {"channel": self._channel_name(channel_id)},
                )
            else:
                ir.async_delete_issue(
                    self.hass, DOMAIN, self._issue_id(f"channel_auth_{channel_id}")
                )

    async def async_probe_extras(self) -> None:
        """Find adjustable settings, PTZ, status, recording and armable inputs (GET only).

        Camera-side resources are read over a direct connection where one exists. Faults of a
        direct connection are reported and skipped; refused logins on the main link raise.
        """
        profile = self.profile
        records = profile.shape.records
        profile.health = await health.read_status(self.link, memory_in_mb=records) is not None
        if records:
            profile.recording = bool(await health.read_recording(self.link))
        armed = await io.read_armed(self.link) or {}
        profile.armable_inputs = tuple(p for p in armed if p in profile.inputs)
        for channel in profile.channels.values():
            if channel.online is False:
                continue
            channel_id = channel.channel_id
            link, remote = self.route(channel_id)
            reaches_camera = self.reaches_camera(channel_id)
            try:
                found = {}
                for source in adjust.SOURCES.values():
                    if source.direct_only and not reaches_camera:
                        continue
                    found.update(await source.read_capabilities(link, remote))
                channel.image = found
                channel.ptz = await ptz.read_capabilities(link, remote)
                if link is not self.link:
                    status = await health.read_status(link, memory_in_mb=False)
                    channel.health = status is not None
                    channel.storage = await storage.read(link)
                    channel.armable_inputs = tuple(await io.read_armed(link) or {})
            except (BadCredentials, LockedOut) as err:
                if self.is_main_link_fault(channel_id):
                    raise
                self.report_channel_fault(channel_id, err)
            except BridgeFault as err:
                _LOGGER.debug("Channel capabilities incomplete: %s", type(err).__name__)

    @callback
    def register_devices(self) -> None:
        registry = dr.async_get(self.hass)
        main = registry.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            **identity.main_device_info(self.profile, self.link.endpoint.base),
        )
        self.device_ids[None] = main.id
        if not identity.uses_channel_devices(self.profile):
            for channel_id in self.profile.channels:
                self.device_ids[channel_id] = main.id
            return
        for channel in self.profile.channels.values():
            route = self.routes.get(channel.channel_id)
            subentry_id = route.subentry_id if route else None
            info = identity.channel_device_info(self.profile, channel, main.id)
            if route is not None:
                info["configuration_url"] = route.link.endpoint.base
            # A channel with a direct camera link belongs to that link; move it explicitly
            # when a link was added or removed since the device was created.
            existing = registry.async_get_device_by_identifier(
                next(iter(info["identifiers"])), self.entry.entry_id
            )
            if existing is not None and existing.config_subentry_id != subentry_id:
                registry.async_update_device(existing.id, new_config_subentry_id=subentry_id)
            device = registry.async_get_or_create(
                config_entry_id=self.entry.entry_id, config_subentry_id=subentry_id, **info
            )
            self.device_ids[channel.channel_id] = device.id

    def subentry_of(self, channel_id: str | None) -> str | None:
        """The channel link sub-entry whose device shows this channel's entities."""
        if channel_id is None or self.device_ids.get(channel_id) == self.device_ids.get(None):
            return None
        route = self.routes.get(channel_id)
        return route.subentry_id if route else None

    def start_feeds(self) -> None:
        if self.profile.shape == DeviceShape.INTERCOM:
            return
        path = self.entry.options.get(OPT_EVENT_PATH, EVENT_PATH_STREAM)
        if path == EVENT_PATH_WEBHOOK:
            return
        camera_channels = frozenset(
            cid for cid, route in self.routes.items() if route.events_from_camera and route.healthy
        )
        main = AlarmFeed(
            self.link,
            self.router.make_handler(Origin(MAIN_FEED, exclusive_channels=camera_channels)),
            lambda up, fault: self.router.feed_state(MAIN_FEED, up, fault),
        )
        self.feeds[MAIN_FEED] = main
        for channel_id in camera_channels:
            route = self.routes[channel_id]
            name = f"channel_{channel_id}"
            self.feeds[name] = AlarmFeed(
                route.link,
                self.router.make_handler(
                    Origin(name, channel_map={route.remote_channel: channel_id})
                ),
                lambda up, fault, n=name: self.router.feed_state(n, up, fault),
            )
        for feed in self.feeds.values():
            feed.start(
                lambda coro, name: self.entry.async_create_background_task(self.hass, coro, name)
            )

    def schedule_audit(self) -> None:
        self.entry.async_create_background_task(self.hass, self.async_run_audit(), "audit")
        self._unsubs.append(async_track_time_interval(self.hass, self._audit_tick, AUDIT_INTERVAL))

    @callback
    def _audit_tick(self, _now: datetime) -> None:
        self.entry.async_create_background_task(self.hass, self.async_run_audit(), "audit")

    async def async_run_audit(self) -> None:
        main_feed = self.feeds.get(MAIN_FEED)
        skew = (
            main_feed.clock_offset.total_seconds()
            if main_feed and main_feed.stats.heartbeats
            else None
        )
        try:
            report = await audit.run_audit(
                self.link,
                self.profile,
                insecure_basic=self.link.auth_scheme == "basic"
                and self.link.endpoint.scheme == "http",
                clock_skew_seconds=skew,
            )
        except BridgeFault as err:
            _LOGGER.debug("Audit skipped: %s", type(err).__name__)
            return
        self.report = report
        self._publish_audit(report)

    def schedule_intercom_poll(self) -> None:
        caps = self.profile.intercom
        if caps is None or not caps.has_call_status:
            return
        self.entry.async_create_task(self.hass, self.async_poll_intercom(), "intercom_poll")
        self._unsubs.append(
            async_track_time_interval(self.hass, self._intercom_tick, INTERCOM_INTERVAL)
        )

    @callback
    def _intercom_tick(self, _now: datetime) -> None:
        self.entry.async_create_background_task(
            self.hass, self.async_poll_intercom(), "intercom_poll"
        )

    async def async_poll_intercom(self) -> None:
        if self._intercom_busy:
            return
        self._intercom_busy = True
        try:
            status = await intercom.read_call_status(self.link)
            if status is None:
                return
            current = self.refresher.data.call_status if self.refresher.data else None
            if status in ("ringing", "on_call") and current not in ("ringing", "on_call"):
                self.router.fire_doorbell()
            if self.refresher.data is not None and self.refresher.data.call_status != status:
                new_data = replace(self.refresher.data, call_status=status)
                self.refresher.async_set_updated_data(new_data)
        except (BadCredentials, LockedOut) as err:
            if isinstance(err, LockedOut):
                self.report_lockout(err)
            elif isinstance(err, BadCredentials):
                self.entry.async_start_reauth(self.hass)
        except BridgeFault as err:
            _LOGGER.debug("Intercom poll skipped: %s", type(err).__name__)
        finally:
            self._intercom_busy = False

    @callback
    def update_cloud_state(self, state: CloudLinkState) -> None:
        """Take over re-read Hik-Connect settings and keep the encryption repair in step."""
        if state == self.profile.cloud:
            return
        self.profile.cloud = state
        if self.report is None:
            return
        items = [i for i in self.report.items if i.finding != audit.Finding.STREAM_ENCRYPTED]
        if state.stream_encrypted:
            items.append(audit.AuditItem(audit.Finding.STREAM_ENCRYPTED))
        self.report.items = sorted(items, key=lambda item: item.key)
        self._publish_audit(self.report)

    @callback
    def _publish_audit(self, report: audit.AuditReport) -> None:
        wanted: set[str] = set()
        for item in report.items:
            issue_key = f"audit_{item.key}"
            placeholders = {
                "device": self.entry.title,
                "channel": self._channel_name(item.channel_id) if item.channel_id else "",
                "kind": item.kind.value if item.kind else "",
                "detail": item.detail or "",
            }
            fixable = item.finding == audit.Finding.CENTER_LINK_MISSING
            self._issue(
                issue_key,
                item.finding.value,
                placeholders,
                fixable=fixable,
                data={"trigger_id": item.trigger_id} if item.trigger_id else None,
                severity=ir.IssueSeverity.ERROR
                if item.finding == audit.Finding.DEFAULT_PASSWORD
                else ir.IssueSeverity.WARNING,
            )
            wanted.add(self._issue_id(issue_key))
        prefix = self._issue_id("audit_")
        registry = ir.async_get(self.hass)
        for domain, issue_id in list(registry.issues):
            if domain == DOMAIN and issue_id.startswith(prefix) and issue_id not in wanted:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    async def async_shutdown(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for feed in self.feeds.values():
            await feed.stop()
        self.feeds.clear()
        self.router.shutdown()

    # --------------------------------------------------------- reporting

    def _issue_id(self, key: str) -> str:
        return f"{self.entry.entry_id}_{key}"

    def _channel_name(self, channel_id: str | None) -> str:
        if channel_id is None:
            return ""
        channel = self.profile.channels.get(channel_id)
        return channel.name if channel else channel_id

    def _issue(
        self,
        key: str,
        translation_key: str,
        placeholders: dict[str, str],
        *,
        fixable: bool = False,
        data: dict | None = None,
        severity: ir.IssueSeverity = ir.IssueSeverity.WARNING,
    ) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._issue_id(key),
            is_fixable=fixable,
            is_persistent=False,
            severity=severity,
            translation_key=translation_key,
            translation_placeholders={"device": self.entry.title, **placeholders},
            data={"entry_id": self.entry.entry_id, **(data or {})},
        )

    @callback
    def report_lockout(self, err: LockedOut) -> None:
        minutes = str(round((err.retry_after or 1800) / 60))
        self._issue("locked", "locked_out", {"minutes": minutes}, severity=ir.IssueSeverity.ERROR)

    @callback
    def report_channel_fault(self, channel_id: str, err: BaseException) -> None:
        route = self.routes.get(channel_id)
        if route is None:
            return
        route.healthy = False
        if isinstance(err, (BadCredentials, LockedOut)):
            self._issue(
                f"channel_auth_{channel_id}",
                "channel_link_auth",
                {"channel": self._channel_name(channel_id)},
                severity=ir.IssueSeverity.ERROR,
            )
        else:
            _LOGGER.info(
                "Direct link of channel %s unavailable (%s); using the recorder instead",
                channel_id,
                type(err).__name__,
            )

    @callback
    def report_feed_fault(self, name: str, fault: BridgeFault) -> None:
        if isinstance(fault, LockedOut):
            self.report_lockout(fault)
        elif isinstance(fault, BadCredentials):
            if name == MAIN_FEED:
                self.entry.async_start_reauth(self.hass)
            else:
                self.report_channel_fault(name.removeprefix("channel_"), fault)
