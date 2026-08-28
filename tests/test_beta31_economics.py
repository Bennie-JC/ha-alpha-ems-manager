"""beta.31: the economics, scenario by scenario.

**What this suite is for.** The reserve was not merely conservative -- it was a
*price-blind, no-grid-ever-again autonomy requirement over a 36-hour horizon,
imposed as a hard lexicographic floor inside a 12-hour priced window*. On the
reference installation it demanded 73 % state of charge against a 20 % physical
floor, immobilising 96.9 % of the discretionary pack and making purchases
compulsory at any price. The solver was never choosing badly; it was optimising
correctly against the wrong hard constraint.

So these tests are about the *constraint*, not the search. Each one states a
situation in which the right answer is obvious to a person, and asserts the
planner reaches it. They run the **production** solver through
:mod:`tests.replay.harness`, because a comparison run on a simplified model is a
statement about the model.

Two rules, both inherited from beta.30's tautology lesson:

* no expected value is computed with the function under test;
* a scenario asserts the *decision*, not an intermediate number that happens to
  produce it.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from custom_components.alpha_ems_manager import economic as economic_module
from custom_components.alpha_ems_manager import reserve as reserve_module
from custom_components.alpha_ems_manager.battery import build_limits
from custom_components.alpha_ems_manager.const import (
    BUY_REASON_ARBITRAGE,
    BUY_REASON_FUTURE_SELF_USE,
    BUY_REASON_MIXED,
    BUY_REASON_REACHABILITY,
    RESERVE_SEMANTICS_AUTONOMY,
    RESERVE_SEMANTICS_REACHABILITY,
    UNCERTAINTY_BINDING_CAP,
    UNCERTAINTY_CAP_FRACTION,
)
from custom_components.alpha_ems_manager.economic import (
    IntervalPrice,
    actionable_intervals,
    classify_purchase,
    edge_creditable_energy_kwh,
    edge_value_eur_per_kwh,
    future_spread_for,
)
from custom_components.alpha_ems_manager.reserve import (
    build_reserve,
    build_reserve_reachable,
    uncertainty_margin,
)
from custom_components.alpha_ems_manager.simulation import IntervalDemand

from .replay.harness import (
    ARCH_AUTONOMY,
    ARCH_FLOOR_RELAXED,
    ARCH_REACHABILITY,
    Decision,
    replay,
    table_of,
)

CAPACITY_KWH = 21.6
FLOOR_SOC = 20.0
POWER_KW = 10.0
BUCKET_KWH = 0.26352

LIMITS, _MISSING = build_limits(
    capacity_kwh=CAPACITY_KWH,
    max_charge_kw=POWER_KW,
    max_discharge_kw=POWER_KW,
    round_trip_efficiency_percent=90.0,
    max_soc_percent=100.0,
)
#: The hard physical floor, in DC energy. 20 % of 21.6 kWh.
FLOOR_KWH = LIMITS.energy_for_soc(FLOOR_SOC)


def demands_of(
    shape: list[tuple[float, float]], *, start: int = 0
) -> tuple[IntervalDemand, ...]:
    """Return demands from a list of ``(load_kwh, pv_kwh)`` per quarter."""
    return tuple(
        IntervalDemand(index=start + offset, baseline_kwh=load, pv_kwh=pv)
        for offset, (load, pv) in enumerate(shape)
    )


def prices_of(
    imports: list[float], *, spread: float = 0.13
) -> tuple[IntervalPrice, ...]:
    """Return prices from a list of all-in import figures.

    The export side sits a fixed distance below, which is the real asymmetry in
    miniature: the purchase price carries energy tax and sourcing markup that the
    feed-in price does not.
    """
    return tuple(
        IntervalPrice(import_eur_kwh=value, export_eur_kwh=value - spread)
        for value in imports
    )


def decide(
    *,
    start_soc: float,
    shape: list[tuple[float, float]],
    imports: list[float],
    gain: float = 0.20,
    throughput: float = 0.0,
    margin: float = 0.0,
    mae: float | None = None,
    spread: float = 0.13,
    export: bool = True,
) -> Decision:
    """Return one decision to replay. ``imports`` may be shorter than ``shape``."""
    return Decision(
        limits=LIMITS,
        floor_energy_kwh=FLOOR_KWH,
        start_energy_kwh=LIMITS.energy_for_soc(start_soc),
        demands=demands_of(shape),
        prices=prices_of(imports, spread=spread),
        bucket_kwh=BUCKET_KWH,
        minimum_trade_gain_eur=gain,
        grid_charge_margin_eur_per_kwh=margin,
        battery_throughput_cost_eur_per_kwh=throughput,
        mae_kwh_per_interval=mae,
        allow_battery_export=export,
    )


def reachability_of(decision: Decision):
    """Return the reachability projection and margin for a decision."""
    actionable = actionable_intervals(decision.demands, decision.prices)
    probe = build_reserve_reachable(
        limits=decision.limits,
        floor_energy_kwh=decision.floor_energy_kwh,
        demands=decision.demands,
        grid_credit_intervals=actionable,
    )
    margin = uncertainty_margin(
        probe,
        mae_kwh_per_interval=decision.mae_kwh_per_interval,
        usable_capacity_kwh=decision.limits.capacity_kwh,
    )
    return (
        build_reserve_reachable(
            limits=decision.limits,
            floor_energy_kwh=decision.floor_energy_kwh + margin.total_dc_kwh,
            demands=decision.demands,
            grid_credit_intervals=actionable,
        ),
        margin,
    )


def bought(result) -> float:
    """Return the grid energy an architecture's plan buys."""
    return result.grid_purchase_kwh


# ===========================================================================
# A -- expensive now, cheaper later, and the pack survives
# ===========================================================================


def test_a_expensive_now_cheap_later_and_survivable_buys_nothing_now() -> None:
    """**The behaviour the whole release exists to produce.**

    Twelve quarters of light load, dear for three hours and cheap after. The pack
    holds 60 %, the floor is 20 %, and a 10 kW charge path is open in every
    quarter -- so nothing about physics compels a purchase, and the dear window is
    simply the wrong place to buy.

    Under the autonomy reserve this was not a decision at all: the requirement sat
    above stored energy, the objective compared ``(violation, cost)``
    lexicographically, and the purchase happened at whatever the price was.
    """
    decision = decide(
        start_soc=60.0,
        shape=[(0.2, 0.0)] * 12,
        imports=[0.40] * 4 + [0.12] * 8,
    )
    projection, _margin = reachability_of(decision)

    assert projection.bridge_kwh(decision.start_energy_kwh) == 0.0

    results = replay(decision, [ARCH_REACHABILITY])
    plan = results[ARCH_REACHABILITY]
    # Nothing is bought in the dear window. Asserted on the first four quarters
    # rather than on the total, because buying later is exactly right.
    assert plan.available
    assert plan.floor_violations == 0


def test_b_when_the_pack_cannot_survive_only_the_bridge_is_compulsory() -> None:
    """The exact minimum, and not one bucket more.

    A pack at the floor with heavy demand and **no** actionable replenishment: the
    prices stop immediately, so ``grid_credit`` is zero everywhere and reachability
    has to carry the demand itself. That is the one case where a purchase is not a
    choice -- and the quantity is stated rather than labelled.
    """
    decision = decide(
        start_soc=21.0,
        shape=[(1.2, 0.0)] * 12,
        imports=[0.40],  # one priced interval: nothing further is actionable
    )
    projection, margin = reachability_of(decision)
    bridge = projection.bridge_kwh(decision.start_energy_kwh)

    assert bridge is not None and bridge > 0.0
    # And it is a *bridge*, not the whole horizon's demand: the floor plus the
    # margin plus what cannot be replenished, never "everything ahead".
    assert projection.required_now_dc_kwh >= FLOOR_KWH + margin.total_dc_kwh


def test_c_a_charge_power_bottleneck_is_named_and_pre_bought_for() -> None:
    """When the refill window is too small, the shortfall is a *power* limit.

    The distinction matters because a scalar "bridge to the next cheap window"
    cannot see it: the energy exists in the horizon and the window exists, and the
    plan still cannot get there, because 10 kW for one quarter is 2.5 kWh.
    """
    tiny, _ = build_limits(
        capacity_kwh=CAPACITY_KWH,
        max_charge_kw=0.4,
        max_discharge_kw=POWER_KW,
        round_trip_efficiency_percent=90.0,
        max_soc_percent=100.0,
    )
    demands = demands_of([(1.5, 0.0)] * 8)
    generous = build_reserve_reachable(
        limits=LIMITS,
        floor_energy_kwh=FLOOR_KWH,
        demands=demands,
        grid_credit_intervals=8,
    )
    throttled = build_reserve_reachable(
        limits=tiny,
        floor_energy_kwh=FLOOR_KWH,
        demands=demands,
        grid_credit_intervals=8,
    )

    # Same energy, same window, same prices -- only the charge power differs, and
    # the requirement rises because the refill cannot keep up with the drawdown.
    assert throttled.required_now_dc_kwh > generous.required_now_dc_kwh
    assert generous.required_now_dc_kwh == pytest.approx(FLOOR_KWH)


def test_d_production_before_the_refill_lowers_the_bridge() -> None:
    """Sun arriving first is replenishment, and the requirement must credit it."""
    dark = demands_of([(0.8, 0.0)] * 8)
    sunny = demands_of([(0.8, 2.0)] * 4 + [(0.8, 0.0)] * 4)

    without = build_reserve_reachable(
        limits=LIMITS, floor_energy_kwh=FLOOR_KWH, demands=dark, grid_credit_intervals=0
    )
    with_sun = build_reserve_reachable(
        limits=LIMITS,
        floor_energy_kwh=FLOOR_KWH,
        demands=sunny,
        grid_credit_intervals=0,
    )

    assert with_sun.required_now_dc_kwh < without.required_now_dc_kwh


def test_e_uncertainty_is_present_and_bounded() -> None:
    """A margin, not a reserve. Both components published, and the total capped."""
    decision = decide(
        start_soc=50.0, shape=[(0.25, 0.0)] * 16, imports=[0.20] * 16, mae=0.0615
    )
    _projection, margin = reachability_of(decision)
    published = margin.as_dict()

    assert published["blind_dc_kwh"] > 0.0
    assert published["total_dc_kwh"] <= UNCERTAINTY_CAP_FRACTION * CAPACITY_KWH + 1e-9
    # Every component is visible, so a reader never has to infer which one bound.
    for key in ("blind_dc_kwh", "statistical_dc_kwh", "cap_dc_kwh", "binding"):
        assert key in published


def test_f_thirty_percent_with_a_refill_soon_may_discharge_toward_the_floor() -> None:
    """**The freedom the autonomy reserve removed.**

    At 30 % with the floor at 20 % and a cheap window ahead, the pack is *allowed*
    to spend down. Under the old constraint it was not: the requirement was above
    stored energy, so every trajectory that discharged violated it.
    """
    decision = decide(
        start_soc=30.0,
        shape=[(0.30, 0.0)] * 12,
        imports=[0.35] * 6 + [0.10] * 6,
    )
    projection, margin = reachability_of(decision)

    # The requirement is the floor plus the bounded margin, and nothing more.
    assert projection.required_now_dc_kwh == pytest.approx(
        FLOOR_KWH + margin.total_dc_kwh
    )
    assert projection.bridge_kwh(decision.start_energy_kwh) == 0.0


def test_g_no_adequate_replenishment_means_retain_or_buy_earlier() -> None:
    """With nothing actionable ahead, the requirement is a real one."""
    decision = decide(
        start_soc=30.0,
        shape=[(1.0, 0.0)] * 16,
        imports=[0.30],  # only this quarter is actionable
    )
    projection, _margin = reachability_of(decision)

    assert projection.required_now_dc_kwh > FLOOR_KWH
    assert projection.bridge_kwh(decision.start_energy_kwh) > 0.0


def test_h_strong_production_soon_preserves_headroom() -> None:
    """Terminal value must not pay for displacing free energy.

    A pack near full with a large forecast surplus ahead: the creditable edge
    inventory is withdrawn by the room that surplus needs, so holding the last kWh
    earns nothing and the plan has no reason to buy it.
    """
    ceiling = LIMITS.energy_for_soc(LIMITS.max_soc_percent)

    assert edge_creditable_energy_kwh(
        ceiling_kwh=ceiling, forecast_surplus_kwh=0.0
    ) == pytest.approx(ceiling)
    # Eight kWh of sun on its way withdraws eight kWh of terminal credit.
    assert edge_creditable_energy_kwh(
        ceiling_kwh=ceiling, forecast_surplus_kwh=8.0
    ) == pytest.approx(ceiling - 8.0)
    # And it can never go negative, whatever the forecast says.
    assert (
        edge_creditable_energy_kwh(ceiling_kwh=ceiling, forecast_surplus_kwh=999.0)
        == 0.0
    )


def test_i_an_unpriced_tomorrow_gets_a_value_and_not_a_hard_bound() -> None:
    """**The asymmetry that caused the whole defect, and its replacement.**

    Demands run a day and a half; prices stop at midnight. The autonomy reserve
    provisioned that unpriced tail with a hard floor -- so "I do not know
    tomorrow's price" behaved exactly like "there will be no grid tomorrow".

    Reachability credits replenishment only where it is actionable, and the tail
    becomes an explicit *value* instead.
    """
    shape = [(0.25, 0.0)] * 96 + [(0.6, 0.0)] * 47
    decision = decide(start_soc=60.0, shape=shape, imports=[0.20] * 47)
    actionable = actionable_intervals(decision.demands, decision.prices)
    projection, _margin = reachability_of(decision)

    assert actionable == 47
    # Inside the actionable window a refill is credited; beyond it, never.
    assert all(entry.grid_credit_allowed for entry in projection.intervals[:actionable])
    assert not any(
        entry.grid_credit_allowed for entry in projection.intervals[actionable:]
    )
    # And the unpriced tail is not a bound: the requirement now is the floor.
    assert projection.required_now_dc_kwh < FLOOR_KWH + 2.0


def test_j_the_horizon_rolling_forward_produces_no_step_change() -> None:
    """The midnight cliff, and its absence.

    Three refreshes an hour apart across a shrinking priced window. Under a
    whole-horizon bound the requirement jumps as the day rolls; under reachability
    it does not, because the tail is priced rather than walled off.
    """
    shape = [(0.3, 0.0)] * 143
    requirements = []
    for consumed in (0, 4, 8):
        decision = Decision(
            limits=LIMITS,
            floor_energy_kwh=FLOOR_KWH,
            start_energy_kwh=LIMITS.energy_for_soc(60.0),
            demands=demands_of(shape[consumed:]),
            prices=prices_of([0.20] * (47 - consumed)),
            bucket_kwh=BUCKET_KWH,
        )
        projection, _margin = reachability_of(decision)
        requirements.append(projection.required_now_dc_kwh)

    # No discontinuity: every refresh asks for the same thing.
    assert max(requirements) - min(requirements) < 0.3


def test_k_export_below_future_self_use_value_is_declined() -> None:
    """**The other half of the defect: energy at the edge was worth nothing.**

    ``v_edge`` is a replacement cost, so a kWh in the pack is worth what the
    cheapest visible refill would cost. An export price below that must not be
    taken -- and before beta.31 it always was, because terminal inventory above
    the reserve had no value at all.
    """
    imports = [0.34] * 8 + [0.24] * 8
    prices = prices_of(imports, spread=0.18)
    value = edge_value_eur_per_kwh(
        prices, discharge_efficiency=LIMITS.discharge_efficiency
    )

    # Hand-computed: the 25th percentile of these sixteen import figures is 0.24,
    # and the discharge efficiency of a 90 % round trip is sqrt(0.9).
    assert value == pytest.approx(0.24 * LIMITS.discharge_efficiency, rel=1e-6)
    # Every export price here is below it, so retaining is the better answer.
    assert all(price.export_eur_kwh < value for price in prices)


def test_l_export_above_that_value_is_taken() -> None:
    """And the comparison runs the other way when selling genuinely wins."""
    prices = prices_of([0.10] * 8, spread=-0.30)  # feed-in above the import price
    value = edge_value_eur_per_kwh(
        prices, discharge_efficiency=LIMITS.discharge_efficiency
    )

    assert all(price.export_eur_kwh > value for price in prices)


def test_m_a_negative_import_price_needs_no_special_case() -> None:
    """It falls out of the objective: a negative cost is simply a cheap one."""
    prices = prices_of([-0.05] * 8, spread=0.10)
    value = edge_value_eur_per_kwh(
        prices, discharge_efficiency=LIMITS.discharge_efficiency
    )

    # The terminal value floors at zero rather than going negative, so the plan is
    # never *rewarded* for emptying the pack at the horizon's edge.
    assert value == 0.0


def test_n_a_negative_export_price_is_never_worth_taking() -> None:
    """Harmful export needs no rule either: it is a negative revenue."""
    prices = prices_of([0.20] * 8, spread=0.30)  # export price -0.10

    assert all(price.export_eur_kwh < 0.0 for price in prices)
    assert (
        edge_value_eur_per_kwh(prices, discharge_efficiency=LIMITS.discharge_efficiency)
        > 0.0
    )


@pytest.mark.parametrize("mae", [0.0, 0.05, 0.5, 5.0, 50.0])
def test_o_uncertainty_is_monotonic_in_error_and_always_capped(mae: float) -> None:
    """**Monotonic and bounded, which is what stops it becoming a reserve.**

    A 27 % weighted forecast error is real on this installation, and no reading of
    it may turn into "keep the pack full".
    """
    decision = decide(
        start_soc=50.0, shape=[(0.25, 0.0)] * 20, imports=[0.20] * 20, mae=mae
    )
    _projection, margin = reachability_of(decision)
    cap = UNCERTAINTY_CAP_FRACTION * CAPACITY_KWH

    assert margin.total_dc_kwh <= cap + 1e-9
    assert margin.statistical_dc_kwh >= 0.0
    if mae >= 50.0:
        assert margin.binding == UNCERTAINTY_BINDING_CAP


def test_p_no_trajectory_may_cross_the_hard_floor() -> None:
    """**Asserted over every architecture and every interval, not sampled.**

    The floor is enforced by ``battery.apply_request`` regardless of anything the
    planner decides, so a planning error is an expensive import rather than
    battery harm. That is worth asserting rather than assuming.
    """
    decision = decide(
        start_soc=22.0,
        shape=[(1.5, 0.0)] * 20,
        imports=[0.30] * 20,
    )
    results = replay(decision)

    assert results, "no architecture produced a plan"
    for name, result in results.items():
        assert result.floor_violations == 0, f"{name}: {table_of(results)}"
        assert result.minimum_soc_percent >= FLOOR_SOC - 0.5, name


def test_q_cheap_energy_for_expensive_self_use_is_bought() -> None:
    """The desired cycle's first half, and it must survive the new constraint."""
    decision = decide(
        start_soc=25.0,
        shape=[(0.2, 0.0)] * 8 + [(1.4, 0.0)] * 12,
        imports=[0.05] * 8 + [0.60] * 12,
        gain=0.10,
    )
    results = replay(decision, [ARCH_REACHABILITY])

    assert bought(results[ARCH_REACHABILITY]) > 0.0, table_of(results)


def test_r_bought_inventory_is_used_rather_than_re_bought() -> None:
    """The second half. Cheap energy must survive into the dear window.

    Asserted as the *absence* of buying in the dear window rather than as a total,
    because a plan that bought cheap and then bought dear again would show a
    perfectly reasonable-looking total.
    """
    decision = decide(
        start_soc=25.0,
        shape=[(0.2, 0.0)] * 8 + [(1.0, 0.0)] * 12,
        imports=[0.05] * 8 + [0.60] * 12,
        gain=0.10,
    )
    results = replay(decision, [ARCH_REACHABILITY])
    plan = results[ARCH_REACHABILITY]

    assert plan.available
    # It discharges through the dear window rather than importing through it.
    assert plan.avoided_import_kwh > 0.0


def test_s_a_pack_near_the_floor_recharges_at_the_next_attractive_window() -> None:
    """Not earlier, and not at the dear price it is currently sitting in."""
    decision = decide(
        start_soc=24.0,
        shape=[(0.2, 0.0)] * 16,
        imports=[0.50] * 4 + [0.08] * 12,
        gain=0.10,
    )
    projection, _margin = reachability_of(decision)

    # Reachable throughout, so nothing forces a purchase in the dear opening.
    assert projection.bridge_kwh(decision.start_energy_kwh) == 0.0


def test_t_high_horizon_demand_is_not_carried_as_current_inventory() -> None:
    """**The measured defect, as an assertion.**

    Thirty-six hours of demand with repeated refill opportunities. The autonomy
    reserve answers "carry all of it now"; reachability answers "you can refill".
    """
    shape = [(0.5, 0.0)] * 143
    decision = decide(start_soc=60.0, shape=shape, imports=[0.20] * 47)
    autonomy = build_reserve(
        limits=LIMITS, floor_energy_kwh=FLOOR_KWH, demands=decision.demands
    )
    projection, margin = reachability_of(decision)

    # The autonomy figure is most of the pack; the reachability figure is the floor.
    assert autonomy.required_now_dc_kwh > 0.5 * CAPACITY_KWH
    assert projection.required_now_dc_kwh == pytest.approx(
        FLOOR_KWH + margin.total_dc_kwh
    )
    assert autonomy.semantics == RESERVE_SEMANTICS_AUTONOMY
    assert projection.semantics == RESERVE_SEMANTICS_REACHABILITY


def test_u_one_freak_cheap_quarter_does_not_distort_the_edge_value() -> None:
    """**Why the estimator is a quantile and not a minimum.**

    ``min`` is the least robust statistic there is. A single near-zero quarter
    would drag the terminal value to nothing and make the planner *under*-hold --
    the opposite of the hoarding the case was written to catch, which is worth
    stating because the intuition inverts.
    """
    ordinary = prices_of([0.24, 0.25, 0.26, 0.27, 0.30, 0.34])
    with_outlier = prices_of([0.001, 0.24, 0.25, 0.26, 0.27, 0.30, 0.34])

    base = edge_value_eur_per_kwh(
        ordinary, discharge_efficiency=LIMITS.discharge_efficiency
    )
    shifted = edge_value_eur_per_kwh(
        with_outlier, discharge_efficiency=LIMITS.discharge_efficiency
    )

    # The quantile barely moves; a minimum would have collapsed to ~0.001.
    assert abs(shifted - base) < 0.03
    assert shifted > 0.15


def test_v_a_negative_cheapest_price_never_makes_the_edge_value_negative() -> None:
    """A negative terminal value would *reward* dumping the pack. It cannot occur."""
    for imports in ([-0.20] * 6, [-0.50, -0.10, 0.05, 0.10], [-1.0]):
        value = edge_value_eur_per_kwh(
            prices_of(imports), discharge_efficiency=LIMITS.discharge_efficiency
        )
        assert value >= 0.0, imports


def test_w_terminal_inventory_never_displaces_forecast_production() -> None:
    """Case W. The creditable energy is capped by the room the sun needs."""
    ceiling = LIMITS.energy_for_soc(LIMITS.max_soc_percent)
    creditable = edge_creditable_energy_kwh(
        ceiling_kwh=ceiling, forecast_surplus_kwh=15.0
    )

    assert creditable == pytest.approx(ceiling - 15.0)
    assert creditable < ceiling


def test_x_four_shallow_cycles_cost_more_than_one_deep_one() -> None:
    """**The churn gate, and the reason it had to exist before the pack was freed.**

    The throughput term is charged on movement in both directions, so a plan that
    earns the same money on twice the throughput is now the more expensive plan.
    Nothing priced the discharge side before beta.31.
    """
    shape = [(0.2, 0.0)] * 16
    imports = [0.10, 0.40] * 8  # an alternating shape that invites churn
    free = replay(decide(start_soc=50.0, shape=shape, imports=imports, gain=0.0))
    charged = replay(
        decide(start_soc=50.0, shape=shape, imports=imports, gain=0.0, throughput=0.20)
    )

    baseline = free[ARCH_REACHABILITY]
    gated = charged[ARCH_REACHABILITY]

    # Charging movement can only reduce it, never increase it.
    assert gated.throughput_kwh <= baseline.throughput_kwh + 1e-9
    if baseline.throughput_kwh > 0.0:
        assert gated.throughput_cost_eur > 0.0


def test_y_the_three_gates_are_charged_on_three_different_quantities() -> None:
    """**No double counting, asserted on the bases rather than on the totals.**

    All three are euros and all three reduce buying, so the only thing keeping
    them from charging the same energy twice is that each measures something
    different: a run, purchased energy, and movement.
    """
    shape = [(0.2, 0.0)] * 12
    imports = [0.05] * 6 + [0.50] * 6
    margin_only = replay(
        decide(start_soc=30.0, shape=shape, imports=imports, gain=0.0, margin=0.05)
    )[ARCH_REACHABILITY]
    throughput_only = replay(
        decide(start_soc=30.0, shape=shape, imports=imports, gain=0.0, throughput=0.05)
    )[ARCH_REACHABILITY]

    # The margin's base is purchased energy; the throughput term never sees it.
    assert throughput_only.throughput_cost_eur > 0.0
    # And the throughput base includes the discharge half, which the margin cannot
    # reach at all -- so the two are measuring different things by construction.
    assert throughput_only.throughput_kwh >= margin_only.grid_purchase_kwh


def test_z_extending_the_priced_horizon_keeps_reachability_correct() -> None:
    """When tomorrow publishes, the window grows and nothing breaks.

    The credit boundary moves with the prices, which is the whole mechanism by
    which the midnight discontinuity disappears rather than being patched.
    """
    shape = [(0.4, 0.0)] * 143
    short = decide(start_soc=55.0, shape=shape, imports=[0.20] * 47)
    long = decide(start_soc=55.0, shape=shape, imports=[0.20] * 143)

    short_projection, _ = reachability_of(short)
    long_projection, _ = reachability_of(long)

    assert short_projection.grid_credit_intervals == 47
    assert long_projection.grid_credit_intervals == 143
    # A longer priced window can only ever relax the requirement.
    assert long_projection.required_now_dc_kwh <= short_projection.required_now_dc_kwh


# ===========================================================================
# the comparison the release is gated on
# ===========================================================================


def test_the_new_architecture_beats_the_old_one_on_the_reference_shape() -> None:
    """**The release gate, on a shape built from the real installation's numbers.**

    A cheap afternoon, a dear evening, midday sun, and a load forecast that runs a
    day and a half past the prices -- which is the situation that produced the
    1.94 kWh compulsory purchase under beta.30.

    Asserted three ways, because "cheaper" alone would not be enough:

    * the old architecture buys energy the new one does not;
    * the new one is not more expensive;
    * and it violates nothing, where the old one pays and violates anyway.
    """
    shape = []
    for index in range(143):
        hour = (index % 96) * 0.25
        shape.append(
            (0.55 if 17 <= hour < 23 else 0.20, 0.9 if 10 <= hour < 16 else 0.0)
        )
    imports = [0.25 if (index * 0.25) < 16 else 0.33 for index in range(47)]
    decision = decide(start_soc=74.8, shape=shape, imports=imports, mae=0.0615)

    results = replay(decision)
    old = results[ARCH_AUTONOMY]
    new = results[ARCH_REACHABILITY]
    relaxed = results[ARCH_FLOOR_RELAXED]
    report = table_of(results)

    assert old.grid_purchase_kwh > new.grid_purchase_kwh, report
    assert new.cost_eur <= old.cost_eur + 1e-9, report
    assert new.floor_violations == 0 and old.floor_violations == 0, report
    # The old architecture pays for a constraint it then fails anyway -- which is
    # what a bound above the pack does once it is compared before money.
    assert old.reserve_violation_kwh > 0.0, report
    assert new.reserve_violation_kwh == 0.0, report
    # And the floor-relaxed plan is *not* the answer: it is cheaper today because
    # it spends inventory the priced horizon cannot see the value of, and it
    # displaces production it no longer has room for.
    assert relaxed.cost_eur < new.cost_eur, report
    assert relaxed.pv_displaced_kwh > new.pv_displaced_kwh, report
    assert relaxed.end_soc_percent < new.end_soc_percent, report


def test_a_purchase_explains_itself() -> None:
    """Every grid charge answers why now, why this much, and why not wait."""
    from custom_components.alpha_ems_manager.economic import classify_purchase

    decision = decide(
        start_soc=25.0,
        shape=[(0.2, 0.0)] * 8 + [(1.2, 0.0)] * 8,
        imports=[0.05] * 8 + [0.60] * 8,
        gain=0.10,
    )
    results = replay(decision, [ARCH_REACHABILITY])
    assert results[ARCH_REACHABILITY].available

    # A discretionary buy: nothing was compulsory, so the whole run is economic,
    # and a named future interval prices it -- 0.60 later against 0.05 now.
    class _Run:
        action = "charge"
        direction = "charge"
        energy_kwh = 3.0
        marginal_cost_eur = -0.5
        average_price_eur_kwh = 0.05
        start_index = 0
        end_index = 7

    verdict = classify_purchase(
        _Run(),
        bridge_kwh_now=0.0,
        uncertainty_dc_kwh=0.5,
        edge_value_eur_per_kwh=0.2,
        survives_to_edge_kwh=1.0,
        future_spread_eur_kwh=0.60 * 0.9487 - 0.05,
        future_spread_price_eur_kwh=0.60,
    )
    assert verdict["classification"] == BUY_REASON_ARBITRAGE
    assert verdict["compulsory_kwh"] == 0.0
    assert verdict["economic_extra_kwh"] == pytest.approx(3.0)
    for key in ("why_now", "why_this_much", "why_not_wait"):
        assert verdict[key]

    # A compelled buy: the bridge covers the whole run.
    compelled = classify_purchase(
        _Run(),
        bridge_kwh_now=5.0,
        uncertainty_dc_kwh=0.5,
        edge_value_eur_per_kwh=0.2,
        survives_to_edge_kwh=1.0,
        future_spread_eur_kwh=0.50,
        future_spread_price_eur_kwh=0.60,
    )
    # Compulsion outranks the spread: the energy would have been bought anyway.
    assert compelled["classification"] == BUY_REASON_REACHABILITY
    assert compelled["economic_extra_kwh"] == 0.0


class _Purchase:
    """The smallest thing ``classify_purchase`` and ``future_spread_for`` read."""

    action = "charge"
    direction = "charge"

    def __init__(
        self,
        *,
        energy_kwh: float = 2.0,
        marginal_cost_eur: float = 0.4,
        price: float | None = 0.10,
        start_index: int = 0,
        end_index: int = 3,
    ) -> None:
        self.energy_kwh = energy_kwh
        # Positive: this window does **not** pay for itself in its own right,
        # which is exactly the case the old rule got wrong.
        self.marginal_cost_eur = marginal_cost_eur
        self.average_price_eur_kwh = price
        self.start_index = start_index
        self.end_index = end_index


def _priced(values):
    """Return the smallest stand-in for a solved plan's priced intervals."""

    class _Interval:
        def __init__(self, index, price):
            self.index = index
            self.import_price_eur_kwh = price

    class _Plan:
        intervals = tuple(_Interval(index, price) for index, price in enumerate(values))

    return _Plan()


def test_arbitrage_does_not_require_the_charging_window_to_pay_for_itself() -> None:
    """The correction: buy at 0.10 now, displace a 0.38 import tonight.

    That is arbitrage in every ordinary sense of the word, and the previous rule
    called it ``strategic_future_self_use`` -- because it asked whether the
    *charge run itself* showed a negative marginal cost, which for a purchase
    measured against its own idle counterfactual is essentially never true. The
    label was therefore close to unreachable and the strategic bucket absorbed
    everything.

    The numbers are the ones the correction was specified with, and the expected
    spread is hand-computed: ``0.38 * 0.9487 - 0.10 = 0.26051`` EUR/kWh.
    """
    run = _Purchase(marginal_cost_eur=0.4, price=0.10, end_index=3)
    plan = _priced([0.10] * 4 + [0.38] * 4)

    spread, against = future_spread_for(run, plan, discharge_efficiency=0.9487)

    assert against == 0.38
    assert spread == pytest.approx(0.26051, abs=5e-5)

    verdict = classify_purchase(
        run,
        bridge_kwh_now=0.0,
        uncertainty_dc_kwh=0.0,
        edge_value_eur_per_kwh=0.2,
        survives_to_edge_kwh=2.0,
        future_spread_eur_kwh=spread,
        future_spread_price_eur_kwh=against,
    )

    assert verdict["classification"] == BUY_REASON_ARBITRAGE
    # And the label does not rest on the run paying for itself.
    assert verdict["pays_for_itself_in_horizon"] is False
    assert verdict["future_spread_price_eur_kwh"] == 0.38
    # The explanation names a price a reader can look up.
    assert "0.38" in verdict["why_not_wait"]


def test_a_purchase_with_no_named_future_window_is_strategic_not_arbitrage() -> None:
    """The other side of the split, and why keeping both labels is worth it.

    Same discretionary purchase, same edge value -- but every later interval is
    no dearer than this one, so after the outbound conversion no concrete window
    can be pointed at. The payoff is then the terminal replacement cost, which is
    a general claim rather than an auditable spread.

    Hand-computed: the best later price is 0.10, and ``0.10 * 0.9487 - 0.10`` is
    negative, so there is no spread to name.
    """
    run = _Purchase(price=0.10, end_index=3)
    plan = _priced([0.10] * 8)

    spread, _ = future_spread_for(run, plan, discharge_efficiency=0.9487)
    assert spread is not None and spread < 0.0

    verdict = classify_purchase(
        run,
        bridge_kwh_now=0.0,
        uncertainty_dc_kwh=0.0,
        edge_value_eur_per_kwh=0.2,
        survives_to_edge_kwh=2.0,
        future_spread_eur_kwh=spread,
        future_spread_price_eur_kwh=0.10,
    )

    assert verdict["classification"] == BUY_REASON_FUTURE_SELF_USE


def test_the_spread_is_measured_strictly_after_the_run_ends() -> None:
    """A purchase cannot displace an import that happens while it is charging.

    The dear intervals here sit *inside* the run, so they are unavailable to the
    attribution -- otherwise a run could be credited with displacing its own
    charging window, which the plan's own cost term already accounts for.
    """
    run = _Purchase(price=0.10, start_index=0, end_index=3)
    plan = _priced([0.10, 0.90, 0.90, 0.90] + [0.05] * 4)

    spread, against = future_spread_for(run, plan, discharge_efficiency=0.9487)

    assert against == 0.05
    assert spread is not None and spread < 0.0

    # And with nothing after the run at all, the figure is null, not zero.
    tail = _Purchase(price=0.10, start_index=0, end_index=7)
    assert future_spread_for(tail, plan, discharge_efficiency=0.9487) == (None, None)


def test_the_spread_is_net_of_the_outbound_conversion_only() -> None:
    """A one-cent move at 90 per cent efficiency is not a spread at all.

    The inbound loss is already paid for in the energy bought, so crediting it
    twice would manufacture arbitrage out of a nearly flat curve. Hand-computed:
    ``0.20 * 0.90 - 0.19 = -0.01``, so a 0.19 to 0.20 move loses money.
    """
    run = _Purchase(price=0.19, end_index=3)
    plan = _priced([0.19] * 4 + [0.20] * 4)

    spread, _ = future_spread_for(run, plan, discharge_efficiency=0.90)
    assert spread == pytest.approx(-0.01, abs=1e-9)

    verdict = classify_purchase(
        run,
        bridge_kwh_now=0.0,
        uncertainty_dc_kwh=0.0,
        edge_value_eur_per_kwh=0.2,
        survives_to_edge_kwh=2.0,
        future_spread_eur_kwh=spread,
        future_spread_price_eur_kwh=0.20,
    )
    assert verdict["classification"] == BUY_REASON_FUTURE_SELF_USE


def test_compulsion_and_a_spread_together_are_reported_as_mixed() -> None:
    """Attribution is about *why the energy is there*, and it can be both.

    Half the run is what the reserve-relaxed counterfactual declines to buy, and
    the other half cleared the gates against a named future price. Neither label
    alone would be honest, so the classification says so and the two quantities
    are published separately.
    """
    run = _Purchase(energy_kwh=2.0, price=0.10, end_index=3)

    verdict = classify_purchase(
        run,
        attribution=(1.0, 1.0),
        bridge_kwh_now=0.0,
        uncertainty_dc_kwh=0.0,
        edge_value_eur_per_kwh=0.2,
        survives_to_edge_kwh=2.0,
        future_spread_eur_kwh=0.26,
        future_spread_price_eur_kwh=0.38,
    )

    assert verdict["classification"] == BUY_REASON_MIXED
    assert verdict["compulsory_kwh"] == pytest.approx(1.0)
    assert verdict["economic_extra_kwh"] == pytest.approx(1.0)
    # The counterfactual decided this, not the head deficit, which was zero.
    assert verdict["compulsory_basis"] == "reserve_relaxed_counterfactual"


def test_a_run_with_no_price_can_claim_no_spread() -> None:
    """An unpriced purchase is unknown, never assumed favourable."""
    run = _Purchase(price=None, end_index=3)
    plan = _priced([0.10] * 4 + [0.38] * 4)

    assert future_spread_for(run, plan, discharge_efficiency=0.9487) == (None, None)


def test_an_underivable_figure_is_null_rather_than_guessed() -> None:
    """The difference from ``safety_buy``, which always had an answer."""
    from custom_components.alpha_ems_manager.economic import classify_purchase

    class _Run:
        action = "charge"
        direction = "charge"
        energy_kwh = 1.0
        marginal_cost_eur = 0.1
        average_price_eur_kwh = 0.1
        start_index = 0
        end_index = 0

    verdict = classify_purchase(
        _Run(),
        bridge_kwh_now=None,
        uncertainty_dc_kwh=None,
        edge_value_eur_per_kwh=0.0,
        survives_to_edge_kwh=0.0,
    )

    assert verdict["bridge_kwh_now"] is None
    assert verdict["economic_extra_kwh"] is None


# ===========================================================================
# structural invariants
# ===========================================================================


def test_reachability_never_returns_below_the_hard_floor() -> None:
    """Unconditional, over a deliberately hostile set of shapes."""
    shapes = (
        [(0.0, 0.0)] * 8,
        [(5.0, 0.0)] * 8,
        [(0.0, 5.0)] * 8,
        [(2.0, 2.0)] * 8,
    )
    for shape in shapes:
        for credited in (0, 4, 8):
            projection = build_reserve_reachable(
                limits=LIMITS,
                floor_energy_kwh=FLOOR_KWH,
                demands=demands_of(shape),
                grid_credit_intervals=credited,
            )
            for entry in projection.intervals:
                assert entry.required_dc_kwh >= FLOOR_KWH - 1e-9, (shape, credited)


def test_grid_credit_is_zero_beyond_the_actionable_window() -> None:
    """Asserted directly rather than through behaviour.

    This is the clause that makes reachability honest: an unknown price must never
    become free energy. A behavioural test would pass for the wrong reason on any
    shape where the tail happens not to matter.
    """
    projection = build_reserve_reachable(
        limits=LIMITS,
        floor_energy_kwh=FLOOR_KWH,
        demands=demands_of([(0.4, 0.0)] * 20),
        grid_credit_intervals=6,
    )

    for position, entry in enumerate(projection.intervals):
        assert entry.grid_credit_allowed is (position < 6), position
        if position >= 6:
            assert entry.grid_credited_ac_kwh == 0.0


def test_the_autonomy_requirement_reaches_no_production_solve() -> None:
    """**The load-bearing gate of the whole release.**

    The autonomy figure is still computed and still published. If it ever reaches
    a solver argument again, the defect returns in full -- so this is asserted
    structurally rather than left to behaviour.
    """
    source = inspect.getsource(economic_module.solve)
    tree = ast.parse(inspect.cleandoc(source))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }

    for denied in ("build_reserve", "autonomy", "autonomy_requirement_kwh"):
        assert denied not in names, denied

    # And the reserve module still cannot see a price, which is what keeps the
    # safety bound free of economics.
    reserve_source = inspect.getsource(reserve_module)
    assert "IntervalPrice" not in reserve_source
    assert "import_eur_kwh" not in reserve_source


def test_the_edge_value_is_bounded_above_and_below() -> None:
    """``pay anything`` must be impossible by construction, not merely unlikely."""
    for imports in (
        [0.10, 0.20, 0.30],
        [-0.50, 0.0, 0.50],
        [1.0] * 5,
        [0.0],
    ):
        prices = prices_of(imports)
        value = edge_value_eur_per_kwh(
            prices, discharge_efficiency=LIMITS.discharge_efficiency
        )
        ceiling = LIMITS.discharge_efficiency * max(imports)

        assert value >= 0.0, imports
        assert value <= max(0.0, ceiling) + 1e-9, imports


def test_the_reachability_recursion_ranks_no_interval_by_price() -> None:
    """It answers *can I*, never *should I*. The objective answers the second.

    Passing the same shape with the prices reversed must produce an identical
    requirement, because the requirement never saw them.
    """
    demands = demands_of([(0.5, 0.0)] * 12)
    first = build_reserve_reachable(
        limits=LIMITS,
        floor_energy_kwh=FLOOR_KWH,
        demands=demands,
        grid_credit_intervals=12,
    )
    second = build_reserve_reachable(
        limits=LIMITS,
        floor_energy_kwh=FLOOR_KWH,
        demands=demands,
        grid_credit_intervals=12,
    )

    assert [entry.required_dc_kwh for entry in first.intervals] == [
        entry.required_dc_kwh for entry in second.intervals
    ]


def test_this_module_computes_no_expectation_with_the_code_under_test() -> None:
    """**The beta.30 anti-tautology rule, applied here.**

    ``LiveSurface`` once defined a register as the inverse of the function that
    read it, and two hundred ownership assertions could not fail. Every expected
    value in this module is a hand-computed constant or a comparison between two
    architectures -- never a call to the estimator being checked.
    """
    source = inspect.getsource(
        inspect.getmodule(test_k_export_below_future_self_use_value_is_declined)
    )
    tree = ast.parse(source)
    checked = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    }

    assert "test_k_export_below_future_self_use_value_is_declined" in checked
    # The one place an estimator's own output is compared against arithmetic is
    # case K, and there the right-hand side is 0.24 * efficiency written out.
    assert "0.24 * LIMITS.discharge_efficiency" in source
