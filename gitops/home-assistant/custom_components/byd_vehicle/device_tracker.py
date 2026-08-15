"""Device tracker for BYD Vehicle."""

from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from pybyd.models.gps import GpsInfo
from pybyd.models.vehicle import Vehicle

from .const import DOMAIN
from .coordinator import BydGpsUpdateCoordinator, get_vehicle_display


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up BYD device tracker entities from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    gps_coordinators: dict[str, BydGpsUpdateCoordinator] = data.get(
        "gps_coordinators", {}
    )
    coordinators = data["coordinators"]

    entities: list[TrackerEntity] = []
    for vin, gps_coordinator in gps_coordinators.items():
        telemetry = coordinators.get(vin)
        if telemetry is None or not telemetry.capability_available("location"):
            continue
        vehicle = (
            telemetry.vehicle
            if telemetry is not None
            else gps_coordinator._vehicle  # noqa: SLF001
        )
        entities.append(BydDeviceTracker(gps_coordinator, vin, vehicle))

    async_add_entities(entities)


class BydDeviceTracker(CoordinatorEntity[BydGpsUpdateCoordinator], TrackerEntity):
    """Representation of a BYD vehicle tracker.

    Uses ``BydGpsUpdateCoordinator`` whose data is a ``VehicleSnapshot``
    with a populated ``gps`` section.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "location"

    def __init__(
        self,
        coordinator: BydGpsUpdateCoordinator,
        vin: str,
        vehicle: Vehicle,
    ) -> None:
        super().__init__(coordinator)
        self._vin = vin
        self._vehicle = vehicle
        self._attr_unique_id = f"{vin}_tracker"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._vin)},
            name=get_vehicle_display(self._vehicle),
            manufacturer=self._vehicle.brand_name or "BYD",
            model=self._vehicle.model_name,
            serial_number=self._vin,
            hw_version=self._vehicle.tbox_version or None,
        )

    def _get_gps(self) -> GpsInfo | None:
        snap = self.coordinator.data
        gps = snap.gps if snap is not None else None
        if gps is not None and (gps.latitude is None or gps.longitude is None):
            return None
        return gps

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self._get_gps() is not None

    @property
    def latitude(self) -> float | None:
        gps = self._get_gps()
        return gps.latitude if gps else None

    @property
    def longitude(self) -> float | None:
        gps = self._get_gps()
        return gps.longitude if gps else None

    @property
    def source_type(self) -> SourceType:
        return SourceType.GPS

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        gps = self._get_gps()
        return {
            "vin": self._vin,
            "gps_speed": gps.speed if gps else None,
            "gps_direction": gps.direction if gps else None,
            "gps_timestamp": gps.gps_timestamp if gps else None,
        }
