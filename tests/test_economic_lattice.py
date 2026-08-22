"""The state-space lattice, and the promise that no installation gets worse.

beta.16 published one figure, ``max_representable_power_kw``, and documented it as
"roughly five per cent of nameplate peak, in both directions". Both halves were
wrong in general:

* it was the **maximum of two different numbers**. A quarter-hour at maximum power
  is a different amount of DC energy charging than discharging -- they differ by
  the round-trip efficiency -- so a lattice that expresses one exactly generally
  truncates the other. On a 15 kWh / 7.5 kW pack the beta.16 lattice reached
  99.5 % of peak charging and 87.6 % discharging, and the published figure showed
  only the first.
* "five per cent" was this installation's number. On a 10 kWh / 3 kW pack the
  charge side reached **70.3 %** of nameplate.

beta.16 also concluded the fix was not worth having, on the grounds that refining
the grid costs solve time as the inverse square of the bucket. That costing was
right and the candidate was wrong: the fix is not a *finer* bucket but an
*aligned* one -- ``quarter_dc / k`` for integer ``k`` -- which on the reference
pack recovers both directions exactly with **fewer** states and a **faster**
solve. Because the bucket stays constant within a solve, every surviving move is
still exactly linear in its delta, so the invariant the per-delta pricing table
rests on is untouched.

The whole point of this file is the guarantee that makes that safe to ship: an
installation is left exactly as it was or improved, **never** traded off.
"""

from __future__ import annotations

import math
import time

import pytest

from custom_components.alpha_ems_manager.battery import BatteryLimits, build_limits
from custom_components.alpha_ems_manager.const import (
    ECONOMIC_BUCKET_BAND_KWH,
    ECONOMIC_BUCKET_KWH,
    ECONOMIC_BUCKET_RULE_ALIGNED,
    ECONOMIC_BUCKET_RULE_CONSTANT,
    ECONOMIC_BUCKET_STATE_BUDGET,
)
from custom_components.alpha_ems_manager.economic import (
    IntervalPrice,
    PhysicsTable,
    build_physics_table,
    select_bucket_kwh,
    solve,
)
from custom_components.alpha_ems_manager.simulation import IntervalDemand

from .test_economic_model import EVERYTHING, FLOOR_PERCENT, horizon_for

#: capacity kWh, charge kW, discharge kW, round-trip %. Round ratios, awkward
#: ratios, asymmetric power, and one installation whose numbers are nobody's
#: idea of a nice example.
MATRIX = (
    (22.0, 10.0, 10.0, 90.0),
    (22.0, 5.0, 5.0, 90.0),
    (10.0, 5.0, 5.0, 90.0),
    (15.0, 3.0, 3.0, 90.0),
    (30.0, 10.0, 10.0, 90.0),
    (22.0, 10.0, 10.0, 85.0),
    (22.0, 10.0, 10.0, 95.0),
    (15.0, 7.5, 7.5, 88.0),
    (22.0, 20.0, 20.0, 90.0),
    (50.0, 12.0, 12.0, 92.0),
    (13.7, 4.6, 6.1, 87.0),
    (9.8, 3.3, 3.3, 91.0),
    (22.0, 10.0, 5.0, 90.0),
    (22.0, 5.0, 10.0, 90.0),
)

REFERENCE = (22.0, 10.0, 10.0, 90.0)


def limits_of(capacity: float, charge: float, discharge: float, eta: float):
    """Return accepted limits for one configuration."""
    limits, why = build_limits(
        capacity_kwh=capacity,
        max_charge_kw=charge,
        max_discharge_kw=discharge,
        round_trip_efficiency_percent=eta,
    )
    assert limits is not None, why
    return limits


def tables_for(config) -> tuple[PhysicsTable, PhysicsTable, str, float]:
    """Return the beta.16 table, the chosen table, its rule and its bucket."""
    limits = limits_of(*config)
    floor = limits.energy_for_soc(FLOOR_PERCENT)
    before = build_physics_table(
        limits, floor_energy_kwh=floor, bucket_kwh=ECONOMIC_BUCKET_KWH
    )
    bucket, rule = select_bucket_kwh(limits, floor_energy_kwh=floor)
    after = build_physics_table(limits, floor_energy_kwh=floor, bucket_kwh=bucket)
    assert before is not None and after is not None
    return before, after, rule, bucket


# ===========================================================================
# A. the guarantee
# ===========================================================================


@pytest.mark.parametrize(
    "config", MATRIX, ids=lambda c: f"{c[0]}kWh-{c[1]}/{c[2]}kW-{c[3]}pc"
)
def test_no_installation_regresses_in_either_direction(config) -> None:
    """**The promise.** Both peaks, every configuration, no exceptions.

    Not "the total improved" and not "the headline figure improved": a lattice
    that bought charge power by losing discharge power would be a different
    compromise, not an improvement, and on a 22 kWh / 5 kW pack a naive alignment
    did exactly that -- taking discharge from 5.1 % short to 10.0 % short.
    """
    before, after, _rule, _bucket = tables_for(config)

    assert after.max_representable_charge_kw >= before.max_representable_charge_kw
    assert after.max_representable_discharge_kw >= before.max_representable_discharge_kw


@pytest.mark.parametrize(
    "config", MATRIX, ids=lambda c: f"{c[0]}kWh-{c[1]}/{c[2]}kW-{c[3]}pc"
)
def test_no_installation_pays_for_complexity_it_did_not_get(config) -> None:
    """A finer lattice is only ever accepted when it buys representable power.

    The state budget is what stops "exact power" being bought at any price. An
    early version of the search, with no budget, proposed a lattice of **ten
    states for a 22 kWh pack**: peak power exact, state of charge resolved to
    2.4 kWh, and every energy and reserve figure ruined.
    """
    before, after, rule, bucket = tables_for(config)

    budget = int(before.buckets * (1.0 + ECONOMIC_BUCKET_STATE_BUDGET)) + 1
    assert after.buckets <= budget
    low, high = ECONOMIC_BUCKET_BAND_KWH
    assert low <= bucket <= high
    if rule == ECONOMIC_BUCKET_RULE_CONSTANT:
        assert bucket == ECONOMIC_BUCKET_KWH
        # A fallback must be a genuine no-op, not a near-miss.
        assert after.buckets == before.buckets
        assert after.max_representable_charge_kw == pytest.approx(
            before.max_representable_charge_kw
        )
        assert after.max_representable_discharge_kw == pytest.approx(
            before.max_representable_discharge_kw
        )


def test_the_reference_installation_reaches_its_configured_power_exactly() -> None:
    """22 kWh / 10 kW: 10.0000 kW representable, charging **and** discharging.

    The specific claim beta.17 rests on. 9.4868 kW was the beta.16 figure in both
    directions -- a quarter at 10 kW is 2.3717 kWh DC, which is 9.487 buckets of
    0.25, so nine buckets were reachable and ten needed 10.54 kW.
    """
    before, after, rule, bucket = tables_for(REFERENCE)

    assert before.max_representable_charge_kw == pytest.approx(9.4868)
    assert before.max_representable_discharge_kw == pytest.approx(9.4868)

    assert rule == ECONOMIC_BUCKET_RULE_ALIGNED
    assert after.max_representable_charge_kw == pytest.approx(10.0, abs=5e-5)
    assert after.max_representable_discharge_kw == pytest.approx(10.0, abs=5e-5)
    # And it cost nothing: the lattice is smaller than the one it replaces.
    assert after.buckets < before.buckets
    assert bucket == pytest.approx(2.5 * math.sqrt(0.9) / 9.0)


def test_the_awkward_installation_falls_back_rather_than_compromising() -> None:
    """22 kWh / 5 kW has no qualifying lattice, and keeps the beta.16 one.

    Recorded as a *test* rather than as a footnote because it is the case that
    proves the guarantee is real: when the search cannot improve both directions
    inside the band and the budget, it declines. Two installations therefore run
    different lattices, which is why the rule is published beside the bucket.
    """
    _before, _after, rule, bucket = tables_for((22.0, 5.0, 5.0, 90.0))

    assert rule == ECONOMIC_BUCKET_RULE_CONSTANT
    assert bucket == ECONOMIC_BUCKET_KWH


def test_the_selector_is_deterministic() -> None:
    """Same configuration, same lattice. It is memoised on exactly this promise."""
    limits = limits_of(*REFERENCE)
    floor = limits.energy_for_soc(FLOOR_PERCENT)

    first = select_bucket_kwh(limits, floor_energy_kwh=floor)
    second = select_bucket_kwh(limits, floor_energy_kwh=floor)

    assert first == second


# ===========================================================================
# B. the physics is unchanged -- only which points of it are visible
# ===========================================================================


@pytest.mark.parametrize(
    "config", MATRIX, ids=lambda c: f"{c[0]}kWh-{c[1]}/{c[2]}kW-{c[3]}pc"
)
def test_no_move_exceeds_the_configured_limits(config) -> None:
    """A different lattice may not buy a single watt the clamp would refuse.

    The bucket decides which transitions are *offered*; ``apply_request`` decides
    what is *possible*, and it is still the only authority. So every move on the
    chosen lattice must respect the configured power, the floor and the ceiling
    -- if realigning the grid could smuggle a move past the clamp, the whole
    change would be unsafe rather than merely wrong.
    """
    limits = limits_of(*config)
    floor = limits.energy_for_soc(FLOOR_PERCENT)
    _before, after, _rule, _bucket = tables_for(config)
    ceiling = limits.energy_for_soc(limits.max_soc_percent)

    for source, row in enumerate(after.moves):
        start = after.energy(source)
        if start < floor - 1e-9:
            # Buckets below the configured floor exist in the lattice but no
            # discharge can reach them -- the clamp forbids it. They are
            # unreachable states, not violations, so there is nothing to assert
            # about where a move from one of them would land.
            continue
        for move in row:
            delta = move.target - source
            limit = limits.max_charge_kw if delta > 0 else limits.max_discharge_kw
            assert move.power_kw <= limit + 1e-9
            landed = start + move.delta_dc_kwh
            assert landed >= floor - 1e-9
            assert landed <= ceiling + 1e-9
            # Charging and discharging are never both non-zero: that would be a
            # physical impossibility the table would then price.
            assert move.charge_ac_kwh == 0.0 or move.discharge_ac_kwh == 0.0


def test_the_measured_conversion_ratios_survive_the_realignment() -> None:
    """The lattice changes; the physics it samples does not.

    Both ratios are measured from the clamp rather than read off the limits, so a
    realigned bucket must reproduce the same efficiency to floating-point noise.
    """
    before, after, _rule, _bucket = tables_for(REFERENCE)

    assert after.charge_dc_per_ac == pytest.approx(before.charge_dc_per_ac, rel=1e-9)
    assert after.discharge_dc_per_ac == pytest.approx(
        before.discharge_dc_per_ac, rel=1e-9
    )
    assert after.charge_dc_per_ac == pytest.approx(math.sqrt(0.9), rel=1e-6)


# ===========================================================================
# C. the value, and the cost
# ===========================================================================


def power_bound_plan(table: PhysicsTable, *, dear_quarters: int, saleable: float):
    """Return a plan whose binding constraint is power, not energy.

    A short, very expensive window with more saleable energy than it can absorb:
    the only thing that decides revenue is how much power a single quarter can
    express, which is exactly the quantity under test.
    """
    floor = table.limits.energy_for_soc(FLOOR_PERCENT)
    count = 4 + dear_quarters
    prices = [0.10] * 4 + [0.80] * dear_quarters
    horizon = horizon_for(
        table,
        demands=[
            IntervalDemand(index=i, baseline_kwh=0.25, pv_kwh=0.0) for i in range(count)
        ],
        prices=[
            IntervalPrice(import_eur_kwh=v, export_eur_kwh=v * 0.9) for v in prices
        ],
        reserve_kwh=[floor] * count,
    )
    return solve(
        table=table,
        horizon=horizon,
        start_energy_kwh=floor + saleable,
        terminal_floor_kwh=floor,
        minimum_trade_gain_eur=0.0,
        permitted=EVERYTHING,
    )


@pytest.mark.parametrize(("dear_quarters", "saleable"), [(1, 5.0), (2, 8.0), (4, 12.0)])
def test_the_recovered_power_is_worth_money_in_a_short_dear_window(
    dear_quarters: int, saleable: float
) -> None:
    """The 0.5132 kW beta.16 could not express is worth having.

    One short expensive window, more energy than it can take: the aligned lattice
    sells more of it, and the difference is real euros rather than a smaller
    residual in a diagnostic.
    """
    before, after, _rule, _bucket = tables_for(REFERENCE)

    old = power_bound_plan(before, dear_quarters=dear_quarters, saleable=saleable)
    new = power_bound_plan(after, dear_quarters=dear_quarters, saleable=saleable)

    assert new.cost_eur < old.cost_eur - 0.01
    peak = max(i.battery_discharge_ac_kwh for i in new.intervals)
    assert peak / 0.25 == pytest.approx(after.max_representable_discharge_kw, abs=5e-5)


def test_the_inverse_case_buys_more_in_a_single_cheap_quarter() -> None:
    """The charge side, in one very cheap quarter, for the same reason.

    Symmetry is the point: a fix that only helped the revenue side would have
    left half of the loss in place.
    """
    before, after, _rule, _bucket = tables_for(REFERENCE)
    results = []
    for table in (before, after):
        floor = table.limits.energy_for_soc(FLOOR_PERCENT)
        horizon = horizon_for(
            table,
            demands=[
                IntervalDemand(index=i, baseline_kwh=0.25, pv_kwh=0.0) for i in range(6)
            ],
            # One cheap quarter, then an expensive block to sell into.
            prices=[
                IntervalPrice(
                    import_eur_kwh=0.02 if i == 0 else 0.70,
                    export_eur_kwh=0.01 if i == 0 else 0.65,
                )
                for i in range(6)
            ],
            reserve_kwh=[floor] * 6,
        )
        plan = solve(
            table=table,
            horizon=horizon,
            start_energy_kwh=floor,
            terminal_floor_kwh=floor,
            minimum_trade_gain_eur=0.0,
            permitted=EVERYTHING,
        )
        results.append((plan, plan.intervals[0].battery_charge_ac_kwh / 0.25))

    (old_plan, old_kw), (new_plan, new_kw) = results
    assert new_kw > old_kw + 0.4
    assert new_kw == pytest.approx(after.max_representable_charge_kw, abs=5e-5)
    assert new_plan.cost_eur < old_plan.cost_eur


def test_a_plan_the_old_lattice_already_expressed_exactly_is_unchanged() -> None:
    """Where power never binds, the lattice must not change the answer.

    The guard against "it got better on the cases I looked at". A gentle horizon
    whose optimum needs no move near peak power has one economic answer, and both
    lattices must find it -- to within the quantisation of stored energy, which is
    the one thing that legitimately differs.
    """
    before, after, _rule, _bucket = tables_for(REFERENCE)
    plans = []
    for table in (before, after):
        floor = table.limits.energy_for_soc(FLOOR_PERCENT)
        horizon = horizon_for(
            table,
            demands=[
                IntervalDemand(index=i, baseline_kwh=0.30, pv_kwh=0.0)
                for i in range(16)
            ],
            prices=[
                IntervalPrice(
                    import_eur_kwh=0.20 if i < 8 else 0.24,
                    export_eur_kwh=0.10 if i < 8 else 0.12,
                )
                for i in range(16)
            ],
            reserve_kwh=[floor] * 16,
        )
        plans.append(
            solve(
                table=table,
                horizon=horizon,
                start_energy_kwh=floor + 4.0,
                terminal_floor_kwh=floor,
                minimum_trade_gain_eur=0.10,
                permitted=EVERYTHING,
            )
        )

    old, new = plans
    assert old.cost_eur == pytest.approx(new.cost_eur, abs=0.05)
    assert [r.action for r in old.runs] == [r.action for r in new.runs]


def test_the_realignment_does_not_cost_solve_time() -> None:
    """The whole reason this fix is affordable and beta.16's candidate was not.

    Refining the bucket to 0.10 kWh -- the candidate beta.16 costed and rejected
    -- reaches only 9.8663 kW and takes several times as long. Aligning it
    reaches 10.0000 kW on a *smaller* lattice. The bound is loose because a
    shared machine is noisy; the ordering is the claim.
    """
    before, after, _rule, _bucket = tables_for(REFERENCE)
    assert after.buckets <= before.buckets

    limits = limits_of(*REFERENCE)
    floor = limits.energy_for_soc(FLOOR_PERCENT)
    fine = build_physics_table(limits, floor_energy_kwh=floor, bucket_kwh=0.10)
    assert fine is not None
    assert fine.buckets > before.buckets * 2
    # And refining does not even reach the configured power.
    assert fine.max_representable_charge_kw < 10.0

    elapsed = []
    for table in (before, after):
        started = time.perf_counter()
        power_bound_plan(table, dear_quarters=4, saleable=12.0)
        elapsed.append(time.perf_counter() - started)
    assert elapsed[1] < elapsed[0] * 2.0


def test_the_directional_pair_is_reported_not_just_the_maximum() -> None:
    """beta.16's single figure hid the asymmetry. Both must be readable."""
    limits = limits_of(15.0, 7.5, 7.5, 88.0)
    floor = limits.energy_for_soc(FLOOR_PERCENT)
    table = build_physics_table(
        limits, floor_energy_kwh=floor, bucket_kwh=ECONOMIC_BUCKET_KWH
    )
    assert table is not None

    assert table.max_representable_charge_kw != pytest.approx(
        table.max_representable_discharge_kw
    )
    assert table.max_representable_power_kw == pytest.approx(
        max(table.max_representable_charge_kw, table.max_representable_discharge_kw)
    )
    # The figure beta.16 would have published, and the one it hid.
    assert table.max_representable_power_kw == pytest.approx(7.4620, abs=5e-4)
    assert table.max_representable_discharge_kw == pytest.approx(6.5666, abs=5e-4)


def test_a_limits_object_is_hashable_so_the_choice_can_be_memoised() -> None:
    """195 ms of searching is fine once and absurd every quarter-hour."""
    limits: BatteryLimits = limits_of(*REFERENCE)

    assert hash(limits) == hash(limits_of(*REFERENCE))
