"""A rejected quarter must say why, once, without burying the log.

Every route to a rejected quarter ends in the same place -- the interval failed
to reach ``MIN_QUARTER_COVERAGE`` -- but the causes are unrelated to each other.
A bare ``rejected_quarters: 1`` could not distinguish a normal restart from a
house-load entity that had been publishing kWh instead of W since the day it was
selected, and learning could stall indefinitely with nothing in the log to
explain it.

Rejections are now attributed to a bounded set of reasons, counted per reason,
and warned about at most once per reason per throttle window. The one reason
that is *expected* -- thin coverage with no source problem behind it -- is logged
at debug, because a message that fires after every restart teaches the user to
ignore the channel.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.coordinator import (
    REJECT_INSUFFICIENT_COVERAGE,
    REJECT_SOURCE_MISSING,
    REJECT_VALUE_IMPLAUSIBLE,
)
from custom_components.alpha_ems_manager.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.alpha_ems_manager.normalization import (
    PROBLEM_STATE_NOT_NUMERIC,
    PROBLEM_STATE_UNAVAILABLE,
    PROBLEM_UNIT_MISSING,
    PROBLEM_UNIT_NOT_POWER,
    describe_power_problem,
)

from .conftest import EV_POWER, HOUSE_LOAD, TZ, set_sensor
from .synthetic import empty_day

# -- the classifier ----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (2000, "W", None),
        (2.0, "kW", None),
        ("2000", "W", None),
        ("unavailable", "W", PROBLEM_STATE_UNAVAILABLE),
        ("unknown", "W", PROBLEM_STATE_UNAVAILABLE),
        ("", "W", PROBLEM_STATE_UNAVAILABLE),
        (None, "W", PROBLEM_STATE_UNAVAILABLE),
        ("idle", "W", PROBLEM_STATE_NOT_NUMERIC),
        ("nan", "W", PROBLEM_STATE_NOT_NUMERIC),
        (float("inf"), "W", PROBLEM_STATE_NOT_NUMERIC),
        (float("nan"), "W", PROBLEM_STATE_NOT_NUMERIC),
        (True, "W", PROBLEM_STATE_NOT_NUMERIC),
        (2000, None, PROBLEM_UNIT_MISSING),
        (2000, "", PROBLEM_UNIT_MISSING),
        (2000, "kWh", PROBLEM_UNIT_NOT_POWER),
        (2000, "%", PROBLEM_UNIT_NOT_POWER),
    ],
)
def test_every_unusable_reading_is_classified(value, unit, expected) -> None:
    """The classifier accepts exactly what ``normalize_power_w`` accepts."""
    from custom_components.alpha_ems_manager.normalization import normalize_power_w

    problem = describe_power_problem(value, unit)
    assert problem == expected
    assert (normalize_power_w(value, unit) is None) == (problem is not None)


# -- attribution through the coordinator -------------------------------------


def close_a_quarter(coordinator, *, covered: bool, hour: int = 10) -> None:
    """Drive the accumulator across one quarter boundary.

    Samples every minute, because a silence longer than
    ``MAX_SAMPLE_GAP_SECONDS`` contributes no coverage at all -- so a helper
    that jumped straight from the start to the end of the quarter would produce
    a rejection whatever the source was doing, and prove nothing.

    ``covered`` decides whether sampling begins at the boundary or ten minutes
    into the quarter, which is the difference between an interval that can be
    accepted and one that cannot.
    """
    from datetime import datetime

    start = datetime(2026, 8, 17, hour, 0, tzinfo=TZ)
    coordinator._accumulator.reset()
    if coordinator._ev_accumulator is not None:
        coordinator._ev_accumulator.reset()
    offset = 0 if covered else 10
    while offset <= 16:
        coordinator._sample(start + timedelta(minutes=offset))
        offset += 1


@pytest.mark.parametrize(
    ("state", "unit", "expected"),
    [
        ("unavailable", "W", PROBLEM_STATE_UNAVAILABLE),
        ("unknown", "W", PROBLEM_STATE_UNAVAILABLE),
        ("broken", "W", PROBLEM_STATE_NOT_NUMERIC),
        ("nan", "W", PROBLEM_STATE_NOT_NUMERIC),
        (2000, None, PROBLEM_UNIT_MISSING),
        (2000, "kWh", PROBLEM_UNIT_NOT_POWER),
        (90_000, "W", REJECT_VALUE_IMPLAUSIBLE),
    ],
)
async def test_a_bad_house_load_reading_names_its_own_fault(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    state,
    unit,
    expected: str,
) -> None:
    """Fails on beta.3, which records only an unattributed count."""
    coordinator = setup_integration.runtime_data
    set_sensor(hass, HOUSE_LOAD, state, unit, "power")
    await hass.async_block_till_done()

    close_a_quarter(coordinator, covered=True)

    assert coordinator.last_rejected_reason == expected
    assert coordinator.rejected_quarters_by_reason[expected] >= 1
    assert coordinator.last_rejected_quarter is not None


async def test_a_missing_house_load_entity_is_named_as_such(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A deleted entity is a different problem from an unavailable one."""
    coordinator = setup_integration.runtime_data
    hass.states.async_remove(HOUSE_LOAD)
    await hass.async_block_till_done()

    close_a_quarter(coordinator, covered=True)

    assert coordinator.last_rejected_reason == REJECT_SOURCE_MISSING


async def test_thin_coverage_with_a_healthy_source_is_reported_as_such(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The expected case after a restart must not masquerade as a fault."""
    coordinator = setup_integration.runtime_data
    set_sensor(hass, HOUSE_LOAD, 2000, "W", "power")
    await hass.async_block_till_done()

    close_a_quarter(coordinator, covered=False)

    assert coordinator.last_rejected_reason == REJECT_INSUFFICIENT_COVERAGE


async def test_learning_resumes_cleanly_once_the_source_recovers(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The counters record history; they must not latch the current state."""
    from datetime import datetime

    coordinator = setup_integration.runtime_data
    set_sensor(hass, HOUSE_LOAD, "unavailable", "W", "power")
    await hass.async_block_till_done()
    close_a_quarter(coordinator, covered=True)
    assert coordinator.last_rejected_reason == PROBLEM_STATE_UNAVAILABLE

    set_sensor(hass, HOUSE_LOAD, 2000, "W", "power")
    await hass.async_block_till_done()
    rejected_before = coordinator.rejected_quarters

    close_a_quarter(coordinator, covered=True, hour=11)

    assert coordinator.rejected_quarters == rejected_before
    stored = coordinator.store.days.get(datetime(2026, 8, 17).date())
    assert stored is not None
    assert stored.measured_valid_count >= 1


# -- the flexible load is attributed separately ------------------------------


async def test_a_bad_flexible_load_reading_is_attributed_without_touching_measured(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    source_entities: None,
) -> None:
    """Measured stays ground truth; only the baseline is invalidated."""
    from datetime import datetime

    from custom_components.alpha_ems_manager.const import CONF_EV_POWER_ENTITY

    from .conftest import TEST_TIMEZONE

    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    set_sensor(hass, EV_POWER, "unavailable", "W", "power")
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, CONF_EV_POWER_ENTITY: EV_POWER},
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = mock_config_entry.runtime_data
    start = datetime(2026, 8, 17, 12, 0, tzinfo=TZ)
    close_a_quarter(coordinator, covered=True, hour=12)

    assert coordinator.invalid_ev_quarters >= 1
    assert coordinator.invalid_ev_quarters_by_reason.get(PROBLEM_STATE_UNAVAILABLE)
    record = coordinator.store.days.get(start.date())
    assert record is not None
    assert record.measured_valid_count >= 1
    assert record.baseline_valid_count == 0


# -- logging must explain without spamming -----------------------------------


async def test_a_source_fault_warns_once_per_throttle_window(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Four rejections an hour must not become four warnings an hour."""

    coordinator = setup_integration.runtime_data
    set_sensor(hass, HOUSE_LOAD, 2000, "kWh", "energy")
    await hass.async_block_till_done()

    with caplog.at_level(logging.WARNING):
        for hour in range(10, 14):
            close_a_quarter(coordinator, covered=True, hour=hour)

    warnings = [
        record for record in caplog.records if "was not learned" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert PROBLEM_UNIT_NOT_POWER in warnings[0].getMessage()
    assert coordinator.rejected_quarters_by_reason[PROBLEM_UNIT_NOT_POWER] == 4


async def test_an_expected_rejection_does_not_warn_at_all(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A partially observed startup quarter is normal, not newsworthy."""
    coordinator = setup_integration.runtime_data
    set_sensor(hass, HOUSE_LOAD, 2000, "W", "power")
    await hass.async_block_till_done()

    with caplog.at_level(logging.WARNING):
        close_a_quarter(coordinator, covered=False)

    assert not [
        record for record in caplog.records if "was not learned" in record.getMessage()
    ]
    assert coordinator.rejected_quarters_by_reason[REJECT_INSUFFICIENT_COVERAGE] == 1


async def test_a_new_kind_of_fault_is_not_silenced_by_an_older_one(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Throttling is per reason, so a second problem still gets to speak."""

    coordinator = setup_integration.runtime_data

    with caplog.at_level(logging.WARNING):
        set_sensor(hass, HOUSE_LOAD, 2000, "kWh", "energy")
        await hass.async_block_till_done()
        close_a_quarter(coordinator, covered=True)

        set_sensor(hass, HOUSE_LOAD, "broken", "W", "power")
        await hass.async_block_till_done()
        close_a_quarter(coordinator, covered=True, hour=13)

    messages = [
        record.getMessage()
        for record in caplog.records
        if "was not learned" in record.getMessage()
    ]
    assert len(messages) == 2
    assert any(PROBLEM_UNIT_NOT_POWER in message for message in messages)
    assert any(PROBLEM_STATE_NOT_NUMERIC in message for message in messages)


# -- diagnostics -------------------------------------------------------------


async def test_diagnostics_carry_the_rejection_breakdown(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The counters must reach the payload a support request actually uses."""
    coordinator = setup_integration.runtime_data
    coordinator.store.days = {}
    set_sensor(hass, HOUSE_LOAD, "unavailable", "W", "power")
    await hass.async_block_till_done()
    close_a_quarter(coordinator, covered=True)

    payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    learning = payload["learning"]

    assert learning["rejected_quarters"] >= 1
    assert learning["rejected_quarters_by_reason"][PROBLEM_STATE_UNAVAILABLE] >= 1
    assert learning["last_rejected_reason"] == PROBLEM_STATE_UNAVAILABLE
    assert learning["last_rejected_quarter"] is not None


def test_the_reason_space_is_bounded() -> None:
    """The counter keys are fixed literals, so the mapping cannot grow."""
    from custom_components.alpha_ems_manager import coordinator as coordinator_module

    reasons = {
        value
        for name, value in vars(coordinator_module).items()
        if name.startswith("REJECT_") and isinstance(value, str)
    }
    problems = {
        value
        for name, value in vars(
            __import__(
                "custom_components.alpha_ems_manager.normalization",
                fromlist=["normalization"],
            )
        ).items()
        if name.startswith("PROBLEM_") and isinstance(value, str)
    }
    assert len(reasons | problems) <= 12
    assert not reasons & problems


def test_an_interval_outside_its_stored_day_is_counted_rather_than_dropped() -> None:
    """The guard in ``record_interval`` must report, not swallow.

    Unreachable under a stable timezone, which is exactly why it must be loud
    when it does fire: reaching it means the stored day's shape and the instant
    being filed already disagree.
    """
    from datetime import date

    record = empty_day(date(2026, 8, 17))

    assert record.record_interval(0, 0.25, None, False) is True
    assert record.record_interval(-1, 0.25, None, False) is False
    assert record.record_interval(record.interval_count, 0.25, None, False) is False
