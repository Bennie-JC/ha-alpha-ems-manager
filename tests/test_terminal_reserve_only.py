"""The dynamic reserve is the only physical floor the optimizer uses.

**The architectural rule this file defends.** The dynamic reserve protects energy
that is physically required. Stage A economically optimises everything above it.
There is no second floor.

Until beta.18 there was one: the coordinator handed the solver the *hold
trajectory's endpoint* as an enforceable terminal floor, meaning "end no lower
than doing nothing would have". That reads like an anti-dumping rule and is not
one. On a horizon with no surplus production ahead the idle walk is flat, so the
requirement collapsed to "end no lower than you are now" -- a prohibition on **net
discharge**. And because it was recomputed from the current state every refresh it
**ratcheted**: a charge raised the floor, the next refresh inherited the higher
floor, and the pack was locked out of late-horizon value permanently.

Measured on a nineteen-quarter horizon ending in four quarters at 1.20 EUR/kWh:
the old rule sold **nothing** into the peak and *bought* 4.74 kWh at peak prices;
without it, 6.67 kWh was sold.

Why the reserve is sufficient on its own
----------------------------------------

Because its own forecast legitimately outlives the price horizon. Prices stop at
the end of the published day; the production forecast and the learned load profile
do not. On the live installation the reserve projection runs **143 intervals
against a 47-interval price horizon**, so the requirement at the price boundary is
computed over the whole remaining day -- 15.7 kWh in summer, 19.4 in winter, not
the configured floor.

That is the post-horizon requirement, derived from physics with no price, no clock
and no assumption about when tomorrow is published. Energy above it is
discretionary by construction.

**Every reserve in this file is built by the real recursion over demands that
outlive the prices.** An earlier investigation of mine passed a *constant* reserve
instead, and that single harness mistake produced two wrong conclusions: it
invented end-of-horizon dumping that justified keeping the floor, and it
exaggerated the hoarding. Tests here must not repeat it, which is why none of them
takes a flat reserve array.
"""

from __future__ import annotations

import inspect
import itertools
import math

import pytest

from custom_components.alpha_ems_manager import coordinator as coordinator_module
from custom_components.alpha_ems_manager.const import (
    ECONOMIC_ACTION_DISCHARGE,
    ECONOMIC_ACTION_EXPORT,
)
from custom_components.alpha_ems_manager.economic import (
    IntervalPrice,
    build_horizon,
    build_outcome,
    solve,
)
from custom_components.alpha_ems_manager.reserve import build_reserve
from custom_components.alpha_ems_manager.simulation import IntervalDemand

from .test_economic_model import EVERYTHING, FLOOR_PERCENT, reference_table

TABLE = reference_table()
LIMITS = TABLE.limits
FLOOR = LIMITS.energy_for_soc(FLOOR_PERCENT)
CEILING = LIMITS.energy_for_soc(100.0)

#: How far the physical forecast outlives the prices, in quarters. One civil day:
#: production forecasts cover today and tomorrow, and the learned load baseline is
#: a diurnal profile defined for any interval. Not a tuning constant -- it is how
#: much forecast exists, and it is what production already does.
PHYSICAL_LOOKAHEAD = 96


def world(*, pv_total=0.0, load=0.30, tail=None, morning=None, days=2):
    """Return per-quarter production, price and load for a shaped world."""
    production, price, demand = [], [], []
    for index in range(96 * days):
        quarter = index % 96
        day = index // 96
        arc = 0.0
        if 32 <= quarter < 80 and pv_total > 0.0:
            arc = max(0.0, pv_total * math.sin(math.pi * (quarter - 32) / 48) / 30.55)
        production.append(arc)
        demand.append(load)
        if quarter < 24:
            value = 0.10
        elif 78 <= quarter < 84:
            value = 0.40
        else:
            value = 0.22
        if day == 0 and tail is not None and quarter >= 92:
            value = tail
        if day >= 1 and morning is not None and quarter < 24:
            value = morning
        price.append(value)
    return production, price, demand


def horizon_from(production, price, demand, *, step, priced_end):
    """Return a horizon whose reserve outlives its prices, as production does.

    The reserve recursion is given a further day of physical forecast; the prices
    stop where they stop, and ``build_horizon`` truncates there. This is the
    production relationship and the thing these tests exist to preserve.
    """
    total = len(price)
    reserve_end = min(total, priced_end + PHYSICAL_LOOKAHEAD)
    window = range(step + 1, reserve_end)
    demands = tuple(
        IntervalDemand(
            index=i - (step + 1), baseline_kwh=demand[i], pv_kwh=production[i]
        )
        for i in window
    )
    projection = build_reserve(limits=LIMITS, floor_energy_kwh=FLOOR, demands=demands)
    raw = tuple(entry.required_dc_kwh for entry in projection.intervals)
    prices = tuple(
        IntervalPrice(
            import_eur_kwh=price[i] if i < priced_end else None,
            export_eur_kwh=price[i] * 0.55 if i < priced_end else None,
        )
        for i in window
    )
    return build_horizon(
        demands=demands, prices=prices, required_reserve_kwh=raw, table=TABLE
    )


def rolled(
    *, production, price, demand, start_kwh, step, priced_end, steps, hold_end=False
):
    """Roll forward, execute one interval, and record what happened.

    ``hold_end`` reproduces the pre-beta.18 caller by asking for a floor the
    solver clamps to the idle-walk endpoint. That is how a *before* case is built
    without resurrecting the old production code.
    """
    soc = start_kwh
    trace = []
    for offset in range(steps):
        at = step + offset
        horizon = horizon_from(
            production, price, demand, step=at, priced_end=max(priced_end, at + 2)
        )
        if not horizon.intervals:
            break
        plan = solve(
            table=TABLE,
            horizon=horizon,
            start_energy_kwh=soc,
            terminal_floor_kwh=CEILING * 2.0 if hold_end else FLOOR,
            minimum_trade_gain_eur=0.10,
            permitted=EVERYTHING,
        )
        if not plan.available or not plan.intervals:
            break
        executed = plan.intervals[0]
        landed = min(
            CEILING,
            max(FLOOR, executed.start_energy_dc_kwh + executed.battery_delta_dc_kwh),
        )
        trace.append(
            {
                "soc": soc,
                "requirement": horizon.planning_reserve_kwh[0],
                "margin": soc - horizon.planning_reserve_kwh[0],
                "enforced_floor": plan.terminal_floor_kwh,
                "action": executed.action,
                "charge": executed.battery_charge_ac_kwh,
                "discharge": executed.battery_discharge_ac_kwh,
                "import": executed.grid_import_kwh,
                "export": executed.grid_export_kwh,
                "price": price[at + 1],
                "landed": landed,
            }
        )
        soc = landed
    return trace


# ===========================================================================
# 1. anti-hoarding: the proven late-peak case
# ===========================================================================


def test_the_late_peak_is_sold_into_rather_than_bought_into() -> None:
    """**The defect, fixed.** Discretionary energy reaches the dearest quarters.

    Four final quarters at 1.20 EUR/kWh, a summer reserve leaving real headroom.
    The old caller held everything and bought at peak prices; the reserve-only
    rule sells the discretionary part and keeps the required part.
    """
    production, price, demand = world(pv_total=30.0, load=0.15, tail=1.20)
    common = {
        "production": production,
        "price": price,
        "demand": demand,
        "start_kwh": 19.5,
        "step": 80,
        "priced_end": 96,
        "steps": 14,
    }
    before = rolled(hold_end=True, **common)
    after = rolled(**common)

    peak = 1.20
    sold_before = sum(r["export"] for r in before if r["price"] >= peak)
    sold_after = sum(r["export"] for r in after if r["price"] >= peak)
    bought_before = sum(r["import"] for r in before if r["price"] >= peak)
    bought_after = sum(r["import"] for r in after if r["price"] >= peak)

    # Before: nothing at all sold into the dearest quarters of the horizon, and
    # energy bought inside them.
    assert sold_before == pytest.approx(0.0, abs=1e-6)
    assert bought_before > 0.1
    # After: 6.665 kWh sold into them, and nothing bought.
    assert sold_after > 6.0
    assert bought_after == pytest.approx(0.0, abs=1e-6)


def test_no_unnecessary_charging_at_peak_prices() -> None:
    """Buying at the dearest price of the horizon is the ratchet's signature."""
    production, price, demand = world(pv_total=30.0, load=0.15, tail=1.20)
    after = rolled(
        production=production,
        price=price,
        demand=demand,
        start_kwh=19.5,
        step=80,
        priced_end=96,
        steps=14,
    )

    for row in after:
        if row["price"] >= 1.20:
            assert row["charge"] == pytest.approx(0.0, abs=1e-6), row


# ===========================================================================
# 2. sell now / buy back cheaper
# ===========================================================================


def test_discretionary_energy_is_sold_when_replacement_is_cheaper() -> None:
    """Reserve satisfied, late export very valuable, replacement cheap later.

    The canonical case the old rule failed: it preserved the current state of
    charge merely because the price horizon ended. The energy above the
    requirement is discretionary and must be usable.
    """
    production, price, demand = world(pv_total=30.0, load=0.15, tail=1.20)
    horizon = horizon_from(production, price, demand, step=80, priced_end=96)
    requirement = horizon.planning_reserve_kwh[-1]
    assert requirement < 19.5, "the fixture must leave discretionary headroom"

    plan = solve(
        table=TABLE,
        horizon=horizon,
        start_energy_kwh=19.5,
        terminal_floor_kwh=FLOOR,
        minimum_trade_gain_eur=0.10,
        permitted=EVERYTHING,
    )

    assert plan.available
    # It spends down toward the requirement rather than sitting on the charge.
    assert plan.end_energy_dc_kwh < 19.5
    assert plan.end_energy_dc_kwh >= requirement - 1e-6
    assert any(
        entry.action in (ECONOMIC_ACTION_EXPORT, ECONOMIC_ACTION_DISCHARGE)
        for entry in plan.intervals
    )


# ===========================================================================
# 3. anti-dumping, with a production-shaped reserve
# ===========================================================================


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("cheap tail", {"pv_total": 30.0, "load": 0.15, "tail": 0.01}),
        (
            "cheap tail, dear morning",
            {"pv_total": 30.0, "load": 0.15, "tail": 0.01, "morning": 0.55},
        ),
        ("negative tail", {"pv_total": 30.0, "load": 0.15, "tail": -0.10}),
    ],
)
def test_the_requirement_is_never_dumped_through(label, kwargs) -> None:
    """**The safety half.** With no terminal floor, the reserve still holds.

    A cheap or negative tail is the temptation to empty the pack; a dear morning
    after it is the punishment. The requirement is respected at every step, and
    the plan stops there rather than at the configured floor -- which is the
    difference between having a post-horizon requirement and not having one.
    """
    production, price, demand = world(**kwargs)
    trace = rolled(
        production=production,
        price=price,
        demand=demand,
        start_kwh=19.5,
        step=80,
        priced_end=96,
        steps=14,
    )

    assert trace
    for row in trace:
        assert row["landed"] >= row["requirement"] - 1e-6, (label, row)
    # And it did not fall to the configured minimum: the requirement is higher.
    assert trace[-1]["landed"] > FLOOR + 1.0, label


def test_the_reserve_used_here_really_does_outlive_the_prices() -> None:
    """A guard on the harness itself, so the old mistake cannot come back.

    An earlier investigation passed a constant reserve co-terminal with the
    prices. That is what made dumping look inevitable without a terminal floor.
    Every case in this file must have a reserve whose requirement at the price
    boundary is well above the configured minimum.
    """
    for pv in (12.0, 30.0):
        production, price, demand = world(pv_total=pv, load=0.15, tail=1.20)
        horizon = horizon_from(production, price, demand, step=80, priced_end=96)

        assert horizon.limited_by == "prices"
        assert horizon.planning_reserve_kwh[-1] > FLOOR + 3.0, pv


# ===========================================================================
# 4. the flat ambient walk no longer implies a floor
# ===========================================================================


def test_a_flat_idle_walk_does_not_force_the_end_to_match_the_start() -> None:
    """The exact inference beta.18 broke, stated as an inference.

    With no surplus production ahead the idle walk is flat, so the *old* rule's
    floor equalled the current state of charge. That must no longer imply
    anything about where the plan ends.
    """
    # Production tomorrow, none left today: the priced evening window has no
    # surplus, so the idle walk across it is flat, while the reserve stays modest
    # because tomorrow's sun is inside *its* forecast. That combination is the
    # pre-publication evening, and it is where the old floor did its damage.
    production, price, demand = world(pv_total=30.0, load=0.15, tail=1.20)
    horizon = horizon_from(production, price, demand, step=80, priced_end=96)

    old_style = solve(
        table=TABLE,
        horizon=horizon,
        start_energy_kwh=19.5,
        terminal_floor_kwh=CEILING * 2.0,
        minimum_trade_gain_eur=0.10,
        permitted=EVERYTHING,
    )
    new_style = solve(
        table=TABLE,
        horizon=horizon,
        start_energy_kwh=19.5,
        terminal_floor_kwh=FLOOR,
        minimum_trade_gain_eur=0.10,
        permitted=EVERYTHING,
    )

    # The premise: asked for the idle endpoint, the floor *is* the start, and the
    # plan duly ends exactly where it began.
    assert old_style.terminal_floor_kwh == pytest.approx(19.5, abs=0.4)
    assert old_style.end_energy_dc_kwh == pytest.approx(19.5, abs=0.4)
    # The conclusion that no longer follows: it now spends down to the
    # requirement, which is 10.00 kWh here rather than 19.5.
    assert new_style.terminal_floor_kwh <= FLOOR + 1e-9
    assert new_style.end_energy_dc_kwh < 19.5
    assert new_style.end_energy_dc_kwh >= horizon.planning_reserve_kwh[-1] - 1e-6


# ===========================================================================
# 5. the ratchet, under rolling execution
# ===========================================================================


def test_a_charge_does_not_raise_the_floor_on_the_next_refresh() -> None:
    """**The ratchet regression.** The floor is fixed, so charging cannot move it.

    Before: every refresh recomputed the floor from the current state, so a charge
    raised it and the next refresh inherited the higher one. After: the floor is
    the configured minimum at every step, whatever the pack did.
    """
    production, price, demand = world(pv_total=30.0, load=0.15, tail=1.20)
    common = {
        "production": production,
        "price": price,
        "demand": demand,
        "start_kwh": 19.5,
        "step": 80,
        "priced_end": 96,
        "steps": 14,
    }
    before = rolled(hold_end=True, **common)
    after = rolled(**common)

    # Before: the floor tracks the state of charge and never decreases.
    floors_before = [row["enforced_floor"] for row in before]
    assert max(floors_before) > FLOOR + 5.0
    assert any(
        b["enforced_floor"] > a["enforced_floor"] + 1e-9
        for a, b in itertools.pairwise(before)
    ), "the before case must actually ratchet"

    # After: one value, forever, and it is the configured minimum.
    floors_after = {round(row["enforced_floor"], 6) for row in after}
    assert len(floors_after) == 1
    assert floors_after.pop() <= FLOOR + 1e-9


# ===========================================================================
# 6 and 7. tomorrow present, and tomorrow absent
# ===========================================================================


def test_a_published_tomorrow_changes_nothing_about_the_floor() -> None:
    """A longer horizon must not produce a discontinuity in the requirement."""
    production, price, demand = world(pv_total=30.0, load=0.15, tail=1.20)
    short = horizon_from(production, price, demand, step=80, priced_end=96)
    long_ = horizon_from(production, price, demand, step=80, priced_end=192)

    assert short.intervals < long_.intervals
    for horizon in (short, long_):
        plan = solve(
            table=TABLE,
            horizon=horizon,
            start_energy_kwh=19.5,
            terminal_floor_kwh=FLOOR,
            minimum_trade_gain_eur=0.10,
            permitted=EVERYTHING,
        )
        assert plan.available
        assert plan.terminal_floor_kwh <= FLOOR + 1e-9


def test_an_absent_tomorrow_invents_no_price_and_reads_no_clock() -> None:
    """Unpriced intervals stay unpriced, and nothing consults the time of day.

    The horizon truncates at the prices while the reserve keeps its longer
    physical forecast -- which is exactly how a post-horizon requirement exists
    without a price for it.
    """
    production, price, demand = world(pv_total=30.0, load=0.15, tail=1.20)
    horizon = horizon_from(production, price, demand, step=80, priced_end=96)

    assert horizon.limited_by == "prices"
    assert horizon.intervals == 96 - 81
    # The requirement at the boundary comes from beyond it, with no price.
    assert horizon.planning_reserve_kwh[-1] > FLOOR + 3.0

    source = inspect.getsource(
        coordinator_module.AlphaEmsCoordinator._async_economic_outcome
    )
    for forbidden in ("13:", "publish", "hour ==", "utcnow"):
        assert forbidden not in source


# ===========================================================================
# the caller contract
# ===========================================================================


def test_the_coordinator_asks_for_the_configured_floor_and_nothing_else() -> None:
    """**Where the fix actually lives.**

    ``solve`` still accepts a terminal floor, and given the idle endpoint it will
    still enforce one -- the reachability clamp is a guard, not a policy. What
    changed is what the coordinator supplies. This pins that, because a future
    caller passing ``plan.reference.end_energy_kwh`` again would restore the whole
    defect without touching the solver.
    """
    source = inspect.getsource(
        coordinator_module.AlphaEmsCoordinator._async_economic_outcome
    )

    assert "terminal = floor_energy" in source
    assert "reference.end_energy_kwh" not in source
    assert "plan.state.energy_kwh\n            if plan.reference" not in source


def test_the_binding_flag_reports_the_configured_floor_and_not_the_reserve() -> None:
    """``terminal_binding`` is now almost always false, on purpose.

    It answers "did the plan end at ``terminal_floor_kwh``?", and that floor is the
    configured minimum. What actually stops these plans is the reserve requirement
    at the horizon's end, several kilowatt-hours higher -- so the flag is false
    while the plan sits exactly on the requirement.

    This is pinned rather than left to be noticed because the tempting change is to
    widen the flag to mean "something bound the plan". That would silently
    redefine every ``false`` a beta.16 or beta.17 installation recorded.
    """
    production, price, demand = world(pv_total=30.0, load=0.15, tail=1.20)
    horizon = horizon_from(production, price, demand, step=80, priced_end=96)
    plan = solve(
        table=TABLE,
        horizon=horizon,
        start_energy_kwh=19.5,
        terminal_floor_kwh=FLOOR,
        minimum_trade_gain_eur=0.10,
        permitted=EVERYTHING,
    )
    requirement = horizon.planning_reserve_kwh[-1]

    # The reserve is what stopped it, and the plan lands on the requirement.
    assert plan.end_energy_dc_kwh == pytest.approx(requirement, abs=0.26)
    assert requirement > FLOOR + 5.0
    # So the flag about the *configured floor* is false, and correctly so.
    assert plan.terminal_binding is False
    assert plan.terminal_floor_kwh <= FLOOR + 1e-9


def test_the_reserve_remains_the_authoritative_requirement() -> None:
    """Phase 7 is untouched, and is the only floor the optimizer consults.

    Not a new reserve, not a second one, not a bridge: the same pointwise
    requirement that has been enforced since Phase 8 shipped.
    """
    production, price, demand = world(pv_total=30.0, load=0.15, tail=1.20)
    horizon = horizon_from(production, price, demand, step=80, priced_end=96)
    outcome = build_outcome(
        table=TABLE,
        horizon=horizon,
        start_energy_kwh=19.5,
        terminal_floor_kwh=FLOOR,
        floor_energy_kwh=FLOOR,
        minimum_trade_gain_eur=0.10,
        allow_grid_charging=True,
        allow_battery_export=True,
    )

    assert outcome.desired.violation_kwh == pytest.approx(0.0)
    for index, entry in enumerate(outcome.desired.intervals):
        landed = entry.start_energy_dc_kwh + entry.battery_delta_dc_kwh
        assert landed >= horizon.planning_reserve_kwh[index] - 1e-6
