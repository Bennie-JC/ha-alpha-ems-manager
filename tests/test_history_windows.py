"""The multi-window forecast model.

The model blends 7, 30, 90, 180 and 365-day look-back windows. These tests fix
the two properties that matter: a forecast appears as soon as *any* window has
data, and recent behaviour dominates when the windows disagree.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from custom_components.alpha_ems_manager.const import (
    FORECAST_WINDOWS,
    MAX_HISTORY_DAYS,
    SLOTS_PER_DAY,
)
from custom_components.alpha_ems_manager.forecast import build_forecast

from .synthetic import TZ, flat_day, history, shaped_day

REFERENCE = date(2026, 8, 17)  # a Monday

#: Stored quarters are rounded to four decimals of a kWh (0.1 Wh). Reassembling
#: a day from 96 of them can therefore drift by up to ~0.01 kWh, so daily totals
#: are compared at this tolerance rather than exactly.
STORED_REL = 1e-3


@pytest.mark.parametrize("window", FORECAST_WINDOWS)
def test_each_window_alone_produces_its_own_average(window: int) -> None:
    """With uniform history, the forecast reproduces the daily total."""
    records = history(REFERENCE, window, 12.0)
    forecast = build_forecast(records, REFERENCE, REFERENCE + timedelta(days=1), TZ)

    assert forecast.available
    assert forecast.total_kwh == pytest.approx(12.0, rel=STORED_REL)


@pytest.mark.parametrize("days", [3, 7, 20, 60])
def test_weights_renormalise_when_history_is_short(days: int) -> None:
    """A partial history still forecasts, without shrinking toward zero.

    Only the windows that actually hold data contribute, and their weights are
    renormalised to sum to one. If they were not, a 3-day-old install would
    forecast 35 % of the real figure because only the 7-day window contributed.
    """
    records = history(REFERENCE, days, 10.0)
    forecast = build_forecast(records, REFERENCE, REFERENCE + timedelta(days=1), TZ)

    assert forecast.available
    assert forecast.total_kwh == pytest.approx(10.0, rel=STORED_REL)


def test_a_forecast_appears_after_two_days() -> None:
    """365 days of history are emphatically not a prerequisite."""
    records = history(REFERENCE, 2, 9.0)
    forecast = build_forecast(records, REFERENCE, REFERENCE + timedelta(days=1), TZ)

    assert forecast.available
    assert forecast.total_kwh == pytest.approx(9.0, rel=STORED_REL)


def test_a_single_day_is_not_yet_enough() -> None:
    """One observation of a slot is not an average; the model waits."""
    records = history(REFERENCE, 1, 9.0)
    forecast = build_forecast(records, REFERENCE, REFERENCE + timedelta(days=1), TZ)

    assert not forecast.available
    assert forecast.total_kwh is None


def test_no_history_yields_no_forecast() -> None:
    """An empty model reports nothing rather than guessing zero."""
    forecast = build_forecast([], REFERENCE, REFERENCE + timedelta(days=1), TZ)

    assert not forecast.available
    assert forecast.total_kwh is None


def test_recent_behaviour_outweighs_the_distant_past() -> None:
    """When old and new disagree, recency pulls the forecast toward the new.

    The last seven days run at 20 kWh and the preceding year at 10 kWh. The
    benchmark is the unweighted 365-day mean, which those seven days barely
    move (10.19 kWh). The weighted blend must land far above it, because the
    recent days sit in every window while the old days miss the 7-day one.

    The assertion is deliberately a comparison against that benchmark rather
    than a fixed number: the window weights are tunable, and a test that pins
    the exact output would forbid tuning them.
    """
    records: list = []
    for offset in range(1, 366):
        day = REFERENCE - timedelta(days=offset)
        records.append(flat_day(day, 20.0 if offset <= 7 else 10.0))

    forecast = build_forecast(records, REFERENCE, REFERENCE + timedelta(days=1), TZ)
    total = forecast.total_kwh

    unweighted_mean = (7 * 20.0 + 358 * 10.0) / 365

    assert total is not None
    assert 10.0 < total < 20.0
    assert total > unweighted_mean * 1.25, (
        f"recency weighting barely moved the forecast: {total:.2f} kWh against "
        f"an unweighted mean of {unweighted_mean:.2f} kWh"
    )


def test_a_step_change_is_tracked_but_not_chased() -> None:
    """A sustained new level moves the forecast without instantly snapping."""
    steady = history(REFERENCE, 365, 10.0)
    steady_total = build_forecast(
        steady, REFERENCE, REFERENCE + timedelta(days=1), TZ
    ).total_kwh

    shifted: list = []
    for offset in range(1, 366):
        day = REFERENCE - timedelta(days=offset)
        shifted.append(flat_day(day, 16.0 if offset <= 30 else 10.0))
    shifted_total = build_forecast(
        shifted, REFERENCE, REFERENCE + timedelta(days=1), TZ
    ).total_kwh

    assert steady_total is not None and shifted_total is not None
    assert shifted_total > steady_total
    assert shifted_total < 16.0


def test_only_windows_with_data_are_reported_as_used() -> None:
    """``windows_used`` reflects what actually contributed."""
    records = history(REFERENCE, 10, 11.0)
    forecast = build_forecast(records, REFERENCE, REFERENCE + timedelta(days=1), TZ)

    # Ten days of history sit inside every window, so every window contributes.
    assert set(forecast.windows_used) == set(FORECAST_WINDOWS)

    sparse = history(REFERENCE, 2, 11.0)
    sparse_forecast = build_forecast(
        sparse, REFERENCE, REFERENCE + timedelta(days=1), TZ
    )
    assert set(sparse_forecast.windows_used) == set(FORECAST_WINDOWS)


def test_days_beyond_the_longest_window_are_ignored() -> None:
    """History older than 365 days cannot influence the forecast."""
    records = [
        flat_day(REFERENCE - timedelta(days=offset), 99.0)
        for offset in range(MAX_HISTORY_DAYS + 1, MAX_HISTORY_DAYS + 40)
    ]
    records += history(REFERENCE, 10, 10.0)

    forecast = build_forecast(records, REFERENCE, REFERENCE + timedelta(days=1), TZ)

    assert forecast.total_kwh == pytest.approx(10.0, rel=STORED_REL)


def test_today_is_never_used_as_its_own_input() -> None:
    """The in-progress day is excluded, so a partial day cannot skew the model."""
    records = history(REFERENCE, 10, 10.0)
    records.append(flat_day(REFERENCE, 1.0, accepted_intervals=8))

    forecast = build_forecast(records, REFERENCE, REFERENCE, TZ)

    assert forecast.total_kwh == pytest.approx(10.0, rel=STORED_REL)


def test_the_shape_of_the_day_is_preserved() -> None:
    """The forecast is per-slot, not a flat daily total spread evenly."""
    profile = [0.05] * SLOTS_PER_DAY
    for slot in range(72, 84):  # 18:00-21:00 evening peak
        profile[slot] = 0.40

    records = [
        shaped_day(REFERENCE - timedelta(days=offset), profile)
        for offset in range(1, 15)
    ]

    forecast = build_forecast(records, REFERENCE, REFERENCE + timedelta(days=1), TZ)

    assert forecast.intervals[78] == pytest.approx(0.40, rel=STORED_REL)
    assert forecast.intervals[10] == pytest.approx(0.05, rel=STORED_REL)
    assert forecast.intervals[78] > forecast.intervals[10] * 5


def test_remaining_kwh_sums_only_the_rest_of_the_day() -> None:
    """``remaining_kwh`` is used by today's adaptation and must be exact."""
    records = history(REFERENCE, 10, 9.6)  # 0.1 kWh per slot
    forecast = build_forecast(records, REFERENCE, REFERENCE, TZ)

    assert forecast.remaining_kwh(0) == pytest.approx(9.6, rel=STORED_REL)
    assert forecast.remaining_kwh(48) == pytest.approx(4.8, rel=STORED_REL)
    assert forecast.remaining_kwh(SLOTS_PER_DAY) == pytest.approx(0.0)
