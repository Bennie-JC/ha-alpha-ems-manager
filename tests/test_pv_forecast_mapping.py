"""Mapping a Solcast series onto the chronological interval identity.

The part most likely to be quietly wrong, so it is tested against a real
response. :data:`LIVE_ROWS` is the verbatim reply from the live account for
2026-08-21 10:00-12:00 UTC, four rows carrying an explicit ``+02:00`` offset.
Working from a real response rather than a hand-written one is what pins the
three traps: the figure is average power in kW and not interval energy, the
timestamp carries an offset and is not UTC, and the requested range is half-open.

The flatness of those four values is itself evidence, incidentally: 2.2661 down to
2.2585 across two hours is what two opposed arrays trading off looks like, which
is independent corroboration that the aggregate really sums an east-west pair.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.alpha_ems_manager.const import (
    PV_AGGREGATE_SITE,
    PV_PERCENTILE_COMONOTONIC_SUM,
    PV_PERCENTILE_SOURCE_AGGREGATE,
    PV_QUERY_MODE_AGGREGATE,
    PV_QUERY_MODE_PER_SITE,
    PV_UNAVAILABLE_NO_ROWS,
    PV_UNAVAILABLE_PERIOD_REFUSED,
    PV_UNAVAILABLE_UNUSABLE_ROWS,
)
from custom_components.alpha_ems_manager.pv_forecast import (
    PvForecast,
    PvProvenance,
    PvSite,
    build_forecast,
    sites_identity,
    sites_model,
)
from custom_components.alpha_ems_manager.storage import (
    expected_quarters_for,
    index_for_start_utc,
    local_slot_for_index,
    utc_midnight,
)

TZ_KEY = "Europe/Amsterdam"
TZ = ZoneInfo(TZ_KEY)

TARGET = date(2026, 8, 21)
SPRING_FORWARD = date(2026, 3, 29)
FALL_BACK = date(2026, 10, 25)

#: A quarter-hour, in hours. Local so the arithmetic below is readable.
QH = 0.25

#: The live response, verbatim.
LIVE_ROWS: tuple[dict[str, object], ...] = (
    {
        "period_start": datetime(2026, 8, 21, 12, 0, tzinfo=TZ),
        "pv_estimate": 2.2661,
        "pv_estimate10": 1.2923,
        "pv_estimate90": 2.9411,
    },
    {
        "period_start": datetime(2026, 8, 21, 12, 30, tzinfo=TZ),
        "pv_estimate": 2.2663,
        "pv_estimate10": 1.2711,
        "pv_estimate90": 2.9041,
    },
    {
        "period_start": datetime(2026, 8, 21, 13, 0, tzinfo=TZ),
        "pv_estimate": 2.2618,
        "pv_estimate10": 1.2764,
        "pv_estimate90": 2.8622,
    },
    {
        "period_start": datetime(2026, 8, 21, 13, 30, tzinfo=TZ),
        "pv_estimate": 2.2585,
        "pv_estimate10": 1.3081,
        "pv_estimate90": 2.7975,
    },
)


def resolver(day: date, tz_key: str = TZ_KEY):
    """Return an index resolver for one civil day.

    Built here rather than imported into the pure module, which deliberately does
    not reach into storage at all.
    """
    tz = ZoneInfo(tz_key)

    def index_of(start: datetime) -> int | None:
        return index_for_start_utc(day, start, tz)

    return index_of


def rows_from(
    start_local: datetime,
    *,
    kw: float,
    count: int,
    period_minutes: int = 30,
    kw10: float | None = None,
    kw90: float | None = None,
) -> list[dict[str, object]]:
    """Return a synthetic series of ``count`` periods of constant power."""
    return [
        {
            "period_start": start_local + timedelta(minutes=period_minutes * step),
            "pv_estimate": kw,
            "pv_estimate10": kw if kw10 is None else kw10,
            "pv_estimate90": kw if kw90 is None else kw90,
        }
        for step in range(count)
    ]


def build(
    rows,
    *,
    day: date = TARGET,
    tz_key: str = TZ_KEY,
    provenance: PvProvenance | None = None,
) -> PvForecast:
    """Map one aggregate series for ``day``."""
    return build_forecast(
        [(PV_AGGREGATE_SITE, rows)],
        target_day=day,
        tz_key=tz_key,
        interval_count=expected_quarters_for(day, ZoneInfo(tz_key)),
        index_of=resolver(day, tz_key),
        provenance=provenance,
    )


# -- the live response, mapped by hand ---------------------------------------


def test_the_live_response_maps_to_eight_hand_computed_quarters() -> None:
    """Four thirty-minute rows become eight quarters, each kW times a quarter-hour.

    10:00 UTC on this day is 12:00 local, which is chronological index 48.
    """
    forecast = build(LIVE_ROWS)

    assert forecast.available is True
    assert forecast.mapping.period_minutes == 30
    assert forecast.mapping.rows_received == 4
    assert forecast.mapping.rows_mapped == 4

    expected = {
        48: 2.2661,
        49: 2.2661,
        50: 2.2663,
        51: 2.2663,
        52: 2.2618,
        53: 2.2618,
        54: 2.2585,
        55: 2.2585,
    }
    for index, kw in expected.items():
        assert forecast.intervals[index] == pytest.approx(kw * QH), index

    # And nothing outside that window was invented.
    assert forecast.forecast_intervals == 8
    assert forecast.intervals[47] is None
    assert forecast.intervals[56] is None


def test_the_figure_is_power_and_not_interval_energy() -> None:
    """Reading it as energy would double every number on a thirty-minute source.

    Stated as an absolute rather than a ratio, so a change that scaled both sides
    could not keep it passing.
    """
    forecast = build(LIVE_ROWS)

    assert forecast.intervals[48] == pytest.approx(2.2661 * 0.25)
    assert forecast.intervals[48] != pytest.approx(2.2661)
    # The whole two-hour window: 2 h of roughly 2.26 kW is roughly 4.5 kWh.
    assert forecast.total_kwh == pytest.approx(4.5264, abs=1e-4)


def test_the_derived_power_recovers_the_source_figure() -> None:
    """Energy is stored and power derived, so the two cannot drift apart."""
    forecast = build(LIVE_ROWS)

    assert forecast.power_kw_at(48) == pytest.approx(2.2661)
    assert forecast.power_kw_at(55) == pytest.approx(2.2585)
    assert forecast.power_kw_at(47) is None


def test_the_offset_is_honoured_rather_than_read_as_utc() -> None:
    """Reading ``12:00+02:00`` as ``12:00Z`` would shift the day by two hours.

    Index 48 is noon local. If the offset were ignored the same rows would land at
    index 56, which is what this pins.
    """
    forecast = build(LIVE_ROWS)

    assert forecast.intervals[48] is not None
    assert forecast.intervals[56] is None


def test_percentile_bands_land_on_the_same_index() -> None:
    """P10 and P90 are mapped, not derived from the median."""
    forecast = build(LIVE_ROWS)

    assert forecast.p10[48] == pytest.approx(1.2923 * QH)
    assert forecast.p90[48] == pytest.approx(2.9411 * QH)
    assert forecast.p10[48] < forecast.intervals[48] < forecast.p90[48]


def test_the_bands_are_retained_at_interval_resolution() -> None:
    """Not daily scalars: a day that has passed cannot be back-filled."""
    forecast = build(LIVE_ROWS)

    assert len([value for value in forecast.p10 if value is not None]) == 8
    assert forecast.total_p10_kwh == pytest.approx(
        sum(row["pv_estimate10"] for row in LIVE_ROWS) * 2 * QH, abs=1e-4
    )


# -- energy conservation and period length -----------------------------------


@pytest.mark.parametrize("period_minutes", [15, 30, 45, 60])
def test_energy_is_conserved_for_every_supported_period(period_minutes: int) -> None:
    """The quarters a period spans sum to exactly the period's energy.

    Swept rather than asserted for thirty minutes alone, because every row this
    project has ever seen was thirty minutes -- which is exactly why assuming it
    would be untestable.
    """
    rows = rows_from(
        datetime(2026, 8, 21, 10, 0, tzinfo=TZ),
        kw=4.0,
        count=2,
        period_minutes=period_minutes,
    )

    forecast = build(rows)
    hours = period_minutes / 60.0

    assert forecast.mapping.period_minutes == period_minutes
    assert forecast.total_kwh == pytest.approx(4.0 * hours * 2)
    assert forecast.forecast_intervals == 2 * (period_minutes // 15)


def test_a_period_is_split_piecewise_constant_and_never_interpolated() -> None:
    """Both quarters of a period carry the same power, not a ramp between rows."""
    rows = [
        {
            "period_start": datetime(2026, 8, 21, 10, 0, tzinfo=TZ),
            "pv_estimate": 1.0,
        },
        {
            "period_start": datetime(2026, 8, 21, 10, 30, tzinfo=TZ),
            "pv_estimate": 3.0,
        },
    ]

    forecast = build(rows)

    assert forecast.intervals[40] == forecast.intervals[41] == pytest.approx(0.25)
    assert forecast.intervals[42] == forecast.intervals[43] == pytest.approx(0.75)


@pytest.mark.parametrize("period_minutes", [10, 20, 25, 7, 40])
def test_a_period_that_is_not_a_whole_quarter_is_refused_and_reported(
    period_minutes: int,
) -> None:
    """There is no honest way to place it, so it is not placed."""
    rows = rows_from(
        datetime(2026, 8, 21, 10, 0, tzinfo=TZ),
        kw=2.0,
        count=4,
        period_minutes=period_minutes,
    )

    forecast = build(rows)

    assert forecast.available is False
    assert forecast.unavailable_reason == PV_UNAVAILABLE_PERIOD_REFUSED
    assert forecast.mapping.periods_refused == 4
    assert forecast.mapping.period_minutes == period_minutes
    # And nothing was stored: not a partial series, not zeros.
    assert set(forecast.intervals) == {None}


def test_a_single_row_has_no_measurable_period_and_is_refused() -> None:
    """One row gives no gap to measure, and a guessed period is a guess."""
    forecast = build(
        rows_from(datetime(2026, 8, 21, 10, 0, tzinfo=TZ), kw=2.0, count=1)
    )

    assert forecast.available is False
    assert forecast.unavailable_reason == PV_UNAVAILABLE_PERIOD_REFUSED


def test_a_mixed_period_series_uses_the_modal_length_and_reports_both() -> None:
    """Reported rather than silently accepted, so a resolution change is visible."""
    rows = [
        {"period_start": datetime(2026, 8, 21, 10, 0, tzinfo=TZ), "pv_estimate": 1.0},
        {"period_start": datetime(2026, 8, 21, 10, 30, tzinfo=TZ), "pv_estimate": 1.0},
        {"period_start": datetime(2026, 8, 21, 11, 0, tzinfo=TZ), "pv_estimate": 1.0},
        {"period_start": datetime(2026, 8, 21, 11, 15, tzinfo=TZ), "pv_estimate": 1.0},
    ]

    forecast = build(rows)

    assert forecast.mapping.period_minutes == 30
    assert forecast.mapping.period_minutes_observed == (15, 30)


# -- missing, malformed, duplicated, out of range ----------------------------


def test_a_missing_interval_is_none_and_never_zero() -> None:
    """Zero is a forecast of no generation. Missing is the absence of a forecast.

    A hole must also not widen the rows around it. The modal period here is
    unambiguously thirty minutes, so the row before the gap covers its own thirty
    minutes and stops -- rather than being stretched across the hour to the next
    row, which would fabricate generation for the two intervals the source
    declined to describe.
    """
    rows = [
        {"period_start": datetime(2026, 8, 21, 10, 0, tzinfo=TZ), "pv_estimate": 2.0},
        {"period_start": datetime(2026, 8, 21, 10, 30, tzinfo=TZ), "pv_estimate": 2.0},
        # 11:00 deliberately absent.
        {"period_start": datetime(2026, 8, 21, 11, 30, tzinfo=TZ), "pv_estimate": 2.0},
        {"period_start": datetime(2026, 8, 21, 12, 0, tzinfo=TZ), "pv_estimate": 2.0},
    ]

    forecast = build(rows)

    assert forecast.mapping.period_minutes == 30
    assert forecast.intervals[40] == pytest.approx(0.5)
    assert forecast.intervals[43] == pytest.approx(0.5)
    assert forecast.intervals[44] is None
    assert forecast.intervals[45] is None
    assert forecast.intervals[46] == pytest.approx(0.5)


def test_two_rows_an_hour_apart_are_read_as_hourly_and_report_it() -> None:
    """The honest limit of measuring a period from the data.

    Two rows sixty minutes apart are indistinguishable from a half-hourly series
    with one row missing. There is no way to tell from the response, so the
    measured period is reported as sixty and the reader can see what was assumed.
    Reporting it is the whole reason the period is measured rather than hard-coded.
    """
    rows = [
        {"period_start": datetime(2026, 8, 21, 10, 0, tzinfo=TZ), "pv_estimate": 2.0},
        {"period_start": datetime(2026, 8, 21, 11, 0, tzinfo=TZ), "pv_estimate": 2.0},
    ]

    forecast = build(rows)

    assert forecast.mapping.period_minutes == 60
    assert forecast.mapping.period_minutes_observed == (60,)
    assert forecast.forecast_intervals == 8


@pytest.mark.parametrize(
    "bad",
    [
        {"pv_estimate": 2.0},
        {"period_start": "2026-08-21T12:00:00+02:00", "pv_estimate": 2.0},
        {"period_start": datetime(2026, 8, 21, 12, 0), "pv_estimate": 2.0},
        {"period_start": datetime(2026, 8, 21, 12, 0, tzinfo=TZ)},
        {"period_start": datetime(2026, 8, 21, 12, 0, tzinfo=TZ), "pv_estimate": None},
        {"period_start": datetime(2026, 8, 21, 12, 0, tzinfo=TZ), "pv_estimate": "2.0"},
        {
            "period_start": datetime(2026, 8, 21, 12, 0, tzinfo=TZ),
            "pv_estimate": float("nan"),
        },
        {
            "period_start": datetime(2026, 8, 21, 12, 0, tzinfo=TZ),
            "pv_estimate": float("inf"),
        },
        {"period_start": datetime(2026, 8, 21, 12, 0, tzinfo=TZ), "pv_estimate": -1.0},
        "not a mapping",
    ],
)
def test_a_malformed_row_is_counted_and_dropped(bad: object) -> None:
    """Refused, never coerced.

    A naive timestamp is in here deliberately: assuming it is UTC or local shifts
    the whole day, and the live source always sends an explicit offset, so a naive
    one means the contract changed.
    """
    rows = [
        bad,
        {"period_start": datetime(2026, 8, 21, 13, 0, tzinfo=TZ), "pv_estimate": 2.0},
        {"period_start": datetime(2026, 8, 21, 13, 30, tzinfo=TZ), "pv_estimate": 2.0},
    ]

    forecast = build(rows)

    assert forecast.mapping.rows_malformed == 1
    assert forecast.mapping.rows_mapped == 2
    assert forecast.available is True


def test_every_row_malformed_is_unavailable_rather_than_empty() -> None:
    """An empty series must never be published as a forecast of nothing."""
    forecast = build([{"pv_estimate": 1.0}, {"pv_estimate": 2.0}])

    assert forecast.available is False
    assert forecast.unavailable_reason in {
        PV_UNAVAILABLE_UNUSABLE_ROWS,
        PV_UNAVAILABLE_PERIOD_REFUSED,
    }


def test_no_rows_at_all_is_unavailable_with_its_own_reason() -> None:
    """Distinguished from unusable rows: a quiet source is not a broken one."""
    forecast = build([])

    assert forecast.available is False
    assert forecast.unavailable_reason == PV_UNAVAILABLE_NO_ROWS
    assert len(forecast.intervals) == 96
    assert set(forecast.intervals) == {None}


def test_a_duplicate_period_start_keeps_the_first_and_counts_it() -> None:
    """Deterministic regardless of iteration order, and asserted rather than hoped."""
    rows = [
        {"period_start": datetime(2026, 8, 21, 10, 0, tzinfo=TZ), "pv_estimate": 1.0},
        {"period_start": datetime(2026, 8, 21, 10, 0, tzinfo=TZ), "pv_estimate": 9.0},
        {"period_start": datetime(2026, 8, 21, 10, 30, tzinfo=TZ), "pv_estimate": 1.0},
    ]

    forecast = build(rows)

    assert forecast.mapping.rows_duplicated == 1
    assert forecast.intervals[40] == pytest.approx(0.25)


def test_the_same_rows_in_any_order_produce_the_same_series() -> None:
    """Determinism, stated as a property rather than as one example."""
    forward = build(list(LIVE_ROWS))
    backward = build(list(reversed(LIVE_ROWS)))

    assert forward.intervals == backward.intervals
    assert forward.p10 == backward.p10


def test_rows_outside_the_requested_day_are_counted_not_dropped_silently() -> None:
    """A row belonging to another day is a fact about the response, not noise."""
    rows = [
        {"period_start": datetime(2026, 8, 20, 10, 0, tzinfo=TZ), "pv_estimate": 2.0},
        {"period_start": datetime(2026, 8, 20, 10, 30, tzinfo=TZ), "pv_estimate": 2.0},
    ]

    forecast = build(rows)

    assert forecast.mapping.rows_out_of_range > 0
    assert forecast.available is False


def test_a_non_monotonic_response_is_reported() -> None:
    """Sorted before mapping, but the disorder itself is worth recording."""
    rows = [
        {"period_start": datetime(2026, 8, 21, 11, 0, tzinfo=TZ), "pv_estimate": 1.0},
        {"period_start": datetime(2026, 8, 21, 10, 0, tzinfo=TZ), "pv_estimate": 2.0},
        {"period_start": datetime(2026, 8, 21, 10, 30, tzinfo=TZ), "pv_estimate": 3.0},
    ]

    forecast = build(rows)

    assert forecast.mapping.rows_non_monotonic == 1
    # Sorted, so the 10:00 row still lands at index 40.
    assert forecast.intervals[40] == pytest.approx(0.5)


def test_a_negative_percentile_leaves_that_band_unknown() -> None:
    """Not a small zero: generation cannot be negative, so the field is unusable."""
    rows = [
        {
            "period_start": datetime(2026, 8, 21, 10, 0, tzinfo=TZ),
            "pv_estimate": 2.0,
            "pv_estimate10": -0.5,
            "pv_estimate90": 3.0,
        },
        {
            "period_start": datetime(2026, 8, 21, 10, 30, tzinfo=TZ),
            "pv_estimate": 2.0,
            "pv_estimate10": 1.0,
            "pv_estimate90": 3.0,
        },
    ]

    forecast = build(rows)

    assert forecast.intervals[40] == pytest.approx(0.5)
    assert forecast.p10[40] is None
    assert forecast.p90[40] == pytest.approx(0.75)


# -- daylight savings --------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "count"), [(SPRING_FORWARD, 92), (TARGET, 96), (FALL_BACK, 100)]
)
def test_the_series_matches_the_real_length_of_the_civil_day(
    day: date, count: int
) -> None:
    """92, 96 or 100, without a special case anywhere in the mapping."""
    start = utc_midnight(day, TZ) + timedelta(hours=10)
    rows = [
        {"period_start": start, "pv_estimate": 2.0},
        {"period_start": start + timedelta(minutes=30), "pv_estimate": 2.0},
    ]

    forecast = build(rows, day=day)

    assert forecast.interval_count == count
    assert len(forecast.intervals) == count
    assert forecast.forecast_intervals == 4


def test_the_repeated_fall_back_hour_produces_two_distinct_indices() -> None:
    """The case that a wall-clock-keyed design silently overwrites.

    On 25 October the local hour 02:00-02:59 happens twice, as two different
    instants. Mapping is by instant, so the two land at different indices and both
    survive -- with deliberately different values, so a collapse would be visible.
    """
    # Local 02:00 happens twice on this day. The first occurrence is 00:00 UTC and
    # the second is 01:00 UTC -- two distinct instants sharing a wall clock.
    first = utc_midnight(FALL_BACK, TZ) + timedelta(hours=2)
    rows = [
        {"period_start": first, "pv_estimate": 1.0},
        {"period_start": first + timedelta(minutes=30), "pv_estimate": 1.0},
        {"period_start": first + timedelta(hours=1), "pv_estimate": 5.0},
        {"period_start": first + timedelta(hours=1, minutes=30), "pv_estimate": 5.0},
    ]

    forecast = build(rows, day=FALL_BACK)

    early = [
        index
        for index, value in enumerate(forecast.intervals)
        if value == pytest.approx(0.25)
    ]
    late = [
        index
        for index, value in enumerate(forecast.intervals)
        if value == pytest.approx(1.25)
    ]

    # Two hours of rows at each power, so four quarters each, and no overlap.
    assert len(early) == len(late) == 4
    assert set(early).isdisjoint(late)
    assert forecast.forecast_intervals == 8

    # The load-bearing part: the two runs occupy *different* chronological
    # indices while sharing the same wall-clock slot. A design keyed on the slot
    # alone would have written the second occurrence over the first and lost an
    # hour of generation from the day.
    assert local_slot_for_index(FALL_BACK, early[0], TZ) == local_slot_for_index(
        FALL_BACK, late[0], TZ
    )
    assert early[0] != late[0]


def test_the_spring_forward_gap_simply_has_no_rows() -> None:
    """No special case: the missing hour has no instants, so nothing maps into it."""
    start = utc_midnight(SPRING_FORWARD, TZ)
    rows = [
        {"period_start": start + timedelta(hours=h), "pv_estimate": 2.0}
        for h in range(4)
    ]

    forecast = build(rows, day=SPRING_FORWARD)

    assert forecast.interval_count == 92
    assert forecast.mapping.period_minutes == 60


# -- daylight is advisory ----------------------------------------------------


def test_generation_outside_daylight_is_counted_and_never_clamped() -> None:
    """The best available detector for a timezone or offset bug.

    Reported rather than corrected: clamping the source because our astronomy
    disagrees would substitute our model for the source's, which is the one thing
    Phase 5 must not do.
    """
    window = [False] * 96
    for index in range(40, 48):
        window[index] = True

    forecast = build_forecast(
        [(PV_AGGREGATE_SITE, list(LIVE_ROWS))],
        target_day=TARGET,
        tz_key=TZ_KEY,
        interval_count=96,
        index_of=resolver(TARGET),
        daylight=window,
    )

    # The live rows land at 48-55, entirely outside the window given.
    assert forecast.non_daylight_generation_intervals == 8
    # And every value is untouched.
    assert forecast.intervals[48] == pytest.approx(2.2661 * QH)


def test_a_short_daylight_window_is_padded_rather_than_trusted() -> None:
    """A caller that supplies too few flags must not shorten the day."""
    forecast = build_forecast(
        [(PV_AGGREGATE_SITE, list(LIVE_ROWS))],
        target_day=TARGET,
        tz_key=TZ_KEY,
        interval_count=96,
        index_of=resolver(TARGET),
        daylight=[True, True],
    )

    assert len(forecast.daylight) == 96


# -- several sites -----------------------------------------------------------


def per_site(*series: tuple[str, list]) -> PvForecast:
    """Map several selected sites and sum them."""
    return build_forecast(
        list(series),
        target_day=TARGET,
        tz_key=TZ_KEY,
        interval_count=96,
        index_of=resolver(TARGET),
        provenance=PvProvenance(
            selected_site_ids=tuple(site for site, _ in series),
            selected_site_count=len(series),
        ),
    )


def test_one_site_selected_behaves_like_any_other_series() -> None:
    """A single-site installation is not a special case."""
    forecast = per_site(("SITE_A", list(LIVE_ROWS)))

    assert forecast.available is True
    assert forecast.provenance.query_mode == PV_QUERY_MODE_PER_SITE
    assert forecast.intervals[48] == pytest.approx(2.2661 * QH)


def test_all_sites_selected_uses_the_sources_own_aggregate() -> None:
    """One call, and the percentile bands are the source's own."""
    forecast = build(LIVE_ROWS)

    assert forecast.provenance.query_mode == PV_QUERY_MODE_AGGREGATE
    assert forecast.provenance.percentile_aggregation == PV_PERCENTILE_SOURCE_AGGREGATE


def test_two_selected_sites_are_summed_per_interval() -> None:
    """Summed, not averaged, and not one overwriting the other."""
    forecast = per_site(
        ("SITE_A", rows_from(datetime(2026, 8, 21, 10, 0, tzinfo=TZ), kw=2.0, count=2)),
        ("SITE_B", rows_from(datetime(2026, 8, 21, 10, 0, tzinfo=TZ), kw=3.0, count=2)),
    )

    assert forecast.intervals[40] == pytest.approx(5.0 * QH)
    assert forecast.sites_contributing[40] == 2
    assert forecast.provenance.percentile_aggregation == PV_PERCENTILE_COMONOTONIC_SUM


def test_an_unselected_site_contributes_nothing() -> None:
    """Three sites exist; two are selected; the third appears nowhere.

    Driven by simply not passing the third series, which is what the caller does:
    the query is never made for a site the user did not declare.
    """
    forecast = per_site(
        ("SITE_A", rows_from(datetime(2026, 8, 21, 10, 0, tzinfo=TZ), kw=2.0, count=2)),
        ("SITE_B", rows_from(datetime(2026, 8, 21, 10, 0, tzinfo=TZ), kw=3.0, count=2)),
    )

    assert forecast.intervals[40] == pytest.approx(1.25)
    assert forecast.provenance.selected_site_count == 2


def test_the_three_bands_are_aggregated_independently() -> None:
    """Three deliberately different shapes, so a crossed wire cannot pass.

    If P10 were derived from P50 by a ratio, or any two were swapped, at least one
    of these three sums would be wrong.
    """
    forecast = per_site(
        (
            "SITE_A",
            rows_from(
                datetime(2026, 8, 21, 10, 0, tzinfo=TZ),
                kw=2.0,
                count=2,
                kw10=0.5,
                kw90=7.0,
            ),
        ),
        (
            "SITE_B",
            rows_from(
                datetime(2026, 8, 21, 10, 0, tzinfo=TZ),
                kw=3.0,
                count=2,
                kw10=0.25,
                kw90=11.0,
            ),
        ),
    )

    assert forecast.intervals[40] == pytest.approx(5.0 * QH)
    assert forecast.p10[40] == pytest.approx(0.75 * QH)
    assert forecast.p90[40] == pytest.approx(18.0 * QH)


def test_a_selected_site_missing_an_interval_is_partial_and_never_zero() -> None:
    """The sum of what reported, tagged, and excluded from scoring.

    A known under-estimate rather than a fabricated total -- and the benign
    direction, because understated PV raises net demand while export protection
    comes from the meter.
    """
    forecast = per_site(
        ("SITE_A", rows_from(datetime(2026, 8, 21, 10, 0, tzinfo=TZ), kw=2.0, count=4)),
        ("SITE_B", rows_from(datetime(2026, 8, 21, 10, 0, tzinfo=TZ), kw=3.0, count=2)),
    )

    # Both sites cover 10:00-11:00; only A covers 11:00-12:00.
    assert forecast.intervals[40] == pytest.approx(5.0 * QH)
    assert forecast.sites_contributing[40] == 2
    assert forecast.intervals[44] == pytest.approx(2.0 * QH)
    assert forecast.sites_contributing[44] == 1
    assert forecast.partial_site_intervals == 4


def test_an_interval_no_selected_site_reported_stays_missing() -> None:
    """Nobody looked, so there is nothing to say -- not a zero."""
    forecast = per_site(
        ("SITE_A", rows_from(datetime(2026, 8, 21, 10, 0, tzinfo=TZ), kw=2.0, count=2)),
        ("SITE_B", rows_from(datetime(2026, 8, 21, 10, 0, tzinfo=TZ), kw=3.0, count=2)),
    )

    assert forecast.intervals[0] is None
    assert forecast.sites_contributing[0] == 0


def test_one_failing_site_does_not_lose_the_other() -> None:
    """A site returning nothing usable leaves the rest of the forecast standing."""
    forecast = per_site(
        ("SITE_A", rows_from(datetime(2026, 8, 21, 10, 0, tzinfo=TZ), kw=2.0, count=4)),
        ("SITE_B", []),
    )

    assert forecast.available is True
    assert forecast.intervals[40] == pytest.approx(0.5)
    assert forecast.sites_contributing[40] == 1


# -- fingerprints ------------------------------------------------------------


SITE_A = PvSite(
    resource_id="a-1",
    name="Achterkant",
    capacity_kw=5.0,
    capacity_dc_kw=3.65,
    azimuth=-75.0,
    tilt=38.0,
    loss_factor=0.9,
)
SITE_B = PvSite(
    resource_id="b-2",
    name="Voorkant",
    capacity_kw=5.0,
    capacity_dc_kw=2.43,
    azimuth=105.0,
    tilt=38.0,
    loss_factor=0.9,
)


def test_renaming_a_site_changes_neither_fingerprint() -> None:
    """The same roof under a different label is the same roof.

    This is the mistake most likely to be made by accident, because the name is
    what a developer sees when looking at the data.
    """
    renamed = PvSite(**{**vars_of(SITE_A), "name": "Back roof"})

    assert sites_identity(["a-1"]) == sites_identity(["a-1"])
    assert sites_model([SITE_A]) == sites_model([renamed])


def vars_of(site: PvSite) -> dict:
    """Return a site's fields as a mapping, for building a variant."""
    return {
        "resource_id": site.resource_id,
        "name": site.name,
        "capacity_kw": site.capacity_kw,
        "capacity_dc_kw": site.capacity_dc_kw,
        "azimuth": site.azimuth,
        "tilt": site.tilt,
        "loss_factor": site.loss_factor,
    }


def test_changing_membership_changes_the_identity_fingerprint() -> None:
    """The hard barrier: evidence either side is never pooled."""
    assert sites_identity(["a-1"]) != sites_identity(["a-1", "b-2"])
    assert sites_identity(["a-1", "b-2"]) != sites_identity(["b-2"])


def test_the_identity_fingerprint_ignores_order_and_duplication() -> None:
    """A set, not a list: the same two roofs in either order are the same set."""
    assert sites_identity(["a-1", "b-2"]) == sites_identity(["b-2", "a-1"])
    assert sites_identity(["a-1", "a-1"]) == sites_identity(["a-1"])


@pytest.mark.parametrize(
    "field",
    ["capacity_kw", "capacity_dc_kw", "azimuth", "tilt", "loss_factor"],
)
def test_a_physical_model_change_changes_the_model_fingerprint(field: str) -> None:
    """Every field that scales or reshapes the forecast is in the model key.

    ``loss_factor`` in particular: it multiplies every figure the source returns,
    and an earlier draft of this design left it out -- so a site whose loss factor
    moved from 0.9 to 0.85 would have produced a different series while looking
    like the same site.
    """
    changed = PvSite(**{**vars_of(SITE_A), field: 0.123})

    assert sites_model([SITE_A]) != sites_model([changed])


def test_excluding_a_site_changes_the_model_fingerprint() -> None:
    """Excluding one changes what the aggregate contains without changing a site."""
    assert sites_model([SITE_A, SITE_B]) != sites_model(
        [SITE_A, SITE_B], excluded=["b-2"]
    )


def test_the_model_fingerprint_ignores_site_order() -> None:
    """Sorted internally, so a reordered response is not a changed roof."""
    assert sites_model([SITE_A, SITE_B]) == sites_model([SITE_B, SITE_A])


def test_a_fingerprint_is_short_and_stable() -> None:
    """Readable in a diagnostics download, and identical across calls."""
    once = sites_model([SITE_A, SITE_B])

    assert once == sites_model([SITE_A, SITE_B])
    assert len(once) == 16


# -- the unavailable shape ---------------------------------------------------


def test_an_unavailable_forecast_has_the_shape_of_every_other_day() -> None:
    """Full length, so a consumer indexing by interval finds "not known"."""
    forecast = PvForecast.unavailable_for(
        target_day=TARGET,
        tz_key=TZ_KEY,
        interval_count=96,
        reason=PV_UNAVAILABLE_NO_ROWS,
    )

    assert len(forecast.intervals) == 96
    assert len(forecast.p10) == 96
    assert len(forecast.daylight) == 96
    assert forecast.energy_at(50) is None
    assert forecast.power_kw_at(50) is None
    assert forecast.total_kwh is None
    assert forecast.total_p10_kwh is None
    assert forecast.coverage == 0.0


def test_the_diagnostics_form_reports_counts_and_not_the_series() -> None:
    """Ninety-six numbers have no business in a payload capped at sixteen."""
    payload = build(LIVE_ROWS).as_dict()

    assert payload["forecast_intervals"] == 8
    assert payload["total_kwh"] == pytest.approx(4.5264, abs=1e-4)
    for value in payload.values():
        assert not isinstance(value, (list, dict)), value


def test_the_provenance_form_carries_the_two_honest_caveats() -> None:
    """The boundary ambiguity and the percentile caveat, both in words."""
    payload = PvProvenance().as_dict()

    assert payload["electrical_correspondence"] == "unknown"
    assert payload["forecast_boundary"] == "unspecified"
    assert "unknown and is never guessed" in payload["boundaries_note"]
    assert "not a calibrated band" in payload["percentile_note"]


def test_utc_rows_are_accepted_as_well_as_offset_rows() -> None:
    """The mapping works by instant, so any correct offset is equivalent."""
    utc_rows = [
        {
            "period_start": row["period_start"].astimezone(UTC),
            "pv_estimate": row["pv_estimate"],
        }
        for row in LIVE_ROWS
    ]

    assert build(utc_rows).intervals == build(LIVE_ROWS).intervals
