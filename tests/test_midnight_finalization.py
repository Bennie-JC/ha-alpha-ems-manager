"""The live midnight rollover, locked in.

``tests/test_day_rollover.py`` covers what the *model* does when the in-progress
day becomes history. This file covers the mechanics underneath it: which civil
date a quarter is filed under either side of midnight, that the boundary can be
crossed more than once without double-counting, and that no path leaves a
prior-day forecast on display after the day has turned.

The first real rollover on the reference installation took the integration from
one learned day and no forecast to two learned days and a published one. These
tests are what stops that transition regressing.
"""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.quarter import QuarterAccumulator
from custom_components.alpha_ems_manager.storage import (
    expected_quarters_for,
    index_for_start_utc,
)

from .conftest import HOUSE_LOAD, TZ, set_sensor
from .synthetic import flat_day

NORMAL = date(2026, 8, 18)
SPRING_FORWARD = date(2026, 3, 29)
FALL_BACK = date(2026, 10, 25)


def local(day: date, hour: int, minute: int = 0, second: int = 0, fold: int = 0):
    """Return a local instant on ``day``."""
    return datetime(
        day.year, day.month, day.day, hour, minute, second, tzinfo=TZ, fold=fold
    )


def run_across(start: datetime, minutes: int, watts: float = 2000.0):
    """Integrate a steady load minute by minute and return the closed quarters.

    Sampling every minute keeps every gap inside ``MAX_SAMPLE_GAP_SECONDS``, so
    the quarters that close are fully covered and genuinely acceptable.
    """
    accumulator = QuarterAccumulator(TZ)
    results = []
    for offset in range(minutes + 1):
        results.extend(accumulator.add_sample(start + timedelta(minutes=offset), watts))
    return results


# -- which day does a quarter belong to --------------------------------------


@pytest.mark.parametrize(
    ("hour", "minute", "expected_day", "expected_slot"),
    [
        (23, 30, NORMAL, 94),
        (23, 45, NORMAL, 95),
        (0, 0, NORMAL + timedelta(days=1), 0),
        (0, 15, NORMAL + timedelta(days=1), 1),
    ],
)
def test_a_quarter_is_filed_under_the_day_it_began_in(
    hour: int, minute: int, expected_day: date, expected_slot: int
) -> None:
    """23:45 belongs to the old day; 00:00 begins the new one."""
    start = local(NORMAL if hour == 23 else NORMAL + timedelta(days=1), hour, minute)
    closed = run_across(start, 16)

    assert closed[0].day == expected_day
    assert closed[0].slot == expected_slot
    assert closed[0].accepted


def test_the_last_quarter_of_a_day_and_the_first_of_the_next_are_adjacent() -> None:
    """No interval may be skipped across the boundary, and none repeated."""
    closed = run_across(local(NORMAL, 23, 30), 45)

    assert [result.slot for result in closed] == [94, 95, 0]
    assert [result.day for result in closed] == [
        NORMAL,
        NORMAL,
        NORMAL + timedelta(days=1),
    ]
    starts = [result.start_utc for result in closed]
    assert starts == sorted(starts)
    assert all(
        later - earlier == timedelta(minutes=15)
        for earlier, later in itertools.pairwise(starts)
    )


def test_energy_is_neither_lost_nor_duplicated_across_midnight() -> None:
    """Three quarters of a steady 2 kW load is 1.5 kWh, wherever it falls."""
    closed = run_across(local(NORMAL, 23, 30), 45)

    assert sum(result.energy_kwh for result in closed) == pytest.approx(1.5, abs=1e-6)
    assert all(result.coverage == pytest.approx(1.0) for result in closed)


@pytest.mark.parametrize("second", [0, 5, 59])
def test_the_boundary_trigger_may_fire_at_any_second_without_double_counting(
    second: int,
) -> None:
    """The quarter tick runs at :05; a late or early one changes nothing.

    Extra samples inside a quarter cannot close it twice, because the
    accumulator only ever integrates forward from its cursor.
    """
    accumulator = QuarterAccumulator(TZ)
    start = local(NORMAL, 23, 45)
    closed = []
    for offset in range(16):
        closed.extend(accumulator.add_sample(start + timedelta(minutes=offset), 2000.0))
        # A duplicate sample at the same instant, as a state change landing on
        # the boundary alongside the timer would produce.
        closed.extend(accumulator.add_sample(start + timedelta(minutes=offset), 2000.0))
    closed.extend(
        accumulator.add_sample(start + timedelta(minutes=15, seconds=second), 2000.0)
    )

    assert len([result for result in closed if result.slot == 95]) == 1
    assert sum(result.energy_kwh for result in closed) == pytest.approx(0.5, abs=1e-6)


def test_an_out_of_order_sample_cannot_reopen_a_closed_quarter() -> None:
    """A late update refreshes the held value and nothing else."""
    accumulator = QuarterAccumulator(TZ)
    start = local(NORMAL, 23, 45)
    closed = run_across(start, 16)
    for offset in range(16):
        accumulator.add_sample(start + timedelta(minutes=offset), 2000.0)

    reopened = accumulator.add_sample(start + timedelta(minutes=2), 9000.0)

    assert reopened == []
    assert len(closed) == 1


# -- daylight saving ---------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "length"),
    [(SPRING_FORWARD, 92), (NORMAL, 96), (FALL_BACK, 100)],
)
def test_each_civil_day_finalises_its_real_number_of_intervals(
    day: date, length: int
) -> None:
    """The last chronological index of a day is its length minus one."""
    assert expected_quarters_for(day, TZ) == length
    last_start = datetime(day.year, day.month, day.day, tzinfo=TZ).astimezone(
        UTC
    ) + timedelta(minutes=15 * (length - 1))
    assert index_for_start_utc(day, last_start, TZ) == length - 1
    # And the next interval belongs to the following day, at index 0.
    assert (
        index_for_start_utc(
            day + timedelta(days=1), last_start + timedelta(minutes=15), TZ
        )
        == 0
    )


def test_the_spring_forward_gap_is_skipped_rather_than_measured() -> None:
    """02:00-03:00 does not exist, so no quarter may be filed inside it."""
    closed = run_across(local(SPRING_FORWARD, 1, 30), 60)
    slots = [result.slot for result in closed]

    # Slots 8-11 are the hour the wall clock skips. They are never observed,
    # which is the whole reason a spring-forward day is 92 intervals and not 96.
    assert not {8, 9, 10, 11} & set(slots)
    # The clock steps straight from 01:59 to 03:00, so 01:45 and 03:00 are
    # adjacent in time while being four apart in wall-clock slot index.
    assert slots == [6, 7, 12, 13]
    starts = [result.start_utc for result in closed]
    assert all(
        later - earlier == timedelta(minutes=15)
        for earlier, later in itertools.pairwise(starts)
    )


def test_the_fall_back_hour_produces_two_distinct_intervals_for_one_slot() -> None:
    """Both occurrences are kept; neither overwrites the other."""
    closed = run_across(local(FALL_BACK, 1, 45), 150)

    repeated = [result for result in closed if result.slot in (8, 9, 10, 11)]
    assert len(repeated) == 8
    indices = [index_for_start_utc(FALL_BACK, r.start_utc, TZ) for r in repeated]
    assert len(set(indices)) == 8


# -- learned days increment exactly once -------------------------------------


async def test_the_learned_day_count_advances_once_when_the_day_turns(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A complete day becomes learned the moment it can no longer gain data.

    The live transition: one learned day before midnight, two after.
    """
    coordinator = setup_integration.runtime_data
    coordinator.store.days = {
        day: flat_day(day, 12.0) for day in (NORMAL - timedelta(days=1), NORMAL)
    }

    during = coordinator.store.learned_days(before=NORMAL)
    after = coordinator.store.learned_days(before=NORMAL + timedelta(days=1))

    assert len(during) == 1
    assert len(after) == 2
    # Idempotent: asking again cannot advance it further.
    assert len(coordinator.store.learned_days(before=NORMAL + timedelta(days=1))) == 2


async def test_the_running_day_is_never_counted_as_learned(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """However complete it looks, today is still gaining intervals."""
    coordinator = setup_integration.runtime_data
    coordinator.store.days = {NORMAL: flat_day(NORMAL, 12.0)}

    assert coordinator.store.days[NORMAL].is_learned is True
    assert coordinator.store.learned_days(before=NORMAL) == []


# -- no stale forecast survives the rollover ---------------------------------


async def test_a_refresh_rebuilds_both_forecasts_for_the_new_dates(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """After midnight, "today" must mean the new day and nothing else."""
    from unittest.mock import patch

    coordinator = setup_integration.runtime_data
    coordinator.store.days = {
        NORMAL - timedelta(days=offset): flat_day(NORMAL - timedelta(days=offset), 12.0)
        for offset in range(1, 6)
    }

    with patch(
        "custom_components.alpha_ems_manager.coordinator.dt_util.now",
        return_value=local(NORMAL, 23, 50),
    ):
        await coordinator.async_refresh()
    before_day = coordinator.data["today_baseline"].day
    before_tomorrow = coordinator.data["tomorrow"].day

    with patch(
        "custom_components.alpha_ems_manager.coordinator.dt_util.now",
        return_value=local(NORMAL + timedelta(days=1), 0, 5),
    ):
        await coordinator.async_refresh()

    assert before_day == NORMAL
    assert before_tomorrow == NORMAL + timedelta(days=1)
    assert coordinator.data["today_baseline"].day == NORMAL + timedelta(days=1)
    assert coordinator.data["tomorrow"].day == NORMAL + timedelta(days=2)


async def test_elapsed_intervals_reset_to_zero_on_the_new_day(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Yesterday's elapsed count must not carry into today's adaptation."""
    from unittest.mock import patch

    coordinator = setup_integration.runtime_data
    coordinator.store.days = {
        NORMAL - timedelta(days=offset): flat_day(NORMAL - timedelta(days=offset), 12.0)
        for offset in range(1, 6)
    }

    with patch(
        "custom_components.alpha_ems_manager.coordinator.dt_util.now",
        return_value=local(NORMAL, 23, 50),
    ):
        await coordinator.async_refresh()
        assert coordinator.data["elapsed_intervals"] == 95

    with patch(
        "custom_components.alpha_ems_manager.coordinator.dt_util.now",
        return_value=local(NORMAL + timedelta(days=1), 0, 5),
    ):
        await coordinator.async_refresh()
        assert coordinator.data["elapsed_intervals"] == 0
        assert coordinator.data["measured_so_far_kwh"] == 0.0


async def test_a_restart_just_after_midnight_reproduces_the_same_forecast(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """00:05 with a restart must equal 00:05 without one.

    The forecast is a pure function of stored history and the date, and the
    in-flight quarter that a restart discards is not an input to it.
    """
    from unittest.mock import patch

    coordinator = setup_integration.runtime_data
    history = {
        NORMAL - timedelta(days=offset): flat_day(NORMAL - timedelta(days=offset), 12.0)
        for offset in range(1, 8)
    }
    coordinator.store.days = dict(history)

    with patch(
        "custom_components.alpha_ems_manager.coordinator.dt_util.now",
        return_value=local(NORMAL, 0, 5),
    ):
        await coordinator.async_refresh()
        uninterrupted = coordinator.data["today_baseline"].intervals

        # A restart: accumulator state is discarded, storage is reloaded intact.
        coordinator._accumulator.reset()
        coordinator.store.days = dict(history)
        await coordinator.async_refresh()
        after_restart = coordinator.data["today_baseline"].intervals

    assert uninterrupted == after_restart


async def test_a_failed_refresh_makes_the_sensors_unavailable_rather_than_stale(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A wrong-day number is worse than no number."""
    from unittest.mock import patch

    coordinator = setup_integration.runtime_data
    set_sensor(hass, HOUSE_LOAD, 2000, "W", "power")

    with patch.object(
        coordinator, "_async_update_data", side_effect=RuntimeError("boom")
    ):
        await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    for key in ("today", "tomorrow"):
        state = hass.states.get(f"sensor.alpha_ems_expected_house_load_{key}")
        assert state is not None
        assert state.state == "unavailable"
