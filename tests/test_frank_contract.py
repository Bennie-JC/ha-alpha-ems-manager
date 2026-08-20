"""The captured live price contract, asserted against the mapping that reads it.

What each test here can and cannot prove
----------------------------------------

These tests prove the mapping reads **the shape that was observed** on a real
installation, and that the fields it ignores stay ignored. They do **not** prove
it reads the source integration: this suite cannot see that repository, and a
test that tried would be skipped in CI -- and a skipped guard is not a guard.
Only the live cross-check, on a running installation, can fail when the *source*
changes.

That distinction is the whole lesson of the last shipped defect, where a fake
written from the parser's own expectations agreed with the parser and hid a wrong
assumption. So the artefact keeps fields nothing reads, and the tests below check
they are still not read.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

from custom_components.alpha_ems_manager.const import (
    FRANK_VAT_RATE,
    PRICE_EXPORT_BASIS_ADJUSTMENT,
    PRICE_EXPORT_BASIS_ADJUSTMENT_VAT,
    PRICE_EXPORT_BASIS_API_FIELD,
    PRICE_FLAG_VAT_RATIO_UNEXPECTED,
)
from custom_components.alpha_ems_manager.frank_source import _BLOCK_FIELDS
from custom_components.alpha_ems_manager.price_forecast import (
    apply_vat_of,
    build_price_forecast,
    feed_in_adjustment_of,
    reconstruct_export_price,
)
from custom_components.alpha_ems_manager.storage import (
    expected_quarters_for,
    index_for_start_utc,
)
from tests.frank_capture import (
    CAPTURED_ACTIVE_BLOCK,
    CAPTURED_BLOCK_COUNT,
    CAPTURED_BLOCK_KEYS,
    CAPTURED_BLOCKS,
    CAPTURED_CURRENT_EXPORT_EUR_KWH,
    CAPTURED_CURRENT_IMPORT_EUR_KWH,
    CAPTURED_ENTRY,
    CAPTURED_IMPORT_FLOOR_EUR_KWH,
    CAPTURED_RESOLUTION_MINUTES,
    CAPTURED_RETURN_ATTRIBUTES,
    CAPTURED_STATES,
    CAPTURED_TODAY_LAST,
    CONSUMED_BLOCK_KEYS,
    SYNTHETIC_ENERGY_TAX,
    SYNTHETIC_FEED_IN_ADJUSTMENT,
    SYNTHETIC_SOURCING_MARKUP,
    synthetic_block,
    synthetic_day,
)

TZ = ZoneInfo("Europe/Amsterdam")
CAPTURE_DAY = date(2026, 8, 20)


def resolver(day: date, tz: ZoneInfo = TZ):
    """Return the injected index resolver the coordinator supplies in production.

    Out of range yields ``None`` rather than a clamped index: an instant outside
    the target day is a fact to count, and folding it to the nearest valid index
    is precisely the defect an earlier release removed.
    """
    count = expected_quarters_for(day, tz)

    def index_of(start: datetime) -> int | None:
        index = index_for_start_utc(day, start, tz)
        return index if 0 <= index < count else None

    return index_of


def build(blocks, day: date = CAPTURE_DAY, *, tomorrow=(), **kwargs):
    """Map one or two source days onto ``day``'s interval identity."""
    kwargs.setdefault("adjustment", SYNTHETIC_FEED_IN_ADJUSTMENT)
    kwargs.setdefault("apply_vat", False)
    kwargs.setdefault("today_available", True)
    kwargs.setdefault("tomorrow_available", bool(tomorrow))
    return build_price_forecast(
        [("today", blocks), ("tomorrow", tomorrow)],
        tz_key="Europe/Amsterdam",
        index_of=resolver(day),
        target_day=day,
        expected_intervals=expected_quarters_for(day, TZ),
        **kwargs,
    )


# --- the artefact itself ------------------------------------------------------


def test_the_captured_block_key_set_and_order_are_unchanged() -> None:
    """Every captured block carries the nine keys, in order.

    Order is asserted as well as membership because the source pins it, and a
    reordering would be a contract change worth seeing even though nothing here
    depends on position.
    """
    for block in CAPTURED_BLOCKS:
        assert tuple(block) == CAPTURED_BLOCK_KEYS


def test_the_import_price_is_the_sum_of_its_four_components() -> None:
    """``total_price_eur_kwh`` is the all-in purchase price, exactly.

    Exact on every captured block at the source's own five decimal places. This
    is what makes ``total_price_eur_kwh`` the import price and ``market_price``
    something else entirely.
    """
    for block in CAPTURED_BLOCKS:
        assert (
            round(
                block["market_price"]
                + block["market_price_tax"]
                + block["sourcing_markup_price"]
                + block["energy_tax_price"],
                5,
            )
            == block["total_price_eur_kwh"]
        )


def test_the_import_side_carries_a_fixed_floor_the_export_side_does_not() -> None:
    """Markup plus energy tax is 0.129 EUR/kWh on every captured block.

    The reason import and export are not two signs of one number: on a negative
    wholesale interval importing still costs money while exporting earns a
    negative amount. A single price field cannot answer both questions.
    """
    for block in CAPTURED_BLOCKS:
        floor = round(block["sourcing_markup_price"] + block["energy_tax_price"], 5)
        assert floor == CAPTURED_IMPORT_FLOOR_EUR_KWH


def test_the_vat_relation_is_an_observation_and_never_a_storage_dependency() -> None:
    """The 21 % relation holds on every captured block -- and is still stored.

    It holds, so it is checked. It is *legislation* rather than arithmetic, so it
    is never used to derive the field away: a stored series that dropped
    ``market_price_tax`` because it looked derivable could not be repaired after a
    rate change, and repairing it is the entire reason for persisting anything.
    """
    for block in CAPTURED_BLOCKS:
        assert (
            round(FRANK_VAT_RATE * block["market_price"], 5)
            == block["market_price_tax"]
        )

    forecast = build(list(CAPTURED_BLOCKS))
    stored = {
        interval.start_utc: interval.market_price_tax_eur_kwh
        for interval in forecast.intervals
    }
    for block in CAPTURED_BLOCKS:
        start = datetime.fromisoformat(block["from"]).astimezone(UTC)
        assert stored[start] == block["market_price_tax"]
    assert PRICE_FLAG_VAT_RATIO_UNEXPECTED not in forecast.flags


def test_a_broken_vat_relation_is_flagged_and_the_source_value_still_stored() -> None:
    """A deviating tax raises the flag; the stored figure stays what was received.

    The point of flagging rather than correcting: a VAT change becomes visible
    evidence instead of silent corruption, and the evidence keeps the number the
    source actually published.
    """
    block = synthetic_block(
        "2026-08-20T00:00:00+02:00",
        "2026-08-20T00:15:00+02:00",
        0.2,
        market_price_tax=0.09,
    )
    forecast = build([block])

    assert PRICE_FLAG_VAT_RATIO_UNEXPECTED in forecast.flags
    assert forecast.intervals[0].market_price_tax_eur_kwh == 0.09


def test_the_captured_day_is_contiguous_and_its_last_till_is_the_next_date() -> None:
    """Half-open intervals, and "one civil date" applies to ``from`` alone.

    The final block of the day ends at midnight *on the following date*. A test
    or fixture that assumed both timestamps share one civil date would be wrong
    on the last block of every day.
    """
    for earlier, later in pairwise(CAPTURED_BLOCKS):
        if earlier is CAPTURED_BLOCKS[2] or later is CAPTURED_TODAY_LAST[0]:
            # The capture is a sample of six blocks plus the active one, so it is
            # deliberately not contiguous across its own gaps. Only the runs
            # within it are.
            continue
        assert earlier["till"] == later["from"]

    last = CAPTURED_TODAY_LAST[-1]
    assert datetime.fromisoformat(last["from"]).date() == CAPTURE_DAY
    assert datetime.fromisoformat(last["till"]).date() == CAPTURE_DAY + timedelta(
        days=1
    )


def test_the_capture_records_a_published_next_day_and_claims_nothing_more() -> None:
    """The artefact says what it observed: the *published* next-day shape.

    The unpublished shape is not evidenced here and is not upgraded to
    live-verified anywhere. It is covered synthetically, and the tests that cover
    it say so.
    """
    assert CAPTURED_STATES["binary_sensor.frank_tomorrow_prices_available"] == "on"
    assert CAPTURED_STATES["sensor.frank_prices_tomorrow"] == str(CAPTURED_BLOCK_COUNT)


# --- the fields nothing reads -------------------------------------------------


def test_the_boundary_copies_only_the_fields_the_mapping_consumes() -> None:
    """``duration_minutes`` and ``per_unit`` do not cross the boundary.

    Asserted as an exact set, in both directions, so a field added to the reader
    without a decision shows up here rather than arriving silently.
    """
    assert set(_BLOCK_FIELDS) - {"feed_in_price"} == CONSUMED_BLOCK_KEYS
    ignored = set(CAPTURED_BLOCK_KEYS) - CONSUMED_BLOCK_KEYS
    assert ignored == {"duration_minutes", "per_unit"}
    assert not ignored & set(_BLOCK_FIELDS)


def test_the_interval_length_is_measured_from_the_instants_not_the_summary() -> None:
    """A block whose ``duration_minutes`` lies is mapped by its instants.

    A reported summary can disagree with what it summarises -- the source derives
    its day-level resolution from the first block alone and snaps it to one of two
    values. So the length is measured, and this proves it: the block says sixty
    and spans fifteen, and exactly one interval is placed.
    """
    lying = synthetic_block(
        "2026-08-20T06:00:00+02:00",
        "2026-08-20T06:15:00+02:00",
        0.2,
        duration_minutes=60,
    )
    forecast = build([lying])

    assert forecast.mapping.period_minutes_observed == (15,)
    assert len(forecast.intervals) == 1
    assert forecast.intervals[0].source_resolution_minutes == 15


def test_a_block_missing_the_ignored_fields_maps_identically() -> None:
    """Dropping ``per_unit`` and ``duration_minutes`` changes nothing.

    The converse of the test above, and the stronger statement: the mapping does
    not merely prefer the instants, it never consults the other two at all.
    """
    full = synthetic_block(
        "2026-08-20T06:00:00+02:00", "2026-08-20T06:15:00+02:00", 0.2
    )
    stripped = {
        key: value
        for key, value in full.items()
        if key not in {"duration_minutes", "per_unit"}
    }

    assert build([full]).fingerprint() == build([stripped]).fingerprint()


# --- alignment, against the live sensors --------------------------------------


def test_instant_alignment_reproduces_the_live_current_price_sensor() -> None:
    """The block covering the capture instant carries the live import figure.

    Matching the full series against the current-price sensor's interval returned
    exactly one block on the live installation. This is that check, run against
    the artefact: alignment by instant, and the aligned block's import price
    equal to the sensor the user can see.
    """
    forecast = build(list(CAPTURED_BLOCKS))
    moment = datetime.fromisoformat("2026-08-20T21:50:00+02:00")
    interval = forecast.interval_at(moment)

    assert interval is not None
    assert interval.start_utc == datetime.fromisoformat(
        CAPTURED_ACTIVE_BLOCK["from"]
    ).astimezone(UTC)
    assert interval.import_price_eur_kwh == CAPTURED_CURRENT_IMPORT_EUR_KWH


def test_the_reconstruction_reproduces_the_live_return_price_sensor() -> None:
    """Export = market + adjustment, matching the live sensor to the digit.

    Using the adjustment and VAT flag read from the *source's own* entry, because
    the figure on the user's dashboard is derived from those. A second copy of
    the setting on this side would drift away from it.
    """
    options = CAPTURED_ENTRY["options"]
    adjustment = feed_in_adjustment_of(options, 0.0)
    apply_vat = apply_vat_of(options, False)

    assert adjustment == CAPTURED_RETURN_ATTRIBUTES["feed_in_adjustment"]
    assert apply_vat is CAPTURED_RETURN_ATTRIBUTES["apply_vat"] is False

    price, basis = reconstruct_export_price(
        CAPTURED_ACTIVE_BLOCK, adjustment, apply_vat
    )
    assert price == CAPTURED_CURRENT_EXPORT_EUR_KWH
    assert basis == PRICE_EXPORT_BASIS_ADJUSTMENT
    assert basis == CAPTURED_RETURN_ATTRIBUTES["calculation_method"]


def test_the_export_basis_labels_match_the_three_the_source_reports() -> None:
    """Each branch of the reconstruction names itself, verbatim.

    An export price is a configuration-derived estimate of a figure the upstream
    endpoint does not publish at all. Carrying the label beside it is what stops a
    later phase treating it as a measurement.
    """
    block = dict(CAPTURED_ACTIVE_BLOCK)

    _, plain = reconstruct_export_price(block, 0.01, False)
    _, with_vat = reconstruct_export_price(block, 0.01, True)
    explicit_price, explicit = reconstruct_export_price(
        {**block, "feed_in_price": 0.0912}, 0.01, True
    )

    assert plain == PRICE_EXPORT_BASIS_ADJUSTMENT
    assert with_vat == PRICE_EXPORT_BASIS_ADJUSTMENT_VAT
    # An explicit upstream field wins outright, VAT flag and adjustment ignored.
    # Absent from every captured block; the branch is forward-compatibility, and
    # dropping it would silently prefer a reconstruction over a real figure.
    assert explicit == PRICE_EXPORT_BASIS_API_FIELD
    assert explicit_price == 0.0912


def test_the_vat_branch_rounds_the_way_the_source_rounds_its_output() -> None:
    """Six decimals on the *computed* figure, matching the source's own output.

    Five is the precision of the fields it receives; six is what it rounds its
    own feed-in calculation to. Mirrored rather than chosen.
    """
    price, basis = reconstruct_export_price({"market_price": 0.18915}, 0.01815, True)

    assert basis == PRICE_EXPORT_BASIS_ADJUSTMENT_VAT
    assert price == round(0.2073 * 1.21, 6)


# --- the collision the live data creates --------------------------------------


def test_the_synthetic_adjustment_is_distinct_from_both_components() -> None:
    """The one live sample has ``feed_in_adjustment == sourcing_markup_price``.

    Both 0.01815. So code that reached for the markup where it meant the
    adjustment -- two small per-kWh addends sitting side by side -- would
    reconstruct the export price correctly and pass every check including the
    live cross-check. Synthetic fixtures keep the three apart so the slip cannot
    hide.
    """
    assert (
        CAPTURED_ENTRY["options"]["feed_in_adjustment"]
        == CAPTURED_ACTIVE_BLOCK["sourcing_markup_price"]
    )
    assert (
        len(
            {
                SYNTHETIC_FEED_IN_ADJUSTMENT,
                SYNTHETIC_SOURCING_MARKUP,
                SYNTHETIC_ENERGY_TAX,
            }
        )
        == 3
    )

    block = synthetic_block(
        "2026-08-20T00:00:00+02:00", "2026-08-20T00:15:00+02:00", 0.2
    )
    price, _ = reconstruct_export_price(block, SYNTHETIC_FEED_IN_ADJUSTMENT, False)
    assert price == round(0.2 + SYNTHETIC_FEED_IN_ADJUSTMENT, 6)
    assert price != round(0.2 + block["sourcing_markup_price"], 6)


# --- the synthetic day takes its shape from the capture -----------------------


def test_the_synthetic_day_matches_the_captured_shape() -> None:
    """A generated day is indistinguishable in shape from the captured sample."""
    blocks = synthetic_day(CAPTURE_DAY)

    assert len(blocks) == CAPTURED_BLOCK_COUNT
    assert all(tuple(block) == CAPTURED_BLOCK_KEYS for block in blocks)
    assert {block["duration_minutes"] for block in blocks} == {
        CAPTURED_RESOLUTION_MINUTES
    }
    assert blocks[0]["from"] == "2026-08-20T00:00:00+02:00"
    assert blocks[-1]["till"] == "2026-08-21T00:00:00+02:00"
    for earlier, later in pairwise(blocks):
        assert earlier["till"] == later["from"]


def test_a_whole_captured_shape_day_maps_to_full_coverage() -> None:
    """Ninety-six blocks in, ninety-six known intervals out, contiguous."""
    forecast = build(synthetic_day(CAPTURE_DAY))

    assert forecast.available is True
    assert forecast.intervals_known == 96
    assert forecast.coverage == 1.0
    assert forecast.missing_intervals == 0
    assert forecast.intervals_beyond_horizon == 0
    assert forecast.economic_price_horizon_end == datetime.fromisoformat(
        "2026-08-21T00:00:00+02:00"
    ).astimezone(UTC)
