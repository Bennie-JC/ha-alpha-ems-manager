"""beta.31: chained replenishment in winter, and the gate that must not launder.

**The situation these tests exist for.** In winter the house draws more than the
connection can put back: 0.7 kWh a quarter against a 2 kW charge path that can
deliver 0.5. Inventory then genuinely has to be *carried* across the expensive
stretch, no single cheap window is large enough to cover the whole horizon, and
ordinary arbitrage may not clear the trade gain on its own.

The desired behaviour is a chain rather than a state machine:

    use cheap inventory -> minimal bridge buy -> main buy at the cheapest
    reachable window -> use it through the expensive hours -> next cheap refill

Three things are asserted here that the A-Z suite states less directly: that the
chain forms, that the economic gate blocks discretionary energy while never
blocking compulsory energy, and that the physical feasibility argument and the
economically chosen path cannot disagree.

**One finding came out of writing this suite**, and it changed production code.
The requirement is a *curve* and it usually peaks ahead of the horizon head, so a
head deficit of zero is entirely compatible with compulsory purchasing. An earlier
classifier used the head figure as the measure of what was unavoidable and
therefore reported genuinely compulsory energy as discretionary -- the mirror image
of the fault this release exists to fix. The compulsory share is now what the
reserve-relaxed counterfactual declines to buy.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.battery import build_limits
from custom_components.alpha_ems_manager.const import (
    BUY_REASON_MIXED,
    BUY_REASON_REACHABILITY,
    BUY_REASON_UNCERTAINTY,
)
from custom_components.alpha_ems_manager.economic import (
    ECONOMIC_DIRECTION_CHARGE,
    IntervalPrice,
    actionable_intervals,
    build_horizon,
    build_outcome,
    build_physics_table,
    classify_purchase,
    edge_creditable_energy_kwh,
    edge_value_eur_per_kwh,
    future_spread_for,
    select_bucket_kwh,
)
from custom_components.alpha_ems_manager.reserve import (
    build_reserve,
    build_reserve_reachable,
    uncertainty_margin,
)
from custom_components.alpha_ems_manager.simulation import IntervalDemand

# --------------------------------------------------------------------------
# the shape
# --------------------------------------------------------------------------

#: **A 2 kW charge path against 0.7 kWh quarters.** The whole scenario turns on
#: this: full-power charging adds 0.5 kWh a quarter while the house takes 0.7, so
#: the pack drains even while buying flat out. That is what makes inventory
#: something that must be *carried* rather than fetched, and it is why one cheap
#: window cannot cover the horizon.
CHARGE_KW = 2.0
CAPACITY_KWH = 21.6
FLOOR_SOC = 20.0

#: **The lattice is chosen the way production chooses it**, and an earlier draft of
#: this suite hardcoded one. That mattered: ``select_bucket_kwh`` aligns the bucket
#: so a maximum-power quarter divides exactly, giving 0.237 kWh for a 2 kW path and
#: two whole buckets per quarter. The hardcoded 0.2635 belonged to the 10 kW
#: installation and left 1.8 buckets, floored to **one** -- halving the effective
#: charge rate, stretching every purchase over twice as many quarters, and
#: producing a transient reachability violation that was pure lattice artefact.
#: Nothing about the optimiser was wrong; the test was measuring a battery
#: production would never configure.

#: A = an intermediate bridge window, dearer. B = the cheapest main refill.
#: C = a later, second-cheapest window.
WINDOW_A = range(0, 4)
WINDOW_B = range(20, 28)
WINDOW_C = range(44, 48)
STRETCHES = list(range(4, 20)) + list(range(28, 44))

PRICE_A = 0.28
PRICE_B = 0.04
PRICE_C = 0.09
PRICE_EXPENSIVE = 0.75


def _limits(charge_kw: float = CHARGE_KW):
    limits, _missing = build_limits(
        capacity_kwh=CAPACITY_KWH,
        max_charge_kw=charge_kw,
        max_discharge_kw=10.0,
        round_trip_efficiency_percent=90.0,
        max_soc_percent=100.0,
    )
    return limits


def _world(charge_kw: float = CHARGE_KW):
    """Return limits, demands and prices for the winter shape. No production."""
    demands: list[IntervalDemand] = []
    prices: list[IntervalPrice] = []
    for index in range(48):
        if index in WINDOW_A:
            load, price = 0.15, PRICE_A
        elif index in WINDOW_B:
            load, price = 0.15, PRICE_B
        elif index in WINDOW_C:
            load, price = 0.15, PRICE_C
        else:
            load, price = 0.70, PRICE_EXPENSIVE
        demands.append(IntervalDemand(index=index, baseline_kwh=load, pv_kwh=0.0))
        prices.append(IntervalPrice(import_eur_kwh=price, export_eur_kwh=price - 0.13))
    return _limits(charge_kw), tuple(demands), tuple(prices)


def _plan(
    start_soc: float,
    *,
    gain: float,
    architecture: str = "reachability",
    charge_kw: float = CHARGE_KW,
    mae: float | None = 0.06,
):
    """Solve the winter shape and return everything the assertions need."""
    limits, demands, prices = _world(charge_kw)
    floor = limits.energy_for_soc(FLOOR_SOC)
    bucket, _rule = select_bucket_kwh(limits, floor_energy_kwh=floor)
    table = build_physics_table(limits, floor_energy_kwh=floor, bucket_kwh=bucket)
    actionable = actionable_intervals(demands, prices)
    probe = build_reserve_reachable(
        limits=limits,
        floor_energy_kwh=floor,
        demands=demands,
        grid_credit_intervals=actionable,
    )
    margin = uncertainty_margin(
        probe, mae_kwh_per_interval=mae, usable_capacity_kwh=CAPACITY_KWH
    )
    reachability = build_reserve_reachable(
        limits=limits,
        floor_energy_kwh=floor + margin.total_dc_kwh,
        demands=demands,
        grid_credit_intervals=actionable,
    )
    autonomy = build_reserve(limits=limits, floor_energy_kwh=floor, demands=demands)

    if architecture == "reachability":
        curve = tuple(entry.required_dc_kwh for entry in reachability.intervals)
    elif architecture == "autonomy":
        curve = tuple(entry.required_dc_kwh for entry in autonomy.intervals)
    else:
        curve = tuple(floor for _ in demands)

    horizon = build_horizon(
        demands=demands, prices=prices, required_reserve_kwh=curve, table=table
    )
    edge = (
        edge_value_eur_per_kwh(
            horizon.prices, discharge_efficiency=limits.discharge_efficiency
        )
        if architecture == "reachability"
        else 0.0
    )
    outcome = build_outcome(
        table=table,
        horizon=horizon,
        start_energy_kwh=limits.energy_for_soc(start_soc),
        terminal_floor_kwh=floor,
        floor_energy_kwh=floor,
        minimum_trade_gain_eur=gain,
        allow_grid_charging=True,
        allow_battery_export=True,
        edge_value_eur_per_kwh=edge,
        edge_creditable_kwh=edge_creditable_energy_kwh(
            ceiling_kwh=limits.energy_for_soc(100.0), forecast_surplus_kwh=0.0
        ),
        autonomy=tuple(entry.required_dc_kwh for entry in autonomy.intervals),
        reachability=reachability,
        uncertainty=margin,
        actionable_interval_count=actionable,
    )
    return {
        "limits": limits,
        "floor": floor,
        "prices": prices,
        "outcome": outcome,
        "plan": outcome.desired,
        "reachability": reachability,
        "autonomy": autonomy,
        "margin": margin,
        "edge": edge,
    }


def _bought_in(plan, window) -> float:
    """Return the marginal grid energy a plan buys inside one window."""
    return sum(
        max(0.0, entry.marginal_grid_import_kwh)
        for entry in plan.intervals
        if entry.index in window
    )


def _avoided_in_stretches(plan) -> float:
    """Return the import the pack avoided across the expensive stretches."""
    return sum(
        max(0.0, -entry.marginal_grid_import_kwh)
        for entry in plan.intervals
        if entry.index in STRETCHES
    )


def _charge_runs(outcome):
    """Return each charge run with its window label and derived explanation."""
    found = []
    for run in outcome.desired.runs:
        if run.direction != ECONOMIC_DIRECTION_CHARGE:
            continue
        label = (
            "A"
            if run.start_index in WINDOW_A
            else "B"
            if run.start_index in WINDOW_B
            else "C"
            if run.start_index in WINDOW_C
            else "stretch"
        )
        spread, against = future_spread_for(
            run, outcome.desired, discharge_efficiency=outcome.discharge_efficiency
        )
        found.append(
            (
                label,
                run,
                classify_purchase(
                    run,
                    attribution=outcome.safety_buy_attribution.get(run.start_index),
                    # **The same arguments production passes**, the future spread
                    # included: a helper that classified with a different input
                    # would be asserting a label the installation never produces.
                    future_spread_eur_kwh=spread,
                    future_spread_price_eur_kwh=against,
                    bridge_kwh_now=outcome.bridge_kwh_now,
                    uncertainty_dc_kwh=outcome.uncertainty.total_dc_kwh,
                    edge_value_eur_per_kwh=outcome.edge_value_eur_per_kwh,
                    survives_to_edge_kwh=outcome.desired.edge_energy_kwh,
                ),
            )
        )
    return found


# ===========================================================================
# 1. the chain
# ===========================================================================


def test_winter_chained_replenishment() -> None:
    """**The release-blocking scenario: a chain, without a state machine.**

    A pack at 45 % faces two expensive stretches it cannot buy its way through in
    real time. The requirement curve therefore rises through each stretch and
    falls through each cheap window -- which is the shape that makes carrying
    inventory necessary at all.

    What must come out of it: a *small* purchase at the dearer intermediate window
    A, a *substantially larger* one at the cheapest window B, the pack discharging
    through the expensive hours in between, and no single purchase sized to cover
    the whole horizon.
    """
    result = _plan(45.0, gain=0.20)
    plan = result["plan"]
    reachability = result["reachability"]

    bought_a = _bought_in(plan, WINDOW_A)
    bought_b = _bought_in(plan, WINDOW_B)
    bought_c = _bought_in(plan, WINDOW_C)
    report = (
        f"A={bought_a:.2f} B={bought_b:.2f} C={bought_c:.2f} "
        f"avoided={_avoided_in_stretches(plan):.2f} "
        f"head_bridge={result['outcome'].bridge_kwh_now:.3f} "
        f"peak={reachability.peak_required_dc_kwh:.3f}"
        f"@{reachability.peak_required_interval}"
    )

    # The curve binds *ahead* of the head, which is what makes this winter.
    assert reachability.peak_required_dc_kwh > reachability.required_now_dc_kwh
    assert reachability.peak_required_interval in STRETCHES or (
        reachability.peak_required_interval == 4
    )

    # 1/2. Something is bought at A, and it is not a tankful.
    assert bought_a > 0.0, report
    # 5. The cheapest reachable window carries the larger purchase.
    assert bought_b > bought_a, report
    # 6. No single purchase covers the whole horizon: the stretches alone need
    #    more than 11 kWh of demand, and nothing here buys that.
    assert bought_a < 4.0, report
    assert bought_b < 6.0, report
    # 7. And the inventory is *used*: the pack carries the expensive hours.
    assert _avoided_in_stretches(plan) > 5.0, report

    # 3/10. The floor is never crossed, whatever the economics chose.
    energies = [entry.start_energy_dc_kwh for entry in plan.intervals]
    energies.append(plan.end_energy_dc_kwh)
    assert min(energies) >= result["floor"] - 1e-9, report


def test_the_chain_puts_the_largest_purchase_at_the_cheapest_window() -> None:
    """Stated separately, because it is the economic half of the chain.

    A is 0.28, B is 0.04. Nothing in the reachability curve prefers either -- it
    cannot see a price at all -- so the concentration at B is the objective's
    doing, which is exactly the division of labour the release is built on.
    """
    result = _plan(45.0, gain=0.20)
    runs = {label: run for label, run, _verdict in _charge_runs(result["outcome"])}

    assert "A" in runs and "B" in runs, sorted(runs)
    assert result["prices"][runs["A"].start_index].import_eur_kwh == PRICE_A
    assert result["prices"][runs["B"].start_index].import_eur_kwh == PRICE_B
    assert runs["B"].energy_kwh > runs["A"].energy_kwh


def test_a_later_window_is_used_only_when_it_earns_its_place() -> None:
    """C is taken below its value and declined above it, and the gate decides.

    The arithmetic is worth stating because it is close: C buys 2.00 kWh at 0.09
    against a terminal value of 0.2656, so it earns about 0.35 EUR. It survives a
    0.30 trade gain and dies at 0.40 -- the same window, the same physics, the same
    price, taken or left on the threshold alone. That is what a gate is for, and it
    is the mechanism that stops a freed pack buying everything that is merely
    cheap.
    """
    assert _bought_in(_plan(45.0, gain=0.30)["plan"], WINDOW_C) > 0.0
    assert _bought_in(_plan(45.0, gain=0.40)["plan"], WINDOW_C) == 0.0
    # And the earlier windows, whose spread against the 0.75 stretch is far
    # larger, are untouched by a threshold that removes C.
    assert _bought_in(_plan(45.0, gain=0.40)["plan"], WINDOW_A) > 0.0
    assert _bought_in(_plan(45.0, gain=0.40)["plan"], WINDOW_B) > 0.0


# ===========================================================================
# 2. the gate must block discretionary energy and never compulsory energy
# ===========================================================================


def test_only_the_unavoidable_amount_bypasses_the_economic_gate() -> None:
    """**The invariant that keeps reachability from laundering purchases.**

    Two solves, identical in every respect but the trade gain: one at zero and one
    at a prohibitive five euros. The compulsory share must survive the second --
    it is not a trade and has nothing to earn -- and every discretionary kWh must
    not.
    """
    generous = _plan(45.0, gain=0.0)
    prohibitive = _plan(45.0, gain=5.0)

    discretionary_prohibitive = sum(
        verdict["economic_extra_kwh"]
        for _label, _run, verdict in _charge_runs(prohibitive["outcome"])
    )
    total_generous = sum(
        _bought_in(generous["plan"], window)
        for window in (WINDOW_A, WINDOW_B, WINDOW_C)
    )
    total_prohibitive = sum(
        _bought_in(prohibitive["plan"], window)
        for window in (WINDOW_A, WINDOW_B, WINDOW_C)
    )

    # A prohibitive gain removes the cheap main refill entirely...
    assert _bought_in(prohibitive["plan"], WINDOW_B) == 0.0
    assert _bought_in(prohibitive["plan"], WINDOW_C) == 0.0
    # ...but not the energy the pack cannot do without.
    assert _bought_in(prohibitive["plan"], WINDOW_A) > 0.0
    # Nothing discretionary survives it, which is the whole purpose of a gate.
    assert discretionary_prohibitive == pytest.approx(0.0, abs=1e-9)
    # And raising the gate can only ever reduce what is bought.
    assert total_prohibitive <= total_generous + 1e-9

    # **Deliberately not asserted: that the *attributed* compulsory share is
    # monotone in the gate.** It is not, and it must not be read as though it
    # were. The share is a comparison between two solves run under the *same*
    # gates, so raising the gate moves both sides -- at a prohibitive gain the
    # relaxed solve buys nothing at all, and every kWh the desired plan still
    # buys is then correctly attributed to the reserve. Comparing attributions
    # across different gates compares two different questions, which the
    # counterfactual method documents as its own boundary. The four invariants
    # above are the ones that hold.


def test_a_zero_head_bridge_does_not_mean_nothing_was_compulsory() -> None:
    """**The defect this suite found, pinned so it cannot return.**

    The requirement is a curve. On this shape the head asks 9.59 kWh while the
    curve peaks at 10.855 four quarters later, so a pack holding 10.80 kWh has a
    head deficit of *zero* and still cannot decline to buy.

    An earlier classifier used the head figure as its measure of what was
    unavoidable, and therefore reported that energy as discretionary -- the mirror
    of the fault this release exists to fix. The compulsory share is now taken from
    the reserve-relaxed counterfactual, and this is the case that separates them.
    """
    result = _plan(46.0, gain=5.0)
    outcome = result["outcome"]
    reachability = result["reachability"]

    # The head is satisfied; the curve ahead is not.
    assert outcome.bridge_kwh_now == 0.0
    assert reachability.peak_required_dc_kwh > result["limits"].energy_for_soc(46.0)

    runs = _charge_runs(outcome)
    assert runs, "a purchase the pack cannot decline must still be planned"
    for _label, _run, verdict in runs:
        assert verdict["compulsory_kwh"] > 0.0, verdict
        assert verdict["compulsory_basis"] == "reserve_relaxed_counterfactual"
        # The head asks 9.590 kWh (44.4 %) and the curve peaks at 10.855 (50.3 %)
        # four quarters later, so a pack at 46 % clears the head and not the peak:
        # 1.25 kWh is compulsory with a head bridge of exactly zero.
        assert verdict["classification"] in (
            BUY_REASON_REACHABILITY,
            BUY_REASON_UNCERTAINTY,
            BUY_REASON_MIXED,
        )
        # And the payload says why the head figure looked satisfied.
        assert verdict["why_not_earlier"] is not None


def test_the_compulsory_amount_shrinks_as_the_pack_rises() -> None:
    """Monotonic, and zero once the pack clears the curve's peak.

    **Asserted on the bridge rather than on the purchase**, and the distinction is
    the point. The compulsory quantity is a physical deficit and it does fall
    monotonically -- 3.11, 2.03, 0.95, 0.00 across 30 to 45 per cent. What a plan
    *buys* is compulsory plus discretionary, and the discretionary half responds to
    the whole trajectory, so it is not monotone in the starting state and must not
    be asserted as though it were.
    """
    bridges = {
        soc: _plan(soc, gain=5.0)["reachability"].bridge_kwh(
            _limits().energy_for_soc(soc)
        )
        for soc in (30.0, 35.0, 40.0, 45.0, 55.0)
    }

    assert bridges[30.0] > bridges[35.0] > bridges[40.0] > 0.0
    # 44.4 % is the head requirement, so anything above it has no head deficit.
    assert bridges[45.0] == 0.0
    assert bridges[55.0] == 0.0


# ===========================================================================
# 3. the feasibility argument and the chosen path cannot disagree
# ===========================================================================


def test_reachability_and_the_chosen_path_cannot_disagree() -> None:
    """**Why crediting a refill the objective might decline is not a gap.**

    The worry is real in shape: ``grid_credit`` credits a full-power charge at
    every actionable interval, so the recursion can be read as promising "safe,
    because you *could* charge at A and again at B" -- while the economic gates
    then decline A and leave B unreachable.

    It cannot happen, for a reason that is structural rather than lucky:

    1. The recursion is a valid backward invariant. ``R[i] = max(F+u, R[i+1] +
       discharge(i) - pv_credit(i) - grid_credit(i))`` means that from any state
       satisfying ``E(i) >= R[i]``, charging at full power gives ``E(i+1) >=
       R[i+1]``. So a zero-violation continuation always exists.
    2. That curve is enforced **pointwise on the trajectory the solver actually
       chooses** -- ``violations`` is summed over every interval of the chosen
       path, not tested once at the head. A path that declines A and drops below
       the curve at A+1 therefore carries a violation.
    3. Violation is the **first** element of a lexicographically compared pair.
       Every economic gate -- the trade gain, the grid-charge margin, the
       throughput cost -- is a *cost*, in the second element. No cost can prefer a
       violating path to a non-violating one.

    So the gates can decline a refill only when declining it still satisfies the
    curve. Asserted here with a gain large enough to reject any trade on merit.
    """
    result = _plan(50.0, gain=5.0)
    plan = result["plan"]
    reachability = result["reachability"]
    floor = result["floor"]

    # The gates would reject every trade on its merits, and yet:
    assert plan.violation_kwh == pytest.approx(0.0, abs=1e-9)
    energies = [entry.start_energy_dc_kwh for entry in plan.intervals]
    energies.append(plan.end_energy_dc_kwh)
    assert min(energies) >= floor - 1e-9

    # And the trajectory honours the curve at every interval, not just the head.
    requirement = {
        entry.index: entry.required_dc_kwh for entry in reachability.intervals
    }
    for entry in plan.intervals:
        needed = requirement.get(entry.index)
        if needed is not None:
            assert entry.start_energy_dc_kwh >= needed - 1e-6, (
                entry.index,
                entry.start_energy_dc_kwh,
                needed,
            )


def test_the_full_power_credit_cannot_over_credit_where_it_matters() -> None:
    """The credit is asked against an empty pack, and that is the safe direction.

    ``grid_credit`` is measured at full charge power with maximum headroom, so it
    could in principle over-state what a nearly-full pack could absorb. It cannot
    matter: the requirement only binds when the pack is *low*, which is exactly
    when headroom is largest. Asserted at both extremes.
    """
    for soc in (21.0, 95.0):
        result = _plan(soc, gain=0.20)
        plan = result["plan"]
        energies = [entry.start_energy_dc_kwh for entry in plan.intervals]
        energies.append(plan.end_energy_dc_kwh)

        assert min(energies) >= result["floor"] - 1e-9, soc
        assert max(energies) <= result["limits"].energy_for_soc(100.0) + 1e-6, soc


def test_a_power_limit_raises_the_requirement_rather_than_hiding_it() -> None:
    """The chain exists because of the power limit, so it must show in the curve.

    With a 10 kW path the pack can always outrun the house and the requirement is
    the floor. With 2 kW it cannot, and the requirement rises -- which is the
    honest representation of a winter connection, and the reason inventory has to
    be carried at all.
    """
    generous = _plan(50.0, gain=0.20, charge_kw=10.0)
    throttled = _plan(50.0, gain=0.20, charge_kw=CHARGE_KW)

    assert generous["reachability"].required_now_dc_kwh == pytest.approx(
        generous["floor"] + generous["margin"].total_dc_kwh
    )
    assert (
        throttled["reachability"].required_now_dc_kwh
        > generous["reachability"].required_now_dc_kwh
    )


# ===========================================================================
# 4. the counterfactual architectures, on the same winter shape
# ===========================================================================


def test_the_winter_chain_against_the_counterfactual_architectures() -> None:
    """What the alternatives do with the same winter decision.

    The autonomy requirement on this shape is 30.46 kWh against a 21.6 kWh pack --
    half again the whole battery. It is therefore *irreducible*, the lexicographic
    first term ties everywhere, and economics resumes: the constraint stops
    constraining exactly where the risk it names is largest. That is the
    self-disabling property, and it is why the old design bit hardest in the
    shoulder seasons where it merely *nearly* fitted.
    """
    reach = _plan(45.0, gain=0.20, architecture="reachability")
    auto = _plan(45.0, gain=0.20, architecture="autonomy")
    relaxed = _plan(45.0, gain=0.20, architecture="floor")

    # The autonomy figure exceeds the pack, so it cannot be satisfied at all.
    assert auto["autonomy"].required_now_dc_kwh > CAPACITY_KWH
    assert auto["plan"].violation_kwh > 0.0
    # Reachability is satisfiable and is satisfied.
    assert reach["reachability"].required_now_dc_kwh < CAPACITY_KWH

    # Every architecture keeps the physical floor -- the clamp does that, not the
    # planner, which is why a planning error is an expensive import rather than
    # battery harm.
    for name, result in (("reach", reach), ("auto", auto), ("relaxed", relaxed)):
        energies = [entry.start_energy_dc_kwh for entry in result["plan"].intervals]
        energies.append(result["plan"].end_energy_dc_kwh)
        assert min(energies) >= result["floor"] - 1e-9, name


def test_the_curve_rises_before_a_stretch_and_falls_through_it() -> None:
    """The shape of the curve *is* the argument for carrying inventory.

    Rising through a stretch the connection cannot keep up with, falling through a
    window it can. Nothing about this reads a price; it is entirely a statement
    about power against demand.
    """
    result = _plan(45.0, gain=0.20)
    curve = {
        entry.index: entry.required_dc_kwh for entry in result["reachability"].intervals
    }

    # Through the first expensive stretch the requirement falls as the stretch is
    # consumed -- the energy still to be covered shrinks.
    assert curve[4] > curve[12] > curve[19]
    # And it rises again ahead of the second stretch.
    assert curve[28] > curve[43]
    # The last window needs only the floor plus the margin.
    assert curve[47] == pytest.approx(
        result["floor"] + result["margin"].total_dc_kwh, abs=1e-6
    )
