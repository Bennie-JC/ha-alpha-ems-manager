"""Daylight-saving correctness across measurement, storage and forecasting.

An earlier design stored a fixed 96-entry list keyed by wall-clock slot. It
looked right on 363 days a year: the fall-back day still reported 100 accepted
quarters and the correct day total, but the repeated 02:00-02:59 hour overwrote
itself in the profile, so an hour of energy vanished from the learned shape and
the day total no longer matched the sum of the stored intervals.

Every test here would have failed against that design. They exist to keep the
chronological interval model honest.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.coordinator import AlphaEmsCoordinator
from custom_components.alpha_ems_manager.forecast import build_forecast
from custom_components.alpha_ems_manager.quarter import QuarterAccumulator
from custom_components.alpha_ems_manager.storage import (
    DayRecord,
    LearningStore,
    expected_quarters_for,
    index_for_start_utc,
    local_slot_for_index,
    utc_midnight,
)

from .synthetic import TZ, history

TZ_KEY = "Europe/Amsterdam"

NORMAL = date(2026, 8, 17)
SPRING_FORWARD = date(2026, 3, 29)  # 23 hours -> 92 intervals
FALL_BACK = date(2026, 10, 25)  # 25 hours -> 100 intervals

DAYS = [(NORMAL, 96), (SPRING_FORWARD, 92), (FALL_BACK, 100)]
DAY_IDS = ["normal", "spring-forward", "fall-back"]

WATTS = 1000.0
KWH_PER_INTERVAL = 0.25


def measure_whole_day(day: date) -> DayRecord:
    """Drive a full civil day at constant power through the real accumulator.

    Samples step through absolute UTC, which is what Home Assistant actually
    hands the coordinator. Stepping a local datetime by ``timedelta`` instead
    would skip or repeat an hour and quietly fake the result.
    """
    accumulator = QuarterAccumulator(TZ)
    record = DayRecord(
        day=day, tz_key=TZ_KEY, interval_count=expected_quarters_for(day, TZ)
    )

    start = utc_midnight(day, TZ)
    end = utc_midnight(day + timedelta(days=1), TZ)

    moment = start
    while moment <= end:
        for result in accumulator.add_sample(moment, WATTS):
            if result.accepted and result.day == day:
                record.record_interval(
                    index_for_start_utc(day, result.start_utc, TZ),
                    measured_kwh=result.energy_kwh,
                    ev_kwh=None,
                    ev_expected=False,
                )
        moment += timedelta(seconds=60)
    return record


# -- interval counts ---------------------------------------------------------


@pytest.mark.parametrize(("day", "expected"), DAYS, ids=DAY_IDS)
def test_a_day_is_sized_to_its_real_length(day: date, expected: int) -> None:
    """92, 96 or 100 intervals, taken from real timezone arithmetic."""
    assert expected_quarters_for(day, TZ) == expected
    assert (
        DayRecord(
            day=day, tz_key=TZ_KEY, interval_count=expected_quarters_for(day, TZ)
        ).interval_count
        == expected
    )


@pytest.mark.parametrize(("day", "expected"), DAYS, ids=DAY_IDS)
def test_every_real_interval_survives_measurement(day: date, expected: int) -> None:
    """All of the day's real quarters are measured and stored distinctly."""
    record = measure_whole_day(day)

    assert record.interval_count == expected
    assert record.measured_valid_count == expected
    assert record.completeness == pytest.approx(1.0)
    assert record.is_learned


@pytest.mark.parametrize(("day", "expected"), DAYS, ids=DAY_IDS)
def test_the_day_total_equals_the_sum_of_retained_intervals(
    day: date, expected: int
) -> None:
    """Total and stored intervals agree -- the invariant the old model broke.

    Under the wall-clock-slot design a fall-back day reported 25.0 kWh while the
    stored profile summed to 24.0 kWh, because four intervals had been
    overwritten. Nothing flagged the discrepancy.
    """
    record = measure_whole_day(day)

    stored_sum = sum(value for value in record.measured if value is not None)
    assert record.measured_total_kwh == pytest.approx(expected * KWH_PER_INTERVAL)
    assert stored_sum == pytest.approx(record.measured_total_kwh)


def test_the_fall_back_day_really_holds_a_hundred_intervals() -> None:
    """The 25-hour day keeps all 100, not 96."""
    record = measure_whole_day(FALL_BACK)

    assert record.interval_count == 100
    assert len(record.measured) == 100
    assert record.measured_valid_count == 100
    assert record.measured_total_kwh == pytest.approx(25.0)


def test_the_repeated_hour_is_stored_twice_and_not_overwritten() -> None:
    """Both passes through 02:00-02:59 survive as distinct intervals."""
    record = measure_whole_day(FALL_BACK)

    slots = [record.local_slot(index) for index in range(record.interval_count)]
    for slot in (8, 9, 10, 11):  # 02:00, 02:15, 02:30, 02:45
        indices = [i for i, value in enumerate(slots) if value == slot]
        assert len(indices) == 2, f"slot {slot} should occur twice on a fall-back day"
        # Both carry real energy; neither was clobbered by the other.
        for index in indices:
            assert record.measured[index] == pytest.approx(KWH_PER_INTERVAL)


def test_the_repeated_hour_maps_to_distinct_instants() -> None:
    """The two occurrences differ in absolute time even though the clock repeats."""
    record = measure_whole_day(FALL_BACK)
    slots = [record.local_slot(index) for index in range(record.interval_count)]
    first, second = [i for i, value in enumerate(slots) if value == 8]

    base = utc_midnight(FALL_BACK, TZ)
    delta = (base + second * timedelta(minutes=15)) - (
        base + first * timedelta(minutes=15)
    )
    assert delta == timedelta(hours=1)


def test_the_spring_forward_hour_is_absent_not_zero() -> None:
    """The hour that never happened is simply not represented.

    Storing it as zero would teach the model that the household consumes nothing
    between 02:00 and 03:00.
    """
    record = measure_whole_day(SPRING_FORWARD)

    assert record.interval_count == 92
    slots = {record.local_slot(index) for index in range(record.interval_count)}
    # 02:00-02:59 does not exist on this day.
    assert slots.isdisjoint({8, 9, 10, 11})
    # And nothing was invented to fill the gap.
    assert all(value == pytest.approx(KWH_PER_INTERVAL) for value in record.measured)


# -- persistence round trip --------------------------------------------------


@pytest.mark.parametrize(("day", "expected"), DAYS, ids=DAY_IDS)
async def test_a_day_round_trips_through_storage(
    hass: HomeAssistant, day: date, expected: int
) -> None:
    """Writing and reloading preserves every interval and the day total."""
    store = LearningStore(hass, f"entry-{day.isoformat()}")
    store.days[day] = measure_whole_day(day)
    await store.async_save_now()

    reloaded = LearningStore(hass, f"entry-{day.isoformat()}")
    await reloaded.async_load(TZ_KEY)
    restored = reloaded.days[day]

    assert restored.interval_count == expected
    assert restored.measured_valid_count == expected
    assert restored.measured_total_kwh == pytest.approx(expected * KWH_PER_INTERVAL)
    assert restored.tz_key == TZ_KEY


async def test_the_fold_distinction_survives_a_round_trip(
    hass: HomeAssistant,
) -> None:
    """After reload, the repeated hour is still two separate intervals."""
    store = LearningStore(hass, "entry-fold")
    original = measure_whole_day(FALL_BACK)
    # Make the two passes distinguishable by value, not just by position.
    slots = [original.local_slot(i) for i in range(original.interval_count)]
    first, second = [i for i, value in enumerate(slots) if value == 8]
    original.record_interval(first, measured_kwh=0.11, ev_kwh=None, ev_expected=False)
    original.record_interval(second, measured_kwh=0.99, ev_kwh=None, ev_expected=False)
    store.days[FALL_BACK] = original
    await store.async_save_now()

    reloaded = LearningStore(hass, "entry-fold")
    await reloaded.async_load(TZ_KEY)
    restored = reloaded.days[FALL_BACK]

    assert restored.measured[first] == pytest.approx(0.11)
    assert restored.measured[second] == pytest.approx(0.99)
    assert restored.local_slot(first) == restored.local_slot(second) == 8


async def test_a_v1_store_is_discarded_rather_than_misread(
    hass: HomeAssistant,
) -> None:
    """The old 96-slot document is rejected with a warning, not reinterpreted.

    Reading a v1 wall-clock array as chronological intervals would silently
    shift every DST day's history.
    """
    from unittest.mock import patch

    legacy = {
        "version": 1,
        "minor_version": 1,
        "key": "alpha_ems_manager.entry-legacy.learning",
        "data": {
            "days": {NORMAL.isoformat(): {"kwh": [0.25] * 96, "tot": 24.0}},
            "balance": {"ok": 5, "total": 5},
        },
    }
    store = LearningStore(hass, "entry-legacy")
    with patch(
        "homeassistant.helpers.storage.Store._async_load_data",
        return_value=legacy,
    ):
        await store.async_load(TZ_KEY)

    # Nothing from the v1 document is carried over, in either direction: no
    # days, and no balance tally that would imply the history was understood.
    assert store.days == {}
    assert store.balance.total_samples == 0


# -- forecasting -------------------------------------------------------------


@pytest.mark.parametrize(("day", "expected"), DAYS, ids=DAY_IDS)
def test_a_forecast_matches_the_target_day_length(day: date, expected: int) -> None:
    """The forecast is as long as the day it forecasts.

    A fixed 96 would over-predict a spring-forward day by an hour and
    under-predict a fall-back day by an hour.
    """
    reference = day - timedelta(days=1)
    forecast = build_forecast(history(reference, 60, 9.6), reference, day, TZ)

    assert forecast.interval_count == expected
    assert len(forecast.intervals) == expected
    assert forecast.available


def test_the_fall_back_forecast_includes_the_repeated_hour() -> None:
    """A 25-hour day is forecast with 25 hours of energy."""
    reference = FALL_BACK - timedelta(days=1)
    normal_ref = NORMAL - timedelta(days=1)

    fall = build_forecast(history(reference, 60, 9.6), reference, FALL_BACK, TZ)
    normal = build_forecast(history(normal_ref, 60, 9.6), normal_ref, NORMAL, TZ)

    assert fall.total_kwh is not None and normal.total_kwh is not None
    # Four extra intervals of roughly the average interval energy.
    assert fall.total_kwh > normal.total_kwh
    assert fall.total_kwh / normal.total_kwh == pytest.approx(100 / 96, rel=0.02)


def test_the_spring_forward_forecast_omits_the_missing_hour() -> None:
    """A 23-hour day is forecast with 23 hours of energy."""
    reference = SPRING_FORWARD - timedelta(days=1)
    normal_ref = NORMAL - timedelta(days=1)

    spring = build_forecast(history(reference, 60, 9.6), reference, SPRING_FORWARD, TZ)
    normal = build_forecast(history(normal_ref, 60, 9.6), normal_ref, NORMAL, TZ)

    assert spring.total_kwh is not None and normal.total_kwh is not None
    assert spring.total_kwh < normal.total_kwh
    assert spring.total_kwh / normal.total_kwh == pytest.approx(92 / 96, rel=0.02)


def test_both_passes_of_the_repeated_hour_feed_the_statistics() -> None:
    """A fall-back day contributes two observations to its repeated slots.

    The old model kept only the later one.
    """
    record = measure_whole_day(FALL_BACK)
    record.record_interval(8, measured_kwh=0.10, ev_kwh=None, ev_expected=False)
    record.record_interval(12, measured_kwh=0.50, ev_kwh=None, ev_expected=False)

    reference = FALL_BACK + timedelta(days=1)
    forecast = build_forecast([record, record], reference, reference, TZ)

    # Slot 8 blends both passes (0.10 and 0.50) rather than showing either alone.
    slot_8_indices = [
        index
        for index in range(forecast.interval_count)
        if local_slot_for_index(reference, index, TZ) == 8
    ]
    value = forecast.intervals[slot_8_indices[0]]
    assert value is not None
    assert 0.10 < value < 0.50


# -- elapsed / remaining through the fold ------------------------------------


def test_elapsed_intervals_never_move_backwards_through_the_fold() -> None:
    """The chronological index advances monotonically across the repeated hour.

    ``hour * 4 + minute // 15`` regressed here -- 10 then 8 -- which made the
    remaining-energy forecast re-count an hour that had already been consumed.
    """
    start = utc_midnight(FALL_BACK, TZ) + timedelta(hours=1)
    previous = -1
    seen: list[int] = []
    for step in range(16):  # four hours across the transition
        moment = (start + step * timedelta(minutes=15)).astimezone(TZ)
        elapsed = AlphaEmsCoordinator._elapsed_intervals(moment, FALL_BACK, TZ)
        assert elapsed > previous, f"elapsed went backwards at local {moment:%H:%M}"
        previous = elapsed
        seen.append(elapsed)

    assert seen == sorted(seen)
    assert len(set(seen)) == len(seen)


def test_remaining_energy_only_shrinks_through_the_fold() -> None:
    """Remaining forecast energy is monotonically non-increasing over the day."""
    reference = FALL_BACK - timedelta(days=1)
    forecast = build_forecast(history(reference, 60, 9.6), reference, FALL_BACK, TZ)

    start = utc_midnight(FALL_BACK, TZ)
    previous = float("inf")
    for step in range(0, 101, 4):
        moment = (start + step * timedelta(minutes=15)).astimezone(TZ)
        elapsed = AlphaEmsCoordinator._elapsed_intervals(moment, FALL_BACK, TZ)
        remaining = forecast.remaining_kwh(elapsed)
        assert remaining <= previous + 1e-9, (
            f"remaining energy increased at local {moment:%H:%M} (interval {elapsed})"
        )
        previous = remaining

    # By the end of the civil day there is nothing left to forecast.
    assert forecast.remaining_kwh(forecast.interval_count) == pytest.approx(0.0)


def test_elapsed_intervals_skip_the_missing_spring_hour() -> None:
    """On a 23-hour day the count reaches only 92 by midnight."""
    end = utc_midnight(SPRING_FORWARD + timedelta(days=1), TZ) - timedelta(minutes=1)
    elapsed = AlphaEmsCoordinator._elapsed_intervals(
        end.astimezone(TZ), SPRING_FORWARD, TZ
    )
    assert elapsed == 91  # the last of 92 intervals, zero-based


def test_elapsed_intervals_reach_a_hundred_on_the_fall_back_day() -> None:
    """On a 25-hour day the count reaches 100."""
    end = utc_midnight(FALL_BACK + timedelta(days=1), TZ) - timedelta(minutes=1)
    elapsed = AlphaEmsCoordinator._elapsed_intervals(end.astimezone(TZ), FALL_BACK, TZ)
    assert elapsed == 99


def test_utc_midnight_is_a_real_instant() -> None:
    """Local midnight resolves to the correct absolute instant on both days."""
    assert utc_midnight(SPRING_FORWARD, TZ) == datetime(2026, 3, 28, 23, 0, tzinfo=UTC)
    assert utc_midnight(FALL_BACK, TZ) == datetime(2026, 10, 24, 22, 0, tzinfo=UTC)


def test_the_timezone_is_recorded_with_the_day() -> None:
    """History carries its own timezone so it cannot be reinterpreted later."""
    record = measure_whole_day(FALL_BACK)
    assert record.tz_key == TZ_KEY
    assert ZoneInfo(record.tz_key) == TZ
