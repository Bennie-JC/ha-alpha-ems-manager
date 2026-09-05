"""A day is sealed once, when it is finalisable, and outlives the evidence.

**The seal exists because the evidence does not outlive the figure.** A day record
is evicted at 365 days; a lifetime investment return has to survive that. So the
day's realised benefit is computed while the record and its prices are both on disk,
persisted beside the day, and folded into a running total when the day is dropped --
which also means eviction needs no prices and no ``await``, and can happen where it
actually happens, inside a synchronous callback.

**This file also corrects the rationale the design was drafted with.** The original
argument was that re-deriving a past day walks its stored state of charge through the
*currently configured* capacity, so a corrected battery capacity would silently
rewrite months of history. Measured against the code, that is false of this figure:
the comparator is four cash legs priced from the meter and reads no capacity, no
efficiency and no state of charge.
:func:`test_the_benefit_is_cash_only_and_no_battery_setting_can_move_it` pins that
rather than leaving it assumed -- it is the boundary property an investment return
actually needs.

What remains, and what these tests hold: the figure is written **once**, it is
written only when the evidence is **complete**, and the version of the comparator
that produced it travels with it -- because beta.42 is itself the release that
corrected the comparator.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.alpha_ems_manager.const import REALIZED_BENEFIT_BASIS_VERSION
from custom_components.alpha_ems_manager.storage import DayRecord, LearningStore

TZ = "Europe/Amsterdam"


def _complete_day(day: date) -> DayRecord:
    """Return a day with every interval measured, so it can qualify."""
    record = DayRecord(day=day, tz_key=TZ)
    for index in range(record.interval_count):
        record.record_interval(
            index,
            measured_kwh=0.2,
            ev_kwh=None,
            ev_expected=False,
            pv_kwh=0.0,
            grid_import_kwh=0.1,
            grid_export_kwh=0.0,
            soc_percent=50.0,
        )
    return record


# ===========================================================================
# written once
# ===========================================================================


def test_a_day_seals_once_and_a_second_attempt_is_refused() -> None:
    """**Idempotence is the correctness, and it is sharper here than for the
    opening valuation.**

    A second opening valuation moves a reference. A second seal *double-counts* into
    a lifetime total, and no later read could tell that it had.
    """
    record = _complete_day(date(2026, 8, 20))

    assert record.note_final_benefit(
        finalized_at="2026-08-21T00:20:00+00:00",
        benefit_eur=1.25,
        basis_version=REALIZED_BENEFIT_BASIS_VERSION,
    )
    assert not record.note_final_benefit(
        finalized_at="2026-08-21T06:00:00+00:00",
        benefit_eur=99.0,
        basis_version=REALIZED_BENEFIT_BASIS_VERSION,
    )
    assert record.benefit_eur_final == 1.25
    assert record.benefit_finalized_at == "2026-08-21T00:20:00+00:00"
    assert record.benefit_basis_version == REALIZED_BENEFIT_BASIS_VERSION


def test_the_seal_survives_a_round_trip_and_carries_its_basis() -> None:
    """All three fields or none, because a lifetime sum of unattributable addends
    is not evidence.

    beta.42 is the release that *corrected* the comparator, so a figure produced by
    the old one has to remain identifiable rather than be quietly added to figures
    produced by the new one.
    """
    day = date(2026, 8, 20)
    record = _complete_day(day)
    record.note_final_benefit(
        finalized_at="2026-08-21T00:20:00+00:00",
        benefit_eur=-0.4,
        basis_version="b41_household_position",
    )

    restored = DayRecord.from_dict(day, record.to_dict(), TZ)

    assert restored is not None
    assert restored.benefit_eur_final == -0.4
    assert restored.benefit_basis_version == "b41_household_position"


def test_a_malformed_seal_reads_as_unsealed_and_never_as_zero() -> None:
    """The two say opposite things.

    A day that reads as sealed-at-nothing is indistinguishable from a day the
    battery genuinely saved nothing, and the first would silently anchor a lifetime
    total. So a partial or non-numeric entry degrades to *unsealed*, which is a
    state that reports itself.
    """
    day = date(2026, 8, 20)
    payload = _complete_day(day).to_dict()

    for broken in (
        {"v": 1.0},  # no instant, no basis
        {"at": "2026-08-21T00:20:00+00:00", "v": 1.0},  # no basis
        {"at": "2026-08-21T00:20:00+00:00", "bv": "b42", "v": "1.0"},  # not numeric
        {"at": "2026-08-21T00:20:00+00:00", "bv": "b42", "v": float("nan")},
        {"at": "", "bv": "b42", "v": 1.0},
    ):
        restored = DayRecord.from_dict(day, {**payload, "bf": broken}, TZ)
        assert restored is not None
        assert restored.final_benefit is None, broken
        assert restored.benefit_eur_final is None, broken


def test_a_sealed_zero_is_kept_because_it_is_a_real_measurement() -> None:
    """A day the battery did not help is evidence, not a missing value."""
    day = date(2026, 8, 20)
    record = _complete_day(day)
    record.note_final_benefit(
        finalized_at="2026-08-21T00:20:00+00:00",
        benefit_eur=0.0,
        basis_version=REALIZED_BENEFIT_BASIS_VERSION,
    )

    restored = DayRecord.from_dict(day, record.to_dict(), TZ)

    assert restored is not None
    assert restored.benefit_eur_final == 0.0
    assert restored.final_benefit is not None


def test_a_document_without_the_key_is_byte_identical_to_a_beta41_one() -> None:
    """Additive, like every storage bump before it.

    An installation that has sealed nothing writes the document beta.41 wrote.
    """
    payload = _complete_day(date(2026, 8, 20)).to_dict()

    assert "bf" not in payload


# ===========================================================================
# the lifetime cursor
# ===========================================================================


def test_the_cursor_refuses_a_day_it_has_already_counted(hass) -> None:
    """**Monotonic by refusal, not by convention.**

    This is what makes a replay after a lost write harmless: the store is rewritten
    from memory on every closed quarter, so the same seal can reach disk twice, and
    a cursor that merely *tended* forwards would add the day twice with it.
    """
    store = LearningStore(hass, "entry")

    assert store.seal_day(date(2026, 8, 20), 1.0)
    assert not store.seal_day(date(2026, 8, 20), 1.0)
    assert not store.seal_day(date(2026, 8, 19), 5.0)

    assert store.sealed_benefit_eur == 1.0
    assert store.sealed_through == date(2026, 8, 20)


def test_a_backwards_clock_cannot_rewind_the_lifetime_total(hass) -> None:
    """A Pi without a real-time clock hands Home Assistant a date years ahead until
    NTP corrects it, and the correction is a jump *backwards*.

    ``prune`` already clamps its reference for this reason. The cursor needs the
    same protection against a different consequence: re-adding days it has counted.
    """
    store = LearningStore(hass, "entry")
    store.seal_day(date(2030, 1, 1), 3.0)

    assert not store.seal_day(date(2026, 8, 20), 2.0)
    assert store.sealed_benefit_eur == 3.0


def test_eviction_folds_a_sealed_day_and_skips_an_unsealed_one(hass) -> None:
    """**The value was computed when the day was finalisable, so eviction needs no
    prices** -- which is what makes this possible in a synchronous callback at all.

    An unsealed day contributes nothing and does not advance the cursor, so the gap
    it leaves in the lifetime coverage stays visible instead of being stepped over.
    """
    store = LearningStore(hass, "entry")
    old = date(2026, 1, 1)
    older = old - timedelta(days=1)
    for day, benefit in ((older, None), (old, 2.5)):
        record = _complete_day(day)
        if benefit is not None:
            record.note_final_benefit(
                finalized_at=f"{day.isoformat()}T00:20:00+00:00",
                benefit_eur=benefit,
                basis_version=REALIZED_BENEFIT_BASIS_VERSION,
            )
        store.days[day] = record
    store.days[date(2027, 6, 1)] = _complete_day(date(2027, 6, 1))

    removed = store.prune(date(2027, 6, 1))

    assert removed == 2
    assert store.sealed_benefit_eur == 2.5
    assert store.sealed_through == old
    # **The count is what makes the gap visible.** Folding the unsealed day at zero
    # would leave the total and the cursor exactly where they are -- the mutation
    # table found that, and it means an average computed from this figure would be
    # divided by a day nobody measured.
    assert store.sealed_day_count == 1


def test_the_lifetime_total_round_trips_with_its_cursor(hass) -> None:
    """Together or not at all.

    A cursor without its total, or a total without its cursor, would let the next
    seal add to a figure whose coverage nothing can state. Half a lifetime total is
    not a smaller one.
    """
    store = LearningStore(hass, "entry")
    store.seal_day(date(2026, 8, 20), 4.25)

    payload = store.to_dict()

    assert payload["sealed"] == {"through": "2026-08-20", "eur": 4.25, "days": 1}


# ===========================================================================
# the predicate, and the property the whole design exists for
# ===========================================================================


@pytest.fixture
async def sealable(hass, setup_integration, source_entities, frank):
    """Return a coordinator holding one complete, priceable yesterday.

    Yesterday's prices sit only in the forecast history, which is the state every
    day older than tomorrow is really in.
    """
    from dataclasses import replace

    from custom_components.alpha_ems_manager.price_forecast import build_price_snapshot

    from .test_beta33_campaign_wiring import planning_coordinator

    coordinator = await planning_coordinator(hass, setup_integration, frank)
    plan = (coordinator.data or {}).get("battery_plan")
    assert plan is not None

    today = plan.target_day
    yesterday = today - timedelta(days=1)

    record = DayRecord(day=yesterday, tz_key=TZ)
    for index in range(record.interval_count):
        record.record_interval(
            index,
            measured_kwh=0.25,
            ev_kwh=None,
            ev_expected=False,
            pv_kwh=0.4 if 30 <= index < 60 else 0.0,
            grid_import_kwh=0.1 if index < 30 else 0.0,
            grid_export_kwh=0.05 if 30 <= index < 60 else 0.0,
            soc_percent=20.0 + (index % 40),
        )
    coordinator.store.days[yesterday] = record

    live = (coordinator.price_forecasts or {}).get(today)
    assert live is not None
    snapshot = build_price_snapshot(
        replace(live, target_day=yesterday),
        issued_at=datetime(
            yesterday.year, yesterday.month, yesterday.day, 14, 0, tzinfo=UTC
        ),
        interval_count=record.interval_count,
    )
    coordinator.history.add_price_snapshot(snapshot)
    coordinator.price_forecasts.pop(yesterday, None)

    return coordinator, plan, yesterday, today


def test_a_complete_priced_past_day_is_finalizable(sealable) -> None:
    """The positive case, so the negatives below are not passing vacuously."""
    coordinator, _plan, yesterday, today = sealable

    assert coordinator.day_finalizable(yesterday, today) == (True, "finalizable")


def test_today_is_never_finalizable(sealable) -> None:
    """**Midnight is not the same thing as final**, and this is the near side of it.

    A day still gaining intervals cannot be sealed at what it has so far.
    """
    coordinator, _plan, _yesterday, today = sealable

    assert coordinator.day_finalizable(today, today) == (False, "day_not_past")


def test_one_missing_interval_withholds_the_seal(sealable) -> None:
    """A quarter spanning midnight closes *after* it and is filed against the day
    it started in, so at 00:00 yesterday's last interval does not exist yet.

    Sealing on the clock would seal the day short, once, permanently.
    """
    coordinator, _plan, yesterday, today = sealable
    record = coordinator.store.days[yesterday]
    record.measured[record.interval_count - 1] = None

    assert coordinator.day_finalizable(yesterday, today) == (False, "intervals_missing")


def test_an_expected_but_unrecorded_flexible_load_withholds_the_seal(
    sealable,
) -> None:
    """The whole-house figure is then unknown by exactly the amount nobody measured.

    That is not a smaller day. It is an unpriceable one, and the failure carries its
    own reason so a reader is not told the intervals are missing when they are all
    present.
    """
    coordinator, _plan, yesterday, today = sealable
    record = coordinator.store.days[yesterday]
    record.ev_expected[10] = True
    record.ev[10] = None

    assert coordinator.day_finalizable(yesterday, today) == (
        False,
        "load_boundary_incomplete",
    )


def test_a_day_with_no_stored_prices_is_not_sealed_at_a_smaller_number(
    sealable,
) -> None:
    """**The clause that matters most for a cumulative figure.**

    An unpriced past interval shrinks the day's total in silence, always in the same
    direction, so a lifetime sum of quietly-short days is biased rather than noisy.
    A day with a price hole is not sealed at a smaller number -- it is not sealed.
    """
    coordinator, _plan, yesterday, today = sealable
    unpriced = yesterday - timedelta(days=1)
    coordinator.store.days[unpriced] = _complete_day(unpriced)

    assert coordinator.day_finalizable(unpriced, today) == (False, "no_stored_prices")


def test_sealing_is_idempotent_across_repeated_refreshes(sealable) -> None:
    """The refresh runs every fifteen minutes and offers every retained past day.

    The second pass must seal nothing, or the lifetime total grows by one day's
    benefit every quarter of an hour.
    """
    coordinator, plan, _yesterday, today = sealable

    assert coordinator.seal_finalizable_days(plan, today) == 1
    assert coordinator.seal_finalizable_days(plan, today) == 0
    assert coordinator.seal_finalizable_days(plan, today) == 0


def test_the_benefit_is_cash_only_and_no_battery_setting_can_move_it(
    sealable,
) -> None:
    """**The boundary test on the investment-return numerator**, and it corrects a
    claim this file was first written to make.

    The rationale drafted for the seal was that re-deriving a past day walks its
    stored state of charge through the *currently configured* capacity, so a
    corrected capacity would rewrite months of history. Measured, that is false of
    *this* figure: the comparator is four cash legs -- import and export, actual and
    counterfactual -- priced from the meter, and it reads no state of charge, no
    capacity and no efficiency. Doubling the capacity moves it by nothing.

    That is worth pinning rather than quietly relying on, because it is exactly the
    property an investment return needs: a numerator no planner term, no forecast
    and no hardware setting can reach. What the seal is actually for is eviction --
    the day record goes at 365 days and the lifetime total has to outlive it.
    """
    from dataclasses import replace as dc_replace

    coordinator, plan, yesterday, today = sealable
    limits = plan.state.limits
    record = coordinator.store.days[yesterday]

    assert coordinator.seal_finalizable_days(plan, today) == 1
    sealed = record.benefit_eur_final
    assert sealed is not None

    for factor, efficiency in ((2.0, 0.7), (0.5, 0.99)):
        moved = dc_replace(
            limits,
            capacity_kwh=limits.capacity_kwh * factor,
            discharge_efficiency=efficiency,
        )
        assert coordinator._day_benefit_eur(record, yesterday, moved) == pytest.approx(
            sealed
        ), (factor, efficiency)

    assert record.benefit_eur_final == sealed
    assert coordinator.seal_finalizable_days(plan, today) == 0


def test_the_lifetime_total_reports_the_days_it_does_not_cover(sealable) -> None:
    """A total missing one of its terms is a different number wearing the same name.

    So a retained past day carrying no sealed figure is counted and published rather
    than hidden: the figure is a true measurement of the days it covers, and a reader
    is told which days those are.

    Measured as a *delta*, because the fixture's own history already contains past
    days this coordinator cannot seal -- which is the realistic case, and pinning an
    absolute count here would pin the fixture rather than the behaviour.
    """
    coordinator, plan, yesterday, today = sealable

    coordinator.seal_finalizable_days(plan, today)
    before = coordinator.lifetime_benefit(today)

    gap = yesterday - timedelta(days=30)
    assert gap not in coordinator.store.days, "the added day must be a new one"
    coordinator.store.days[gap] = _complete_day(gap)
    coordinator.seal_finalizable_days(plan, today)
    after = coordinator.lifetime_benefit(today)

    # The added day has no prices, so it cannot be sealed -- and it is reported as a
    # day the total does not cover rather than passed over in silence.
    assert after["sealed_days"] == before["sealed_days"] == 1
    assert after["unsealed_past_days"] == before["unsealed_past_days"] + 1
    assert after["sealed_through"] == yesterday.isoformat()
    assert after["basis_version"] == REALIZED_BENEFIT_BASIS_VERSION


# ===========================================================================
# the comparator itself, computed a second way
# ===========================================================================
#
# **The capacity test above is not enough, and the mutation table is what proved
# it.** ``F7`` swaps ``realized_battery_benefit_eur`` for
# ``realized_net_value_eur`` and ``B1`` feeds the counterfactual the EV-excluded
# baseline again -- the two defects this release exists to correct -- and *both
# survived*. Neither figure reads a capacity, so a test that only varies the
# hardware settings cannot tell them apart.
#
# So the arithmetic is checked against an independent walk of the same record: the
# same four cash legs, summed here from the stored series and the stored prices,
# with the whole-house load. A wrong formula and a wrong load boundary both move
# the number, and both now fail.


def _expected_benefit(coordinator, record, day) -> float:
    """Return the day's benefit computed independently of the coordinator.

    ``N = max(0, L - PV)`` is what the meter would have imported with no battery
    and ``X = max(0, PV - L)`` what it would have exported anyway, both against the
    **whole household** load -- the boundary ``grid_import_at`` is measured on.
    """
    buy, sell, _basis = coordinator._prices_for_day(day, record.interval_count)
    counterfactual = actual = 0.0
    for index in range(record.interval_count):
        price_in, price_out = buy[index], sell[index]
        load = record.total_load_at(index)
        pv = record.pv_at(index) or 0.0
        imported = record.grid_import_at(index)
        exported = record.grid_export_at(index)
        if None in (price_in, price_out, load, imported, exported):
            continue
        counterfactual += price_in * max(0.0, load - pv) - price_out * max(
            0.0, pv - load
        )
        actual += price_in * imported - price_out * exported
    return round(counterfactual - actual, 4)


def test_the_sealed_benefit_is_the_cash_comparator_and_not_the_household_position(
    sealable,
) -> None:
    """**The correction, checked against arithmetic done a second way.**

    ``realized_net_value_eur`` equals ``TRUE - sum(p*min(I,N)) + sum(s*X)``: it
    subtracts an import bill no battery could have avoided and credits PV export
    that needed none, so it is structurally negative for any household that imports
    anything and would have reported that the battery destroys value.

    Both figures are asserted -- the right one matched, the wrong one *not* matched
    -- because an equality alone would pass on a day where the two happened to
    coincide.
    """
    coordinator, plan, yesterday, _today = sealable
    record = coordinator.store.days[yesterday]
    limits = plan.state.limits

    benefit = coordinator._day_benefit_eur(record, yesterday, limits)
    expected = _expected_benefit(coordinator, record, yesterday)

    assert benefit == pytest.approx(expected, abs=1e-3), (benefit, expected)

    buy, sell, _ = coordinator._prices_for_day(yesterday, record.interval_count)
    window = coordinator._realized_window_for(
        coordinator._realized_series(record, (buy, sell), limits), limits
    )
    assert window.realized_net_value_eur != pytest.approx(benefit, abs=1e-3), (
        "the household position and the battery comparator must be distinguishable "
        "on this fixture, or this test proves nothing"
    )


def test_the_counterfactual_is_differenced_against_the_meter_it_is_compared_to(
    sealable,
) -> None:
    """**Phase 7's cross-phase defect, pinned.**

    Phase 1 and Phase 2 establish the EV-excluded baseline at every hop, and for a
    stated reason: the optimiser must not reserve energy to cover a charging session
    it may itself schedule. Phase 7 then differenced that baseline against
    ``grid_import_at``, which *includes* the vehicle. Only one of the two terms had
    been re-based, so on any interval the EV drew, ``max(0, load - pv) - import``
    collapsed to zero and the battery's whole contribution vanished from the
    realised figures with nothing saying so.

    The witness is the vehicle itself: with a draw recorded, the two boundaries give
    different answers, and the published figure has to be the one measured on the
    same meter as the import it is compared against.
    """
    coordinator, plan, yesterday, _today = sealable
    record = coordinator.store.days[yesterday]
    for index in range(8, 20):
        record.ev_expected[index] = True
        record.ev[index] = 0.15

    baseline = [record.baseline_at(i) for i in range(record.interval_count)]
    whole = [record.total_load_at(i) for i in range(record.interval_count)]
    assert baseline != whole, "the fixture must record a draw, or nothing is proved"

    benefit = coordinator._day_benefit_eur(record, yesterday, plan.state.limits)

    assert benefit == pytest.approx(
        _expected_benefit(coordinator, record, yesterday), abs=1e-3
    )


def test_a_sealed_day_is_never_priced_again(sealable) -> None:
    """The pass runs every fifteen minutes over every retained past day.

    Re-pricing a sealed one cannot change its figure -- the write-once guard sees to
    that -- but it walks a year of days through the whole realised window each time,
    for an answer already on disk. The skip is what keeps a refresh cheap, so it is
    asserted rather than assumed.
    """
    coordinator, plan, yesterday, today = sealable
    priced: list = []
    original = coordinator._day_benefit_eur

    def _counting(record, day, limits):
        priced.append(day)
        return original(record, day, limits)

    coordinator._day_benefit_eur = _counting
    try:
        assert coordinator.seal_finalizable_days(plan, today) == 1
        assert priced == [yesterday]
        priced.clear()
        assert coordinator.seal_finalizable_days(plan, today) == 0
        assert priced == []
    finally:
        coordinator._day_benefit_eur = original
