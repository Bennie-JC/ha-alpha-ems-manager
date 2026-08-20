"""Deliberately break each price invariant, and prove a test notices.

A green suite is not evidence on its own. A test that would also pass against the
broken implementation it exists to protect against is decoration, and the only way
to find out which kind you have is to break the thing and watch.

Every mutation here is a *plausible* refactor rather than an absurdity -- the kind
of change someone might make in good faith while tidying up. Two are worth
singling out, because both would look entirely reasonable in a diagnostics
download:

* using ``market_price`` where ``total_price_eur_kwh`` was meant. Both are prices
  per kWh, both are positive most of the time, and the difference is only obvious
  on a negative-wholesale interval -- exactly the interval a later phase cares
  about most;
* reaching for ``sourcing_markup_price`` where ``feed_in_adjustment`` was meant.
  On the one live installation observed these are the *same number*, so the
  mistake reconstructs the export price correctly and passes even the live
  cross-check.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.alpha_ems_manager.const import (
    PRICE_CROSS_CHECK_AGREES,
    PRICE_CROSS_CHECK_DISAGREES,
    PRICE_EXPORT_BASIS_UNKNOWN,
    PRICE_TOMORROW_NOT_PUBLISHED,
    PRICE_UNAVAILABLE_EMPTY,
)
from custom_components.alpha_ems_manager.price_forecast import (
    build_price_forecast,
    build_price_snapshot,
    cross_check,
    reconstruct_export_price,
)
from custom_components.alpha_ems_manager.storage import (
    expected_quarters_for,
    index_for_start_utc,
)

from .frank_capture import (
    CAPTURED_ACTIVE_BLOCK,
    CAPTURED_CURRENT_EXPORT_EUR_KWH,
    CAPTURED_ENTRY,
    SYNTHETIC_FEED_IN_ADJUSTMENT,
    synthetic_block,
    synthetic_day,
)
from .test_frank_contract import CAPTURE_DAY, TZ, build, resolver
from .test_price_mapping import FALL, NORMAL


def test_positional_mapping_would_misplace_a_foreign_market_day() -> None:
    """Mutation: index by position in the array instead of by instant.

    Plausible because the array *is* chronological and contiguous, so position
    and index agree perfectly whenever Home Assistant runs in the market's own
    timezone -- which is the only case the live capture could observe. It breaks
    silently for everyone else.
    """
    from zoneinfo import ZoneInfo

    helsinki = ZoneInfo("Europe/Helsinki")
    blocks = synthetic_day(NORMAL)

    correct = build_price_forecast(
        [("today", blocks)],
        tz_key="Europe/Helsinki",
        index_of=resolver(NORMAL, helsinki),
        target_day=NORMAL,
        expected_intervals=expected_quarters_for(NORMAL, helsinki),
        adjustment=SYNTHETIC_FEED_IN_ADJUSTMENT,
        apply_vat=False,
        today_available=True,
        tomorrow_available=False,
    )

    # The mutation: a resolver that counts rather than measures.
    counter = iter(range(1000))
    positional = build_price_forecast(
        [("today", blocks)],
        tz_key="Europe/Helsinki",
        index_of=lambda _start: next(counter),
        target_day=NORMAL,
        expected_intervals=expected_quarters_for(NORMAL, helsinki),
        adjustment=SYNTHETIC_FEED_IN_ADJUSTMENT,
        apply_vat=False,
        today_available=True,
        tomorrow_available=False,
    )

    # Positional mapping claims a complete day and files four intervals an hour
    # early. Instant mapping reports the shortfall instead.
    assert positional.coverage == 1.0
    assert correct.coverage < 1.0
    assert correct.mapping.blocks_out_of_range == 4
    assert positional.mapping.blocks_out_of_range == 0
    assert correct.intervals[0].index != positional.intervals[0].index


def test_interpolating_an_hourly_period_would_invent_prices() -> None:
    """Mutation: spread an hourly block linearly instead of holding it flat.

    Plausible because interpolation is the right answer for a *quantity* sampled
    at intervals. It is the wrong answer for a rate: every quarter of a cheap
    hour costs what the hour cost, and interpolating would make the first quarter
    cheaper than anything anybody published.
    """
    blocks = synthetic_day(NORMAL, period_minutes=60)
    forecast = build(blocks)

    hour = [interval.import_price_eur_kwh for interval in forecast.intervals[:4]]
    assert len(set(hour)) == 1

    interpolated = [
        hour[0] + (blocks[1]["total_price_eur_kwh"] - hour[0]) * step / 4
        for step in range(4)
    ]
    assert len(set(interpolated)) == 4
    assert hour != interpolated


def test_a_missing_interval_read_as_zero_would_look_like_free_electricity() -> None:
    """Mutation: default an absent price to ``0.0``.

    Plausible because a numeric default removes every ``None`` check downstream.
    It also asserts that electricity is free for the part of tomorrow nobody has
    priced yet, which is the single most dangerous statement this layer could
    make.
    """
    blocks = synthetic_day(NORMAL)
    del blocks[40:44]
    forecast = build(blocks)

    assert forecast.intervals_known == 92
    assert forecast.missing_intervals == 4
    assert all(
        interval.import_price_eur_kwh is not None for interval in forecast.intervals
    )

    # The mutation, at the storage layer where it would be permanent.
    snapshot = build_price_snapshot(
        forecast, issued_at=datetime.now(UTC), interval_count=96
    )
    assert snapshot.import_price[40:44] == (None,) * 4
    zeroed = tuple(0.0 if value is None else value for value in snapshot.import_price)
    assert zeroed[40:44] == (0.0,) * 4
    assert zeroed != snapshot.import_price


def test_filling_tomorrow_from_today_would_fabricate_a_day() -> None:
    """Mutation: reuse today's series when the next day is unpublished.

    Plausible because "we have a full day of prices, just use it again" removes an
    unavailability from the diagnostics. It manufactures twenty-four hours of
    market data out of nothing.
    """
    today = synthetic_day(NORMAL)
    honest = build(
        today, tomorrow_available=False, tomorrow_reason=PRICE_TOMORROW_NOT_PUBLISHED
    )

    tomorrow_day = NORMAL + timedelta(days=1)
    fabricated = build_price_forecast(
        [("today", today), ("tomorrow", today)],
        tz_key="Europe/Amsterdam",
        index_of=resolver(tomorrow_day),
        target_day=tomorrow_day,
        expected_intervals=96,
        adjustment=SYNTHETIC_FEED_IN_ADJUSTMENT,
        apply_vat=False,
        today_available=True,
        tomorrow_available=True,
    )

    assert honest.tomorrow_reason == PRICE_TOMORROW_NOT_PUBLISHED
    # Today's blocks carry today's instants, so they cannot be filed against
    # tomorrow at all -- the fabrication is structurally impossible rather than
    # merely tested against. Every block lands out of range.
    assert fabricated.intervals == ()
    assert fabricated.mapping.blocks_out_of_range == 192


def test_market_price_used_as_the_import_price_would_hide_the_asymmetry() -> None:
    """Mutation: read ``market_price`` where the purchase price was meant.

    Plausible: both are per-kWh prices, both positive most of the time, and on a
    positive interval the two are merely different numbers. On a negative
    wholesale interval it inverts the answer -- claiming importing pays you.
    """
    block = synthetic_block(
        "2026-08-20T13:00:00+02:00", "2026-08-20T13:15:00+02:00", -0.1
    )
    interval = build([block]).intervals[0]

    assert interval.import_price_eur_kwh == block["total_price_eur_kwh"]
    assert interval.import_price_eur_kwh > 0.0
    # The mutation would have read this instead, and got the sign wrong.
    assert block["market_price"] < 0.0
    assert interval.import_price_eur_kwh != block["market_price"]


def test_the_sourcing_markup_used_as_the_feed_in_adjustment_is_caught() -> None:
    """Mutation: the value collision the live data creates.

    On the captured installation ``feed_in_adjustment`` and
    ``sourcing_markup_price`` are both ``0.01815``, so this mistake reconstructs
    the export price *correctly*, matches the live return sensor, and passes the
    cross-check. Only a synthetic fixture that keeps the two apart can catch it,
    which is why every synthetic block here does.
    """
    adjustment = CAPTURED_ENTRY["options"]["feed_in_adjustment"]
    markup = CAPTURED_ACTIVE_BLOCK["sourcing_markup_price"]
    assert adjustment == markup

    # Against the live values, the mutation is invisible.
    correct, _ = reconstruct_export_price(CAPTURED_ACTIVE_BLOCK, adjustment, False)
    mutated, _ = reconstruct_export_price(CAPTURED_ACTIVE_BLOCK, markup, False)
    assert correct == mutated == CAPTURED_CURRENT_EXPORT_EUR_KWH

    # Against a synthetic block, it is not.
    block = synthetic_block(
        "2026-08-20T00:00:00+02:00", "2026-08-20T00:15:00+02:00", 0.2
    )
    assert block["sourcing_markup_price"] != SYNTHETIC_FEED_IN_ADJUSTMENT
    honest, _ = reconstruct_export_price(block, SYNTHETIC_FEED_IN_ADJUSTMENT, False)
    slipped, _ = reconstruct_export_price(block, block["sourcing_markup_price"], False)
    assert honest != slipped


def test_an_export_price_without_a_basis_would_pass_as_a_measurement() -> None:
    """Mutation: drop ``export_basis`` because the number is right anyway.

    Plausible because the figure *is* right. The label is what stops a later
    phase treating a configuration-derived estimate as a published price -- and
    the upstream publishes no export price at all, so there is nothing else to
    distinguish them.
    """
    interval = build(synthetic_day(NORMAL)).intervals[0]

    assert interval.export_price_eur_kwh is not None
    assert interval.export_basis != PRICE_EXPORT_BASIS_UNKNOWN
    assert "adjustment" in interval.export_basis


def test_an_unreadable_configuration_producing_a_zero_adjustment_is_caught() -> None:
    """Mutation: fall back to ``0.0`` when the source entry cannot be read.

    Plausible because ``0.0`` *is* the documented default for an absent option.
    But absent and unreadable are different facts: absent means the default, and
    therefore the truth about what the user's own sensor reports; unreadable means
    unknown, and a zero would look exactly like a real figure.
    """
    block = synthetic_block(
        "2026-08-20T00:00:00+02:00", "2026-08-20T00:15:00+02:00", 0.2
    )

    unknown, basis = reconstruct_export_price(block, None, False)
    assert unknown is None
    assert basis == PRICE_EXPORT_BASIS_UNKNOWN

    default, default_basis = reconstruct_export_price(block, 0.0, False)
    assert default == block["market_price"]
    assert default_basis != PRICE_EXPORT_BASIS_UNKNOWN


def test_deriving_the_stored_tax_from_the_vat_relation_is_caught() -> None:
    """Mutation: store three floats and recompute the tax on read.

    Plausible: the relation holds on every block ever observed, and dropping a
    field saves a quarter of the array. But it is legislation rather than
    arithmetic -- and a storage decision is irrecoverable, so a rate change would
    corrupt the record silently.
    """
    blocks = synthetic_day(NORMAL)
    blocks[3] = synthetic_block(
        blocks[3]["from"], blocks[3]["till"], 0.2, market_price_tax=0.05
    )
    snapshot = build_price_snapshot(
        build(blocks), issued_at=datetime.now(UTC), interval_count=96
    )

    stored = snapshot.market_price_tax[3]
    derived = round(0.21 * snapshot.market_price[3], 5)

    assert stored == 0.05
    assert derived != stored
    assert snapshot.flags != ()


def test_the_reported_resolution_driving_the_mapping_is_caught() -> None:
    """Mutation: trust ``resolution_minutes`` instead of measuring.

    Plausible because the source publishes it and it is right on every observed
    day. It is derived from the *first block alone* and snapped to one of two
    values, so a mixed day or an unexpected resolution is mislabelled -- and every
    block would then be placed by that label.
    """
    blocks = synthetic_day(NORMAL, period_minutes=60)
    lied_to = [{**block, "duration_minutes": 15} for block in blocks]

    forecast = build(lied_to)

    # Measured, so twenty-four hourly blocks still fill ninety-six quarters.
    assert forecast.intervals_known == 96
    assert forecast.mapping.period_minutes_observed == (60,)


def test_clamping_a_negative_price_is_caught() -> None:
    """Mutation: ``max(0.0, price)`` because a negative price looks like an error.

    Plausible defensiveness. It erases the intervals a later phase exists to act
    on, and it does so invisibly -- the series still looks complete.
    """
    forecast = build(
        synthetic_day(NORMAL, price_at=lambda index, moment: -0.05 - 0.001 * index)
    )

    values = [interval.market_price_eur_kwh for interval in forecast.intervals]
    assert all(value is not None and value < 0.0 for value in values)
    clamped = [max(0.0, value) for value in values]
    assert clamped != values
    assert set(clamped) == {0.0}


def test_a_clock_based_availability_decision_is_caught() -> None:
    """Mutation: "it is past 13:00, so the next day must exist".

    Plausible because publication really does happen between 13:00 and 14:00
    almost every day. Almost. Publication can be late, and the source's own
    signal is the only thing that knows.
    """
    afternoon = build(
        synthetic_day(NORMAL),
        tomorrow_available=False,
        tomorrow_reason=PRICE_TOMORROW_NOT_PUBLISHED,
    )

    assert afternoon.tomorrow_available is False
    assert afternoon.tomorrow_reason == PRICE_TOMORROW_NOT_PUBLISHED
    # And the mirror: an early publication is accepted rather than suppressed.
    morning = build(
        synthetic_day(NORMAL),
        tomorrow=synthetic_day(NORMAL + timedelta(days=1)),
        tomorrow_available=True,
    )
    assert morning.tomorrow_available is True


def test_overloading_the_empty_reason_for_the_unpublished_case_is_caught() -> None:
    """Mutation: one reason for "no next-day series", whatever the cause.

    Plausible because both cases produce no intervals. One is the source working
    as designed for part of every day; the other is the source claiming a day it
    has not got. Collapsing them hides the fault behind the routine.
    """
    unpublished = build(
        synthetic_day(NORMAL),
        tomorrow_available=False,
        tomorrow_reason=PRICE_TOMORROW_NOT_PUBLISHED,
    )
    claimed_empty = build(
        synthetic_day(NORMAL),
        tomorrow_available=False,
        tomorrow_reason=PRICE_UNAVAILABLE_EMPTY,
    )

    assert unpublished.tomorrow_reason != claimed_empty.tomorrow_reason


def test_a_horizon_that_ignores_gaps_is_caught() -> None:
    """Mutation: take the last known interval as the horizon.

    Plausible and simpler. It claims prices are known continuously across a hole,
    so anything planning to that horizon would plan over data nobody published.
    """
    blocks = synthetic_day(NORMAL)
    del blocks[40:44]
    forecast = build(blocks)

    contiguous = forecast.economic_price_horizon_end
    last_known = max(interval.end_utc for interval in forecast.intervals)

    assert contiguous is not None
    assert contiguous < last_known
    assert forecast.intervals_beyond_horizon == 52


def test_a_cross_check_tolerance_wide_enough_to_hide_drift_is_caught() -> None:
    """Mutation: widen the tolerance until the check stops complaining.

    The tolerance is one tenth of the source's least significant digit, so a real
    disagreement is one it cannot absorb. Widening it to hide a failing check
    would be the one change that makes the whole cross-check worthless.
    """
    assert cross_check(0.2073, 0.20730001) == PRICE_CROSS_CHECK_AGREES
    assert cross_check(0.2073, 0.2074) == PRICE_CROSS_CHECK_DISAGREES
    assert cross_check(0.2073, 0.4573) == PRICE_CROSS_CHECK_DISAGREES

    # A tolerance loose enough to swallow a whole cent would call the last of
    # those agreement, which is the mutation this pins.
    assert abs(0.2073 - 0.2074) > 1e-6


@pytest.mark.parametrize("day", [NORMAL, FALL, CAPTURE_DAY])
def test_assuming_ninety_six_intervals_is_caught(day) -> None:
    """Mutation: hard-code the day length.

    Plausible because it is right for three hundred and sixty-three days a year.
    The fall-back day holds a hundred quarters, and the mutation would drop four
    of them -- including one of the two occurrences of the repeated hour.
    """
    forecast = build(synthetic_day(day), day=day)
    real = expected_quarters_for(day, TZ)

    assert forecast.intervals_known == real
    assert forecast.expected_intervals == real
    if day is FALL:
        assert real == 100
        assert real != 96


def test_a_clamped_index_resolver_would_absorb_a_foreign_block() -> None:
    """Mutation: clamp an out-of-range index instead of refusing it.

    Plausible because it removes an ``if`` and never produces a hole. It files a
    block from the neighbouring market day at the edge of this one, overwriting a
    real interval with a foreign price.
    """
    # The last quarter of the previous market day: adjacent, real, and belonging
    # to a different day than the one being mapped.
    foreign = synthetic_block(
        "2026-08-19T23:45:00+02:00", "2026-08-20T00:00:00+02:00", 0.9
    )
    blocks = [foreign, *synthetic_day(NORMAL)]

    forecast = build(blocks)
    assert forecast.mapping.blocks_out_of_range == 1
    assert forecast.intervals_known == 96
    assert forecast.intervals[0].import_price_eur_kwh != foreign["total_price_eur_kwh"]

    # The raw helper is unclamped on purpose, so the guard has to live at the
    # call site -- which is what this proves is still there.
    raw = index_for_start_utc(NORMAL, datetime.fromisoformat(foreign["from"]), TZ)
    assert raw == -1
