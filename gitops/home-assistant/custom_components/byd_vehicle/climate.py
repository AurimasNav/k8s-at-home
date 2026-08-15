"""Climate control for BYD Vehicle."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature
from homeassistant.components.climate.const import HVACMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from pybyd.models.vehicle import Vehicle

from .const import (
    CONF_CLIMATE_DURATION,
    DEFAULT_CLIMATE_DURATION,
    DOMAIN,
)
from .coordinator import BydDataUpdateCoordinator
from .entity import BydActionEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up BYD climate entities from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinators: dict[str, BydDataUpdateCoordinator] = data["coordinators"]
    climate_duration = entry.options.get(
        CONF_CLIMATE_DURATION,
        DEFAULT_CLIMATE_DURATION,
    )

    entities: list[ClimateEntity] = []

    for vin, coordinator in coordinators.items():
        if not coordinator.capability_available("climate"):
            continue
        entities.append(
            BydClimate(coordinator, vin, coordinator.vehicle, climate_duration)
        )

    async_add_entities(entities)


class BydClimate(BydActionEntity, ClimateEntity):
    """Representation of BYD climate control.

    Reads state from ``VehicleSnapshot.hvac``.  Commands go through
    ``car.hvac.start()`` / ``car.hvac.stop()`` which handle
    projections, guard windows, and reconcile internally.
    """

    _TEMP_MIN_C = 15
    _TEMP_MAX_C = 31
    _PRESET_MAX_HEAT = "max_heat"
    _PRESET_MAX_COOL = "max_cool"
    _DEFAULT_TEMP_C = 21.0

    _attr_has_entity_name = True
    _attr_translation_key = "climate"
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT_COOL]
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = (
        ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
    )
    _attr_min_temp = _TEMP_MIN_C
    _attr_max_temp = _TEMP_MAX_C
    _attr_target_temperature_step = 1
    _attr_preset_modes = [_PRESET_MAX_HEAT, _PRESET_MAX_COOL]

    def __init__(
        self,
        coordinator: BydDataUpdateCoordinator,
        vin: str,
        vehicle: Vehicle,
        climate_duration: int = DEFAULT_CLIMATE_DURATION,
    ) -> None:
        super().__init__(coordinator)
        self._vin = vin
        self._vehicle = vehicle
        self._climate_duration = climate_duration
        self._attr_unique_id = f"{vin}_climate"

    @staticmethod
    def _clamp_temp(temp_c: float | int | None) -> float | None:
        """Clamp a temperature to the valid range, or return None."""
        if temp_c is None:
            return None
        val = float(temp_c)
        if BydClimate._TEMP_MIN_C <= val <= BydClimate._TEMP_MAX_C:
            return val
        return None

    @staticmethod
    def _preset_from_temp(temp_c: float | None) -> str | None:
        """Return a preset name if the temperature matches a boundary."""
        if temp_c is None:
            return None
        rounded = round(temp_c)
        if rounded >= BydClimate._TEMP_MAX_C:
            return BydClimate._PRESET_MAX_HEAT
        if rounded <= BydClimate._TEMP_MIN_C:
            return BydClimate._PRESET_MAX_COOL
        return None

    # ------------------------------------------------------------------
    # State properties — read from VehicleSnapshot
    # ------------------------------------------------------------------

    @property
    def hvac_mode(self) -> HVACMode:
        """Return the current HVAC mode."""
        hvac = self._get_hvac_status()
        if hvac is not None:
            return HVACMode.HEAT_COOL if hvac.is_ac_on else HVACMode.OFF
        return HVACMode.OFF

    @property
    def assumed_state(self) -> bool:
        """Return True when state is assumed (no HVAC data)."""
        return self._get_hvac_status() is None

    @property
    def current_temperature(self) -> float | None:
        """Return the current interior temperature."""
        hvac = self._get_hvac_status()
        if hvac is not None and hvac.interior_temp_available:
            return hvac.temp_in_car
        realtime = self._get_realtime()
        if realtime is not None:
            temp = getattr(realtime, "temp_in_car", None)
            if temp is not None:
                return temp
        return None

    @property
    def target_temperature(self) -> float | None:
        """Return the target temperature."""
        hvac = self._get_hvac_status()
        if hvac is not None:
            temp_c = self._clamp_temp(hvac.main_setting_temp_new)
            if temp_c is not None:
                return temp_c
        return self._DEFAULT_TEMP_C

    @property
    def preset_mode(self) -> str | None:
        """Return the active preset mode, if any."""
        hvac = self._get_hvac_status()
        if hvac is not None and hvac.is_ac_on:
            temp_c = self._clamp_temp(hvac.main_setting_temp_new)
            if temp_c is not None:
                return self._preset_from_temp(temp_c)
        return None

    # ------------------------------------------------------------------
    # Commands — delegate to BydCar.hvac
    # ------------------------------------------------------------------

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set HVAC mode (on/off)."""
        car = self.coordinator.car
        if car is None:
            return
        if hvac_mode == HVACMode.OFF:
            await self._execute_car_command(
                car.hvac.stop(),
                command="stop_climate",
            )
        else:
            temp = self.target_temperature or self._DEFAULT_TEMP_C
            await self._execute_car_command(
                car.hvac.start(
                    temperature=temp,
                    duration=self._climate_duration,
                ),
                command="start_climate",
            )

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set target temperature."""
        self._ensure_action_allowed()
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        clamped = max(self._TEMP_MIN_C, min(self._TEMP_MAX_C, float(temp)))

        # If climate is currently on, send the update immediately
        if self.hvac_mode != HVACMode.OFF:
            car = self.coordinator.car
            if car is None:
                return
            await self._execute_car_command(
                car.hvac.start(
                    temperature=clamped,
                    duration=self._climate_duration,
                ),
                command="start_climate",
            )

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Activate a preset mode."""
        self._ensure_action_allowed()
        if preset_mode not in self._attr_preset_modes:
            raise HomeAssistantError(f"Unsupported preset mode: {preset_mode}")
        temp_c = (
            float(self._TEMP_MAX_C)
            if preset_mode == self._PRESET_MAX_HEAT
            else float(self._TEMP_MIN_C)
        )
        car = self.coordinator.car
        if car is None:
            return
        await self._execute_car_command(
            car.hvac.start(
                temperature=temp_c,
                duration=self._climate_duration,
            ),
            command="start_climate",
        )

    # ------------------------------------------------------------------
    # Extra attributes
    # ------------------------------------------------------------------

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional HVAC attributes."""
        attrs = {**super().extra_state_attributes}
        hvac = self._get_hvac_status()
        if hvac is not None:
            attrs["exterior_temperature"] = hvac.temp_out_car
            attrs["passenger_set_temperature"] = hvac.copilot_setting_temp_new
            attrs["fan_speed"] = hvac.wind_mode
            attrs["airflow_direction"] = hvac.wind_position
            attrs["recirculation"] = hvac.cycle_choice
            attrs["front_defrost"] = hvac.front_defrost_status
            attrs["rear_defrost"] = hvac.electric_defrost_status
            attrs["wiper_heat"] = hvac.wiper_heat_status
            attrs["pm25"] = hvac.pm
            attrs["pm25_exterior_state"] = hvac.pm25_state_out_car
            attrs["rapid_heating"] = hvac.rapid_increase_temp_state
            attrs["rapid_cooling"] = hvac.rapid_decrease_temp_state
        return attrs
