"""Sensor platform for Alpha EMS Manager.

Phase 1 exposes exactly four entities. Every other quantity the integration
computes -- per-slot profiles, window means, balance residuals, coverage
statistics -- is available through diagnostics instead. Ninety-six quarter
sensors and five window averages would be technically easy and practically
awful.

Entity names are literal English rather than translation keys, matching the
sibling Frank Quarter Prices integration. Home Assistant derives an entity id
from the *translated* name, so a translation key would hand a Dutch user
``sensor.alpha_ems_verwachte_huisbelasting_vandaag``. Stable ids that automations
can rely on are worth more here than a translated default name the user can
override in the UI anyway.
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
from homeassistant.const import PERCENTAGE, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AlphaEmsConfigEntry
from .const import (
    DOMAIN,
    NAME,
    SENSOR_EXPECTED_LOAD_TODAY,
    SENSOR_EXPECTED_LOAD_TOMORROW,
    SENSOR_LEARNING_CONFIDENCE,
    SENSOR_LEARNING_DAYS,
)
from .coordinator import AlphaEmsCoordinator


@dataclass(frozen=True, kw_only=True)
class AlphaEmsSensorDescription(SensorEntityDescription):
    """Describes one Alpha EMS sensor and how to derive it."""

    value_fn: Callable[[AlphaEmsCoordinator], float | int | None]
    attributes_fn: Callable[[AlphaEmsCoordinator], dict[str, Any]] | None = None


def _round(value: float | None, digits: int = 2) -> float | None:
    """Round a forecast, preserving ``None``."""
    return None if value is None else round(value, digits)


def _today_value(coordinator: AlphaEmsCoordinator) -> float | None:
    """Return today's expected total household consumption."""
    forecast = coordinator.today_forecast
    baseline = (coordinator.data or {}).get("today_baseline")
    if forecast is None or baseline is None or not baseline.available:
        return None
    return _round(forecast.forecast_total_kwh)


def _today_attributes(coordinator: AlphaEmsCoordinator) -> dict[str, Any]:
    """Return the small attribute set for today's forecast."""
    forecast = coordinator.today_forecast
    baseline = (coordinator.data or {}).get("today_baseline")
    confidence = coordinator.confidence
    if forecast is None or baseline is None:
        return {}
    data = coordinator.data or {}
    # The same gate the state uses. Without it an unavailable forecast still
    # published `forecast_total_kwh: 0.0`, so a template reading the attribute
    # got a plausible-looking zero-kWh prediction instead of nothing -- the
    # "learned nothing must never read as zero" failure the storage layer goes to
    # such lengths to avoid, reintroduced one layer up.
    predicted = baseline.available
    return {
        # Baseline: measured household load minus any configured flexible load.
        "actual_so_far_kwh": _round(forecast.actual_so_far_kwh),
        "forecast_remaining_kwh": (
            _round(forecast.forecast_remaining_kwh) if predicted else None
        ),
        "forecast_total_kwh": (
            _round(forecast.forecast_total_kwh) if predicted else None
        ),
        # Measured ground truth, shown alongside so the two never get confused.
        "measured_so_far_kwh": _round(data.get("measured_so_far_kwh")),
        "flexible_load_so_far_kwh": (
            _round(data.get("ev_so_far_kwh")) if coordinator.ev_configured else None
        ),
        "model_days": baseline.source_days,
        "confidence_percent": (
            None if confidence is None else round(confidence.percent, 1)
        ),
        "adaptation_applied": forecast.adapted if predicted else False,
        "adaptation_ratio": round(forecast.adaptation_ratio, 3),
        "day_type": baseline.day_type,
        "intervals_today": baseline.interval_count,
    }


def _tomorrow_value(coordinator: AlphaEmsCoordinator) -> float | None:
    """Return tomorrow's expected total household consumption."""
    forecast = coordinator.tomorrow_forecast
    if forecast is None or not forecast.available:
        return None
    return _round(forecast.total_kwh)


def _tomorrow_attributes(coordinator: AlphaEmsCoordinator) -> dict[str, Any]:
    """Return the small attribute set for tomorrow's forecast."""
    forecast = coordinator.tomorrow_forecast
    confidence = coordinator.confidence
    if forecast is None:
        return {}
    return {
        "forecast_total_kwh": _round(forecast.total_kwh),
        "model_days": forecast.source_days,
        "day_type": forecast.day_type,
        "day_type_pooled": forecast.day_type_pooled,
        "windows_used_days": list(forecast.windows_used),
        # 92 / 96 / 100 depending on the target day's daylight-saving shape.
        "intervals_tomorrow": forecast.interval_count,
        "confidence_percent": (
            None if confidence is None else round(confidence.percent, 1)
        ),
    }


def _confidence_value(coordinator: AlphaEmsCoordinator) -> float | None:
    """Return the learning confidence percentage."""
    confidence = coordinator.confidence
    return None if confidence is None else round(confidence.percent, 1)


def _confidence_attributes(coordinator: AlphaEmsCoordinator) -> dict[str, Any]:
    """Return the component breakdown behind the confidence score."""
    confidence = coordinator.confidence
    if confidence is None:
        return {}
    breakdown = confidence.as_dict()
    breakdown.pop("percent", None)
    return breakdown


def _days_value(coordinator: AlphaEmsCoordinator) -> int | None:
    """Return the number of calendar days that count as learned."""
    confidence = coordinator.confidence
    return None if confidence is None else confidence.learned_days


def _days_attributes(coordinator: AlphaEmsCoordinator) -> dict[str, Any]:
    """Return retention and rejection context for the learned-day count."""
    oldest, newest = coordinator.store.span
    return {
        "retained_days": len(coordinator.store.days),
        "retained_intervals": coordinator.store.retained_intervals,
        "history_start": None if oldest is None else oldest.isoformat(),
        "history_end": None if newest is None else newest.isoformat(),
        "rejected_quarters": coordinator.rejected_quarters,
        "flexible_load_configured": coordinator.ev_configured,
        "intervals_without_flexible_data": coordinator.invalid_ev_quarters,
        "open_quarter_coverage": round(coordinator.open_quarter_coverage, 3),
    }


SENSORS: tuple[AlphaEmsSensorDescription, ...] = (
    AlphaEmsSensorDescription(
        key=SENSOR_EXPECTED_LOAD_TODAY,
        name="Expected House Load Today",
        icon="mdi:home-lightning-bolt",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        # No state_class: this is a forecast, and long-term statistics or an
        # Energy dashboard entry built on a prediction would be misleading.
        value_fn=_today_value,
        attributes_fn=_today_attributes,
    ),
    AlphaEmsSensorDescription(
        key=SENSOR_EXPECTED_LOAD_TOMORROW,
        name="Expected House Load Tomorrow",
        icon="mdi:home-clock",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=_tomorrow_value,
        attributes_fn=_tomorrow_attributes,
    ),
    AlphaEmsSensorDescription(
        key=SENSOR_LEARNING_CONFIDENCE,
        name="Learning Confidence",
        icon="mdi:gauge",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_confidence_value,
        attributes_fn=_confidence_attributes,
    ),
    AlphaEmsSensorDescription(
        key=SENSOR_LEARNING_DAYS,
        name="Learning Days",
        icon="mdi:calendar-check",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_days_value,
        attributes_fn=_days_attributes,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AlphaEmsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the four Alpha EMS sensors."""
    coordinator: AlphaEmsCoordinator = entry.runtime_data
    async_add_entities(
        AlphaEmsSensor(coordinator, description) for description in SENSORS
    )


class AlphaEmsSensor(CoordinatorEntity[AlphaEmsCoordinator], SensorEntity):
    """A coordinator-backed Alpha EMS sensor."""

    _attr_has_entity_name = True
    entity_description: AlphaEmsSensorDescription

    def __init__(
        self,
        coordinator: AlphaEmsCoordinator,
        description: AlphaEmsSensorDescription,
    ) -> None:
        """Bind the sensor to its coordinator and description."""
        super().__init__(coordinator)
        self.entity_description = description
        entry = coordinator.entry
        # Config-entry scoped, so two Alpha EMS instances never collide.
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Alpha EMS",
            model=NAME,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> float | int | None:
        """Return the current sensor value."""
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the sensor's small attribute set."""
        if self.entity_description.attributes_fn is None:
            return {}
        return self.entity_description.attributes_fn(self.coordinator)
