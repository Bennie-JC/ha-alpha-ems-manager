"""Sensor platform for Alpha EMS Manager.

These sensors expose the learned load, PV forecast, reserve and recommendation
values produced by the coordinator. No control is performed here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME, VERSION
from .coordinator import AlphaEmsCoordinator


@dataclass(frozen=True, kw_only=True)
class AlphaEmsSensorDescription(SensorEntityDescription):
    """Describe an Alpha EMS sensor and how to read it from coordinator data."""

    value_fn: Callable[[dict[str, Any]], Any]
    attributes_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None


# Debug/diagnostic attributes surfaced on the predicted daily load sensor.
_DEBUG_ATTRIBUTE_KEYS = (
    "source_entity",
    "source_value",
    "last_house_load",
    "last_delta",
    "last_slot",
    "learned_slots_count",
    "update_count",
    "last_update",
)

# Full lifecycle/diagnostic attributes surfaced on the profile status sensor.
_PROFILE_STATUS_ATTRIBUTE_KEYS = (
    "source_entity",
    "source_value",
    "previous_house_load",
    "current_house_load",
    "last_raw_delta",
    "last_delta_per_slot",
    "previous_slot",
    "current_slot",
    "distributed_slots",
    "learned_slots_count",
    "update_count",
    "last_update",
    "season",
    "day_type",
    "profile_key",
    "storage_loaded",
    "storage_saved",
    # EV exclusion diagnostics.
    "ev_charger_power_sensor",
    "ev_charger_power_kw",
    "ev_excluded_last_quarter_kwh",
    "ev_excluded_today_kwh",
    "house_load_raw_last_quarter_kwh",
    "house_load_corrected_last_quarter_kwh",
    "ev_exclusion_active",
)


def _debug_attributes(data: dict[str, Any]) -> dict[str, Any]:
    """Return the debug attributes dict from coordinator data."""
    return {key: data.get(key) for key in _DEBUG_ATTRIBUTE_KEYS}


def _profile_status_attributes(data: dict[str, Any]) -> dict[str, Any]:
    """Return the profile status attributes dict from coordinator data."""
    return {key: data.get(key) for key in _PROFILE_STATUS_ATTRIBUTE_KEYS}


def _pv_profile_status_attributes(data: dict[str, Any]) -> dict[str, Any]:
    """Return the PV profile status attributes from coordinator data."""
    return {
        "actual_pv_today": data.get("pv_actual_today_kwh"),
        "raw_forecast_today": data.get("pv_forecast_today_kwh"),
        "raw_forecast_tomorrow": data.get("pv_forecast_tomorrow_kwh"),
        "corrected_forecast_today": data.get("corrected_pv_forecast_today_kwh"),
        "corrected_forecast_tomorrow": data.get(
            "corrected_pv_forecast_tomorrow_kwh"
        ),
        "expected_remaining_pv_today": data.get(
            "expected_remaining_pv_today_kwh"
        ),
        "global_pv_factor": data.get("global_pv_factor"),
        "season_pv_factor": data.get("season_pv_factor"),
        "last_pv_error": data.get("last_pv_error"),
        "last_pv_error_factor": data.get("last_pv_error_factor"),
        "pv_learning_days": data.get("pv_learning_days"),
        "season": data.get("season"),
        "storage_loaded": data.get("storage_loaded"),
        "storage_saved": data.get("storage_saved"),
        "last_update": data.get("last_update"),
    }


def _reserve_profile_status_attributes(data: dict[str, Any]) -> dict[str, Any]:
    """Return the reserve profile status attributes from coordinator data."""
    return {
        "battery_floor_kwh": data.get("reserve_floor_kwh"),
        "battery_current_energy": data.get("battery_current_kwh"),
        "reserve_correction_factor": data.get("reserve_correction_factor"),
        "reserve_learning_days": data.get("reserve_learning_days"),
        "reserve_miss_count": data.get("reserve_miss_count"),
        "reserve_success_count": data.get("reserve_success_count"),
        "last_reserve_miss": data.get("last_reserve_miss"),
        "last_reserve_success": data.get("last_reserve_success"),
        "required_reserve": data.get("required_reserve_kwh"),
        "predicted_remaining_load": data.get("predicted_remaining_load_kwh"),
        "expected_remaining_pv_today": data.get(
            "expected_remaining_pv_today_kwh"
        ),
        "day_min_battery_energy": data.get("reserve_day_min_battery_energy"),
        "last_miss_date": data.get("reserve_last_miss_date"),
        "last_success_date": data.get("reserve_last_success_date"),
        "storage_loaded": data.get("storage_loaded"),
        "storage_saved": data.get("storage_saved"),
        "last_update": data.get("last_update"),
    }


SENSOR_DESCRIPTIONS: tuple[AlphaEmsSensorDescription, ...] = (
    AlphaEmsSensorDescription(
        key="predicted_daily_load",
        translation_key="predicted_daily_load",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:home-lightning-bolt",
        value_fn=lambda data: data.get("predicted_daily_load_kwh"),
        attributes_fn=_debug_attributes,
    ),
    AlphaEmsSensorDescription(
        key="predicted_remaining_load",
        translation_key="predicted_remaining_load",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:home-clock",
        value_fn=lambda data: data.get("predicted_remaining_load_kwh"),
    ),
    AlphaEmsSensorDescription(
        key="required_reserve",
        translation_key="required_reserve",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:battery-charging-medium",
        value_fn=lambda data: data.get("required_reserve_kwh"),
    ),
    AlphaEmsSensorDescription(
        key="pv_forecast_today",
        translation_key="pv_forecast_today",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:solar-power",
        value_fn=lambda data: data.get("pv_forecast_today_kwh"),
    ),
    AlphaEmsSensorDescription(
        key="pv_forecast_tomorrow",
        translation_key="pv_forecast_tomorrow",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:solar-power",
        value_fn=lambda data: data.get("pv_forecast_tomorrow_kwh"),
    ),
    AlphaEmsSensorDescription(
        key="battery_current",
        translation_key="battery_current",
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:battery",
        value_fn=lambda data: data.get("battery_current_kwh"),
    ),
    AlphaEmsSensorDescription(
        key="recommendation",
        translation_key="recommendation",
        icon="mdi:lightbulb-on",
        value_fn=lambda data: (
            "hold" if data.get("reserve_satisfied") else "charge"
        ),
    ),
    AlphaEmsSensorDescription(
        key="learning_confidence",
        translation_key="learning_confidence",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:gauge",
        value_fn=lambda data: data.get("learning_confidence"),
    ),
    AlphaEmsSensorDescription(
        key="learning_days",
        translation_key="learning_days",
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement="d",
        icon="mdi:calendar-check",
        value_fn=lambda data: data.get("learning_days"),
    ),
    AlphaEmsSensorDescription(
        key="learned_slots_count",
        translation_key="learned_slots_count",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:view-grid",
        value_fn=lambda data: data.get("learned_slots_count"),
    ),
    AlphaEmsSensorDescription(
        key="last_quarter_load",
        translation_key="last_quarter_load",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:home-clock-outline",
        value_fn=lambda data: data.get("last_quarter_load_kwh"),
    ),
    AlphaEmsSensorDescription(
        key="profile_status",
        translation_key="profile_status",
        icon="mdi:clipboard-pulse",
        value_fn=lambda data: data.get("profile_status"),
        attributes_fn=_profile_status_attributes,
    ),
    AlphaEmsSensorDescription(
        key="pv_correction_factor",
        translation_key="pv_correction_factor",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:tune-variant",
        value_fn=lambda data: data.get("pv_correction_factor"),
    ),
    AlphaEmsSensorDescription(
        key="corrected_pv_forecast_today",
        translation_key="corrected_pv_forecast_today",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:solar-power-variant",
        value_fn=lambda data: data.get("corrected_pv_forecast_today_kwh"),
    ),
    AlphaEmsSensorDescription(
        key="corrected_pv_forecast_tomorrow",
        translation_key="corrected_pv_forecast_tomorrow",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:solar-power-variant",
        value_fn=lambda data: data.get("corrected_pv_forecast_tomorrow_kwh"),
    ),
    AlphaEmsSensorDescription(
        key="expected_remaining_pv_today",
        translation_key="expected_remaining_pv_today",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:weather-sunset",
        value_fn=lambda data: data.get("expected_remaining_pv_today_kwh"),
    ),
    AlphaEmsSensorDescription(
        key="pv_learning_confidence",
        translation_key="pv_learning_confidence",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:gauge",
        value_fn=lambda data: data.get("pv_learning_confidence"),
    ),
    AlphaEmsSensorDescription(
        key="pv_profile_status",
        translation_key="pv_profile_status",
        icon="mdi:clipboard-pulse-outline",
        value_fn=lambda data: data.get("pv_profile_status"),
        attributes_fn=_pv_profile_status_attributes,
    ),
    AlphaEmsSensorDescription(
        key="reserve_correction_factor",
        translation_key="reserve_correction_factor",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:tune-vertical",
        value_fn=lambda data: data.get("reserve_correction_factor"),
        attributes_fn=_reserve_profile_status_attributes,
    ),
    AlphaEmsSensorDescription(
        key="reserve_floor",
        translation_key="reserve_floor",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:battery-low",
        value_fn=lambda data: data.get("reserve_floor_kwh"),
    ),
    AlphaEmsSensorDescription(
        key="reserve_learning_days",
        translation_key="reserve_learning_days",
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement="d",
        icon="mdi:calendar-check-outline",
        value_fn=lambda data: data.get("reserve_learning_days"),
    ),
    AlphaEmsSensorDescription(
        key="reserve_miss_count",
        translation_key="reserve_miss_count",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:battery-alert",
        value_fn=lambda data: data.get("reserve_miss_count"),
    ),
    AlphaEmsSensorDescription(
        key="reserve_success_count",
        translation_key="reserve_success_count",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:battery-check",
        value_fn=lambda data: data.get("reserve_success_count"),
    ),
    AlphaEmsSensorDescription(
        key="reserve_learning_status",
        translation_key="reserve_learning_status",
        icon="mdi:clipboard-pulse",
        value_fn=lambda data: data.get("reserve_learning_status"),
        attributes_fn=_reserve_profile_status_attributes,
    ),
    # --- Trade Prediction Engine sensors --------------------------------------
    AlphaEmsSensorDescription(
        key="trade_possible",
        translation_key="trade_possible",
        icon="mdi:swap-horizontal-bold",
        value_fn=lambda data: data.get("trade_possible"),
        attributes_fn=lambda data: {
            "buy_price": data.get("predicted_buy_price"),
            "sell_price": data.get("predicted_sell_price"),
            "minimum_spread": data.get("minimum_spread"),
            "next_buy_source": data.get("next_buy_source"),
            "current_battery_energy": data.get("battery_current_kwh"),
            "battery_capacity_kwh": data.get("battery_capacity_kwh"),
            "battery_capacity_source": data.get("battery_capacity_source"),
            "expected_pv_until_sell": data.get("expected_pv_until_sell"),
            "expected_load_until_sell": data.get("expected_load_until_sell"),
            "expected_pv_until_buy": data.get("expected_pv_until_buy"),
            "expected_load_until_buy": data.get("expected_load_until_buy"),
            "expected_pv_sell_to_next_buy": data.get("expected_pv_sell_to_next_buy"),
            "expected_load_sell_to_next_buy": data.get("expected_load_sell_to_next_buy"),
            "reserve_floor_kwh": data.get("reserve_floor_kwh"),
            "reserve_correction_factor": data.get("reserve_correction_factor"),
            "pv_correction_factor": data.get("pv_correction_factor"),
            "buy_cost": data.get("buy_cost"),
            "sell_income": data.get("sell_income"),
            "predicted_profit": data.get("predicted_profit"),
            "gross_profit": data.get("gross_profit"),
            "efficiency_loss_kwh": data.get("efficiency_loss_kwh"),
            "battery_can_reach_full_before_sell": data.get(
                "battery_can_reach_full_before_sell"
            ),
            "buy_limited_by_charge_power": data.get("buy_limited_by_charge_power"),
            "sell_limited_by_discharge_power": data.get(
                "sell_limited_by_discharge_power"
            ),
            "max_charge_power_kw": data.get("max_charge_power_kw"),
            "max_discharge_power_kw": data.get("max_discharge_power_kw"),
            "max_buy_kwh_per_quarter": data.get("max_buy_kwh_per_quarter"),
            "max_sell_kwh_per_quarter": data.get("max_sell_kwh_per_quarter"),
            "charge_efficiency": data.get("charge_efficiency"),
            "discharge_efficiency": data.get("discharge_efficiency"),
            "roundtrip_efficiency": data.get("roundtrip_efficiency"),
            "pv_distribution_mode": data.get("pv_distribution_mode"),
            "pv_sunrise_slot": data.get("pv_sunrise_slot"),
            "pv_sunset_slot": data.get("pv_sunset_slot"),
            "pv_daylight_slots": data.get("pv_daylight_slots"),
            "pv_curve_peak_slot": data.get("pv_curve_peak_slot"),
            "pv_east_used": data.get("pv_east_used"),
            "pv_west_used": data.get("pv_west_used"),
            "ev_exclusion_used": data.get("ev_exclusion_used"),
        },
    ),
    AlphaEmsSensorDescription(
        key="predicted_buy_kwh",
        translation_key="predicted_buy_kwh",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:battery-arrow-down",
        value_fn=lambda data: data.get("predicted_buy_kwh"),
    ),
    AlphaEmsSensorDescription(
        key="predicted_buy_time",
        translation_key="predicted_buy_time",
        icon="mdi:clock-arrow-down",
        value_fn=lambda data: data.get("predicted_buy_time"),
    ),
    AlphaEmsSensorDescription(
        key="predicted_buy_price",
        translation_key="predicted_buy_price",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="€/kWh",
        icon="mdi:currency-eur",
        value_fn=lambda data: data.get("predicted_buy_price"),
    ),
    AlphaEmsSensorDescription(
        key="predicted_sell_kwh",
        translation_key="predicted_sell_kwh",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:battery-arrow-up",
        value_fn=lambda data: data.get("predicted_sell_kwh"),
    ),
    AlphaEmsSensorDescription(
        key="predicted_sell_time",
        translation_key="predicted_sell_time",
        icon="mdi:clock-arrow-up",
        value_fn=lambda data: data.get("predicted_sell_time"),
    ),
    AlphaEmsSensorDescription(
        key="predicted_sell_price",
        translation_key="predicted_sell_price",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="€/kWh",
        icon="mdi:currency-eur",
        value_fn=lambda data: data.get("predicted_sell_price"),
    ),
    AlphaEmsSensorDescription(
        key="predicted_profit",
        translation_key="predicted_profit",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="€",
        icon="mdi:cash-plus",
        value_fn=lambda data: data.get("predicted_profit"),
    ),
    AlphaEmsSensorDescription(
        key="required_reserve_after_sell",
        translation_key="required_reserve_after_sell",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:battery-charging-outline",
        value_fn=lambda data: data.get("required_reserve_after_sell"),
    ),
    AlphaEmsSensorDescription(
        key="expected_battery_at_sell",
        translation_key="expected_battery_at_sell",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:battery-high",
        value_fn=lambda data: data.get("expected_battery_at_sell"),
    ),
    AlphaEmsSensorDescription(
        key="expected_battery_at_buy",
        translation_key="expected_battery_at_buy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:battery-medium",
        value_fn=lambda data: data.get("expected_battery_at_buy"),
    ),
    AlphaEmsSensorDescription(
        key="battery_can_reach_full_before_sell",
        translation_key="battery_can_reach_full_before_sell",
        icon="mdi:battery-check",
        value_fn=lambda data: data.get("battery_can_reach_full_before_sell"),
    ),
    AlphaEmsSensorDescription(
        key="predicted_missing_kwh_for_full",
        translation_key="predicted_missing_kwh_for_full",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:battery-alert-variant",
        value_fn=lambda data: data.get("predicted_missing_kwh_for_full"),
    ),
    AlphaEmsSensorDescription(
        key="safety_buy_needed",
        translation_key="safety_buy_needed",
        icon="mdi:shield-battery",
        value_fn=lambda data: data.get("safety_buy_needed"),
    ),
    AlphaEmsSensorDescription(
        key="safety_buy_kwh",
        translation_key="safety_buy_kwh",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:shield-arrow-down",
        value_fn=lambda data: data.get("safety_buy_kwh"),
    ),
    AlphaEmsSensorDescription(
        key="safety_buy_time",
        translation_key="safety_buy_time",
        icon="mdi:clock-alert",
        value_fn=lambda data: data.get("safety_buy_time"),
    ),
    AlphaEmsSensorDescription(
        key="safety_buy_reason",
        translation_key="safety_buy_reason",
        icon="mdi:information-variant-circle",
        value_fn=lambda data: data.get("safety_buy_reason"),
    ),
    AlphaEmsSensorDescription(
        key="trade_prediction_days",
        translation_key="trade_prediction_days",
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement="d",
        icon="mdi:calendar-clock",
        value_fn=lambda data: data.get("trade_prediction_days"),
    ),
    AlphaEmsSensorDescription(
        key="trade_prediction_confidence",
        translation_key="trade_prediction_confidence",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:gauge",
        value_fn=lambda data: data.get("trade_prediction_confidence"),
    ),
    AlphaEmsSensorDescription(
        key="trade_prediction_status",
        translation_key="trade_prediction_status",
        icon="mdi:clipboard-clock",
        value_fn=lambda data: data.get("trade_prediction_status"),
    ),
    AlphaEmsSensorDescription(
        key="next_buy_source",
        translation_key="next_buy_source",
        icon="mdi:calendar-question",
        value_fn=lambda data: data.get("next_buy_source"),
    ),
    # --- EV exclusion sensors --------------------------------------------------
    AlphaEmsSensorDescription(
        key="ev_charger_power",
        translation_key="ev_charger_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        icon="mdi:ev-station",
        value_fn=lambda data: data.get("ev_charger_power_kw"),
    ),
    AlphaEmsSensorDescription(
        key="ev_excluded_last_quarter",
        translation_key="ev_excluded_last_quarter",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:ev-station",
        value_fn=lambda data: data.get("ev_excluded_last_quarter_kwh"),
    ),
    AlphaEmsSensorDescription(
        key="ev_excluded_today",
        translation_key="ev_excluded_today",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:ev-station",
        value_fn=lambda data: data.get("ev_excluded_today_kwh"),
    ),
    AlphaEmsSensorDescription(
        key="house_load_raw_last_quarter",
        translation_key="house_load_raw_last_quarter",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:home-lightning-bolt-outline",
        value_fn=lambda data: data.get("house_load_raw_last_quarter_kwh"),
    ),
    AlphaEmsSensorDescription(
        key="house_load_corrected_last_quarter",
        translation_key="house_load_corrected_last_quarter",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:home-lightning-bolt",
        value_fn=lambda data: data.get("house_load_corrected_last_quarter_kwh"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor entities."""
    coordinator: AlphaEmsCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        AlphaEmsSensor(coordinator, entry, description)
        for description in SENSOR_DESCRIPTIONS
    )


class AlphaEmsSensor(CoordinatorEntity[AlphaEmsCoordinator], SensorEntity):
    """A sensor backed by the Alpha EMS coordinator."""

    _attr_has_entity_name = True
    entity_description: AlphaEmsSensorDescription

    def __init__(
        self,
        coordinator: AlphaEmsCoordinator,
        entry: ConfigEntry,
        description: AlphaEmsSensorDescription,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=NAME,
            manufacturer="Bennie-JC",
            model="Learning EMS",
            sw_version=VERSION,
        )

    @property
    def native_value(self) -> Any:
        """Return the current value from the coordinator data."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return debug/diagnostic attributes, if this sensor exposes any."""
        if (
            self.coordinator.data is None
            or self.entity_description.attributes_fn is None
        ):
            return None
        return self.entity_description.attributes_fn(self.coordinator.data)
