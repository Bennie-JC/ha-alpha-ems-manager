"""An unusable day must take part in no decision at all.

A retained day with zero valid baseline intervals -- a house-load outage, a
flexible-load sensor down all day, the stub a restart leaves behind -- supplies
no observation to any behavioural slot. It was nevertheless counted toward
``MIN_DAYS_FOR_DAY_TYPE``, so it could engage the weekday/weekend split on the
strength of contributing nothing. ``counted`` then narrowed to that one day
type, and ``model_days`` fell below what the same history pooled would support.

The related defect is the gate itself: ``source_days`` counted *learned* days of
the type while the split engaged on merely *present* days of the type. Two
partial weekends therefore produced ``source_days == 0`` and a withheld forecast
carrying no reason -- and deleting one of those two days brought the forecast
back, so acquiring history removed one.

Five populations must stay distinct, and these tests hold them apart:

retained
    every day in the store.
learned
    finalised days at or above ``MIN_DAY_COMPLETENESS`` baseline coverage.
usable
    past days inside the longest window with at least one valid baseline
    interval; the only days that reach the model.
source (``model_days``)
    learned days among those actually counted for this target's day type.
modelled intervals
    slots of the target day the model could blend, distinct from all of the
    above.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from custom_components.alpha_ems_manager.const import (
    FORECAST_WINDOWS,
    MIN_DAYS_FOR_DAY_TYPE,
)
from custom_components.alpha_ems_manager.forecast import (
    REASON_NO_HISTORY,
    build_forecast,
)

from .conftest import TZ
from .synthetic import empty_day, flat_day

#: 2026-08-19 is a Wednesday, so weekday and weekend targets are both nearby.
REFERENCE = date(2026, 8, 19)
NEXT_WEEKDAY = date(2026, 8, 20)  # Thursday
NEXT_WEEKEND = date(2026, 8, 22)  # Saturday


def forecast_for(records, target: date = NEXT_WEEKDAY):
    """Build a forecast for ``target`` from ``records`` at the fixed reference."""
    return build_forecast(records, REFERENCE, target, TZ)


def weekdays(count: int, *, kwh: float = 12.0, start_offset: int = 1):
    """Return ``count`` learned weekdays ending before the reference."""
    days = []
    offset = start_offset
    while len(days) < count:
        day = REFERENCE - timedelta(days=offset)
        if day.weekday() < 5:
            days.append(flat_day(day, kwh))
        offset += 1
    return days


def weekend_days(count: int, *, kwh: float = 18.0):
    """Return ``count`` learned weekend days ending before the reference."""
    days = []
    offset = 1
    while len(days) < count:
        day = REFERENCE - timedelta(days=offset)
        if day.weekday() >= 5:
            days.append(flat_day(day, kwh))
        offset += 1
    return days


def blank(day: date):
    """Return a retained day carrying no valid baseline interval whatsoever."""
    return empty_day(day)


# -- an unusable day cannot change the day-type decision ---------------------


def test_an_empty_weekday_cannot_engage_the_weekday_split() -> None:
    """One real weekday plus one blank weekday must still pool.

    Fails on beta.3: the blank day makes ``len(same_type) == 2``, the split
    engages, ``counted`` drops the weekend history, and the forecast is built
    from -- or withheld for -- a single day.
    """
    real = weekdays(1)
    blanks = [blank(date(2026, 8, 18))]  # Tuesday
    weekend = weekend_days(2)

    forecast = forecast_for(real + blanks + weekend)

    assert forecast.day_type_pooled is True
    assert forecast.usable_days == 3
    assert forecast.available is True


def test_an_empty_weekend_day_cannot_engage_the_weekend_split() -> None:
    """The same, for a weekend target."""
    real_weekend = weekend_days(1)
    blanks = [blank(date(2026, 8, 15))]  # Saturday
    week = weekdays(3)

    forecast = forecast_for(real_weekend + blanks + week, target=NEXT_WEEKEND)

    assert forecast.day_type_pooled is True
    assert forecast.available is True


def test_two_real_weekdays_do_engage_the_split_even_with_a_blank_present() -> None:
    """The guard must not disable the split, only stop a blank day forcing it."""
    forecast = forecast_for([*weekdays(2), blank(date(2026, 8, 12)), *weekend_days(2)])

    assert forecast.day_type_pooled is False
    assert forecast.source_days == 2


def test_a_blank_day_never_appears_in_usable_days() -> None:
    """``usable_days`` counts days that contributed, and a blank contributed none."""
    forecast = forecast_for([*weekdays(3), blank(date(2026, 8, 16))])

    assert forecast.usable_days == 3


def test_a_blank_day_between_two_valid_days_changes_nothing() -> None:
    """Insertion order and position must be irrelevant."""
    without = forecast_for(weekdays(4))
    with_blank = forecast_for([*weekdays(4), blank(date(2026, 8, 16))])

    assert with_blank.total_kwh == pytest.approx(without.total_kwh)
    assert with_blank.source_days == without.source_days
    assert with_blank.day_type_pooled == without.day_type_pooled
    assert with_blank.modelled_intervals == without.modelled_intervals


@pytest.mark.parametrize(
    "cause",
    ["ev_source_down", "house_load_outage", "restart_stub"],
)
def test_the_cause_of_an_empty_day_does_not_matter(cause: str) -> None:
    """However a day came to be unusable, it is unusable in the same way."""
    day = date(2026, 8, 17)
    if cause == "ev_source_down":
        # Measured perfectly, but a configured flexible load never read, so no
        # interval has a valid baseline.
        record = flat_day(day, 12.0, ev_expected=True, ev_valid_intervals=0)
    elif cause == "house_load_outage":
        record = flat_day(day, 12.0, accepted_intervals=0)
    else:
        record = blank(day)

    assert record.baseline_valid_count == 0

    forecast = forecast_for([*weekdays(1, start_offset=3), record, *weekend_days(2)])

    assert forecast.day_type_pooled is True
    assert forecast.usable_days == 3


def test_an_empty_daylight_saving_day_is_excluded_on_its_real_length() -> None:
    """A 100-interval blank day is still a blank day."""
    fall_back = date(2026, 10, 25)
    reference = date(2026, 10, 28)
    records = [
        flat_day(reference - timedelta(days=1), 12.0),
        blank(fall_back),
    ]
    assert records[1].interval_count == 100

    forecast = build_forecast(records, reference, reference + timedelta(days=1), TZ)

    assert forecast.usable_days == 1


def test_a_history_of_nothing_but_empty_days_reports_no_history() -> None:
    """Withholding is right, and the reason must say which safeguard fired."""
    forecast = forecast_for([blank(REFERENCE - timedelta(days=n)) for n in (1, 2, 3)])

    assert forecast.available is False
    assert forecast.unavailable_reason == REASON_NO_HISTORY
    assert forecast.usable_days == 0
    assert forecast.source_days == 0


def test_pruning_an_empty_day_leaves_the_forecast_identical() -> None:
    """Retention removing a blank day must be a no-op for the model."""
    kept = weekdays(3)
    before = forecast_for([*kept, blank(date(2026, 8, 16))])
    after = forecast_for(kept)

    assert before.total_kwh == pytest.approx(after.total_kwh)
    assert before.usable_days == after.usable_days


# -- the availability gate must always explain itself ------------------------


def test_unlearned_same_type_days_no_longer_withhold_a_forecast_silently() -> None:
    """The high-severity case found in audit: available False, reason None.

    Twenty-seven learned weekdays plus two *partial* weekend days. On beta.3 the
    split engages on the two present weekend days, ``source_days`` counts only
    learned ones and comes out zero, and the Saturday forecast is withheld with
    no reason at all -- while diagnostics simultaneously claims 96 of 96
    modelled intervals and all five windows used.
    """
    partial_weekends = [
        flat_day(day, 18.0, accepted_intervals=20)
        for day in (date(2026, 8, 15), date(2026, 8, 16))
    ]
    records = weekdays(27, start_offset=1) + partial_weekends
    assert all(not record.is_learned for record in partial_weekends)

    forecast = forecast_for(records, target=NEXT_WEEKEND)

    assert forecast.day_type_pooled is True
    assert forecast.source_days >= MIN_DAYS_FOR_DAY_TYPE
    assert forecast.available is True
    assert forecast.unavailable_reason is None


def test_acquiring_history_never_removes_a_working_forecast() -> None:
    """The defect was non-monotonic in data; this pins that it no longer is."""
    base = weekdays(27, start_offset=1)
    one_partial = [*base, flat_day(date(2026, 8, 16), 18.0, accepted_intervals=20)]
    two_partial = [
        *one_partial,
        flat_day(date(2026, 8, 15), 18.0, accepted_intervals=20),
    ]

    assert forecast_for(one_partial, target=NEXT_WEEKEND).available is True
    assert forecast_for(two_partial, target=NEXT_WEEKEND).available is True


def test_a_withheld_forecast_always_carries_a_reason() -> None:
    """The invariant behind both fixes, asserted directly."""
    for records in (
        [],
        [blank(date(2026, 8, 18))],
        weekdays(1),
        [*weekdays(1), blank(date(2026, 8, 18))],
    ):
        forecast = forecast_for(records)
        if not forecast.available:
            assert forecast.unavailable_reason is not None


# -- days no window can reach must not be claimed as model input -------------


def test_days_beyond_the_longest_window_are_not_counted_as_model_days() -> None:
    """``_mean`` already ignores them, so reporting them overstated the model."""
    horizon = max(FORECAST_WINDOWS)
    recent = weekdays(3)
    ancient = [
        flat_day(REFERENCE - timedelta(days=horizon + offset), 30.0)
        for offset in (1, 5, 50)
    ]

    forecast = forecast_for(recent + ancient)

    assert forecast.usable_days == 3
    assert forecast.source_days == 3
    # And the ancient days did not shift the numbers either.
    assert forecast.total_kwh == pytest.approx(forecast_for(recent).total_kwh)
