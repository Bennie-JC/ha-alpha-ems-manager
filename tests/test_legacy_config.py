"""Protection against loading a config entry from the previous source model.

The v1 integration configured a cumulative daily house-load counter, six
individual Frank entities and a battery capacity entity. v2 configures an
instantaneous power sensor, sign conventions and config-entry references. The
two share **no** keys.

Before this guard existed, installing v2 over a v1 entry produced the worst
possible outcome: setup succeeded, healthy-looking sensors appeared, no
house-load listener was registered, nothing was ever learned, and nothing was
logged. These tests make that outcome impossible.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.const import (
    CONFIG_ENTRY_VERSION,
    DOMAIN,
    LEGACY_CONF_MARKER,
)

#: The v1 configuration exactly as the previous release wrote it.
LEGACY_DATA: dict[str, Any] = {
    "cumulative_house_load_sensor": "sensor.alphaess_today_s_house_load",
    "pv_actual_today_sensor": "sensor.alphaess_today_s_energy_from_pv",
    "pv_forecast_today_sensor": "sensor.solcast_pv_forecast_forecast_today",
    "pv_forecast_tomorrow_sensor": "sensor.solcast_pv_forecast_forecast_tomorrow",
    "frank_prices_today_sensor": "sensor.frank_prices_today",
    "frank_prices_tomorrow_sensor": "sensor.frank_prices_tomorrow",
    "frank_cheapest_time_today_sensor": "sensor.frank_cheapest_time_today",
    "battery_current_kwh_sensor": "sensor.alphaess_battery_energy",
    "battery_capacity_kwh_entity": "input_number.battery_capacity",
    "battery_soc_sensor": "sensor.alphaess_soc_battery",
    "ev_charger_power_sensor": "sensor.epp82dew_vermogen",
}


def legacy_entry() -> MockConfigEntry:
    """Return a config entry as written by the previous integration version."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Alpha EMS Manager",
        data=LEGACY_DATA,
        options={},
        version=1,
        unique_id=DOMAIN,
    )


def test_the_two_models_share_no_configuration_keys() -> None:
    """The premise of the guard, asserted rather than assumed."""
    from custom_components.alpha_ems_manager import const

    new_keys = {
        value
        for name, value in vars(const).items()
        if name.startswith("CONF_") and isinstance(value, str)
    }
    assert new_keys.isdisjoint(LEGACY_DATA)
    assert LEGACY_CONF_MARKER in LEGACY_DATA


async def test_a_legacy_entry_refuses_to_load(
    hass: HomeAssistant, source_entities: None
) -> None:
    """Migration fails loudly instead of loading a source-less integration."""
    entry = legacy_entry()
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.MIGRATION_ERROR


async def test_a_legacy_entry_creates_no_entities(
    hass: HomeAssistant, source_entities: None
) -> None:
    """No healthy-looking sensors appear for an entry that cannot work.

    This is the specific symptom the guard exists to prevent.
    """
    entry = legacy_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    created = [
        item.entity_id for item in registry.entities.values() if item.platform == DOMAIN
    ]
    assert created == []


async def test_a_legacy_entry_registers_no_listeners(
    hass: HomeAssistant, source_entities: None
) -> None:
    """Nothing is subscribed, so nothing can be silently mis-measured."""
    entry = legacy_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert getattr(entry, "runtime_data", None) is None


async def test_the_failure_names_the_version_and_the_remedy(
    hass: HomeAssistant, source_entities: None
) -> None:
    """The log tells the user exactly what to do about it.

    Asserted against the integration's own logger rather than ``caplog``, which
    recurses under the Home Assistant test plugin.
    """
    from unittest.mock import patch

    import custom_components.alpha_ems_manager as integration

    entry = legacy_entry()
    entry.add_to_hass(hass)

    with patch.object(integration._LOGGER, "error") as logged:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert logged.call_count == 1
    rendered = logged.call_args[0][0] % logged.call_args[0][1:]
    assert "version 1 source model" in rendered
    assert "add the integration again" in rendered
    assert "cumulative daily house-load counter" in rendered


async def test_a_current_entry_loads_normally(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The guard does not get in the way of a correctly configured entry."""
    assert setup_integration.version == CONFIG_ENTRY_VERSION
    assert setup_integration.state is ConfigEntryState.LOADED
    assert setup_integration.runtime_data is not None


# -- the empty-source guard --------------------------------------------------


async def test_an_entry_without_a_house_load_source_fails_setup(
    hass: HomeAssistant, source_entities: None, config_data: dict
) -> None:
    """A current-version entry missing its learning source also refuses to load.

    Migration only catches the old schema. This catches any other route to an
    entry with no house-load entity -- a hand-edited ``.storage`` file, or a
    future bug in the flow.
    """
    data = dict(config_data)
    data.pop("house_load_entity")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Alpha EMS",
        data=data,
        options={},
        version=CONFIG_ENTRY_VERSION,
    )
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_an_empty_house_load_string_also_fails_setup(
    hass: HomeAssistant, source_entities: None, config_data: dict
) -> None:
    """An empty string is treated exactly like a missing key."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Alpha EMS",
        data={**config_data, "house_load_entity": ""},
        options={},
        version=CONFIG_ENTRY_VERSION,
    )
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_clearing_the_house_load_source_via_options_fails_loudly(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Even an options edit cannot leave a silently non-learning integration."""
    hass.config_entries.async_update_entry(
        setup_integration,
        options={**setup_integration.options, "house_load_entity": ""},
    )
    await hass.async_block_till_done()

    assert setup_integration.state is ConfigEntryState.SETUP_ERROR


async def test_the_setup_error_is_translated(hass: HomeAssistant) -> None:
    """The failure message is a translated string, not an internal key."""
    from homeassistant.helpers.translation import async_get_translations

    for language in ("en", "nl"):
        payload = await async_get_translations(hass, language, "exceptions", {DOMAIN})
        key = f"component.{DOMAIN}.exceptions.house_load_source_missing.message"
        assert key in payload, f"missing in {language}"
        assert payload[key] != key
        assert len(payload[key]) > 30


# -- no duplicates on the healthy path ---------------------------------------


async def test_reload_creates_no_duplicate_entities(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The version bump did not disturb reload behaviour."""
    for _ in range(3):
        await hass.config_entries.async_reload(setup_integration.entry_id)
        await hass.async_block_till_done()

    registry = er.async_get(hass)
    created = [
        item.entity_id for item in registry.entities.values() if item.platform == DOMAIN
    ]
    assert len(created) == 6
    assert len(set(created)) == 6
    assert not [entity_id for entity_id in created if entity_id.endswith("_2")]


async def test_unique_ids_stay_config_entry_scoped(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Unique IDs remain entry-scoped, so a fresh entry cannot collide.

    The old integration used the same ``{entry_id}_{key}`` pattern for
    ``learning_confidence`` and ``learning_days``, but its entry can no longer
    load and a replacement entry gets a new ``entry_id`` -- so the changed unit
    and state class of those two sensors cannot land on an existing statistics
    series.
    """
    registry = er.async_get(hass)
    for item in registry.entities.values():
        if item.platform != DOMAIN:
            continue
        assert item.unique_id.startswith(f"{setup_integration.entry_id}_")
