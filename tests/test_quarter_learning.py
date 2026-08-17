"""Quarter-hour measurement: time-weighted integration and coverage rules.

These tests drive the accumulator with synthetic timelines rather than through
Home Assistant, so the measurement rules can be pinned down precisely: what
counts as covered, what a gap does, and what happens at a day or DST boundary.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.alpha_ems_manager.const import (
    MAX_SAMPLE_GAP_SECONDS,
    MIN_QUARTER_COVERAGE,
)
from custom_components.alpha_ems_manager.quarter import (
    QuarterAccumulator,
    QuarterResult,
    sanitize_load_w,
)

TZ = ZoneInfo("Europe/Amsterdam")


def drive(
    accumulator: QuarterAccumulator,
    start: datetime,
    samples: list[tuple[int, float | None]],
) -> list[QuarterResult]:
    """Feed ``(offset_seconds, value_w)`` pairs and collect closed quarters.

    Offsets step through *absolute* time. Adding a ``timedelta`` to an aware
    local datetime is wall-clock arithmetic in Python, which silently skips or
    repeats an hour across a DST transition, so the base is converted to UTC
    first. Home Assistant hands the coordinator real instants from
    ``dt_util.now()``, so this matches production rather than working around it.
    """
    base = start.astimezone(UTC)
    results: list[QuarterResult] = []
    for offset, value in samples:
        results.extend(accumulator.add_sample(base + timedelta(seconds=offset), value))
    return results


def steady(
    start: datetime, watts: float, seconds: int, step: int = 60
) -> list[tuple[int, float | None]]:
    """Return a constant-power sample train covering ``seconds``."""
    return [(offset, watts) for offset in range(0, seconds + 1, step)]


def test_one_kilowatt_for_fifteen_minutes_is_a_quarter_kilowatt_hour() -> None:
    """The headline case: 1 kW held for one quarter integrates to 0.25 kWh."""
    start = datetime(2026, 8, 17, 10, 0, tzinfo=TZ)
    accumulator = QuarterAccumulator(TZ)

    results = drive(accumulator, start, steady(start, 1000.0, 900))

    assert len(results) == 1
    quarter = results[0]
    assert quarter.energy_kwh == pytest.approx(0.25)
    assert quarter.coverage == pytest.approx(1.0)
    assert quarter.accepted
    assert quarter.slot == 40  # 10:00 -> 10 * 4
    assert quarter.day == start.date()


def test_power_is_time_weighted_not_averaged_over_samples() -> None:
    """Unevenly spaced changes weight by duration, not by sample count.

    2000 W for the first five minutes and 500 W for the remaining ten gives
    2000*(5/60) + 500*(10/60) = 250 Wh. A naive mean of the two readings would
    have said 1250 W -> 312.5 Wh, which is the bug this rules out.
    """
    start = datetime(2026, 8, 17, 10, 0, tzinfo=TZ)
    accumulator = QuarterAccumulator(TZ)

    samples: list[tuple[int, float | None]] = [(0, 2000.0)]
    samples += [(offset, 2000.0) for offset in range(60, 301, 60)]
    samples += [(offset, 500.0) for offset in range(300, 901, 60)]

    results = drive(accumulator, start, samples)

    assert len(results) == 1
    assert results[0].energy_kwh == pytest.approx(0.25, abs=1e-6)
    assert results[0].coverage == pytest.approx(1.0)


def test_many_changes_inside_one_quarter_integrate_correctly() -> None:
    """A rapidly changing signal still integrates to the true energy."""
    start = datetime(2026, 8, 17, 10, 0, tzinfo=TZ)
    accumulator = QuarterAccumulator(TZ)

    # Alternate 0 W and 2000 W every 30 seconds for the whole quarter. The
    # signal is held from each sample to the next, so exactly half the quarter
    # sits at 2000 W.
    samples: list[tuple[int, float | None]] = [
        (offset, 2000.0 if (offset // 30) % 2 == 0 else 0.0)
        for offset in range(0, 901, 30)
    ]
    results = drive(accumulator, start, samples)

    assert len(results) == 1
    assert results[0].energy_kwh == pytest.approx(0.25, abs=1e-6)


def test_a_short_gap_still_counts_as_covered() -> None:
    """A gap within the tolerance holds the last value and stays valid."""
    start = datetime(2026, 8, 17, 10, 0, tzinfo=TZ)
    accumulator = QuarterAccumulator(TZ)

    # One sample at the start, then silence for four minutes, then resume.
    samples: list[tuple[int, float | None]] = [(0, 1000.0), (240, 1000.0)]
    samples += [(offset, 1000.0) for offset in range(300, 901, 60)]

    results = drive(accumulator, start, samples)

    assert len(results) == 1
    assert results[0].coverage == pytest.approx(1.0)
    assert results[0].energy_kwh == pytest.approx(0.25)


def test_a_long_gap_contributes_no_energy_and_no_coverage() -> None:
    """Silence beyond the tolerance is missing data, not held consumption."""
    start = datetime(2026, 8, 17, 10, 0, tzinfo=TZ)
    accumulator = QuarterAccumulator(TZ)

    gap = MAX_SAMPLE_GAP_SECONDS + 120
    results = drive(
        accumulator,
        start,
        [(0, 1000.0), (gap, 1000.0)]
        + [(offset, 1000.0) for offset in range(gap + 60, 901, 60)],
    )

    assert len(results) == 1
    quarter = results[0]
    # The gap contributed nothing at all, so coverage is short by its length.
    assert quarter.coverage == pytest.approx((900 - gap) / 900, abs=1e-6)
    assert quarter.energy_kwh == pytest.approx((900 - gap) / 3600, abs=1e-6)


def test_missing_five_percent_is_still_accepted() -> None:
    """A brief outage does not disqualify an otherwise good quarter."""
    start = datetime(2026, 8, 17, 10, 0, tzinfo=TZ)
    accumulator = QuarterAccumulator(TZ)

    # Unavailable for 45 s (5 % of the quarter) right at the start.
    samples: list[tuple[int, float | None]] = [(0, None), (45, 1000.0)]
    samples += [(offset, 1000.0) for offset in range(105, 900, 60)]
    samples += [(900, 1000.0)]

    results = drive(accumulator, start, samples)

    assert len(results) == 1
    assert results[0].coverage == pytest.approx(0.95, abs=1e-6)
    assert results[0].accepted


def test_missing_too_much_is_rejected() -> None:
    """A quarter that lost most of its coverage is not learned."""
    start = datetime(2026, 8, 17, 10, 0, tzinfo=TZ)
    accumulator = QuarterAccumulator(TZ)

    # Unavailable for the first ten minutes, live for the last five.
    samples: list[tuple[int, float | None]] = [(0, None), (600, 1000.0)]
    samples += [(offset, 1000.0) for offset in range(660, 901, 60)]

    results = drive(accumulator, start, samples)

    assert len(results) == 1
    assert results[0].coverage == pytest.approx(300 / 900, abs=1e-6)
    assert not results[0].accepted


def test_unavailable_never_becomes_zero_consumption() -> None:
    """An entirely unavailable quarter yields no energy and no coverage.

    Critically it is also *rejected*, so it cannot be stored as a genuine
    zero-consumption quarter.
    """
    start = datetime(2026, 8, 17, 10, 0, tzinfo=TZ)
    accumulator = QuarterAccumulator(TZ)

    results = drive(
        accumulator, start, [(offset, None) for offset in range(0, 901, 60)]
    )

    assert len(results) == 1
    assert results[0].energy_kwh == 0.0
    assert results[0].coverage == 0.0
    assert not results[0].accepted


def test_a_quarter_joined_late_cannot_be_accepted() -> None:
    """Coverage is measured against the whole quarter, not the observed part.

    Starting mid-quarter -- which is what happens on every restart -- must not
    produce a full-looking bucket from a few minutes of data.
    """
    start = datetime(2026, 8, 17, 10, 10, tzinfo=TZ)  # 5 minutes before the close
    accumulator = QuarterAccumulator(TZ)

    results = drive(accumulator, start, steady(start, 1000.0, 300))

    assert len(results) == 1
    assert results[0].coverage == pytest.approx(300 / 900, abs=1e-6)
    assert not results[0].accepted
    assert results[0].coverage < MIN_QUARTER_COVERAGE


def test_several_quarters_close_in_order() -> None:
    """A long steady run closes one accepted bucket per quarter."""
    start = datetime(2026, 8, 17, 10, 0, tzinfo=TZ)
    accumulator = QuarterAccumulator(TZ)

    results = drive(accumulator, start, steady(start, 800.0, 3600))

    assert len(results) == 4
    assert [q.slot for q in results] == [40, 41, 42, 43]
    assert all(q.accepted for q in results)
    assert all(q.energy_kwh == pytest.approx(0.2) for q in results)


def test_midnight_boundary_splits_days_correctly() -> None:
    """Quarters either side of midnight are attributed to their own dates."""
    start = datetime(2026, 8, 17, 23, 45, tzinfo=TZ)
    accumulator = QuarterAccumulator(TZ)

    results = drive(accumulator, start, steady(start, 1200.0, 1800))

    assert len(results) == 2
    assert results[0].day == date(2026, 8, 17)
    assert results[0].slot == 95
    assert results[1].day == date(2026, 8, 18)
    assert results[1].slot == 0
    assert all(q.energy_kwh == pytest.approx(0.3) for q in results)


def test_out_of_order_sample_does_not_corrupt_the_bucket() -> None:
    """A late-arriving timestamp is ignored rather than rewinding the cursor."""
    start = datetime(2026, 8, 17, 10, 0, tzinfo=TZ)
    accumulator = QuarterAccumulator(TZ)

    drive(accumulator, start, steady(start, 1000.0, 300))
    coverage_before = accumulator.open_coverage
    energy_before = accumulator.open_energy_kwh

    # A sample stamped two minutes in the past.
    accumulator.add_sample(start + timedelta(seconds=180), 9999.0)

    assert accumulator.open_coverage == pytest.approx(coverage_before)
    assert accumulator.open_energy_kwh == pytest.approx(energy_before)


def test_reset_discards_the_open_quarter() -> None:
    """A reload drops the in-flight bucket rather than guessing its remainder."""
    start = datetime(2026, 8, 17, 10, 0, tzinfo=TZ)
    accumulator = QuarterAccumulator(TZ)
    drive(accumulator, start, steady(start, 1000.0, 300))

    assert accumulator.started
    accumulator.reset()

    assert not accumulator.started
    assert accumulator.open_energy_kwh == 0.0
    assert accumulator.open_coverage == 0.0


def test_downtime_across_quarters_fabricates_nothing() -> None:
    """A multi-hour outage produces rejected quarters, not invented energy."""
    start = datetime(2026, 8, 17, 10, 0, tzinfo=TZ)
    accumulator = QuarterAccumulator(TZ)

    results = drive(accumulator, start, [(0, 1000.0), (7200, 1000.0)])

    # Eight boundaries elapsed; none of them may claim real consumption.
    assert len(results) == 8
    assert all(not q.accepted for q in results)
    assert sum(q.energy_kwh for q in results) == pytest.approx(0.0)


# -- DST --------------------------------------------------------------------


def test_spring_forward_skips_the_lost_slots() -> None:
    """On a 23-hour day the vanished wall-clock hour is simply never observed."""
    start = datetime(2026, 3, 29, 1, 45, tzinfo=TZ)
    accumulator = QuarterAccumulator(TZ)

    results = drive(accumulator, start, steady(start, 1000.0, 1800))

    assert len(results) == 2
    # 01:45 is slot 7; the clock then jumps to 03:00, which is slot 12.
    assert [q.slot for q in results] == [7, 12]
    assert all(q.energy_kwh == pytest.approx(0.25) for q in results)


def test_fall_back_observes_the_repeated_hour_twice() -> None:
    """On a 25-hour day the repeated wall-clock slots appear twice."""
    start = datetime(2026, 10, 25, 1, 45, tzinfo=TZ, fold=0)
    accumulator = QuarterAccumulator(TZ)

    # Two and a quarter hours of absolute time spans the repeated 02:00 hour.
    results = drive(accumulator, start, steady(start, 1000.0, 8100))

    slots = [q.slot for q in results]
    # Slots 8..11 (02:00-02:59) occur once before the fold and once after.
    assert slots.count(8) == 2
    assert slots.count(9) == 2
    # Every quarter is a genuine 15 minutes of absolute time.
    assert all(q.energy_kwh == pytest.approx(0.25) for q in results)
    assert all(q.day == date(2026, 10, 25) for q in results)


# -- plausibility -----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0.0, 0.0),
        (1500.0, 1500.0),
        (-20.0, 0.0),  # noise around zero is clamped up
        (-5000.0, None),  # implausibly negative for a house-load sensor
        (999_999.0, None),  # implausibly large
        (None, None),
    ],
)
def test_implausible_readings_are_filtered(raw, expected) -> None:
    """Obvious sensor glitches become missing data, not extreme load."""
    assert sanitize_load_w(raw) == expected
