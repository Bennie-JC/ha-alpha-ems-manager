"""Tomorrow's prices arrive, and an admitted run stops over-delivering.

**The defect this file exists for.** ``build_horizon`` truncates at the first
unpriced interval, and the dynamic reserve forecast legitimately outlives the
price horizon -- 143 intervals against 47 on the live installation. So a Safety
Buy can be forced at the horizon edge *because tomorrow is unknown*.

Then ``CarriedRun`` freezes its energy figures, and ``affirms`` is purely temporal:
same intent and overlapping windows re-affirm, with no price, ranking or
preference involved. A rolling publication always overlaps. So tomorrow's prices
arrived, Stage A wanted materially less, the fresh publication re-affirmed the run
-- and it kept delivering the obsolete figure. Withdrawal only fires on *absence*,
so a shrunken-but-present target could not shrink it.

The fix is two caps on separate domains. The naive single ``min`` is wrong and its
wrongness is asserted here too: a fresh publication reports remaining energy from
the **next boundary** while the frozen remainder is measured from the **admitted
window start**, so comparing them trims a healthy run by an interval per refresh.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.alpha_ems_manager.const import (
    CAP_FORWARD,
    CAP_FROZEN,
    ECONOMIC_ACTION_CHARGE,
)
from custom_components.alpha_ems_manager.economic import (
    IntervalPrice,
    _safety_buy_runs,
    solve,
)
from custom_components.alpha_ems_manager.execution import (
    ForwardAuthorisation,
    forward_authorisation,
    parse_target,
    remaining_authorised_kwh,
)
from custom_components.alpha_ems_manager.simulation import IntervalDemand

from .test_economic_model import (
    EVERYTHING,
    FLOOR_PERCENT,
    horizon_for,
    reference_table,
)
from .test_stage_b_controller import raw_target

TABLE = reference_table()
FLOOR = TABLE.limits.energy_for_soc(FLOOR_PERCENT)
#: A requirement comfortably below the start, for intervals meant to be slack.
#: ``build_horizon`` quantises the requirement **up** to a bucket, so passing the
#: floor itself asks for the bucket above it -- which is above the start.
SLACK = FLOOR - 1.0

BOUNDARY = datetime(2026, 8, 26, 10, 15, tzinfo=UTC)
AFTER = BOUNDARY + timedelta(minutes=1)


def solved(
    *,
    house: list[float],
    prices: list[tuple[float, float] | None],
    reserve: list[float],
    start_offset_kwh: float = 0.0,
):
    """Solve a horizon in which ``None`` prices are simply not yet published."""
    priced = [
        IntervalPrice()
        if pair is None
        else IntervalPrice(import_eur_kwh=pair[0], export_eur_kwh=pair[1])
        for pair in prices
    ]
    horizon = horizon_for(
        TABLE,
        demands=[
            IntervalDemand(index=index, baseline_kwh=load, pv_kwh=0.0)
            for index, load in enumerate(house)
        ],
        prices=priced,
        reserve_kwh=reserve,
    )
    return solve(
        table=TABLE,
        horizon=horizon,
        start_energy_kwh=FLOOR + start_offset_kwh,
        terminal_floor_kwh=FLOOR,
        minimum_trade_gain_eur=0.0,
        permitted=EVERYTHING,
    )


def charge_of(plan) -> float:
    """Return the plan's total grid-caused battery charge."""
    return sum(
        entry.battery_charge_ac_kwh
        for entry in plan.intervals
        if entry.marginal_grid_import_kwh > 1e-6
    )


# == the defect, and the shrink ==============================================


def test_tomorrow_prices_shrink_admitted_safety_buy() -> None:
    """**The named regression.**

    Tomorrow is unknown, so the horizon stops after three intervals while the
    reserve requirement sits at the edge of it -- and the plan has to buy at
    today's dear price because nothing cheaper is *visible*. Then tomorrow arrives
    and a much cheaper interval appears, still in time to meet the same
    requirement.

    The fresh solve must want materially less now, and the forward cap must carry
    that reduction into an admitted run rather than letting the frozen figure
    stand.
    """
    house = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    # Tomorrow unpriced: only the dear intervals are visible, and the requirement
    # lands inside the visible horizon.
    blind = solved(
        house=house,
        prices=[(0.40, 0.01), (0.40, 0.01), (0.40, 0.01), None, None, None],
        reserve=[SLACK, SLACK, FLOOR + 1.0, SLACK, SLACK, SLACK],
    )
    # Tomorrow arrives: a far cheaper interval, and the requirement moves out with
    # it because the reserve forecast always outlived the prices.
    seeing = solved(
        house=house,
        prices=[
            (0.40, 0.01),
            (0.40, 0.01),
            (0.40, 0.01),
            (0.05, 0.01),
            (0.05, 0.01),
            (0.05, 0.01),
        ],
        reserve=[SLACK, SLACK, SLACK, SLACK, SLACK, FLOOR + 1.0],
    )

    assert blind.available and seeing.available
    blind_early = sum(
        entry.battery_charge_ac_kwh for entry in blind.intervals if entry.index <= 2
    )
    seeing_early = sum(
        entry.battery_charge_ac_kwh for entry in seeing.intervals if entry.index <= 2
    )
    assert blind_early > 0.0, "blind, the requirement had to be met at 0.40"
    assert seeing_early < blind_early, (
        f"seeing tomorrow, {seeing_early} should be less than {blind_early}"
    )

    # **And the cap carries the reduction into the admitted run.** The frozen
    # figure is what the blind solve wanted; the forward allowance is what the
    # informed one still wants.
    allowed, cap = remaining_authorised_kwh(
        now=AFTER,
        frozen_remaining_kwh=blind_early,
        forward=ForwardAuthorisation(
            authorised_kwh=seeing_early, forward_from=BOUNDARY
        ),
    )
    assert allowed == pytest.approx(seeing_early)
    assert cap == CAP_FORWARD
    assert allowed < blind_early


def test_tomorrow_prices_do_not_shrink_when_early_energy_still_required() -> None:
    """**The inverse, so the fix is not just "always buy less later".**

    Tomorrow is cheaper, but the requirement falls *before* it, so waiting is
    infeasible however attractive the later price is. The necessary minimum early
    buy has to survive -- reserve feasibility is lexicographically prior to cost,
    and the cap may only reduce what the plan asks for, never what it needs.
    """
    house = [1.0, 1.0, 1.0, 1.0]
    seeing = solved(
        house=house,
        prices=[(0.40, 0.01), (0.40, 0.01), (0.05, 0.01), (0.05, 0.01)],
        # The requirement is at index 1, before the cheap intervals exist.
        reserve=[SLACK, FLOOR + 1.0, SLACK, SLACK],
    )

    assert seeing.available, seeing.unavailable_reason
    early = sum(
        entry.battery_charge_ac_kwh for entry in seeing.intervals if entry.index <= 1
    )
    assert early > 0.0, "the reserve is unreachable unless it buys before index 1"

    # A fresh publication that still wants that energy leaves the run alone.
    allowed, cap = remaining_authorised_kwh(
        now=AFTER,
        frozen_remaining_kwh=early,
        forward=ForwardAuthorisation(authorised_kwh=early, forward_from=BOUNDARY),
    )
    assert allowed == pytest.approx(early)
    assert cap == CAP_FROZEN


def test_a_conservative_blind_buy_is_attributed_to_the_reserve() -> None:
    """The blind buy is a *Safety* buy, which is why it is bounded by the reserve.

    Attributed by the reserve-relaxed counterfactual rather than by its price: at
    0.40 with nothing cheaper visible, a purely economic optimiser would buy
    nothing at all.
    """
    house = [1.0, 1.0, 1.0]
    prices = [(0.40, 0.01), (0.40, 0.01), (0.40, 0.01)]
    desired = solved(house=house, prices=prices, reserve=[SLACK, SLACK, FLOOR + 1.0])
    relaxed = solved(house=house, prices=prices, reserve=[SLACK, SLACK, SLACK])

    assert desired.available and relaxed.available
    assert charge_of(desired) > charge_of(relaxed)
    assert _safety_buy_runs(desired, relaxed, TABLE.bucket_kwh), (
        "a buy that exists only because of the reserve is a safety buy"
    )
    assert any(run.action == ECONOMIC_ACTION_CHARGE for run in desired.runs), (
        desired.runs
    )


# == the invariants the fix must not break ===================================


def test_the_naive_single_min_would_ratchet_a_healthy_run_down() -> None:
    """**Why there are two caps rather than one comparison.**

    Asserted directly, because it is the mistake the design exists to avoid and
    an assertion is the only thing that stops it being reintroduced as a
    simplification. The two figures do not share an origin: a fresh publication
    reports remaining energy from the *next boundary*, the frozen remainder from
    the *admitted window start*.
    """
    frozen = 6.0
    naive = []
    for quarter in range(4):
        fresh = 6.0 - 1.5 * (quarter + 1)  # the horizon eating the run
        naive.append(min(frozen, fresh))
        frozen -= 1.5

    # A steady 6.0 kWh run would have been trimmed to 4.5 on the very first
    # refresh, and to 3.0 on the second.
    assert naive[0] == pytest.approx(4.5)
    assert naive[1] == pytest.approx(3.0)

    # The implemented form leaves it alone, because the cap is inactive while its
    # own boundary is still ahead and a fresh affirmation moves it on.
    start = datetime(2026, 8, 26, 10, 0, tzinfo=UTC)
    frozen = 6.0
    actual = []
    for quarter in range(4):
        refresh = start + timedelta(minutes=15 * quarter)
        cap = ForwardAuthorisation(
            authorised_kwh=6.0 - 1.5 * (quarter + 1),
            forward_from=refresh + timedelta(minutes=15),
        )
        allowed, _which = remaining_authorised_kwh(
            now=refresh + timedelta(minutes=1),
            frozen_remaining_kwh=frozen,
            forward=cap,
        )
        actual.append(allowed)
        frozen -= 1.5

    assert actual == pytest.approx([6.0, 4.5, 3.0, 1.5])


def test_the_forward_cap_can_never_raise_the_frozen_authorisation() -> None:
    """The mutation this fix must be immune to: growth through carry-forward.

    More energy has to go through normal fresh admission. A carry-forward path
    that could expand an admitted run would be a way to buy without ever being
    authorised to start.
    """
    for frozen_kwh in (0.0, 0.5, 2.0, 6.0):
        for authorised in (0.0, 1.0, 6.0, 999.0):
            allowed, _cap = remaining_authorised_kwh(
                now=AFTER,
                frozen_remaining_kwh=frozen_kwh,
                forward=ForwardAuthorisation(
                    authorised_kwh=authorised, forward_from=BOUNDARY
                ),
            )
            assert allowed <= frozen_kwh + 1e-9, (frozen_kwh, authorised)


def test_delivered_energy_is_not_counted_twice() -> None:
    """Each cap has its own accumulator, measured from its own origin.

    The frozen cap subtracts realised delivery from the admitted window start; the
    forward cap subtracts delivery since its boundary. Mixing the two accumulators
    is the double count this separation exists to prevent.
    """
    # Two kilowatt-hours delivered in total, of which one since the boundary.
    frozen_remaining = 6.0 - 2.0
    forward = ForwardAuthorisation(
        authorised_kwh=3.0, forward_from=BOUNDARY, delivered_since_kwh=1.0
    )

    allowed, cap = remaining_authorised_kwh(
        now=AFTER, frozen_remaining_kwh=frozen_remaining, forward=forward
    )

    # 3.0 authorised from the boundary, 1.0 of it spent, so 2.0 remains -- and the
    # 1.0 delivered *before* the boundary is not subtracted a second time.
    assert allowed == pytest.approx(2.0)
    assert cap == CAP_FORWARD


def test_the_cap_is_read_off_the_publication_and_nothing_else() -> None:
    """``forward_authorisation`` reads the fresh figure and its own window start.

    No comparison, no reconciliation, and no price: the cap is "this much, from
    there", which is exactly what the publication says. Stage B never chooses a
    window.
    """
    published = parse_target(raw_target(battery_target_kwh=2.5))
    assert published is not None

    cap = forward_authorisation(published)

    assert cap.authorised_kwh == pytest.approx(2.5)
    assert cap.forward_from == published.window_start
    assert cap.delivered_since_kwh == pytest.approx(0.0)


def test_a_negative_published_target_cannot_become_an_allowance() -> None:
    """A malformed publication clamps to zero rather than authorising anything."""
    published = parse_target(raw_target(battery_target_kwh=-4.0))
    assert published is not None

    assert forward_authorisation(published).authorised_kwh == pytest.approx(0.0)
