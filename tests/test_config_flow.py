"""The config and options flows.

The flow's job is to stop a bad selection from ever reaching the learning
pipeline. Most of these tests are therefore about rejection: wrong unit, wrong
device class, missing entity, non-numeric state.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_MAX_CHARGE_KW,
    CONF_BATTERY_MAX_DISCHARGE_KW,
    CONF_BATTERY_MIN_SOC_PERCENT,
    CONF_BATTERY_POWER_ENTITY,
    CONF_BATTERY_POWER_SIGN,
    CONF_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
    CONF_BATTERY_SOC_ENTITY,
    CONF_DAILY_HOUSE_LOAD_ENTITY,
    CONF_EV_POWER_ENTITY,
    CONF_FRANK_ENTRY_ID,
    CONF_GRID_EXPORT_ENERGY_ENTITY,
    CONF_GRID_POWER_ENTITY,
    CONF_GRID_POWER_SIGN,
    CONF_HAS_PV,
    CONF_HOUSE_LOAD_ENTITY,
    CONF_NAME,
    CONF_PV_POWER_ENTITY,
    CONF_SOLCAST_ENTRY_ID,
    CONF_USE_PV_FORECAST,
    DEFAULT_BATTERY_MIN_SOC_PERCENT,
    DEFAULT_BATTERY_POWER_SIGN,
    DEFAULT_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
    DEFAULT_GRID_POWER_SIGN,
    DOMAIN,
    SIGN_BATTERY_POSITIVE_IS_CHARGE,
    SIGN_GRID_NEGATIVE_IS_IMPORT,
)

from .conftest import (
    BATTERY_POWER,
    BATTERY_SOC,
    DAILY_HOUSE_LOAD,
    GRID_POWER,
    HOUSE_LOAD,
    PV_POWER,
    set_sensor,
)

# An alternative grid meter, to prove HomeWizard is not special-cased.
DSMR_GRID = "sensor.dsmr_power_delivered"


def user_step(**overrides: Any) -> dict[str, Any]:
    """Return a valid first-step payload."""
    return {
        CONF_NAME: "Alpha EMS",
        CONF_HOUSE_LOAD_ENTITY: HOUSE_LOAD,
        CONF_DAILY_HOUSE_LOAD_ENTITY: DAILY_HOUSE_LOAD,
        CONF_HAS_PV: True,
        CONF_USE_PV_FORECAST: False,
        **overrides,
    }


#: A plausible 10 kWh battery with a 5 kW inverter, used wherever a test needs
#: the Phase-3 planning figures to be present and does not care what they are.
BATTERY_PLANNING = {
    CONF_BATTERY_CAPACITY_KWH: 10.0,
    CONF_BATTERY_MIN_SOC_PERCENT: DEFAULT_BATTERY_MIN_SOC_PERCENT,
    CONF_BATTERY_MAX_CHARGE_KW: 5.0,
    CONF_BATTERY_MAX_DISCHARGE_KW: 5.0,
    CONF_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT: (
        DEFAULT_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT
    ),
}


def battery_step(**overrides: Any) -> dict[str, Any]:
    """Return a valid battery-step payload, sources and planning figures alike."""
    return {
        CONF_BATTERY_SOC_ENTITY: BATTERY_SOC,
        CONF_BATTERY_POWER_ENTITY: BATTERY_POWER,
        CONF_BATTERY_POWER_SIGN: DEFAULT_BATTERY_POWER_SIGN,
        **BATTERY_PLANNING,
        **overrides,
    }


def battery_options_payload(**overrides: Any) -> dict[str, Any]:
    """Return a valid submission for the battery-planning options page."""
    return {**BATTERY_PLANNING, **overrides}


def grid_step(**overrides: Any) -> dict[str, Any]:
    """Return a valid grid-step payload."""
    return {
        CONF_GRID_POWER_ENTITY: GRID_POWER,
        CONF_GRID_POWER_SIGN: DEFAULT_GRID_POWER_SIGN,
        **overrides,
    }


async def start(hass: HomeAssistant) -> dict[str, Any]:
    """Begin the user flow."""
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )


async def submit(hass: HomeAssistant, flow_id: str, payload: dict) -> dict[str, Any]:
    """Submit one step."""
    return await hass.config_entries.flow.async_configure(flow_id, payload)


async def open_options(
    hass: HomeAssistant, entry_id: str, page: str = "sources"
) -> dict[str, Any]:
    """Open one page of the options flow.

    The options flow is a menu since Phase 3 added the battery-planning figures:
    thirteen source selections and five hardware numbers are edited on different
    occasions, and appending the numbers to the existing form would have buried
    them. Every options test goes through here so the navigation lives in one
    place.
    """
    result = await hass.config_entries.options.async_init(entry_id)
    if result["type"] is FlowResultType.MENU:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": page}
        )
    return result


# -- happy paths -------------------------------------------------------------


async def test_a_complete_setup_creates_the_entry(
    hass: HomeAssistant, source_entities: None, frank_config_entry: MockConfigEntry
) -> None:
    """The full five-step flow produces a config entry with every selection."""
    result = await start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await submit(hass, result["flow_id"], user_step())
    assert result["step_id"] == "battery"

    result = await submit(hass, result["flow_id"], battery_step())
    assert result["step_id"] == "solar"

    result = await submit(hass, result["flow_id"], {CONF_PV_POWER_ENTITY: PV_POWER})
    assert result["step_id"] == "grid"

    result = await submit(hass, result["flow_id"], grid_step())
    assert result["step_id"] == "sources"

    result = await submit(
        hass, result["flow_id"], {CONF_FRANK_ENTRY_ID: frank_config_entry.entry_id}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Alpha EMS"
    data = result["data"]
    assert data[CONF_HOUSE_LOAD_ENTITY] == HOUSE_LOAD
    assert data[CONF_PV_POWER_ENTITY] == PV_POWER
    assert data[CONF_GRID_POWER_ENTITY] == GRID_POWER
    assert data[CONF_FRANK_ENTRY_ID] == frank_config_entry.entry_id
    assert data[CONF_BATTERY_POWER_SIGN] == DEFAULT_BATTERY_POWER_SIGN


async def test_without_pv_the_solar_step_is_skipped(
    hass: HomeAssistant, source_entities: None, frank_config_entry: MockConfigEntry
) -> None:
    """A PV-less system is never asked where its panels are."""
    result = await start(hass)
    result = await submit(hass, result["flow_id"], user_step(**{CONF_HAS_PV: False}))
    assert result["step_id"] == "battery"

    result = await submit(hass, result["flow_id"], battery_step())
    assert result["step_id"] == "grid", "solar step should have been skipped"

    result = await submit(hass, result["flow_id"], grid_step())
    result = await submit(
        hass, result["flow_id"], {CONF_FRANK_ENTRY_ID: frank_config_entry.entry_id}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_PV_POWER_ENTITY not in result["data"]


async def test_pv_forecast_requires_and_records_solcast(
    hass: HomeAssistant,
    source_entities: None,
    frank_config_entry: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
) -> None:
    """Enabling the forecast adds a Solcast selection to the last step."""
    result = await start(hass)
    result = await submit(
        hass, result["flow_id"], user_step(**{CONF_USE_PV_FORECAST: True})
    )
    result = await submit(hass, result["flow_id"], battery_step())
    result = await submit(hass, result["flow_id"], {CONF_PV_POWER_ENTITY: PV_POWER})
    result = await submit(hass, result["flow_id"], grid_step())
    assert result["step_id"] == "sources"

    result = await submit(
        hass,
        result["flow_id"],
        {
            CONF_FRANK_ENTRY_ID: frank_config_entry.entry_id,
            CONF_SOLCAST_ENTRY_ID: solcast_config_entry.entry_id,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SOLCAST_ENTRY_ID] == solcast_config_entry.entry_id


async def test_an_alternative_grid_meter_is_accepted(
    hass: HomeAssistant, source_entities: None, frank_config_entry: MockConfigEntry
) -> None:
    """A DSMR sensor works exactly as well as a HomeWizard one.

    Nothing in the flow special-cases a meter integration; only the unit is
    checked.
    """
    set_sensor(hass, DSMR_GRID, 1.234, "kW", "power")

    result = await start(hass)
    result = await submit(hass, result["flow_id"], user_step(**{CONF_HAS_PV: False}))
    result = await submit(hass, result["flow_id"], battery_step())
    result = await submit(
        hass,
        result["flow_id"],
        grid_step(**{CONF_GRID_POWER_ENTITY: DSMR_GRID}),
    )
    result = await submit(
        hass, result["flow_id"], {CONF_FRANK_ENTRY_ID: frank_config_entry.entry_id}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_GRID_POWER_ENTITY] == DSMR_GRID


async def test_both_sign_conventions_can_be_chosen(
    hass: HomeAssistant, source_entities: None, frank_config_entry: MockConfigEntry
) -> None:
    """Neither sign convention is hard-coded."""
    result = await start(hass)
    result = await submit(hass, result["flow_id"], user_step(**{CONF_HAS_PV: False}))
    result = await submit(
        hass,
        result["flow_id"],
        battery_step(**{CONF_BATTERY_POWER_SIGN: SIGN_BATTERY_POSITIVE_IS_CHARGE}),
    )
    result = await submit(
        hass,
        result["flow_id"],
        grid_step(**{CONF_GRID_POWER_SIGN: SIGN_GRID_NEGATIVE_IS_IMPORT}),
    )
    result = await submit(
        hass, result["flow_id"], {CONF_FRANK_ENTRY_ID: frank_config_entry.entry_id}
    )

    assert result["data"][CONF_BATTERY_POWER_SIGN] == SIGN_BATTERY_POSITIVE_IS_CHARGE
    assert result["data"][CONF_GRID_POWER_SIGN] == SIGN_GRID_NEGATIVE_IS_IMPORT


# -- missing prerequisites ---------------------------------------------------


async def test_without_frank_the_flow_aborts_immediately(
    hass: HomeAssistant, source_entities: None
) -> None:
    """The hard requirement is reported before any form is filled in."""
    result = await start(hass)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "frank_not_configured"


async def test_pv_forecast_without_solcast_is_reported_inline(
    hass: HomeAssistant, source_entities: None, frank_config_entry: MockConfigEntry
) -> None:
    """Asking for a forecast with no Solcast is a field error, not an abort.

    An abort would discard everything the user had already typed, when simply
    turning the toggle off is a perfectly good way forward.
    """
    result = await start(hass)
    result = await submit(
        hass, result["flow_id"], user_step(**{CONF_USE_PV_FORECAST: True})
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {CONF_USE_PV_FORECAST: "solcast_not_configured"}


async def test_pv_disabled_without_solcast_is_fine(
    hass: HomeAssistant, source_entities: None, frank_config_entry: MockConfigEntry
) -> None:
    """Solcast is only required when the forecast is actually wanted."""
    result = await start(hass)
    result = await submit(
        hass, result["flow_id"], user_step(**{CONF_USE_PV_FORECAST: False})
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "battery"


# -- validation --------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "unit", "device_class", "expected"),
    [
        (100, "kWh", "energy", "invalid_power_entity"),
        (100, "%", "battery", "invalid_power_entity"),
        (100, None, None, "invalid_power_entity"),
        (21.5, "°C", "temperature", "invalid_power_entity"),
        ("closed", "W", "power", "entity_not_numeric"),
    ],
)
async def test_an_incompatible_house_load_entity_is_rejected(
    hass: HomeAssistant,
    source_entities: None,
    frank_config_entry: MockConfigEntry,
    state,
    unit,
    device_class,
    expected,
) -> None:
    """The learning source must be a numeric power sensor."""
    bad = "sensor.wrong_kind_of_thing"
    set_sensor(hass, bad, state, unit, device_class)

    result = await start(hass)
    result = await submit(
        hass, result["flow_id"], user_step(**{CONF_HOUSE_LOAD_ENTITY: bad})
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_HOUSE_LOAD_ENTITY: expected}


async def test_a_missing_house_load_entity_is_rejected(
    hass: HomeAssistant, source_entities: None, frank_config_entry: MockConfigEntry
) -> None:
    """An entity that does not exist cannot be the learning source."""
    result = await start(hass)
    result = await submit(
        hass,
        result["flow_id"],
        user_step(**{CONF_HOUSE_LOAD_ENTITY: "sensor.does_not_exist"}),
    )

    assert result["errors"] == {CONF_HOUSE_LOAD_ENTITY: "entity_not_found"}


async def test_an_unavailable_source_is_still_accepted(
    hass: HomeAssistant, source_entities: None, frank_config_entry: MockConfigEntry
) -> None:
    """A momentarily unavailable sensor does not block setup.

    Cloud-backed integrations drop out regularly; refusing the selection would
    be far more annoying than useful, and the unit is still readable.
    """
    flaky = "sensor.flaky_power"
    set_sensor(hass, flaky, "unavailable", "W", "power")

    result = await start(hass)
    result = await submit(
        hass, result["flow_id"], user_step(**{CONF_HOUSE_LOAD_ENTITY: flaky})
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "battery"


async def test_a_non_percentage_battery_soc_is_rejected(
    hass: HomeAssistant, source_entities: None, frank_config_entry: MockConfigEntry
) -> None:
    """State of charge must be a percentage."""
    result = await start(hass)
    result = await submit(hass, result["flow_id"], user_step())
    result = await submit(
        hass,
        result["flow_id"],
        battery_step(**{CONF_BATTERY_SOC_ENTITY: BATTERY_POWER}),
    )

    assert result["errors"] == {CONF_BATTERY_SOC_ENTITY: "invalid_percentage_entity"}


async def test_a_non_energy_validation_sensor_is_rejected(
    hass: HomeAssistant, source_entities: None, frank_config_entry: MockConfigEntry
) -> None:
    """The optional daily cross-check must be an energy sensor."""
    result = await start(hass)
    result = await submit(
        hass,
        result["flow_id"],
        user_step(**{CONF_DAILY_HOUSE_LOAD_ENTITY: HOUSE_LOAD}),
    )

    assert result["errors"] == {CONF_DAILY_HOUSE_LOAD_ENTITY: "invalid_energy_entity"}


async def test_the_validation_sensor_is_optional(
    hass: HomeAssistant, source_entities: None, frank_config_entry: MockConfigEntry
) -> None:
    """Omitting the daily cross-check is allowed."""
    payload = user_step()
    payload.pop(CONF_DAILY_HOUSE_LOAD_ENTITY)

    result = await start(hass)
    result = await submit(hass, result["flow_id"], payload)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "battery"


async def test_a_bad_pv_entity_is_rejected(
    hass: HomeAssistant, source_entities: None, frank_config_entry: MockConfigEntry
) -> None:
    """The PV step validates just as strictly as the others."""
    result = await start(hass)
    result = await submit(hass, result["flow_id"], user_step())
    result = await submit(hass, result["flow_id"], battery_step())
    result = await submit(
        hass, result["flow_id"], {CONF_PV_POWER_ENTITY: DAILY_HOUSE_LOAD}
    )

    assert result["errors"] == {CONF_PV_POWER_ENTITY: "invalid_power_entity"}


async def test_a_bad_grid_entity_is_rejected(
    hass: HomeAssistant, source_entities: None, frank_config_entry: MockConfigEntry
) -> None:
    """The grid step validates the unit too."""
    result = await start(hass)
    result = await submit(hass, result["flow_id"], user_step(**{CONF_HAS_PV: False}))
    result = await submit(hass, result["flow_id"], battery_step())
    result = await submit(
        hass, result["flow_id"], grid_step(**{CONF_GRID_POWER_ENTITY: BATTERY_SOC})
    )

    assert result["errors"] == {CONF_GRID_POWER_ENTITY: "invalid_power_entity"}


async def test_recovering_from_an_error_continues_the_flow(
    hass: HomeAssistant, source_entities: None, frank_config_entry: MockConfigEntry
) -> None:
    """Correcting a rejected field lets the flow proceed normally."""
    result = await start(hass)
    result = await submit(
        hass,
        result["flow_id"],
        user_step(**{CONF_HOUSE_LOAD_ENTITY: "sensor.does_not_exist"}),
    )
    assert result["errors"]

    result = await submit(hass, result["flow_id"], user_step())
    assert result["step_id"] == "battery"
    assert not result.get("errors")


# -- multiple instances ------------------------------------------------------


async def test_a_second_instance_can_be_added(
    hass: HomeAssistant, source_entities: None, frank_config_entry: MockConfigEntry
) -> None:
    """Two houses, two entries. Nothing aborts as already-configured."""
    for name in ("Alpha EMS", "Alpha EMS Cottage"):
        result = await start(hass)
        result = await submit(
            hass,
            result["flow_id"],
            user_step(**{CONF_NAME: name, CONF_HAS_PV: False}),
        )
        result = await submit(hass, result["flow_id"], battery_step())
        result = await submit(hass, result["flow_id"], grid_step())
        result = await submit(
            hass, result["flow_id"], {CONF_FRANK_ENTRY_ID: frank_config_entry.entry_id}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["title"] == name

    assert len(hass.config_entries.async_entries(DOMAIN)) == 2


# -- options flow ------------------------------------------------------------


def options_payload(frank_entry_id: str, **overrides: Any) -> dict[str, Any]:
    """Return a valid options submission."""
    return {
        CONF_HOUSE_LOAD_ENTITY: HOUSE_LOAD,
        CONF_DAILY_HOUSE_LOAD_ENTITY: DAILY_HOUSE_LOAD,
        CONF_BATTERY_SOC_ENTITY: BATTERY_SOC,
        CONF_BATTERY_POWER_ENTITY: BATTERY_POWER,
        CONF_BATTERY_POWER_SIGN: DEFAULT_BATTERY_POWER_SIGN,
        CONF_HAS_PV: True,
        CONF_PV_POWER_ENTITY: PV_POWER,
        CONF_GRID_POWER_ENTITY: GRID_POWER,
        CONF_GRID_POWER_SIGN: DEFAULT_GRID_POWER_SIGN,
        CONF_FRANK_ENTRY_ID: frank_entry_id,
        CONF_USE_PV_FORECAST: False,
        **overrides,
    }


async def test_the_options_form_renders(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank_config_entry: MockConfigEntry,
) -> None:
    """Every changeable source appears on one page."""
    result = await open_options(hass, setup_integration.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "sources"
    keys = {str(marker) for marker in result["data_schema"].schema}
    # Every source listed in the README as changeable, plus the Solcast slot,
    # which is always rendered but only required when the forecast is enabled,
    # and the beta.48 export counter, which is rendered always and required never.
    assert keys == set(options_payload(frank_config_entry.entry_id)) | {
        CONF_SOLCAST_ENTRY_ID,
        CONF_EV_POWER_ENTITY,
        CONF_GRID_EXPORT_ENERGY_ENTITY,
    }


async def test_changing_a_source_is_saved_and_reloads(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank_config_entry: MockConfigEntry,
) -> None:
    """A new house-load entity takes effect without re-adding the integration."""
    replacement = "sensor.new_house_load"
    set_sensor(hass, replacement, 1500, "W", "power")

    result = await open_options(hass, setup_integration.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        options_payload(
            frank_config_entry.entry_id, **{CONF_HOUSE_LOAD_ENTITY: replacement}
        ),
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert setup_integration.options[CONF_HOUSE_LOAD_ENTITY] == replacement
    assert setup_integration.runtime_data.config.house_load_entity == replacement


async def test_unrelated_options_are_preserved(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank_config_entry: MockConfigEntry,
) -> None:
    """An edit never silently drops a key the form does not render.

    A future release adding an option must not have it wiped by a user changing
    something unrelated in an older form.
    """
    hass.config_entries.async_update_entry(
        setup_integration,
        options={**setup_integration.options, "future_option": "keep me"},
    )
    await hass.async_block_till_done()

    result = await open_options(hass, setup_integration.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        options_payload(
            frank_config_entry.entry_id,
            **{CONF_BATTERY_POWER_SIGN: SIGN_BATTERY_POSITIVE_IS_CHARGE},
        ),
    )
    await hass.async_block_till_done()

    assert setup_integration.options["future_option"] == "keep me"
    assert (
        setup_integration.options[CONF_BATTERY_POWER_SIGN]
        == SIGN_BATTERY_POSITIVE_IS_CHARGE
    )


async def test_options_reject_an_incompatible_entity(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank_config_entry: MockConfigEntry,
) -> None:
    """The options form validates as strictly as the config flow."""
    result = await open_options(hass, setup_integration.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        options_payload(
            frank_config_entry.entry_id, **{CONF_HOUSE_LOAD_ENTITY: DAILY_HOUSE_LOAD}
        ),
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_HOUSE_LOAD_ENTITY: "invalid_power_entity"}


async def test_declaring_pv_without_a_pv_entity_is_rejected(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank_config_entry: MockConfigEntry,
) -> None:
    """Consistency between the toggle and the entity is enforced."""
    payload = options_payload(frank_config_entry.entry_id, **{CONF_HAS_PV: True})
    payload.pop(CONF_PV_POWER_ENTITY)

    result = await open_options(hass, setup_integration.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], payload
    )

    assert result["errors"] == {CONF_PV_POWER_ENTITY: "pv_entity_required"}


async def test_enabling_the_forecast_without_solcast_is_rejected(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank_config_entry: MockConfigEntry,
) -> None:
    """The same consistency rule applies to the PV forecast."""
    result = await open_options(hass, setup_integration.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        options_payload(frank_config_entry.entry_id, **{CONF_USE_PV_FORECAST: True}),
    )

    assert result["errors"] == {CONF_SOLCAST_ENTRY_ID: "solcast_entry_required"}


async def test_clearing_the_optional_validation_sensor_removes_it(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank_config_entry: MockConfigEntry,
) -> None:
    """An optional field left blank is cleared, not silently retained."""
    payload = options_payload(frank_config_entry.entry_id)
    payload.pop(CONF_DAILY_HOUSE_LOAD_ENTITY)

    result = await open_options(hass, setup_integration.entry_id)
    await hass.config_entries.options.async_configure(result["flow_id"], payload)
    await hass.async_block_till_done()

    # Recorded as an explicit None rather than removed: the effective config
    # falls back to entry.data, so a missing key would restore the old value.
    assert setup_integration.options[CONF_DAILY_HOUSE_LOAD_ENTITY] is None
    assert setup_integration.runtime_data.config.daily_house_load_entity is None


async def test_reload_after_options_creates_no_duplicate_entities(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank_config_entry: MockConfigEntry,
) -> None:
    """Changing an option must not leave ``_2`` entities behind."""
    result = await open_options(hass, setup_integration.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        options_payload(
            frank_config_entry.entry_id,
            **{CONF_GRID_POWER_SIGN: SIGN_GRID_NEGATIVE_IS_IMPORT},
        ),
    )
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    ours = [
        entity.entity_id
        for entity in registry.entities.values()
        if entity.platform == DOMAIN
    ]
    assert len(ours) == 18
    assert not [entity_id for entity_id in ours if entity_id.endswith("_2")]
