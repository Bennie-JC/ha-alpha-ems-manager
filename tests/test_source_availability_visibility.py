"""A dead balance source must not fail silently.

The energy-balance check never affects learning, which is exactly why a source
it depends on can die unnoticed: house load logs its own problems because it is
on the learning path, but the battery, PV and grid entities went through
``_read_power``, which logs nothing at all. A dead battery therefore produced
``unavailable_samples: 500``, no log line, and no indication of *which* of four
sources was missing.

Also covers the Learning Days attribute that could only ever read ~0.0.
"""

from __future__ import annotations

import logging

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import BATTERY_POWER, GRID_POWER, PV_POWER, set_sensor


async def test_an_unreadable_balance_source_is_named_in_diagnostics(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """``unavailable_samples`` alone did not say which source was missing."""
    coordinator = setup_integration.runtime_data
    set_sensor(hass, BATTERY_POWER, "unavailable", "W", "power")
    await hass.async_block_till_done()

    coordinator._sample_balance()
    coordinator._sample_balance()

    payload = coordinator.balance.as_dict()
    assert payload["unavailable_samples"] == 2
    assert payload["unavailable_source_counts"] == {BATTERY_POWER: 2}


@pytest.mark.parametrize("entity_id", [BATTERY_POWER, GRID_POWER, PV_POWER])
async def test_every_non_learning_source_is_attributed(
    hass: HomeAssistant, setup_integration: MockConfigEntry, entity_id: str
) -> None:
    """None of the three logs anything of its own, so all three need this."""
    coordinator = setup_integration.runtime_data
    set_sensor(hass, entity_id, "unavailable", "W", "power")
    await hass.async_block_till_done()

    coordinator._sample_balance()

    assert coordinator.balance.unavailable_source_counts == {entity_id: 1}


async def test_a_dead_balance_source_warns_once_rather_than_never(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sampled every minute, so it must speak once and then stay quiet."""
    coordinator = setup_integration.runtime_data
    set_sensor(hass, GRID_POWER, "unavailable", "W", "power")
    await hass.async_block_till_done()

    with caplog.at_level(logging.WARNING):
        for _ in range(30):
            coordinator._sample_balance()

    warnings = [
        record for record in caplog.records if "cannot be read" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert GRID_POWER in warnings[0].getMessage()
    # It must also say that learning is unaffected, because it is.
    assert "Learning is unaffected" in warnings[0].getMessage()


async def test_no_warning_while_every_source_reads(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The warning must be reachable only by the failure it describes."""
    coordinator = setup_integration.runtime_data

    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            coordinator._sample_balance()

    assert not [
        record for record in caplog.records if "cannot be read" in record.getMessage()
    ]
    assert coordinator.balance.unavailable_source_counts == {}


async def test_learning_days_does_not_publish_a_permanently_zero_coverage(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Attributes are captured when the coordinator writes state.

    It writes at the quarter tick plus five seconds, so the open quarter is five
    seconds old every time this is evaluated and its coverage is always about
    0.0 -- a true number that reads as a fault. Diagnostics keep the figure,
    where the payload is built on demand and it means something.
    """
    from custom_components.alpha_ems_manager.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    state = hass.states.get("sensor.alpha_ems_learning_days")
    assert state is not None
    assert "open_quarter_coverage" not in state.attributes

    payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    assert "open_quarter_coverage" in payload["learning"]
