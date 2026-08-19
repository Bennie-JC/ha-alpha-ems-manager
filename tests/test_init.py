"""Entry lifecycle: setup, unload, reload, and behaviour around the edges.

The recurring risk in a listener-heavy integration is that a reload leaves the
old subscriptions in place. Every timer and listener here is registered through
``entry.async_on_unload``, and these tests hold that to account.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, EVENT_STATE_CHANGED
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.alpha_ems_manager.const import CONFIG_ENTRY_VERSION, DOMAIN

from .conftest import HOUSE_LOAD, TEST_TIMEZONE, set_sensor

TZ = ZoneInfo(TEST_TIMEZONE)
START = datetime(2026, 8, 17, 10, 0, 0, tzinfo=TZ)

SENSORS = (
    "sensor.alpha_ems_expected_house_load_today",
    "sensor.alpha_ems_expected_house_load_tomorrow",
    "sensor.alpha_ems_learning_confidence",
    "sensor.alpha_ems_learning_days",
    "sensor.alpha_ems_forecast_error_yesterday",
    "sensor.alpha_ems_forecast_error_7_days",
)


async def advance(hass: HomeAssistant, freezer, seconds: int, step: int = 60) -> None:
    """Move the frozen clock forward, firing Home Assistant's time triggers."""
    for _ in range(seconds // step):
        freezer.tick(timedelta(seconds=step))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


async def setup_at(
    hass: HomeAssistant, freezer, entry: MockConfigEntry, moment: datetime
) -> None:
    """Set the integration up with the clock frozen at ``moment``."""
    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    freezer.move_to(moment)
    set_sensor(hass, HOUSE_LOAD, 2000, "W", "power")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


# -- basic lifecycle ---------------------------------------------------------


async def test_setup_loads_and_creates_the_sensors(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A configured entry loads and publishes its sensors."""
    assert setup_integration.state is ConfigEntryState.LOADED
    for entity_id in SENSORS:
        assert hass.states.get(entity_id) is not None


async def test_unload_removes_the_entities(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Unloading tears the platform down cleanly."""
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()

    assert setup_integration.state is ConfigEntryState.NOT_LOADED
    for entity_id in SENSORS:
        state = hass.states.get(entity_id)
        assert state is None or state.state == "unavailable"


async def test_reload_keeps_exactly_the_documented_entities(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Reloading repeatedly never produces ``_2`` duplicates."""
    for _ in range(3):
        await hass.config_entries.async_reload(setup_integration.entry_id)
        await hass.async_block_till_done()

    registry = er.async_get(hass)
    ours = [
        entity.entity_id
        for entity in registry.entities.values()
        if entity.platform == DOMAIN
    ]
    assert sorted(ours) == sorted(SENSORS)


async def test_reload_does_not_accumulate_listeners(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """State-change subscriptions are torn down on every reload.

    A leaked listener would double-count every sample, quietly inflating the
    learned household load.
    """
    baseline = hass.bus.async_listeners().get(EVENT_STATE_CHANGED, 0)

    for _ in range(3):
        await hass.config_entries.async_reload(setup_integration.entry_id)
        await hass.async_block_till_done()

    assert hass.bus.async_listeners().get(EVENT_STATE_CHANGED, 0) == baseline


async def test_reload_does_not_double_count_measurements(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """After several reloads a 2 kW quarter is still 0.5 kWh, not a multiple."""
    await setup_at(hass, freezer, mock_config_entry, START)

    for _ in range(3):
        await hass.config_entries.async_reload(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    await advance(hass, freezer, 960)

    record = mock_config_entry.runtime_data.store.days[START.date()]
    assert record.measured[40] == pytest.approx(0.5, rel=1e-3)
    assert record.measured_valid_count == 1


# -- unavailable sources -----------------------------------------------------


async def test_an_unavailable_source_does_not_break_setup(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """The entry still loads when the house-load sensor is unavailable."""
    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    freezer.move_to(START)
    set_sensor(hass, HOUSE_LOAD, "unavailable", "W", "power")

    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    for entity_id in SENSORS:
        assert hass.states.get(entity_id) is not None


async def test_an_unavailable_source_learns_nothing_rather_than_zero(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """A quarter spent unavailable is rejected, not stored as no consumption."""
    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    freezer.move_to(START)
    set_sensor(hass, HOUSE_LOAD, "unavailable", "W", "power")

    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    await advance(hass, freezer, 960)

    coordinator = mock_config_entry.runtime_data
    record = coordinator.store.days.get(START.date())
    assert record is None or record.measured_valid_count == 0
    assert coordinator.rejected_quarters >= 1


async def test_a_missing_source_entity_is_survived(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """A deleted source entity degrades gracefully instead of raising."""
    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    freezer.move_to(START)
    # Deliberately do not create the house-load entity at all.

    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    await advance(hass, freezer, 300)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(SENSORS[0]) is not None


async def test_recovery_after_an_outage_resumes_learning(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """Once the source returns, the next full quarter is learned normally."""
    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    freezer.move_to(START)
    set_sensor(hass, HOUSE_LOAD, "unavailable", "W", "power")

    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    # The 10:00 quarter is lost, then the sensor comes back one minute into the
    # 10:15 quarter.
    await advance(hass, freezer, 960)
    set_sensor(hass, HOUSE_LOAD, 2000, "W", "power")
    await hass.async_block_till_done()
    await advance(hass, freezer, 1800)

    record = mock_config_entry.runtime_data.store.days[START.date()]

    # The lost quarter stays a gap rather than becoming a zero.
    assert record.measured[40] is None

    # 10:15 recovered partway through: 840 of its 900 seconds were measured, so
    # it is accepted but genuinely holds less than a full quarter of energy.
    assert record.measured[41] == pytest.approx(2000 * 840 / 3600 / 1000, rel=1e-2)
    assert record.measured[41] < 0.5

    # 10:30 is the first fully clean quarter after recovery.
    assert record.measured[42] == pytest.approx(0.5, rel=1e-3)


# -- boundaries --------------------------------------------------------------


async def test_learning_continues_across_midnight(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """Quarters either side of midnight land on their own calendar days."""
    before_midnight = datetime(2026, 8, 17, 23, 45, 0, tzinfo=TZ)
    await setup_at(hass, freezer, mock_config_entry, before_midnight)

    await advance(hass, freezer, 1860)  # 31 minutes, spanning midnight

    store = mock_config_entry.runtime_data.store
    assert store.days[before_midnight.date()].measured[95] == pytest.approx(
        0.5, rel=1e-2
    )
    assert store.days[(before_midnight + timedelta(hours=1)).date()].measured[
        0
    ] == pytest.approx(0.5, rel=1e-2)


async def test_a_stop_event_flushes_learning_to_disk(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """Shutting down persists whatever has been learned."""
    await setup_at(hass, freezer, mock_config_entry, START)
    await advance(hass, freezer, 960)

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    from custom_components.alpha_ems_manager.storage import LearningStore

    reloaded = LearningStore(hass, mock_config_entry.entry_id)
    await reloaded.async_load(TEST_TIMEZONE)
    assert reloaded.days[START.date()].measured[40] == pytest.approx(0.5, rel=1e-3)


# -- multiple instances ------------------------------------------------------


async def test_two_instances_coexist_without_sharing_state(
    hass: HomeAssistant, freezer, config_data: dict
) -> None:
    """Two entries keep separate coordinators, stores and entities."""
    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    freezer.move_to(START)
    set_sensor(hass, HOUSE_LOAD, 2000, "W", "power")

    second_load = "sensor.cottage_house_load"
    set_sensor(hass, second_load, 500, "W", "power")

    first = MockConfigEntry(
        domain=DOMAIN,
        title="Alpha EMS",
        data=config_data,
        version=CONFIG_ENTRY_VERSION,
    )
    second = MockConfigEntry(
        domain=DOMAIN,
        title="Alpha EMS Cottage",
        data={**config_data, "house_load_entity": second_load},
        version=CONFIG_ENTRY_VERSION,
    )
    for entry in (first, second):
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await advance(hass, freezer, 960)

    assert first.runtime_data is not second.runtime_data
    assert first.runtime_data.store is not second.runtime_data.store
    assert first.runtime_data.history is not second.runtime_data.history
    assert first.runtime_data.store.days[START.date()].measured[40] == pytest.approx(
        0.5, rel=1e-2
    )
    assert second.runtime_data.store.days[START.date()].measured[40] == pytest.approx(
        0.125, rel=1e-2
    )

    registry = er.async_get(hass)
    ours = [
        entity.entity_id
        for entity in registry.entities.values()
        if entity.platform == DOMAIN
    ]
    # Two entries, six entities each, no id shared between them.
    assert len(ours) == 12
    assert len(set(ours)) == 12
