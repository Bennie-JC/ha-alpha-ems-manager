"""Mapping source blocks onto the planning interval identity.

Every case here is **synthetic and says so**. The live capture observed one
healthy 15-minute day in the market's own timezone with the next day published;
it could not reach an hourly source, a DST day, a mismatched timezone, an empty
array or a malformed block. Those are covered by construction rather than by
observation, and none of them is claimed as live-verified.

The block shape they are built from does come from the capture -- key set, order,
five-decimal precision, offset-aware timestamps -- so the shapes are realistic
even where the situations are not observed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

from custom_components.alpha_ems_manager.const import (
    PRICE_EXPORT_BASIS_ADJUSTMENT,
    PRICE_FLAG_COMPONENTS_VARIED,
    PRICE_FLAG_RESOLUTION_DISAGREES,
    PRICE_TOMORROW_NOT_PUBLISHED,
    PRICE_UNAVAILABLE_EMPTY,
    PRICE_UNAVAILABLE_UNUSABLE_ROWS,
)
from custom_components.alpha_ems_manager.price_forecast import (
    PriceProvenance,
    build_price_forecast,
    unavailable_price_forecast,
)
from custom_components.alpha_ems_manager.storage import expected_quarters_for
from tests.frank_capture import (
    SYNTHETIC_ENERGY_TAX,
    SYNTHETIC_FEED_IN_ADJUSTMENT,
    SYNTHETIC_SOURCING_MARKUP,
    synthetic_block,
    synthetic_day,
)
from tests.test_frank_contract import TZ, build, resolver

SPRING = date(2026, 3, 29)  # 92 quarters, one hour skipped
FALL = date(2026, 10, 25)  # 100 quarters, one hour repeated
NORMAL = date(2026, 8, 20)


# --- resolution: measured, fanned out, never interpolated ---------------------


def test_an_hourly_block_fans_out_piecewise_constant() -> None:
    """Four quarters carry the same rate, not four interpolated ones.

    A price is a **rate**, so every quarter of an hourly period costs the same
    per kWh. Interpolating between periods would invent prices nobody published,
    and would make the first quarter of a cheap hour cheaper than the hour was.
    """
    blocks = synthetic_day(NORMAL, period_minutes=60)
    forecast = build(blocks)

    assert len(blocks) == 24
    assert forecast.intervals_known == 96
    assert forecast.coverage == 1.0
    assert forecast.mapping.period_minutes_observed == (60,)

    first_hour = forecast.intervals[:4]
    assert len({interval.import_price_eur_kwh for interval in first_hour}) == 1
    assert all(interval.source_resolution_minutes == 60 for interval in first_hour)
    # and adjacent hours genuinely differ, so "all equal" is not vacuous
    assert forecast.intervals[4].import_price_eur_kwh != (
        forecast.intervals[3].import_price_eur_kwh
    )


def test_a_period_that_is_not_a_whole_number_of_intervals_is_refused() -> None:
    """A ten-minute block is counted as refused, never rounded into place.

    There is no honest way to place it: rounding up would claim a price for five
    minutes nobody priced, and rounding down would discard five that were.
    """
    forecast = build(
        [synthetic_block("2026-08-20T06:00:00+02:00", "2026-08-20T06:10:00+02:00", 0.2)]
    )

    assert forecast.mapping.periods_refused == 1
    assert forecast.intervals == ()
    assert forecast.available is False
    assert forecast.today_reason == PRICE_UNAVAILABLE_UNUSABLE_ROWS


def test_a_thirty_minute_period_still_maps_as_two_quarters() -> None:
    """Whole multiples other than the two observed ones are handled, not assumed.

    The source reports its own resolution by snapping to 15 or 60, so a
    thirty-minute source would be *reported* as 15. Measuring means the mapping
    places it correctly regardless of what the summary says.
    """
    forecast = build(
        [synthetic_block("2026-08-20T06:00:00+02:00", "2026-08-20T06:30:00+02:00", 0.2)]
    )

    assert forecast.intervals_known == 2
    assert forecast.mapping.periods_refused == 0
    assert (
        forecast.intervals[0].import_price_eur_kwh
        == forecast.intervals[1].import_price_eur_kwh
    )


def test_a_reported_resolution_disagreeing_with_the_measurement_is_flagged() -> None:
    """The measured value drives the mapping; the disagreement is reported.

    Both facts are kept. Silently preferring the measurement without recording
    the conflict would hide a source change; preferring the summary would
    mis-place every block.
    """
    forecast = build(
        synthetic_day(NORMAL, period_minutes=60),
        provenance=PriceProvenance(reported_resolution_minutes=15),
    )

    assert PRICE_FLAG_RESOLUTION_DISAGREES in forecast.flags
    assert forecast.provenance.measured_resolution_minutes == 60
    assert forecast.provenance.reported_resolution_minutes == 15
    assert forecast.intervals_known == 96


# --- DST: 92 and 100, by instant ---------------------------------------------


def test_the_spring_forward_day_holds_ninety_two_intervals() -> None:
    """An hour that does not exist is not priced, and coverage is still complete."""
    forecast = build(synthetic_day(SPRING), day=SPRING)

    assert expected_quarters_for(SPRING, TZ) == 92
    assert forecast.intervals_known == 92
    assert forecast.coverage == 1.0
    assert forecast.missing_intervals == 0
    assert forecast.mapping.blocks_out_of_range == 0


def test_the_fall_back_day_prices_the_repeated_hour_twice() -> None:
    """The repeated hour yields two distinct instants and two distinct indices.

    The identity is chronological rather than wall-clock, which is what lets the
    same local time appear twice without either occurrence overwriting the other.
    """
    forecast = build(synthetic_day(FALL), day=FALL)

    assert expected_quarters_for(FALL, TZ) == 100
    assert forecast.intervals_known == 100
    assert forecast.coverage == 1.0
    assert len({interval.index for interval in forecast.intervals}) == 100
    assert len({interval.start_utc for interval in forecast.intervals}) == 100

    repeated = [
        interval
        for interval in forecast.intervals
        if interval.start_utc.astimezone(TZ).hour == 2
        and interval.start_utc.astimezone(TZ).minute == 0
    ]
    assert len(repeated) == 2
    assert repeated[0].index != repeated[1].index
    assert repeated[1].start_utc - repeated[0].start_utc == timedelta(hours=1)


def test_the_horizon_on_a_dst_day_comes_from_instants_not_a_block_count() -> None:
    """No assumption that a market day holds ninety-six blocks."""
    for day, count in ((SPRING, 92), (FALL, 100)):
        forecast = build(synthetic_day(day), day=day)
        end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=TZ)
        assert forecast.intervals_known == count
        assert forecast.economic_price_horizon_end == end.astimezone(UTC)


# --- the local day and the market day are not the same span ------------------


def test_a_market_day_read_in_another_timezone_reports_partial_coverage() -> None:
    """Coverage below 1.0 is reported, and nothing is mis-placed to hide it.

    Home Assistant in Helsinki, the market in Amsterdam: one market day covers
    only part of the local day, and the rest is priced by a market day that may
    not be published yet. Positional mapping would silently file every block an
    hour wrong; instant mapping files them correctly and reports the shortfall.
    """
    helsinki = ZoneInfo("Europe/Helsinki")
    blocks = synthetic_day(NORMAL)  # a market day, Europe/Amsterdam
    count = expected_quarters_for(NORMAL, helsinki)

    forecast = build_price_forecast(
        [("today", blocks)],
        tz_key="Europe/Helsinki",
        index_of=resolver(NORMAL, helsinki),
        target_day=NORMAL,
        expected_intervals=count,
        adjustment=SYNTHETIC_FEED_IN_ADJUSTMENT,
        apply_vat=False,
        today_available=True,
        tomorrow_available=False,
    )

    # Helsinki is one hour ahead, so the first four quarters of the local day are
    # priced by the *previous* market day and fall outside this array.
    assert forecast.available is True
    assert forecast.intervals_known == 92
    assert forecast.missing_intervals == 4
    assert 0.95 < forecast.coverage < 1.0
    assert forecast.mapping.blocks_out_of_range == 4

    # Every placed interval still sits at its true instant.
    for interval in forecast.intervals:
        assert interval.start_utc.astimezone(helsinki).date() == NORMAL


def test_the_source_day_survives_the_merge_of_both_days() -> None:
    """Two source days feed one series, and each interval remembers its origin."""
    forecast = build_price_forecast(
        [
            ("today", synthetic_day(NORMAL)),
            ("tomorrow", synthetic_day(NORMAL + timedelta(days=1))),
        ],
        tz_key="Europe/Amsterdam",
        index_of=resolver(NORMAL),
        target_day=NORMAL,
        expected_intervals=96,
        adjustment=SYNTHETIC_FEED_IN_ADJUSTMENT,
        apply_vat=False,
        today_available=True,
        tomorrow_available=True,
    )

    # The next market day lies wholly outside this local day, so it is counted
    # out of range rather than folded in.
    assert forecast.intervals_known == 96
    assert {interval.source_day for interval in forecast.intervals} == {NORMAL}
    assert forecast.mapping.blocks_out_of_range == 96
    assert forecast.tomorrow_available is True


# --- malformed input is refused, never coerced -------------------------------


def test_a_naive_timestamp_is_refused_rather_than_assigned_a_zone() -> None:
    """Guessing the zone would place the block up to a day wrong.

    The source publishes offset-aware timestamps. One without an offset is a
    contract change, and assuming it means local time would be a silent guess.
    """
    block = synthetic_block(
        "2026-08-20T06:00:00+02:00", "2026-08-20T06:15:00+02:00", 0.2
    )
    forecast = build([{**block, "from": "2026-08-20T06:00:00"}])

    assert forecast.mapping.blocks_malformed == 1
    assert forecast.intervals == ()


def test_a_block_without_an_import_price_is_refused() -> None:
    """An interval with no purchase price is not an interval, and not a zero."""
    block = synthetic_block(
        "2026-08-20T06:00:00+02:00", "2026-08-20T06:15:00+02:00", 0.2
    )
    del block["total_price_eur_kwh"]
    forecast = build([block])

    assert forecast.mapping.blocks_malformed == 1
    assert forecast.intervals == ()


def test_a_stringified_price_is_refused_rather_than_parsed() -> None:
    """``"0.185"`` instead of ``0.185`` is a contract change, not a formatting one.

    Coercing it would hide the change. The mapping refuses and counts it, which
    is what makes the change visible on the installation where it happens.
    """
    block = synthetic_block(
        "2026-08-20T06:00:00+02:00", "2026-08-20T06:15:00+02:00", 0.2
    )
    forecast = build([{**block, "total_price_eur_kwh": "0.35"}])

    assert forecast.mapping.blocks_malformed == 1
    assert forecast.intervals == ()


def test_a_missing_market_price_leaves_the_import_price_usable() -> None:
    """Each field stands alone: no market price means no export reconstruction.

    The import price is a published fact and survives. The export price cannot be
    reconstructed without a wholesale figure, so it is ``None`` with an honest
    basis rather than a guess.
    """
    block = synthetic_block(
        "2026-08-20T06:00:00+02:00", "2026-08-20T06:15:00+02:00", 0.2
    )
    del block["market_price"]
    forecast = build([block])

    interval = forecast.intervals[0]
    assert interval.import_price_eur_kwh is not None
    assert interval.market_price_eur_kwh is None
    assert interval.export_price_eur_kwh is None
    assert forecast.import_price_available is True
    assert forecast.export_price_available is False


def test_a_duplicate_instant_keeps_the_first_and_counts_the_rest() -> None:
    """Deterministic, and counted. First wins so a re-read cannot reorder history."""
    first = synthetic_block(
        "2026-08-20T06:00:00+02:00", "2026-08-20T06:15:00+02:00", 0.2
    )
    second = synthetic_block(
        "2026-08-20T06:00:00+02:00", "2026-08-20T06:15:00+02:00", 0.9
    )
    forecast = build([first, second])

    assert forecast.mapping.blocks_duplicated == 1
    assert forecast.intervals_known == 1
    assert forecast.intervals[0].import_price_eur_kwh == first["total_price_eur_kwh"]


def test_blocks_arriving_out_of_order_are_counted_and_still_sorted() -> None:
    """The series is chronological on output whatever order it arrived in."""
    blocks = synthetic_day(NORMAL)
    forecast = build(list(reversed(blocks)))

    assert forecast.mapping.blocks_non_monotonic > 0
    assert forecast.intervals_known == 96
    for earlier, later in pairwise(forecast.intervals):
        assert earlier.start_utc < later.start_utc


def test_a_within_day_change_in_the_fixed_components_is_flagged() -> None:
    """Markup and energy tax are contract terms; a mid-day change is evidence.

    Not an error -- an energy tax genuinely changes on the first of January --
    but not something to average away either. Flagged, and the day-level
    provenance declines to claim a single value.
    """
    blocks = synthetic_day(NORMAL)
    blocks[50] = {**blocks[50], "energy_tax_price": SYNTHETIC_ENERGY_TAX + 0.01}
    forecast = build(blocks)

    assert PRICE_FLAG_COMPONENTS_VARIED in forecast.flags
    assert forecast.provenance.energy_tax_eur_kwh is None
    assert forecast.provenance.sourcing_markup_eur_kwh == SYNTHETIC_SOURCING_MARKUP


# --- negative prices, and the asymmetry ---------------------------------------


def test_a_negative_wholesale_interval_still_costs_money_to_import() -> None:
    """Import stays positive while export goes negative. The core asymmetry.

    The import side carries the markup and energy tax floor; the export side
    carries neither. So these are not two signs of one number, and no single
    price field could answer both questions.
    """
    block = synthetic_block(
        "2026-08-20T13:00:00+02:00", "2026-08-20T13:15:00+02:00", -0.1
    )
    forecast = build([block])
    interval = forecast.intervals[0]

    assert interval.market_price_eur_kwh == -0.1
    assert interval.import_price_eur_kwh > 0.0
    assert interval.export_price_eur_kwh is not None
    assert interval.export_price_eur_kwh < 0.0
    assert interval.export_basis == PRICE_EXPORT_BASIS_ADJUSTMENT


def test_a_negative_price_is_never_clamped_or_made_absolute() -> None:
    """Passed through exactly. Clamping would erase the interval that matters most."""
    forecast = build(
        synthetic_day(NORMAL, price_at=lambda index, moment: -0.05 - 0.001 * index)
    )

    assert all(
        interval.market_price_eur_kwh is not None
        and interval.market_price_eur_kwh < 0.0
        for interval in forecast.intervals
    )


# --- unknown is never zero ----------------------------------------------------


def test_a_known_zero_and_an_absent_interval_are_distinguishable() -> None:
    """The one distinction a later phase cannot be allowed to lose.

    A zero-priced interval is *present* and priced zero. Beyond the horizon there
    is no interval at all -- not a placeholder, not a zero. A phase that confused
    them would plan free electricity across a gap in the data.
    """
    priced_zero = build(
        [synthetic_block("2026-08-20T06:00:00+02:00", "2026-08-20T06:15:00+02:00", 0.0)]
    )
    absent = build([])

    zero = priced_zero.intervals[0]
    assert zero.market_price_eur_kwh == 0.0
    assert zero.known is True
    assert priced_zero.intervals_known == 1

    assert absent.intervals == ()
    assert absent.intervals_known == 0
    assert absent.economic_price_horizon_end is None
    assert absent.today_reason == PRICE_UNAVAILABLE_EMPTY


def test_a_zero_priced_interval_is_not_treated_as_a_missing_one() -> None:
    """A whole day priced at zero is a complete day, not an empty one."""
    forecast = build(synthetic_day(NORMAL, price_at=lambda index, moment: 0.0))

    assert forecast.available is True
    assert forecast.intervals_known == 96
    assert forecast.coverage == 1.0
    assert forecast.missing_intervals == 0


# --- the horizon --------------------------------------------------------------


def test_the_horizon_stops_at_the_first_gap_and_the_rest_stays_visible() -> None:
    """Contiguity is deliberate, and the isolated remainder is still counted.

    Knowing prices on both sides of a hole is not knowing them continuously, so
    the horizon is the conservative reading. But the later intervals are real
    data and must not become invisible, which is what the beyond-horizon count is
    for.
    """
    blocks = synthetic_day(NORMAL)
    del blocks[40:44]
    forecast = build(blocks)

    assert forecast.intervals_known == 92
    assert forecast.economic_price_horizon_end == datetime.fromisoformat(
        "2026-08-20T10:00:00+02:00"
    ).astimezone(UTC)
    assert forecast.intervals_beyond_horizon == 52
    assert forecast.missing_intervals == 4


def test_the_horizon_ends_with_today_when_the_next_day_is_unpublished() -> None:
    """Today alone is a complete, correct result. The healthy morning state.

    The horizon lands on the last block's ``till``, which is midnight on the
    following civil date -- and the reason the next day is absent is the normal
    one, not a fault.
    """
    forecast = build(
        synthetic_day(NORMAL),
        tomorrow_available=False,
        tomorrow_reason=PRICE_TOMORROW_NOT_PUBLISHED,
    )

    assert forecast.available is True
    assert forecast.today_available is True
    assert forecast.tomorrow_available is False
    assert forecast.tomorrow_reason == PRICE_TOMORROW_NOT_PUBLISHED
    assert forecast.economic_price_horizon_end == datetime.fromisoformat(
        "2026-08-21T00:00:00+02:00"
    ).astimezone(UTC)
    assert forecast.intervals_beyond_horizon == 0


def test_an_unavailable_series_carries_a_reason_and_no_intervals() -> None:
    """The empty case still says why, and invents nothing to fill the gap."""
    forecast = unavailable_price_forecast(
        tz_key="Europe/Amsterdam",
        reason=PRICE_UNAVAILABLE_EMPTY,
        target_day=NORMAL,
        expected_intervals=96,
    )

    assert forecast.available is False
    assert forecast.intervals == ()
    assert forecast.today_reason == PRICE_UNAVAILABLE_EMPTY
    assert forecast.coverage == 0.0
    assert forecast.missing_intervals == 96
    assert forecast.economic_price_horizon_end is None


# --- change detection ---------------------------------------------------------


def test_the_fingerprint_changes_with_content_and_not_with_re_reading() -> None:
    """Issuance is change-triggered, so the fingerprint must track content only."""
    blocks = synthetic_day(NORMAL)

    assert build(blocks).fingerprint() == build(list(blocks)).fingerprint()

    moved = list(blocks)
    moved[7] = {
        **moved[7],
        "total_price_eur_kwh": moved[7]["total_price_eur_kwh"] + 0.01,
    }
    assert build(moved).fingerprint() != build(blocks).fingerprint()

    # The export series is part of the content, so a changed adjustment is a
    # changed series even though every source field is identical.
    assert build(blocks, adjustment=0.5).fingerprint() != build(blocks).fingerprint()


def test_the_diagnostics_form_is_bounded_and_holds_no_series() -> None:
    """Counts, edges and status. A day of intervals has no place in a payload."""
    payload = build(synthetic_day(NORMAL)).as_dict()

    assert payload["intervals_known"] == 96
    assert payload["coverage"] == 1.0
    assert not any(
        isinstance(value, (list, tuple)) and len(value) > 16
        for value in payload.values()
    )
    assert "prices" not in payload
    assert "intervals" not in payload
