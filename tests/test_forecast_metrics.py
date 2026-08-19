"""Forecast-error statistics, and the numbers they refuse to produce.

Two failure modes matter more than accuracy here. The first is a percentage
computed against a near-zero denominator, which turns one quiet overnight
interval into a four-hundred-per-cent error and destroys any average it enters.
The second is comparing a whole-day prediction against a partly observed day,
which manufactures a systematic bias out of a sensor outage.

Both are tested by asserting that nothing is published, rather than that
something plausible is.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.alpha_ems_manager.const import (
    FLAG_NO_RECORD,
    STATUS_MEASURED_MISSING,
    STATUS_VALID,
)
from custom_components.alpha_ems_manager.forecast_history import (
    DayOutcome,
    ForecastSnapshot,
)
from custom_components.alpha_ems_manager.metrics import (
    best_snapshot,
    compute_window,
    day_error_from_summary,
    score_day,
    summary_row,
    window_from_summaries,
)

from .forecast_helpers import FALL_BACK, NORMAL

TZ_KEY = "Europe/Amsterdam"


def snapshot(
    day: date,
    predicted: list[float],
    *,
    horizon: int = 0,
    filled: list[bool] | None = None,
    available: bool = True,
    issued_hour: int = 12,
) -> ForecastSnapshot:
    """Return a snapshot carrying an explicit per-interval prediction."""
    count = len(predicted)
    return ForecastSnapshot(
        issued_at=datetime(day.year, day.month, day.day, issued_hour, tzinfo=UTC),
        target_day=day,
        tz_key=TZ_KEY,
        interval_count=count,
        horizon_days=horizon,
        available=available,
        unavailable_reason=None if available else "no_history",
        predicted=tuple(predicted),
        filled=tuple(filled or [False] * count),
        fingerprint=f"{hash((day, tuple(predicted), horizon)) & 0xFFFFFFFF:016x}",
        model_version=1,
        model_params="0000000000000000",
        baseline_definition="none",
        context={"load_model": {"v": 1, "model_days": 5, "day_type": "weekday"}},
    )


def outcome(
    day: date,
    actual: list[float | None],
    *,
    flags: tuple[str, ...] = (),
) -> DayOutcome:
    """Return an outcome whose status follows directly from its values."""
    status = "".join(
        STATUS_VALID if value is not None else STATUS_MEASURED_MISSING
        for value in actual
    )
    return DayOutcome(
        target_day=day,
        finalized_at=datetime(day.year, day.month, day.day, 23, tzinfo=UTC),
        tz_key=TZ_KEY,
        interval_count=len(actual),
        actual=tuple(actual),
        status=status,
        flexible_total_kwh=None,
        flags=flags,
    )


# -- sign convention ---------------------------------------------------------


def test_a_positive_error_means_the_model_over_predicted() -> None:
    """Fixed once, here, because every consumer inherits it."""
    scored = score_day(snapshot(NORMAL, [1.0, 1.0]), outcome(NORMAL, [0.6, 0.6]))

    assert scored is not None
    assert scored.signed_error_kwh == pytest.approx(0.8)
    assert compute_window([scored]).bias_kwh == pytest.approx(0.4)


def test_a_negative_error_means_the_model_under_predicted() -> None:
    """The other direction, pinned so a refactor cannot flip the sign."""
    scored = score_day(snapshot(NORMAL, [1.0, 1.0]), outcome(NORMAL, [1.5, 1.5]))

    assert scored is not None
    assert scored.signed_error_kwh == pytest.approx(-1.0)
    assert compute_window([scored]).bias_kwh == pytest.approx(-0.5)


def test_bias_and_mae_disagree_when_errors_cancel() -> None:
    """Which is exactly why both are reported.

    A model that is half a kilowatt-hour high every morning and half a
    kilowatt-hour low every evening has zero bias and is not accurate.
    """
    scored = score_day(snapshot(NORMAL, [1.5, 0.5]), outcome(NORMAL, [1.0, 1.0]))

    assert scored is not None
    metrics = compute_window([scored])
    assert metrics.bias_kwh == pytest.approx(0.0)
    assert metrics.mae_kwh == pytest.approx(0.5)


# -- what is never computed --------------------------------------------------


def test_a_near_zero_actual_produces_no_percentage_explosion() -> None:
    """The reason MAPE is not computed at all rather than floored.

    A 0.01 kWh interval predicted at 0.05 kWh is a four-hundred-per-cent error
    and roughly forty watt-hours. Only one of those numbers is worth acting on.
    """
    scored = score_day(snapshot(NORMAL, [0.05] * 96), outcome(NORMAL, [0.01] * 96))

    assert scored is not None
    metrics = compute_window([scored])
    assert metrics.mae_kwh == pytest.approx(0.04)
    # WAPE divides by the window total, not by an interval, so it stays finite
    # and readable even here.
    assert metrics.wape_percent == pytest.approx(400.0)
    assert metrics.wape_percent != float("inf")


def test_an_all_zero_day_yields_no_percentage_at_all() -> None:
    """No denominator, so no figure. Never an infinity or a NaN."""
    scored = score_day(snapshot(NORMAL, [0.1] * 96), outcome(NORMAL, [0.0] * 96))

    assert scored is not None
    metrics = compute_window([scored])
    assert metrics.wape_percent is None
    assert metrics.mae_kwh == pytest.approx(0.1)
    assert scored.error_percent is None


def test_a_zero_actual_and_a_zero_prediction_is_a_zero_error() -> None:
    """A genuinely perfect quiet interval is not a fault."""
    scored = score_day(snapshot(NORMAL, [0.0] * 96), outcome(NORMAL, [0.0] * 96))

    assert scored is not None
    metrics = compute_window([scored])
    assert metrics.mae_kwh == pytest.approx(0.0)
    assert metrics.bias_kwh == pytest.approx(0.0)
    assert metrics.wape_percent is None


def test_an_empty_window_produces_no_numbers() -> None:
    """Nothing measured means nothing reported, not a confident zero."""
    metrics = compute_window([])

    assert metrics.days_compared == 0
    assert metrics.mae_kwh is None
    assert metrics.bias_kwh is None
    assert metrics.wape_percent is None
    assert metrics.rmse_kwh is None


# -- what may be compared ----------------------------------------------------


def test_only_intervals_with_both_halves_are_scored() -> None:
    """A partly observed day is scored on the part that was observed.

    Comparing a whole-day prediction against a part-day measurement would
    report the unmeasured hours as a forecast that came in high -- a systematic
    bias manufactured out of an outage.
    """
    actual: list[float | None] = [1.0] * 48 + [None] * 48
    scored = score_day(snapshot(NORMAL, [1.2] * 96), outcome(NORMAL, actual))

    assert scored is not None
    assert scored.intervals_compared == 48
    assert scored.predicted_kwh == pytest.approx(57.6)
    assert scored.actual_kwh == pytest.approx(48.0)
    # Not 115.2 vs 48.0, which is what a naive whole-day comparison would give.
    assert scored.signed_error_kwh == pytest.approx(9.6)


def test_a_flagged_day_is_never_scored() -> None:
    """The two sides describe different things, so there is nothing to compare."""
    assert (
        score_day(
            snapshot(NORMAL, [1.0] * 4),
            outcome(NORMAL, [1.0] * 4, flags=(FLAG_NO_RECORD,)),
        )
        is None
    )


def test_a_withheld_forecast_is_never_scored() -> None:
    """There was no prediction, so there is no error."""
    assert (
        score_day(snapshot(NORMAL, [], available=False), outcome(NORMAL, [1.0] * 4))
        is None
    )


def test_a_day_with_no_usable_actual_is_never_scored() -> None:
    """Every interval missing means no comparison exists."""
    assert score_day(snapshot(NORMAL, [1.0] * 4), outcome(NORMAL, [None] * 4)) is None


def test_mismatched_day_lengths_are_never_scored() -> None:
    """A 96-interval prediction against a 100-interval day is nonsense."""
    assert score_day(snapshot(NORMAL, [1.0] * 96), outcome(NORMAL, [1.0] * 100)) is None


# -- breakdowns --------------------------------------------------------------


def test_modelled_and_filled_intervals_are_reported_separately() -> None:
    """The question Phase 9 exists to answer, made answerable now."""
    predicted = [1.0] * 96
    filled = [index < 8 for index in range(96)]
    # The filled intervals are badly wrong; the modelled ones are close.
    actual: list[float | None] = [0.2] * 8 + [0.95] * 88

    scored = score_day(
        snapshot(NORMAL, predicted, filled=filled), outcome(NORMAL, actual)
    )
    assert scored is not None
    metrics = compute_window([scored])

    assert metrics.intervals_filled == 8
    assert metrics.intervals_modelled == 88
    assert metrics.mae_filled_kwh == pytest.approx(0.8)
    assert metrics.mae_modelled_kwh == pytest.approx(0.05)
    assert metrics.mae_filled_kwh > metrics.mae_modelled_kwh


def test_slot_bands_follow_the_wall_clock_not_the_index() -> None:
    """Derived from the interval's own local time, so a fold stays correct."""
    predicted = [1.0] * 96
    actual: list[float | None] = [1.0] * 96
    # Only the evening is wrong.
    for index in range(72, 96):
        actual[index] = 0.5

    scored = score_day(snapshot(NORMAL, predicted), outcome(NORMAL, actual))
    assert scored is not None
    metrics = compute_window([scored])

    assert metrics.mae_by_band["evening"] == pytest.approx(0.5)
    assert metrics.mae_by_band["night"] == pytest.approx(0.0)
    assert metrics.mae_by_band["morning"] == pytest.approx(0.0)
    assert metrics.mae_by_band["afternoon"] == pytest.approx(0.0)


def test_the_repeated_fall_back_hour_lands_in_the_night_band_twice() -> None:
    """Two chronological indices, one wall-clock band, both counted."""
    predicted = [1.0] * 100
    actual: list[float | None] = [1.0] * 100
    for index in range(8, 16):
        actual[index] = 0.0

    scored = score_day(snapshot(FALL_BACK, predicted), outcome(FALL_BACK, actual))
    assert scored is not None
    metrics = compute_window([scored])

    # Both passes of 02:00-02:59 are inside the night band.
    assert metrics.intervals_compared == 100
    assert metrics.mae_by_band["night"] == pytest.approx(8 / 28)


def test_an_unresolvable_timezone_drops_the_bands_rather_than_raising() -> None:
    """A statistics call must not fail because a zone was renamed."""
    broken = outcome(NORMAL, [1.0] * 4)
    broken = DayOutcome(
        target_day=broken.target_day,
        finalized_at=broken.finalized_at,
        tz_key="Mars/Olympus",
        interval_count=4,
        actual=broken.actual,
        status=broken.status,
        flexible_total_kwh=None,
    )
    scored = score_day(snapshot(NORMAL, [1.2] * 4), broken)

    assert scored is not None
    metrics = compute_window([scored])
    assert metrics.mae_kwh == pytest.approx(0.2)
    assert all(value is None for value in metrics.mae_by_band.values())


# -- which prediction is scored ----------------------------------------------


def test_the_lowest_horizon_is_the_one_scored() -> None:
    """The model's final word on a day is its headline prediction."""
    day_ahead = snapshot(NORMAL, [1.0] * 4, horizon=1, issued_hour=6)
    day_of = snapshot(NORMAL, [0.9] * 4, horizon=0, issued_hour=9)

    assert best_snapshot([day_ahead, day_of]) is day_of
    assert best_snapshot([day_of, day_ahead]) is day_of


def test_a_withheld_snapshot_is_never_chosen_over_a_published_one() -> None:
    """Scoring silence against reality would produce nothing useful."""
    withheld = snapshot(NORMAL, [], horizon=0, available=False)
    published = snapshot(NORMAL, [1.0] * 4, horizon=1)

    assert best_snapshot([withheld, published]) is published
    assert best_snapshot([withheld]) is None


# -- summary rows as sufficient statistics -----------------------------------


def test_a_window_rebuilt_from_summaries_matches_the_full_computation() -> None:
    """The published sensors read summaries, so the two must agree exactly."""
    days = []
    rows = []
    for offset in range(1, 4):
        day = NORMAL - timedelta(days=offset)
        scored = score_day(
            snapshot(day, [1.0] * 96), outcome(day, [0.9 + 0.05 * offset] * 96)
        )
        assert scored is not None
        days.append(scored)
        rows.append(summary_row(scored, interval_count=96, flags=()))

    full = compute_window(days)
    cheap = window_from_summaries(rows)

    assert cheap.days_compared == full.days_compared == 3
    assert cheap.intervals_compared == full.intervals_compared == 288
    assert cheap.mae_kwh == pytest.approx(full.mae_kwh)
    assert cheap.bias_kwh == pytest.approx(full.bias_kwh)
    assert cheap.wape_percent == pytest.approx(full.wape_percent)


def test_a_flagged_summary_row_contributes_nothing() -> None:
    """An incomparable day must not be averaged in through the cheap path."""
    good = summary_row(
        score_day(snapshot(NORMAL, [1.0] * 96), outcome(NORMAL, [0.9] * 96)),
        interval_count=96,
        flags=(),
    )
    bad = summary_row(None, interval_count=96, flags=(FLAG_NO_RECORD,))

    assert window_from_summaries([good, bad]).days_compared == 1
    assert window_from_summaries([bad]).intervals_compared == 0
    assert window_from_summaries([bad]).wape_percent is None


def test_a_summary_row_records_the_facts_not_a_metric() -> None:
    """Sums, so the way they are reported can still change later."""
    scored = score_day(
        snapshot(NORMAL, [1.0] * 96, filled=[i < 4 for i in range(96)]),
        outcome(NORMAL, [0.8] * 96),
    )
    assert scored is not None
    row = summary_row(scored, interval_count=96, flags=())

    assert row["c"] == 96
    assert row["ps"] == pytest.approx(96.0)
    assert row["as"] == pytest.approx(76.8)
    assert row["ae"] == pytest.approx(19.2)
    assert row["fn"] == 4
    assert row["dt"] == "weekday"
    assert row["h"] == 0
    # MAE and WAPE are rebuilt from these, never stored.
    assert row["ae"] / row["c"] == pytest.approx(0.2)
    assert 100 * row["ae"] / row["as"] == pytest.approx(25.0)


# -- day-level reporting -----------------------------------------------------


def test_the_day_level_percentage_is_safe_because_the_day_is_not_zero() -> None:
    """Ninety-six intervals of household demand is a real denominator."""
    scored = score_day(snapshot(NORMAL, [0.125] * 96), outcome(NORMAL, [0.1] * 96))
    assert scored is not None
    facts = day_error_from_summary(summary_row(scored, interval_count=96, flags=()))

    assert facts is not None
    assert facts["signed_error_kwh"] == pytest.approx(2.4)
    assert facts["error_percent"] == pytest.approx(25.0)
    assert facts["intervals_compared"] == 96
    assert facts["intervals_in_day"] == 96


def test_no_day_level_figure_is_produced_for_a_flagged_day() -> None:
    """A number would be worse than nothing here."""
    assert (
        day_error_from_summary(
            summary_row(None, interval_count=96, flags=(FLAG_NO_RECORD,))
        )
        is None
    )
    assert day_error_from_summary(None) is None
    assert day_error_from_summary({}) is None


def test_a_day_that_measured_nothing_reports_no_percentage() -> None:
    """Zero actual, so no denominator -- but the signed error is still real."""
    scored = score_day(snapshot(NORMAL, [0.1] * 96), outcome(NORMAL, [0.0] * 96))
    assert scored is not None
    facts = day_error_from_summary(summary_row(scored, interval_count=96, flags=()))

    assert facts is not None
    assert facts["error_percent"] is None
    assert facts["signed_error_kwh"] == pytest.approx(9.6)
