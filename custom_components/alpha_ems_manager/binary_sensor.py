"""Binary sensor platform for Alpha EMS Manager."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME, VERSION
from .coordinator import AlphaEmsCoordinator


@dataclass(frozen=True, kw_only=True)
class AlphaEmsBinarySensorDescription(BinarySensorEntityDescription):
    """Describe an Alpha EMS binary sensor."""

    value_fn: Callable[[dict[str, Any]], bool | None]


BINARY_SENSOR_DESCRIPTIONS: tuple[AlphaEmsBinarySensorDescription, ...] = (
    AlphaEmsBinarySensorDescription(
        key="reserve_satisfied",
        translation_key="reserve_satisfied",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        icon="mdi:battery-check",
        value_fn=lambda data: data.get("reserve_satisfied"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the binary sensor entities."""
    coordinator: AlphaEmsCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        AlphaEmsBinarySensor(coordinator, entry, description)
        for description in BINARY_SENSOR_DESCRIPTIONS
    )


class AlphaEmsBinarySensor(
    CoordinatorEntity[AlphaEmsCoordinator], BinarySensorEntity
):
    """A binary sensor backed by the Alpha EMS coordinator."""

    _attr_has_entity_name = True
    entity_description: AlphaEmsBinarySensorDescription

    def __init__(
        self,
        coordinator: AlphaEmsCoordinator,
        entry: ConfigEntry,
        description: AlphaEmsBinarySensorDescription,
    ) -> None:
        """Initialise the binary sensor."""
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
    def is_on(self) -> bool | None:
        """Return whether the binary sensor is on."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)
