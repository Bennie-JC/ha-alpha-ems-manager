"""Per-interval fill provenance on ``DayForecast``.

``filled_intervals`` says how many intervals were extrapolated from a
neighbour. It cannot say *which*, and ``_fill_unknown_intervals`` overwrites the
values in place, so at the moment of filling the provenance is destroyed: a
filled interval and a modelled one are afterwards indistinguishable in
``intervals``.

A later phase comparing forecast error on modelled versus extrapolated intervals
needs that distinction, and needs it captured at issuance. It cannot be
recovered from a published forecast, and rebuilding it by re-running the blend
elsewhere would put a second copy of the model in the codebase.

These tests pin the mask's identity and, just as importantly, prove the addition
moved no forecast number.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from custom_components.alpha_ems_manager.forecast import build_forecast
from custom_components.alpha_ems_manager.storage import DayRecord

from .conftest import TZ
from .synthetic import empty_day, flat_day

REFERENCE = date(2026, 8, 19)
SPRING_FORWARD = date(2026, 3, 29)
FALL_BACK = date(2026, 10, 25)


def gapped_history(
    reference: date, *, blank_before: int, days: int = 5
) -> list[DayRecord]:
    """Return days whose first ``blank_before`` intervals are never valid.

    This is the real shape the filler exists for: a flexible-load sensor that
    drops out overnight invalidates the same early slots on every day, so those
    slots never accumulate enough observations to blend.
    """
    records: list[DayRecord] = []
    for offset in range(1, days + 1):
        target = reference - timedelta(days=offset)
        record = empty_day(target)
        for index in range(blank_before, record.interval_count):
            record.record_interval(
                index, measured_kwh=0.12, ev_kwh=None, ev_expected=False
            )
        records.append(record)
    return records


# -- identity ----------------------------------------------------------------


def test_the_mask_names_exactly_the_intervals_that_were_filled() -> None:
    """Intervals 0-7 have no observations of their own slot, so only they fill."""
    records = gapped_history(REFERENCE, blank_before=8)

    forecast = build_forecast(records, REFERENCE, REFERENCE + timedelta(days=1), TZ)

    assert forecast.available is True
    assert [index for index, flag in enumerate(forecast.filled) if flag] == list(
        range(8)
    )


def test_the_mask_agrees_with_the_count_on_a_published_forecast() -> None:
    """``sum(filled)`` and ``filled_intervals`` may never drift apart."""
    records = gapped_history(REFERENCE, blank_before=8)

    forecast = build_forecast(records, REFERENCE, REFERENCE + timedelta(days=1), TZ)

    assert sum(forecast.filled) == forecast.filled_intervals == 8
    assert forecast.modelled_intervals == 88


def test_a_fully_modelled_day_has_an_all_false_mask() -> None:
    """Nothing was extrapolated, and the mask must say so rather than be empty."""
    records = [flat_day(REFERENCE - timedelta(days=n), 12.0) for n in range(1, 6)]

    forecast = build_forecast(records, REFERENCE, REFERENCE + timedelta(days=1), TZ)

    assert forecast.filled == [False] * 96
    assert sum(forecast.filled) == forecast.filled_intervals == 0


def test_the_mask_is_always_as_long_as_the_day() -> None:
    """A short or absent mask would silently mis-align with the interval list."""
    records = gapped_history(REFERENCE, blank_before=8)

    forecast = build_forecast(records, REFERENCE, REFERENCE + timedelta(days=1), TZ)

    assert len(forecast.filled) == len(forecast.intervals) == forecast.interval_count


def test_a_withheld_forecast_reports_no_filled_intervals() -> None:
    """Nothing was filled, because the fill step never ran.

    ``filled_intervals`` still counts what *would* have needed filling. The two
    fields answer different questions on a withheld day and are expected to
    differ; the mask must not claim work that did not happen.
    """
    # Only the evening was ever observed, so the day is far below the
    # completeness gate and the forecast is withheld before filling.
    records = gapped_history(REFERENCE, blank_before=77)

    forecast = build_forecast(records, REFERENCE, REFERENCE + timedelta(days=1), TZ)

    assert forecast.available is False
    assert any(forecast.filled) is False
    assert forecast.filled_intervals > 0


def test_a_leading_gap_inherits_the_first_known_interval() -> None:
    """The mask must not disagree with where the value actually came from."""
    records = gapped_history(REFERENCE, blank_before=8)

    forecast = build_forecast(records, REFERENCE, REFERENCE + timedelta(days=1), TZ)

    first_known = forecast.intervals[8]
    assert all(forecast.filled[index] for index in range(8))
    assert all(forecast.intervals[index] == first_known for index in range(8))


# -- daylight saving ---------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "length"),
    [(SPRING_FORWARD, 92), (date(2026, 8, 20), 96), (FALL_BACK, 100)],
)
def test_the_mask_is_sized_to_the_real_civil_day(target: date, length: int) -> None:
    """92 and 100 are as real as 96, and the mask must match the day it labels."""
    reference = target - timedelta(days=1)
    records = [flat_day(reference - timedelta(days=n), 12.0) for n in range(1, 6)]

    forecast = build_forecast(records, reference, target, TZ)

    assert forecast.interval_count == length
    assert len(forecast.filled) == length


def test_the_repeated_fall_back_hour_is_modelled_not_filled() -> None:
    """Both passes read the same behavioural slot, which has observations."""
    reference = FALL_BACK - timedelta(days=1)
    records = [flat_day(reference - timedelta(days=n), 12.0) for n in range(1, 6)]

    forecast = build_forecast(records, reference, FALL_BACK, TZ)

    assert forecast.interval_count == 100
    # Chronological intervals 8-15 are the two passes of 02:00-02:59.
    assert not any(forecast.filled[8:16])
    assert sum(forecast.filled) == forecast.filled_intervals


def test_the_spring_forward_day_has_no_mask_entry_for_the_missing_hour() -> None:
    """A 92-interval day simply has no index for a wall clock that never ran."""
    reference = SPRING_FORWARD - timedelta(days=1)
    records = [flat_day(reference - timedelta(days=n), 12.0) for n in range(1, 6)]

    forecast = build_forecast(records, reference, SPRING_FORWARD, TZ)

    assert len(forecast.filled) == 92
    assert sum(forecast.filled) == forecast.filled_intervals


# -- the addition changed no forecast number ---------------------------------


def test_forecast_totals_are_unchanged_by_the_addition() -> None:
    """The mask is provenance only; it may not touch a single predicted value.

    Each figure is a whole-day total over a fully specified history, so any
    drift in blending, weighting, pooling, filling or day length moves one of
    them. 2026-08-20 is a Thursday and 2026-08-22 a Saturday.
    """
    flat = [flat_day(REFERENCE - timedelta(days=n), 12.0) for n in range(1, 6)]

    weekday = build_forecast(flat, REFERENCE, date(2026, 8, 20), TZ)
    weekend = build_forecast(flat, REFERENCE, date(2026, 8, 22), TZ)
    gapped = build_forecast(
        gapped_history(REFERENCE, blank_before=8),
        REFERENCE,
        date(2026, 8, 20),
        TZ,
    )

    fb_reference = FALL_BACK - timedelta(days=1)
    fall_back = build_forecast(
        [flat_day(fb_reference - timedelta(days=n), 12.0) for n in range(1, 6)],
        fb_reference,
        FALL_BACK,
        TZ,
    )
    sf_reference = SPRING_FORWARD - timedelta(days=1)
    spring_forward = build_forecast(
        [flat_day(sf_reference - timedelta(days=n), 12.0) for n in range(1, 6)],
        sf_reference,
        SPRING_FORWARD,
        TZ,
    )

    assert weekday.total_kwh == pytest.approx(12.0)
    assert weekend.total_kwh == pytest.approx(12.0)
    # 96 intervals at the modelled 0.12 kWh rate: the eight filled ones inherit
    # the same rate, so the day still totals 11.52 rather than being scaled.
    assert gapped.total_kwh == pytest.approx(11.52)
    # A 100-interval day forecast from 96-interval history repeats an hour.
    assert fall_back.total_kwh == pytest.approx(12.5)
    # A 92-interval day drops one.
    assert spring_forward.total_kwh == pytest.approx(11.5)


def test_filling_still_copies_the_nearest_neighbour_not_the_daily_mean() -> None:
    """The honesty rule the filler exists for is untouched by the mask.

    Deliberately shaped rather than flat: with a flat profile the neighbour and
    the whole-day mean are the same number, so a flat fixture would pass whether
    the filler reached for one or the other.
    """
    records: list[DayRecord] = []
    for offset in range(1, 6):
        day = REFERENCE - timedelta(days=offset)
        record = empty_day(day)
        for index in range(8, record.interval_count):
            # A quiet night and a heavy evening, so the day mean sits far above
            # the early-morning rate the leading gap must inherit.
            watts = 2.0 if index >= 72 else 0.05
            record.record_interval(
                index, measured_kwh=watts, ev_kwh=None, ev_expected=False
            )
        records.append(record)

    forecast = build_forecast(records, REFERENCE, REFERENCE + timedelta(days=1), TZ)

    assert forecast.available is True
    assert forecast.total_kwh is not None
    day_mean = forecast.total_kwh / forecast.interval_count

    # The leading gap inherits interval 8, not the average of the whole day.
    assert forecast.intervals[0] == pytest.approx(0.05)
    assert forecast.intervals[0] == pytest.approx(forecast.intervals[8])
    assert day_mean > 0.4
    assert forecast.intervals[0] != pytest.approx(day_mean)
    assert all(forecast.filled[index] for index in range(8))
