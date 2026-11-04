"""Constants shared across the hikvision_bridge integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "hikvision_bridge"
BUS_EVENT: Final = f"{DOMAIN}_event"

# Config entry kinds
CONF_ENTRY_KIND: Final = "entry_kind"
KIND_DEVICE: Final = "device"
KIND_CLOUD: Final = "cloud"

# Device connection data
CONF_SCHEME: Final = "scheme"
CONF_VERIFY_TLS: Final = "verify_tls"
CONF_SERIAL: Final = "serial"
CONF_CONNECTION: Final = "connection"

SCHEME_HTTP: Final = "http"
SCHEME_HTTPS: Final = "https"
CONNECTION_AUTO: Final = "auto"
DEFAULT_PORTS: Final = {SCHEME_HTTP: 80, SCHEME_HTTPS: 443}

# Cloud account data
CONF_ACCOUNT: Final = "account"
CONF_REGION_HOST: Final = "region_host"

# Subentry: direct connection of an NVR channel
SUBENTRY_CHANNEL_LINK: Final = "channel_link"
CONF_CHANNEL_ID: Final = "channel_id"
CONF_CAMERA_SERIAL: Final = "camera_serial"
CONF_EVENT_SOURCE: Final = "event_source"
CONF_RTSP_PORT: Final = "rtsp_port"
CONF_STREAM_SOURCE: Final = "stream_source"
EVENT_SOURCE_RECORDER: Final = "recorder"
EVENT_SOURCE_CAMERA: Final = "camera"
STREAM_SOURCE_CAMERA: Final = "camera"
STREAM_SOURCE_RECORDER: Final = "recorder"

# Options
OPT_EVENT_PATH: Final = "event_path"
EVENT_PATH_STREAM: Final = "stream"
EVENT_PATH_WEBHOOK: Final = "webhook"
EVENT_PATH_BOTH: Final = "both"
OPT_HOLD_SECONDS: Final = "hold_seconds"
OPT_RTSP_PORT: Final = "rtsp_port"
OPT_RTSP_TRANSPORT: Final = "rtsp_transport"
OPT_TARGET_SENSORS: Final = "target_sensors"
OPT_WEBHOOK_ID: Final = "webhook_id"
OPT_PLAYBACK: Final = "playback"
OPT_STREAM_KEY: Final = "stream_key"
OPT_TRY_ENCRYPTED_STREAM: Final = "try_encrypted_stream"
OPT_SIDEBAR_PANEL: Final = "sidebar_panel"
OPT_PANEL_ADMIN_ONLY: Final = "panel_admin_only"
PLAYBACK_MP4: Final = "mp4"
PLAYBACK_H264: Final = "h264"
PLAYBACK_STREAM: Final = "stream"
OPT_RECORDING_CLOCK: Final = "recording_clock"
RECORDING_CLOCK_AUTO: Final = "auto"
RECORDING_CLOCK_DEVICE: Final = "device"
RECORDING_CLOCK_UTC: Final = "utc"

DEFAULT_HOLD_SECONDS: Final = 5
RTSP_PORT_DEFAULT: Final = 554
RTSP_TCP: Final = "tcp"
RTSP_UDP: Final = "udp"

# Refresh cadence
FAST_INTERVAL: Final = timedelta(seconds=30)
INTERCOM_INTERVAL: Final = timedelta(seconds=2)
MEDIUM_INTERVAL: Final = timedelta(seconds=120)
SLOW_INTERVAL: Final = timedelta(minutes=10)
AUDIT_INTERVAL: Final = timedelta(hours=24)
CLOUD_INTERVAL: Final = timedelta(seconds=30)

# Availability hysteresis
FAILURES_BEFORE_UNAVAILABLE: Final = 3
GRACE_BEFORE_UNAVAILABLE: Final = timedelta(seconds=90)
