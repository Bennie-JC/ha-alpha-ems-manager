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
from homeassistant.const import UnitOfEnergy
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


def _debug_attributes(data: dict[str, Any]) -> dict[str, Any]:
    """Return the debug attributes dict from coordinator data."""
    return {key: data.get(key) for key in _DEBUG_ATTRIBUTE_KEYS}


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
