"""The forecast must never invent a number to avoid an empty state.

An `unknown` forecast is a correct answer when the model has not learned enough.
A confident wrong number is not: a user who sees 38 kWh predicted against a real
14 kWh loses trust in every other figure the integration reports, and a future
optimisation phase built on top of it would size a battery reserve from fiction.

Two ways that used to happen, both pinned here:

* **Whole-day extrapolation.** Intervals that were never observed were filled
  with the mean of the intervals that *were*. On a fresh install, where only the
  evening had been seen twice, that painted the evening rate across the whole day.
* **Counting days that were never learned.** ``source_days`` -- published as the
  ``model_days`` attribute -- counted every past record, including a partial day
  that contributed almost nothing, so it disagreed with the Learning Days sensor.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from custom_components.alpha_ems_manager.const import MIN_DAY_COMPLETENESS
from custom_components.alpha_ems_manager.forecast import build_forecast

from .synthetic import TZ, empty_day, flat_day

#: A Monday, so the two history days below share a day type.
MONDAY = date(2026, 8, 17)
TUESDAY = MONDAY + timedelta(days=1)
WEDNESDAY = MONDAY + timedelta(days=2)

#: The shape used throughout: a quiet night and a busy evening.
NIGHT_KWH = 0.10
EVENING_KWH = 0.40
#: Intervals 80..95 are 20:00 onwards in a 96-interval day.
EVENING_START = 80


def evening_only_day(day: date):
    """Return a day where only the evening was ever measured.

    This is what an installation performed at 20:00 leaves behind: the intervals
    before the install simply never happened.
    """
    record = empty_day(day, TZ)
    for index in range(EVENING_START, record.interval_count):
        record.record_interval(
            index, measured_kwh=EVENING_KWH, ev_kwh=None, ev_expected=False
        )
    return record


def full_day(day: date):
    """Return a complete day: quiet night, busy evening."""
    record = empty_day(day, TZ)
    for index in range(record.interval_count):
        value = EVENING_KWH if index >= EVENING_START else NIGHT_KWH
        record.record_interval(
            index, measured_kwh=value, ev_kwh=None, ev_expected=False
        )
    return record


#: What ``full_day`` actually consumes: 80 quiet + 16 busy intervals.
REAL_DAY_KWH = EVENING_START * NIGHT_KWH + 16 * EVENING_KWH


def test_the_reference_day_totals_what_we_think_it_does() -> None:
    """Guard the arithmetic the assertions below depend on."""
    assert abs(REAL_DAY_KWH - 14.4) < 1e-9
    assert full_day(MONDAY).measured_total_kwh == pytest.approx(14.4)


# -- whole-day extrapolation --------------------------------------------------


def test_a_fresh_install_does_not_paint_the_evening_across_the_whole_day() -> None:
    """The decisive case: one evening-only day plus one complete day.

    Only the evening slots have the two observations a window needs, so every
    other slot is genuinely unknown. Filling them from the evening mean produced
    96 x 0.40 = 38.4 kWh -- 167 % of the real 14.4 kWh -- and published it as
    available.
    """
    records = [evening_only_day(MONDAY), full_day(TUESDAY)]

    forecast = build_forecast(records, WEDNESDAY, WEDNESDAY, TZ)

    if forecast.available:
        assert forecast.total_kwh is not None
        # If a number is published at all it must be of the right order. The old
        # behaviour landed at 38.4 kWh.
        assert forecast.total_kwh < REAL_DAY_KWH * 1.5, (
            f"forecast {forecast.total_kwh:.1f} kWh against a real "
            f"{REAL_DAY_KWH:.1f} kWh"
        )
    else:
        # Refusing to answer is the better outcome here, and is what the model
        # now does: too little of the day has ever been observed.
        assert forecast.total_kwh is None


def test_a_mostly_unobserved_day_is_reported_as_unavailable() -> None:
    """Below the completeness threshold the honest answer is `unknown`."""
    records = [evening_only_day(MONDAY), full_day(TUESDAY)]

    forecast = build_forecast(records, WEDNESDAY, WEDNESDAY, TZ)

    known = [value for value in forecast.intervals if value is not None]
    assert len(known) / forecast.interval_count < MIN_DAY_COMPLETENESS
    assert not forecast.available
    assert forecast.total_kwh is None


def test_a_never_observed_slot_range_is_filled_from_its_neighbour() -> None:
    """A systematically gappy slot range must not inherit the day mean.

    An EV charger that reports `unavailable` overnight invalidates the baseline
    for the small hours on *every* day, so those slots never reach the minimum
    observation count. Filling them with the whole-day mean overstated the night
    by more than half while the day still counted as learned.

    Three complete days give every other slot its observations, so enough of the
    day is known for a forecast to be published -- and the filler for the missing
    night must come from the adjacent night intervals, not from the evening.
    """
    records = []
    for offset in range(1, 4):
        day = MONDAY + timedelta(days=offset)
        record = empty_day(day, TZ)
        for index in range(record.interval_count):
            # Intervals 0..7 (00:00-02:00) are never valid, as an idle-unavailable
            # charger would leave them.
            if index < 8:
                continue
            value = EVENING_KWH if index >= EVENING_START else NIGHT_KWH
            record.record_interval(
                index, measured_kwh=value, ev_kwh=None, ev_expected=False
            )
        records.append(record)

    reference = MONDAY + timedelta(days=4)
    forecast = build_forecast(records, reference, reference, TZ)

    assert forecast.available
    # The unobserved night must look like night, not like the 0.1625 kWh day mean.
    for index in range(8):
        assert forecast.intervals[index] == pytest.approx(NIGHT_KWH, abs=1e-6), (
            f"interval {index} filled with {forecast.intervals[index]}"
        )


def test_a_complete_history_is_still_forecast_normally() -> None:
    """The guard must not suppress a forecast the model can legitimately make."""
    records = [full_day(MONDAY + timedelta(days=offset)) for offset in range(3)]
    reference = MONDAY + timedelta(days=3)

    forecast = build_forecast(records, reference, reference, TZ)

    assert forecast.available
    assert forecast.total_kwh == pytest.approx(REAL_DAY_KWH, rel=1e-3)
    assert all(value is not None for value in forecast.intervals)


# -- source_days honesty ------------------------------------------------------


def test_source_days_counts_only_days_that_were_actually_learned() -> None:
    """`model_days` must agree with the Learning Days sensor.

    The evening-only day carries 16 of 96 intervals, far below the completeness
    a learned day requires, so it must not be counted as one.
    """
    partial = evening_only_day(MONDAY)
    complete = full_day(TUESDAY)

    assert not partial.is_learned
    assert complete.is_learned

    forecast = build_forecast([partial, complete], WEDNESDAY, WEDNESDAY, TZ)

    assert forecast.source_days <= 1


def test_source_days_matches_the_learned_day_count_on_clean_history() -> None:
    """With nothing partial in play the two counts are identical."""
    records = [full_day(MONDAY + timedelta(days=offset)) for offset in range(4)]
    reference = MONDAY + timedelta(days=4)

    forecast = build_forecast(records, reference, reference, TZ)

    assert forecast.source_days == 4
    assert all(record.is_learned for record in records)


def test_no_history_at_all_stays_unavailable() -> None:
    """Unchanged behaviour, restated so the guard cannot break it."""
    forecast = build_forecast([], WEDNESDAY, WEDNESDAY, TZ)

    assert not forecast.available
    assert forecast.total_kwh is None
    assert forecast.source_days == 0


def test_a_single_learned_day_is_not_enough_to_forecast() -> None:
    """One day cannot satisfy the two-observation minimum for any slot."""
    forecast = build_forecast([full_day(MONDAY)], TUESDAY, TUESDAY, TZ)

    assert not forecast.available
    assert forecast.total_kwh is None


def test_flat_day_history_is_unaffected_by_the_guard() -> None:
    """The existing synthetic history used across the suite still forecasts."""
    records = [flat_day(MONDAY + timedelta(days=offset), 10.0) for offset in range(5)]
    reference = MONDAY + timedelta(days=5)

    forecast = build_forecast(records, reference, reference, TZ)

    assert forecast.available
    # Stored intervals are rounded to a fixed precision, so an even 10 kWh split
    # across 96 intervals does not round-trip exactly.
    assert forecast.total_kwh == pytest.approx(10.0, rel=1e-3)
