"""The exact entity surface Alpha EMS Manager is allowed to create.

Six sensors and nothing else: four from Phase 1, two from Phase 2. This module
freezes that promise, so an accidental extra entity, a changed unique id or a
lost unit fails here rather than surprising a user whose dashboard silently
gained a row.

The two Phase-2 sensors deliberately break the Phase-1 pattern in one respect
and follow it in another. They carry a state class, because a measurement of
error that has already happened belongs in long-term statistics; they carry no
energy device class, because the yesterday figure is signed and an energy class
would offer a difference to the Energy dashboard as if it were consumption.
"""

from __future__ import annotations

import pytest
from homeassistant.const import ATTR_FRIENDLY_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.alpha_ems_manager.const import DOMAIN

pytestmark = pytest.mark.usefixtures("setup_integration")

#: entity_id -> expected registry and state contract.
CONTRACT: dict[str, dict[str, object]] = {
    "sensor.alpha_ems_expected_house_load_today": {
        "unique_id_suffix": "expected_house_load_today",
        "name": "Alpha EMS Expected House Load Today",
        "unit": "kWh",
        "device_class": "energy",
        "state_class": None,
        "icon": "mdi:home-lightning-bolt",
    },
    "sensor.alpha_ems_expected_house_load_tomorrow": {
        "unique_id_suffix": "expected_house_load_tomorrow",
        "name": "Alpha EMS Expected House Load Tomorrow",
        "unit": "kWh",
        "device_class": "energy",
        "state_class": None,
        "icon": "mdi:home-clock",
    },
    "sensor.alpha_ems_learning_confidence": {
        "unique_id_suffix": "learning_confidence",
        "name": "Alpha EMS Learning Confidence",
        "unit": "%",
        "device_class": None,
        "state_class": "measurement",
        "icon": "mdi:gauge",
    },
    "sensor.alpha_ems_learning_days": {
        "unique_id_suffix": "learning_days",
        "name": "Alpha EMS Learning Days",
        "unit": None,
        "device_class": None,
        "state_class": "measurement",
        "icon": "mdi:calendar-check",
    },
    "sensor.alpha_ems_forecast_error_yesterday": {
        "unique_id_suffix": "forecast_error_yesterday",
        "name": "Alpha EMS Forecast Error Yesterday",
        "unit": "kWh",
        "device_class": None,
        "state_class": "measurement",
        "icon": "mdi:delta",
    },
    "sensor.alpha_ems_forecast_error_7_days": {
        "unique_id_suffix": "forecast_error_7d",
        "name": "Alpha EMS Forecast Error 7 Days",
        "unit": "%",
        "device_class": None,
        "state_class": "measurement",
        "icon": "mdi:chart-timeline-variant",
    },
}


def test_no_entity_is_missing_or_extra(hass: HomeAssistant) -> None:
    """Exactly the six documented entities exist."""
    registry = er.async_get(hass)
    created = {
        entity.entity_id
        for entity in registry.entities.values()
        if entity.platform == DOMAIN
    }
    assert created == set(CONTRACT)
    assert len(created) == len(CONTRACT) == 6


def test_phase_two_added_exactly_two_entities(hass: HomeAssistant) -> None:
    """The evidence layer is worth two rows on a dashboard, and no more.

    Everything else it records -- the snapshot inventory, per-horizon and
    per-slot error breakdowns, modelled-versus-filled performance, storage
    health, lifecycle counts -- is diagnostics-only by design. Naming the four
    Phase-1 entities explicitly means a future entity cannot be waved through by
    adjusting a single number.
    """
    phase_one = {
        "sensor.alpha_ems_expected_house_load_today",
        "sensor.alpha_ems_expected_house_load_tomorrow",
        "sensor.alpha_ems_learning_confidence",
        "sensor.alpha_ems_learning_days",
    }
    phase_two = set(CONTRACT) - phase_one

    assert phase_two == {
        "sensor.alpha_ems_forecast_error_yesterday",
        "sensor.alpha_ems_forecast_error_7_days",
    }


def test_the_forecast_error_sensors_are_measurements_not_predictions(
    hass: HomeAssistant,
) -> None:
    """They record what already happened, so statistics are legitimate here."""
    for entity_id in (
        "sensor.alpha_ems_forecast_error_yesterday",
        "sensor.alpha_ems_forecast_error_7_days",
    ):
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.attributes.get("state_class") == "measurement"
        # Never an energy device class: the yesterday figure is a signed
        # difference, and the Energy dashboard must not be offered it.
        assert state.attributes.get("device_class") is None


def test_an_unresolved_forecast_error_reads_unknown_rather_than_zero(
    hass: HomeAssistant,
) -> None:
    """A fresh installation has nothing to compare, and must say so.

    Zero is the value of a perfect forecast. Publishing it where no forecast has
    yet been scored would be the "learned nothing must never read as zero" rule
    broken at the last possible moment.
    """
    for entity_id in (
        "sensor.alpha_ems_forecast_error_yesterday",
        "sensor.alpha_ems_forecast_error_7_days",
    ):
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == "unknown"


def test_no_binary_sensors_or_other_platforms(hass: HomeAssistant) -> None:
    """Phase 1 ships sensors only; no binary sensor or switch sneaks in."""
    registry = er.async_get(hass)
    domains = {
        entity.entity_id.split(".")[0]
        for entity in registry.entities.values()
        if entity.platform == DOMAIN
    }
    assert domains == {"sensor"}


@pytest.mark.parametrize("entity_id", sorted(CONTRACT))
def test_registry_metadata(hass: HomeAssistant, entity_id: str) -> None:
    """Registry metadata matches the frozen contract."""
    expected = CONTRACT[entity_id]
    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    assert entry is not None

    assert entry.unique_id.endswith(f"_{expected['unique_id_suffix']}")
    assert entry.unique_id == f"{entry.config_entry_id}_{expected['unique_id_suffix']}"
    assert entry.unit_of_measurement == expected["unit"]
    assert entry.original_device_class == expected["device_class"]
    assert entry.original_icon == expected["icon"]
    assert entry.entity_category is None
    assert entry.disabled_by is None
    assert entry.hidden_by is None


@pytest.mark.parametrize("entity_id", sorted(CONTRACT))
def test_state_metadata(hass: HomeAssistant, entity_id: str) -> None:
    """The live state carries the documented name, unit and state class."""
    expected = CONTRACT[entity_id]
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes[ATTR_FRIENDLY_NAME] == expected["name"]
    assert state.attributes.get("unit_of_measurement") == expected["unit"]
    assert state.attributes.get("state_class") == expected["state_class"]


def test_forecast_sensors_have_no_state_class(hass: HomeAssistant) -> None:
    """A forecast must not become a long-term statistic.

    Both expected-load sensors carry ``device_class: energy`` so the UI formats
    them sensibly, but neither declares a state class. Giving a prediction one
    would put it into long-term statistics and make it eligible for the Energy
    dashboard, where it would be indistinguishable from measured consumption.
    """
    for entity_id in (
        "sensor.alpha_ems_expected_house_load_today",
        "sensor.alpha_ems_expected_house_load_tomorrow",
    ):
        state = hass.states.get(entity_id)
        assert state is not None
        assert "state_class" not in state.attributes


def test_all_entities_share_one_service_device(hass: HomeAssistant) -> None:
    """Every entity lives on a single service device."""
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)

    device_ids = {
        entity.device_id
        for entity in entity_registry.entities.values()
        if entity.platform == DOMAIN
    }
    assert len(device_ids) == 1

    device = device_registry.async_get(device_ids.pop())
    assert device is not None
    assert device.name == "Alpha EMS"
    assert device.manufacturer == "Alpha EMS"
    assert device.model == "Alpha EMS Manager"
    assert device.entry_type is dr.DeviceEntryType.SERVICE


def test_no_entity_exposes_a_large_array(hass: HomeAssistant) -> None:
    """No attribute carries a 96-slot profile or any other bulky structure.

    Recorder writes every attribute on every state change, so a quarter-hour
    profile hidden in an attribute would bloat the database indefinitely.
    """
    for entity_id in CONTRACT:
        state = hass.states.get(entity_id)
        assert state is not None
        for key, value in state.attributes.items():
            if isinstance(value, (list, tuple)):
                assert len(value) <= 8, f"{entity_id}.{key} exposes {len(value)} items"
            assert not isinstance(value, dict), f"{entity_id}.{key} exposes a mapping"
