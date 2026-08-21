"""The exact entity surface Alpha EMS Manager is allowed to create.

Thirteen entities and nothing else: four from Phase 1, two from Phase 2, three
from Phase 3, one sensor and one select from Phase 4, one from Phase 7 and one
from Phase 8. This module freezes that promise, so an accidental extra entity, a
changed unique id or a lost unit fails here rather than surprising a user whose
dashboard silently gained a row.

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
    # Phase 3. The recommendation is an enum, so it carries a device class and
    # deliberately no state class; the other two are measurements of a plan.
    "sensor.alpha_ems_battery_recommendation": {
        "unique_id_suffix": "battery_recommendation",
        "name": "Alpha EMS Battery Recommendation",
        "unit": None,
        "device_class": "enum",
        "state_class": None,
        "icon": "mdi:battery-heart-variant",
    },
    "sensor.alpha_ems_planned_battery_power": {
        "unique_id_suffix": "battery_planned_power",
        "name": "Alpha EMS Planned Battery Power",
        "unit": "kW",
        "device_class": None,
        "state_class": "measurement",
        "icon": "mdi:transmission-tower",
    },
    "sensor.alpha_ems_usable_battery_energy": {
        "unique_id_suffix": "battery_usable_energy",
        "name": "Alpha EMS Usable Battery Energy",
        "unit": "kWh",
        "device_class": "energy_storage",
        "state_class": "measurement",
        "icon": "mdi:battery-charging-medium",
    },
    # Phase 7. A stored-energy level like Usable Battery Energy, and
    # deliberately not ENERGY: a requirement is not consumption, and offering it
    # to the Energy dashboard would invite exactly that reading.
    "sensor.alpha_ems_dynamic_battery_reserve": {
        "unique_id_suffix": "dynamic_battery_reserve",
        "name": "Alpha EMS Dynamic Battery Reserve",
        "unit": "kWh",
        "device_class": "energy_storage",
        "state_class": "measurement",
        "icon": "mdi:battery-lock",
    },
    # Phase 8. An enum like the recommendation and the control state, and for the
    # same reason: it is categorical, so a long-term statistic over it means
    # nothing.
    "sensor.alpha_ems_economic_action": {
        "unique_id_suffix": "economic_action",
        "name": "Alpha EMS Economic Action",
        "unit": None,
        "device_class": "enum",
        "state_class": None,
        "icon": "mdi:cash-clock",
    },
    "sensor.alpha_ems_control_state": {
        "unique_id_suffix": "control_state",
        "name": "Alpha EMS Control State",
        "unit": None,
        "device_class": "enum",
        "state_class": None,
        "icon": "mdi:shield-check-outline",
    },
    "select.alpha_ems_control_mode": {
        "unique_id_suffix": "control_mode",
        "name": "Alpha EMS Control Mode",
        "unit": None,
        "device_class": None,
        "state_class": None,
        "icon": "mdi:tune-variant",
    },
}


def test_no_entity_is_missing_or_extra(hass: HomeAssistant) -> None:
    """Exactly the thirteen documented entities exist.

    Covers both platforms, so the select is held to the same table as the
    sensors rather than being checked somewhere looser.
    """
    registry = er.async_get(hass)
    created = {
        entity.entity_id
        for entity in registry.entities.values()
        if entity.platform == DOMAIN
    }
    assert created == set(CONTRACT)
    assert len(created) == len(CONTRACT) == 13


#: The entity surface, phase by phase. Named rather than counted, so a new
#: entity cannot be waved through by adjusting a single number -- and so it is
#: visible which phase each one belongs to.
PHASE_ONE_ENTITIES = {
    "sensor.alpha_ems_expected_house_load_today",
    "sensor.alpha_ems_expected_house_load_tomorrow",
    "sensor.alpha_ems_learning_confidence",
    "sensor.alpha_ems_learning_days",
}
PHASE_TWO_ENTITIES = {
    "sensor.alpha_ems_forecast_error_yesterday",
    "sensor.alpha_ems_forecast_error_7_days",
}
PHASE_THREE_ENTITIES = {
    "sensor.alpha_ems_battery_recommendation",
    "sensor.alpha_ems_planned_battery_power",
    "sensor.alpha_ems_usable_battery_energy",
}
#: Phase 4 adds one sensor here and one select on its own platform. Every gate
#: condition, capability finding, read-back value and planned command step stays
#: in attributes and diagnostics: a pipeline with twenty-five ways to refuse must
#: not become twenty-five rows on a dashboard.
PHASE_FOUR_ENTITIES = {
    "sensor.alpha_ems_control_state",
    "select.alpha_ems_control_mode",
}
#: The select half of that, kept separate because it is the first writable
#: entity this integration has ever had.
PHASE_FOUR_SELECTS = {
    "select.alpha_ems_control_mode",
}
#: Phase 7 adds exactly one. The two counterfactuals the requirement is
#: bracketed by, the peak requirement, the per-interval trajectory, the
#: constraint tallies and the provenance the calculation deliberately does not
#: consult are all diagnostics-only: a requirement with six ways of being wrong
#: must not become six rows on a dashboard.
PHASE_SEVEN_ENTITIES = {
    "sensor.alpha_ems_dynamic_battery_reserve",
}
#: Phase 8 adds exactly one. Both counterfactual plans, every planned run, the
#: solver figures, the reserve-protection cost and the provenance the calculation
#: does not consult are diagnostics-only: an optimizer with six ways of being
#: wrong must not become six rows.
PHASE_EIGHT_ENTITIES = {
    "sensor.alpha_ems_economic_action",
}


def test_phase_two_added_exactly_two_entities(hass: HomeAssistant) -> None:
    """The evidence layer is worth two rows on a dashboard, and no more.

    Everything else it records -- the snapshot inventory, per-horizon and
    per-slot error breakdowns, modelled-versus-filled performance, storage
    health, lifecycle counts -- is diagnostics-only by design.
    """
    assert (
        set(CONTRACT)
        - PHASE_ONE_ENTITIES
        - PHASE_THREE_ENTITIES
        - PHASE_FOUR_ENTITIES
        - PHASE_SEVEN_ENTITIES
        - PHASE_EIGHT_ENTITIES
        == PHASE_TWO_ENTITIES
    )


def test_phase_three_added_exactly_three_entities(hass: HomeAssistant) -> None:
    """The decision layer is worth three rows, and no more.

    The simulated trajectory, the per-band split, the binding-constraint tally,
    the what-if comparison and the PV-blind projected state of charge are all
    diagnostics-only. A ninety-six-interval plan has no place in an attribute,
    and a projection the simulator cannot honestly make has no place in an
    entity.
    """
    assert (
        set(CONTRACT)
        - PHASE_ONE_ENTITIES
        - PHASE_TWO_ENTITIES
        - PHASE_FOUR_ENTITIES
        - PHASE_SEVEN_ENTITIES
        - PHASE_EIGHT_ENTITIES
        == PHASE_THREE_ENTITIES
    )
    assert len(PHASE_THREE_ENTITIES) == 3


def test_phase_four_added_exactly_one_sensor_and_one_select(
    hass: HomeAssistant,
) -> None:
    """The control layer is worth one control and one state, and no more.

    Everything the pipeline computes -- which parts of the control surface were
    found, what the inverter is doing, the intent, the quantised command, the
    exact ordered command list, the safety verdict, the authorization refusal and
    the event trail -- is in attributes and diagnostics.
    """
    assert (
        set(CONTRACT)
        - PHASE_ONE_ENTITIES
        - PHASE_TWO_ENTITIES
        - PHASE_THREE_ENTITIES
        - PHASE_SEVEN_ENTITIES
        - PHASE_EIGHT_ENTITIES
        == PHASE_FOUR_ENTITIES
    )
    assert len(PHASE_FOUR_ENTITIES) == 2

    registry = er.async_get(hass)
    selects = {
        entry.entity_id
        for entry in registry.entities.values()
        if entry.platform == DOMAIN and entry.domain == "select"
    }
    assert selects == PHASE_FOUR_SELECTS


def test_the_earlier_phase_entities_are_untouched_by_phase_four(
    hass: HomeAssistant,
) -> None:
    """Phase 4 is additive. The nine existing rows keep their exact contract.

    A second literal table, for the reason the first one exists: editing
    ``CONTRACT`` to accommodate a Phase-4 change must not be able to relax an
    earlier entity at the same time.
    """
    existing = {
        "sensor.alpha_ems_expected_house_load_today": ("kWh", "energy", None),
        "sensor.alpha_ems_expected_house_load_tomorrow": ("kWh", "energy", None),
        "sensor.alpha_ems_learning_confidence": ("%", None, "measurement"),
        "sensor.alpha_ems_learning_days": (None, None, "measurement"),
        "sensor.alpha_ems_forecast_error_yesterday": ("kWh", None, "measurement"),
        "sensor.alpha_ems_forecast_error_7_days": ("%", None, "measurement"),
        "sensor.alpha_ems_battery_recommendation": (None, "enum", None),
        "sensor.alpha_ems_planned_battery_power": ("kW", None, "measurement"),
        "sensor.alpha_ems_usable_battery_energy": (
            "kWh",
            "energy_storage",
            "measurement",
        ),
    }
    registry = er.async_get(hass)
    for entity_id, (unit, device_class, state_class) in existing.items():
        entry = registry.async_get(entity_id)
        assert entry is not None, entity_id
        assert entry.unit_of_measurement == unit, entity_id
        assert entry.original_device_class == device_class, entity_id
        state = hass.states.get(entity_id)
        assert state is not None, entity_id
        assert state.attributes.get("state_class") == state_class, entity_id


def test_the_phase_one_and_two_entities_are_untouched_by_phase_three(
    hass: HomeAssistant,
) -> None:
    """Phase 3 is additive. The six existing rows keep their exact contract.

    Asserted against the literal table rather than against ``CONTRACT``, so
    editing the table to accommodate a Phase-3 change cannot silently relax a
    Phase-1 or Phase-2 entity at the same time.
    """
    existing = {
        "sensor.alpha_ems_expected_house_load_today": ("kWh", "energy", None),
        "sensor.alpha_ems_expected_house_load_tomorrow": ("kWh", "energy", None),
        "sensor.alpha_ems_learning_confidence": ("%", None, "measurement"),
        "sensor.alpha_ems_learning_days": (None, None, "measurement"),
        "sensor.alpha_ems_forecast_error_yesterday": ("kWh", None, "measurement"),
        "sensor.alpha_ems_forecast_error_7_days": ("%", None, "measurement"),
    }
    registry = er.async_get(hass)
    for entity_id, (unit, device_class, state_class) in existing.items():
        entry = registry.async_get(entity_id)
        assert entry is not None, entity_id
        assert entry.unit_of_measurement == unit, entity_id
        assert entry.original_device_class == device_class, entity_id
        state = hass.states.get(entity_id)
        assert state is not None, entity_id
        assert state.attributes.get("state_class") == state_class, entity_id


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
    # Widened for Phase 4, not relaxed: ``select`` is named explicitly, so a
    # third platform appearing still fails. The control mode has to be a runtime
    # control rather than a configuration field -- a user stopping the
    # integration should not have to open a dialog to do it.
    assert domains == {"sensor", "select"}


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
