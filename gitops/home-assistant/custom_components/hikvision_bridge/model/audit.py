"""Setup checks that explain why events will not arrive. Read-only; returns findings as data."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum

from ..wire.link import IsapiLink
from .features import detection
from .inventory import DeviceProfile
from .vocabulary import Kind


class Finding(StrEnum):
    CENTER_LINK_MISSING = "center_link_missing"
    AREA_MISSING = "area_missing"
    DETECTION_OFF = "detection_off"
    RECORDER_TARGET_FILTER = "recorder_target_filter"
    STREAM_ENCRYPTED = "stream_encrypted"
    DEFAULT_PASSWORD = "default_password"  # noqa: S105 - finding name, not a secret
    BASIC_OVER_HTTP = "basic_over_http"
    CLOCK_SKEW = "clock_skew"


@dataclass(frozen=True, slots=True)
class AuditItem:
    finding: Finding
    channel_id: str | None = None
    kind: Kind | None = None
    trigger_id: str | None = None
    detail: str | None = None

    @property
    def key(self) -> str:
        parts = [
            self.finding.value,
            self.channel_id or "device",
            self.kind.value if self.kind else "",
        ]
        return "_".join(p for p in parts if p)


@dataclass(slots=True)
class AuditReport:
    items: list[AuditItem] = field(default_factory=list)

    def of(self, finding: Finding) -> list[AuditItem]:
        return [item for item in self.items if item.finding == finding]


async def run_audit(
    link: IsapiLink,
    profile: DeviceProfile,
    *,
    insecure_basic: bool = False,
    clock_skew_seconds: float | None = None,
) -> AuditReport:
    report = AuditReport()

    if profile.cloud and profile.cloud.stream_encrypted:
        report.items.append(AuditItem(Finding.STREAM_ENCRYPTED))
    if profile.default_password or profile.risky_password:
        report.items.append(AuditItem(Finding.DEFAULT_PASSWORD))
    if insecure_basic:
        report.items.append(AuditItem(Finding.BASIC_OVER_HTTP))
    if clock_skew_seconds is not None and abs(clock_skew_seconds) > 300:
        report.items.append(AuditItem(Finding.CLOCK_SKEW, detail=f"{clock_skew_seconds:.0f}"))

    async def inspect(channel_id: str, kind: Kind) -> None:
        state = await detection.read(link, kind, channel_id)
        if state is None:
            return
        trigger = profile.trigger_for(kind, channel_id)
        if state.enabled:
            if trigger is not None and not trigger.notifies_center:
                report.items.append(
                    AuditItem(Finding.CENTER_LINK_MISSING, channel_id, kind, trigger.trigger_id)
                )
            if state.area_defined is False:
                report.items.append(AuditItem(Finding.AREA_MISSING, channel_id, kind))
        elif trigger is not None and trigger.notifies_center and kind != Kind.MOTION:
            report.items.append(AuditItem(Finding.DETECTION_OFF, channel_id, kind))
        if kind == Kind.MOTION and profile.shape.records and state.enabled and state.target_filter:
            report.items.append(
                AuditItem(
                    Finding.RECORDER_TARGET_FILTER,
                    channel_id,
                    kind,
                    detail=",".join(state.target_filter),
                )
            )

    jobs = [
        inspect(channel.channel_id, kind)
        for channel in profile.channels.values()
        if channel.online is not False
        for kind in sorted(channel.switchable)
    ]
    await asyncio.gather(*jobs)
    report.items.sort(key=lambda item: item.key)
    return report
