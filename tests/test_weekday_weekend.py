"""Weekday and weekend behaviour, and today's adaptation.

Households usually run differently at the weekend. The model must pick that up
once it has enough of each day type, and must degrade to pooled statistics
rather than overfitting when it does not.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from custom_components.alpha_ems_manager.const import (
    DAY_TYPE_WEEKDAY,
    DAY_TYPE_WEEKEND,
    SLOTS_PER_DAY,
    TODAY_ADAPT_RATIO_MAX,
    TODAY_ADAPT_RATIO_MIN,
)
from custom_components.alpha_ems_manager.forecast import adapt_today, build_forecast
from custom_components.alpha_ems_manager.storage import day_type_of

from .synthetic import TZ, history

#: See test_history_windows.STORED_REL -- stored quarters round to 0.1 Wh.
STORED_REL = 1e-3

#: Monday 17 August 2026. The Saturday after it is the 22nd.
MONDAY = date(2026, 8, 17)
SATURDAY = date(2026, 8, 22)

WEEKDAY_KWH = 10.0
WEEKEND_KWH = 18.0
SPLIT = {DAY_TYPE_WEEKDAY: WEEKDAY_KWH, DAY_TYPE_WEEKEND: WEEKEND_KWH}


def test_the_calendar_assumptions_hold() -> None:
    """Guard the dates the rest of this module reasons about."""
    assert day_type_of(MONDAY) == DAY_TYPE_WEEKDAY
    assert day_type_of(SATURDAY) == DAY_TYPE_WEEKEND
    assert MONDAY.weekday() == 0
    assert SATURDAY.weekday() == 5


def test_a_saturday_forecast_trends_toward_weekend_behaviour() -> None:
    """With ample history, Saturday is forecast near the weekend level."""
    records = history(MONDAY, 60, SPLIT)
    forecast = build_forecast(records, MONDAY, SATURDAY, TZ)

    assert forecast.day_type == DAY_TYPE_WEEKEND
    assert not forecast.day_type_pooled
    assert forecast.total_kwh == pytest.approx(WEEKEND_KWH, rel=STORED_REL)


def test_a_monday_forecast_trends_toward_weekday_behaviour() -> None:
    """The same history forecasts a weekday near the weekday level."""
    records = history(MONDAY, 60, SPLIT)
    forecast = build_forecast(records, MONDAY, MONDAY + timedelta(days=1), TZ)

    assert forecast.day_type == DAY_TYPE_WEEKDAY
    assert not forecast.day_type_pooled
    assert forecast.total_kwh == pytest.approx(WEEKDAY_KWH, rel=STORED_REL)


def test_the_two_day_types_are_forecast_differently() -> None:
    """The split is real, not cosmetic."""
    records = history(MONDAY, 60, SPLIT)
    weekday = build_forecast(records, MONDAY, MONDAY + timedelta(days=1), TZ).total_kwh
    weekend = build_forecast(records, MONDAY, SATURDAY, TZ).total_kwh

    assert weekday is not None and weekend is not None
    assert weekend > weekday * 1.5


def test_short_history_pools_both_day_types() -> None:
    """With only a couple of days, the split is not yet trusted.

    Three days of history from a Monday reach back to Friday, Saturday and
    Sunday. That is one weekday and two weekend days -- far too little to claim
    knowledge of "weekends", so the model pools everything instead.
    """
    records = history(MONDAY, 3, SPLIT)
    forecast = build_forecast(records, MONDAY, MONDAY + timedelta(days=1), TZ)

    assert forecast.available
    assert forecast.day_type_pooled
    # Pooled, so the answer sits between the two levels rather than at either.
    assert WEEKDAY_KWH < forecast.total_kwh < WEEKEND_KWH


def test_low_history_fallback_is_still_sensible() -> None:
    """A pooled forecast stays inside the range of what was observed."""
    records = history(MONDAY, 4, SPLIT)
    for target in (MONDAY + timedelta(days=1), SATURDAY):
        forecast = build_forecast(records, MONDAY, target, TZ)
        assert forecast.available
        assert WEEKDAY_KWH <= forecast.total_kwh <= WEEKEND_KWH


def test_the_split_engages_once_enough_of_each_type_exists() -> None:
    """Given a full month, both day types are modelled separately."""
    records = history(MONDAY, 30, SPLIT)
    weekday = build_forecast(records, MONDAY, MONDAY + timedelta(days=1), TZ)
    weekend = build_forecast(records, MONDAY, SATURDAY, TZ)

    assert not weekday.day_type_pooled
    assert not weekend.day_type_pooled


# -- today adaptation -------------------------------------------------------


def _baseline(reference: date, daily_kwh: float):
    """Return a flat baseline forecast for ``reference``."""
    return build_forecast(history(reference, 30, daily_kwh), reference, reference, TZ)


def test_no_adaptation_before_the_day_has_developed() -> None:
    """Too early in the day, the forecast is left alone.

    A large ratio computed from twenty minutes of data says nothing useful.
    """
    baseline = _baseline(MONDAY, 9.6)
    measured: list[float | None] = [None] * SLOTS_PER_DAY
    measured[0] = 1.0  # a huge first quarter

    result = adapt_today(baseline, measured, 1.0, elapsed_intervals=1)

    assert not result.adapted
    assert result.adaptation_ratio == pytest.approx(1.0)
    assert result.forecast_remaining_kwh == pytest.approx(
        baseline.remaining_kwh(1), rel=STORED_REL
    )


def test_running_hot_raises_the_remainder_but_only_halfway() -> None:
    """Consistent over-consumption nudges the rest of the day upward.

    Twice the modelled consumption so far gives a raw ratio of 2.0; damping
    halves the correction to 1.5 rather than doubling the remaining forecast.
    """
    baseline = _baseline(MONDAY, 9.6)  # 0.1 kWh per slot
    elapsed = 48
    measured: list[float | None] = [None] * SLOTS_PER_DAY
    for slot in range(elapsed):
        measured[slot] = 0.2

    result = adapt_today(baseline, measured, 9.6, elapsed_intervals=elapsed)

    assert result.adapted
    assert result.adaptation_ratio == pytest.approx(1.5, rel=1e-3)
    assert result.forecast_remaining_kwh == pytest.approx(
        baseline.remaining_kwh(elapsed) * 1.5, rel=1e-3
    )


def test_running_cold_lowers_the_remainder() -> None:
    """Under-consumption pulls the remaining forecast down symmetrically."""
    baseline = _baseline(MONDAY, 9.6)
    elapsed = 48
    measured: list[float | None] = [None] * SLOTS_PER_DAY
    for slot in range(elapsed):
        measured[slot] = 0.05

    result = adapt_today(baseline, measured, 2.4, elapsed_intervals=elapsed)

    assert result.adapted
    assert result.adaptation_ratio == pytest.approx(0.75, rel=1e-3)
    assert result.forecast_remaining_kwh < baseline.remaining_kwh(elapsed)


def test_one_extreme_quarter_cannot_dominate_the_day() -> None:
    """A single appliance spike is clamped, not extrapolated.

    An oven drawing many times the usual load for fifteen minutes produces a
    very large raw ratio. Damping plus the clamp keep the remaining forecast
    within a sane multiple of the baseline.
    """
    baseline = _baseline(MONDAY, 9.6)
    elapsed = 12
    measured: list[float | None] = [None] * SLOTS_PER_DAY
    for slot in range(elapsed):
        measured[slot] = 0.1
    measured[5] = 8.0  # one enormous quarter

    result = adapt_today(baseline, measured, 9.1, elapsed_intervals=elapsed)

    assert result.adaptation_ratio == pytest.approx(TODAY_ADAPT_RATIO_MAX)
    assert result.forecast_remaining_kwh <= baseline.remaining_kwh(elapsed) * (
        TODAY_ADAPT_RATIO_MAX + 1e-9
    )


@pytest.mark.parametrize("factor", [0.0, 0.01, 0.5, 1.0, 2.0, 10.0, 100.0])
def test_the_adaptation_ratio_is_always_clamped(factor: float) -> None:
    """However strange the day, the ratio stays inside its documented bounds."""
    baseline = _baseline(MONDAY, 9.6)
    elapsed = 48
    measured: list[float | None] = [0.1 * factor for _ in range(SLOTS_PER_DAY)]

    result = adapt_today(baseline, measured, 4.8 * factor, elapsed_intervals=elapsed)

    assert TODAY_ADAPT_RATIO_MIN <= result.adaptation_ratio <= TODAY_ADAPT_RATIO_MAX


def test_the_total_is_measured_so_far_plus_the_adapted_remainder() -> None:
    """The published total never re-forecasts what has already been measured."""
    baseline = _baseline(MONDAY, 9.6)
    elapsed = 48
    measured: list[float | None] = [None] * SLOTS_PER_DAY
    for slot in range(elapsed):
        measured[slot] = 0.15
    measured_total = 7.2

    result = adapt_today(baseline, measured, measured_total, elapsed_intervals=elapsed)

    assert result.actual_so_far_kwh == pytest.approx(measured_total)
    assert result.forecast_total_kwh == pytest.approx(
        measured_total + result.forecast_remaining_kwh
    )


def test_at_end_of_day_the_total_equals_what_was_measured() -> None:
    """Once every slot has elapsed there is nothing left to forecast."""
    baseline = _baseline(MONDAY, 9.6)
    measured: list[float | None] = [0.1] * SLOTS_PER_DAY

    result = adapt_today(baseline, measured, 9.6, elapsed_intervals=SLOTS_PER_DAY)

    assert result.forecast_remaining_kwh == pytest.approx(0.0)
    assert result.forecast_total_kwh == pytest.approx(9.6)
