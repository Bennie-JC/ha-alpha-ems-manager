"""What the optimizer says about itself, and the proof that it is now true.

beta.16 changed almost nothing about *what the optimizer decides*. It changed what
the plan reports, because a live diagnostics download made four things read as
defects that were not:

* ``23.19 kWh`` of battery charging read as ``23.19 kWh`` of buying. It was not:
  most of it was the sun, and the ``grid_import_kwh`` printed beside it was
  **site** import including house load, which is a third quantity again.
* Seven runs read as seven trades. The switching fee had been charged three
  times: one physical discharge carries both the ``discharge`` and ``export``
  labels as house load rises and falls beneath it.
* Every charge run showed a negative ``expected_value_eur``. It always will --
  the field was a negated cash flow, and a charge imports at a positive price by
  construction. Worse, a discharge that exactly covers house load read ``0.00``
  while saving the entire import bill.
* ``+2.243 EUR`` was measured against a baseline that *sold* the surplus
  production the plan was forced to *bank*. Two different physical worlds.

Each section below fixes one of those and proves it against the real solver. Two
genuine model defects are also covered: absorption splitting a paid charging
campaign, and the power the state space can actually represent.

Arithmetic is asserted at exact values wherever the arithmetic is exact.
"""

from __future__ import annotations

import ast
import math
import pathlib
import time

import pytest

from custom_components.alpha_ems_manager import economic as economic_module
from custom_components.alpha_ems_manager.battery import (
    INTERVAL_HOURS,
    build_state,
    static_reserve,
)
from custom_components.alpha_ems_manager.const import (
    ECONOMIC_ACTION_CHARGE,
    ECONOMIC_ACTION_CURTAIL,
    ECONOMIC_ACTION_DISCHARGE,
    ECONOMIC_ACTION_EXPORT,
    ECONOMIC_BUCKET_KWH,
    ECONOMIC_CHARGE_SOURCE_GRID,
    ECONOMIC_CHARGE_SOURCE_MIXED,
    ECONOMIC_CHARGE_SOURCE_NONE,
    ECONOMIC_CHARGE_SOURCE_PRODUCTION,
    ECONOMIC_DIRECTION_CHARGE,
    ECONOMIC_DIRECTION_DISCHARGE,
)
from custom_components.alpha_ems_manager.economic import (
    IntervalPrice,
    build_horizon,
    hold_cost,
    solve,
)
from custom_components.alpha_ems_manager.policy import HoldPolicy
from custom_components.alpha_ems_manager.simulation import IntervalDemand, simulate

from .test_economic_actions import outcome_for
from .test_economic_model import (
    ETA,
    EVERYTHING,
    FLOOR_PERCENT,
    IMPLEMENTED,
    START_KWH,
    eight_interval_horizon,
    flat_demands,
    horizon_for,
    reference_table,
    solved,
    two_tier_prices,
)

#: One quarter's charge at the largest representable power, in AC kWh. Every
#: hand-checked figure below is a multiple of it.
FULL_CHARGE_AC = 2.25 / ETA


def arbitrage(
    table,
    *,
    pv_kwh: float = 0.0,
    pv_only_at: int | None = None,
    load_kwh: float = 0.10,
    cheap: int = 3,
    dear: int = 3,
    start_kwh: float | None = None,
    gain: float = 0.10,
):
    """Return a plan for a cheap block followed by an expensive one.

    Starting at the floor, so there is nothing to sell and the only way to profit
    is to buy cheap and sell dear. That makes the charge run *chosen* rather than
    forced, which matters: the terminal bound is clamped to the ambient walk and
    therefore can never compel a purchase.
    """
    total = cheap + dear
    floor = table.limits.energy_for_soc(FLOOR_PERCENT)
    pvs = [
        pv_kwh if (pv_only_at is None or index == pv_only_at) and index < cheap else 0.0
        for index in range(total)
    ]
    horizon = horizon_for(
        table,
        demands=[
            IntervalDemand(index=index, baseline_kwh=load_kwh, pv_kwh=pvs[index])
            for index in range(total)
        ],
        prices=[
            IntervalPrice(import_eur_kwh=0.05, export_eur_kwh=0.02)
            if index < cheap
            else IntervalPrice(import_eur_kwh=0.60, export_eur_kwh=0.55)
            for index in range(total)
        ],
    )
    return solve(
        table=table,
        horizon=horizon,
        start_energy_kwh=floor if start_kwh is None else start_kwh,
        terminal_floor_kwh=floor,
        minimum_trade_gain_eur=gain,
        permitted=EVERYTHING,
    )


def charge_run(plan):
    """Return the plan's charging run, asserting there is exactly one."""
    found = [run for run in plan.runs if run.battery_charge_ac_kwh > 0.0]
    assert len(found) == 1, [r.action for r in plan.runs]
    return found[0]


# ===========================================================================
# A. absorption must not split a paid charging campaign
# ===========================================================================


def test_absorption_inside_a_charge_campaign_pays_one_fee() -> None:
    """The model defect, fixed and pinned.

    A sunny quarter in the middle of a paid charging window draws nothing extra
    from the grid, so it is classified as absorption -- and absorption used to be
    plain idle, which **broke the run**. The next purchasing quarter then started
    a second campaign and paid ``minimum_trade_gain_eur`` again.

    The campaign is one decision. It pays one fee.
    """
    table = reference_table()
    plan = arbitrage(table, pv_kwh=2.5, pv_only_at=1)

    middle = plan.intervals[1]
    assert middle.absorbing is True
    assert middle.battery_charge_ac_kwh == pytest.approx(FULL_CHARGE_AC)
    assert middle.marginal_grid_import_kwh == pytest.approx(0.0)
    # The interval after the absorbing one does not restart the run.
    assert plan.intervals[2].run_start is False

    run = charge_run(plan)
    assert run.interval_count == 3
    assert run.charged_switching_fee is True
    assert plan.switching_cost_eur == pytest.approx(0.20)  # charge + export, once each


def test_the_sunny_quarter_costs_no_more_fees_than_a_dark_one() -> None:
    """The regression stated as an equivalence, which is the strongest form.

    The same prices and the same campaign, with and without the sun arriving in
    the middle. Production may change what the campaign *costs*; it must not
    change how many decisions the plan is charged for.
    """
    table = reference_table()
    sunny = arbitrage(table, pv_kwh=2.5, pv_only_at=1)
    dark = arbitrage(table, pv_kwh=0.0)

    assert sunny.switching_cost_eur == pytest.approx(dark.switching_cost_eur)
    assert sunny.direction_changes == dark.direction_changes == 2


def test_absorption_still_breaks_a_discharge_run() -> None:
    """Transparent to a charge campaign, and to nothing else.

    Absorption *is* a charge. Letting it continue a discharge run would claim the
    battery kept discharging while it charged, and would suppress the fee a
    genuine reversal has to pay.
    """
    from custom_components.alpha_ems_manager.economic import (
        _RUN_ABSORB,
        _RUN_CHARGE,
        _RUN_DISCHARGE,
        _RUN_IDLE,
        _resolved_run_state,
    )

    assert _resolved_run_state(_RUN_ABSORB, _RUN_CHARGE) == _RUN_CHARGE
    assert _resolved_run_state(_RUN_ABSORB, _RUN_DISCHARGE) == _RUN_IDLE
    assert _resolved_run_state(_RUN_ABSORB, _RUN_IDLE) == _RUN_IDLE
    # Everything else passes through untouched.
    for state in (_RUN_IDLE, _RUN_CHARGE, _RUN_DISCHARGE):
        for incoming in (_RUN_IDLE, _RUN_CHARGE, _RUN_DISCHARGE):
            assert _resolved_run_state(state, incoming) == state


def test_a_true_idle_interval_still_breaks_a_run() -> None:
    """Deliberate, and unchanged: doing nothing for a quarter is a real break."""
    from custom_components.alpha_ems_manager.economic import (
        _RUN_CHARGE,
        _RUN_IDLE,
        _resolved_run_state,
    )

    assert _resolved_run_state(_RUN_IDLE, _RUN_CHARGE) == _RUN_IDLE


# ===========================================================================
# B. runs, directions, and how many fees were really charged
# ===========================================================================


def test_the_run_count_over_states_the_number_of_switches() -> None:
    """One physical discharge, several reported runs, one fee.

    The label flips between ``discharge`` and ``export`` interval by interval as
    house load rises and falls beneath a constant battery discharge. The direction
    does not flip, and the direction is what the fee is charged against.
    """
    table = reference_table()
    horizon = horizon_for(
        table,
        demands=[
            IntervalDemand(index=index, baseline_kwh=load, pv_kwh=0.0)
            for index, load in enumerate([0.0, 0.0, 3.0, 3.0, 0.0, 0.0, 3.0, 3.0])
        ],
        prices=[IntervalPrice(import_eur_kwh=0.50, export_eur_kwh=0.45)] * 8,
    )
    plan = solve(
        table=table,
        horizon=horizon,
        start_energy_kwh=20.0,
        terminal_floor_kwh=table.limits.energy_for_soc(FLOOR_PERCENT),
        minimum_trade_gain_eur=0.10,
        permitted=EVERYTHING,
    )

    assert len(plan.runs) > 1
    assert {run.direction for run in plan.runs} == {ECONOMIC_DIRECTION_DISCHARGE}
    assert plan.direction_changes == 1
    assert plan.switching_cost_eur == pytest.approx(0.10)
    assert plan.direction_changes < len(plan.runs)


def test_direction_changes_always_equals_the_fees_charged() -> None:
    """The identity that makes the figure trustworthy, swept."""
    table = reference_table()
    for gain in (0.10, 0.25, 0.50):
        plan = solved(
            table, eight_interval_horizon(table), start_kwh=START_KWH, gain=gain
        )
        assert plan.switching_cost_eur == pytest.approx(plan.direction_changes * gain)


def test_a_charge_run_reports_the_charge_direction() -> None:
    """And a discharge run the discharge direction, whatever the label says."""
    table = reference_table()
    plan = arbitrage(table)

    directions = {run.action: run.direction for run in plan.runs}
    assert directions["charge"] == ECONOMIC_DIRECTION_CHARGE
    assert directions["export"] == ECONOMIC_DIRECTION_DISCHARGE


# ===========================================================================
# C. marginal attribution: what the run actually caused
# ===========================================================================


@pytest.mark.parametrize(
    ("pv_kwh", "expected_source"),
    [
        (0.0, ECONOMIC_CHARGE_SOURCE_GRID),
        (1.0, ECONOMIC_CHARGE_SOURCE_MIXED),
        (2.6, ECONOMIC_CHARGE_SOURCE_PRODUCTION),
    ],
)
def test_a_charge_run_says_where_its_energy_came_from(
    pv_kwh: float, expected_source: str
) -> None:
    """Exact, from the interval's own idle baseline -- never apportioned.

    This is the field that stops "charged 4.48 kWh" reading as "bought
    4.48 kWh". The boundary is one state-space bucket, because below that the grid
    contribution is unrepresentable and calling it anything but production would
    over-claim.
    """
    table = reference_table()
    plan = arbitrage(table, pv_kwh=pv_kwh, load_kwh=0.25)
    run = charge_run(plan)

    assert run.charge_source == expected_source
    assert run.battery_charge_ac_kwh == pytest.approx(3 * FULL_CHARGE_AC)
    assert 0.0 <= run.marginal_grid_import_kwh <= run.battery_charge_ac_kwh + 1e-9


def test_site_import_is_never_reported_as_the_run_s_own_purchase() -> None:
    """The over-claim the old field made, measured exactly.

    A charge run's ``grid_import_kwh`` is what the *whole site* drew, house load
    included. Its ``marginal_grid_import_kwh`` is what the battery caused. With no
    production the difference is precisely the house load over the run.
    """
    table = reference_table()
    load = 0.25
    plan = arbitrage(table, pv_kwh=0.0, load_kwh=load)
    run = charge_run(plan)

    assert run.grid_import_kwh > run.marginal_grid_import_kwh
    assert run.grid_import_kwh - run.marginal_grid_import_kwh == pytest.approx(
        run.interval_count * load, abs=1e-9
    )
    # With no production the battery bought every kilowatt-hour it stored.
    assert run.marginal_grid_import_kwh == pytest.approx(run.battery_charge_ac_kwh)


def test_a_non_charging_run_reports_no_charge_source() -> None:
    """A field that always answered would be a field that means nothing."""
    table = reference_table()
    plan = arbitrage(table)
    discharge = next(r for r in plan.runs if r.battery_discharge_ac_kwh > 0.0)

    assert discharge.charge_source == ECONOMIC_CHARGE_SOURCE_NONE


def test_the_marginal_figures_sum_from_the_intervals() -> None:
    """A run is exactly its intervals. No apportioning anywhere."""
    table = reference_table()
    plan = arbitrage(table, pv_kwh=1.0, load_kwh=0.25)

    for run in plan.runs:
        members = [
            entry
            for entry in plan.intervals
            if run.start_index <= entry.index <= run.end_index
        ]
        assert run.marginal_grid_import_kwh == pytest.approx(
            sum(e.marginal_grid_import_kwh for e in members)
        )
        assert run.marginal_cost_eur == pytest.approx(
            sum(e.marginal_cost_eur for e in members)
        )


# ===========================================================================
# D. the economics of a run, versus its cash flow
# ===========================================================================


def test_a_load_serving_discharge_shows_the_money_it_saves() -> None:
    """The defect that made the battery's best trick invisible.

    A discharge sized exactly to house load leaves the meter at zero, so the raw
    cash flow is zero -- while the entire import bill for those quarters has been
    avoided. Only a difference against the counterfactual shows it.
    """
    table = reference_table()
    horizon = horizon_for(
        table,
        demands=flat_demands(4, load_kwh=FULL_CHARGE_AC),
        prices=[IntervalPrice(import_eur_kwh=0.50, export_eur_kwh=0.45)] * 4,
    )
    plan = solve(
        table=table,
        horizon=horizon,
        start_energy_kwh=20.0,
        terminal_floor_kwh=table.limits.energy_for_soc(FLOOR_PERCENT),
        minimum_trade_gain_eur=0.10,
        permitted=IMPLEMENTED,
    )
    run = next(r for r in plan.runs if r.battery_discharge_ac_kwh > 0.0)

    assert run.net_cash_flow_eur == pytest.approx(0.0, abs=0.01)
    # Four quarters of 2.372 kWh avoided at 0.50 EUR/kWh.
    assert run.marginal_cost_eur == pytest.approx(-4 * FULL_CHARGE_AC * 0.50, abs=0.02)
    assert run.marginal_cost_eur < -4.0


def test_every_charge_run_has_a_negative_cash_flow_by_construction() -> None:
    """Which is why the cash flow is not the economics, and is named accordingly."""
    table = reference_table()
    for pv_kwh in (0.0, 1.0, 2.0):
        run = charge_run(arbitrage(table, pv_kwh=pv_kwh, load_kwh=0.25))
        assert run.net_cash_flow_eur <= 0.0


def test_the_plan_still_profits_while_its_charge_run_shows_a_loss() -> None:
    """The live case's shape: locally negative runs, globally positive plan.

    Not a defect and worth pinning. A charge run pays; the value it creates is
    realised in a later discharge, and no per-run figure can connect the two.
    """
    table = reference_table()
    plan = arbitrage(table)

    assert charge_run(plan).net_cash_flow_eur < 0.0
    assert plan.expected_net_value_eur > 0.0


# ===========================================================================
# E. the hold baseline prices the same physical world as the plan
# ===========================================================================


def test_the_baseline_absorbs_production_exactly_as_the_plan_must() -> None:
    """The reporting defect, measured.

    Until beta.16 the baseline froze the battery, so it *sold* every kilowatt-hour
    of surplus while the plan -- held to the ambient endpoint by the terminal
    bound -- *banked* it and was credited nothing. The gain was understated by
    roughly the export value of everything absorbed.
    """
    table = reference_table()
    pv, load, export_price = 2.5, 0.0, 0.10
    horizon = horizon_for(
        table,
        demands=flat_demands(8, load_kwh=load, pv_kwh=pv),
        prices=[IntervalPrice(import_eur_kwh=0.20, export_eur_kwh=export_price)] * 8,
    )

    ambient = hold_cost(horizon=horizon, table=table, start_energy_kwh=6.0)
    frozen_battery = -8 * pv * export_price

    assert frozen_battery == pytest.approx(-2.0)
    assert ambient > frozen_battery
    # The old bias: the export value of what the ambient walk stored instead.
    assert ambient - frozen_battery > 1.0


def test_the_baseline_is_the_same_trajectory_the_terminal_bound_uses() -> None:
    """One definition of "doing nothing", consulted twice.

    The endpoint the terminal bound enforces and the trajectory the baseline
    prices come from the same walk, so the plan and the figure it is judged
    against cannot describe different physics.
    """
    table = reference_table()
    demands = flat_demands(4, load_kwh=0.0, pv_kwh=2.5)
    horizon = horizon_for(
        table,
        demands=demands,
        prices=[IntervalPrice(import_eur_kwh=0.20, export_eur_kwh=0.05)] * 4,
    )
    state = build_state(
        soc_percent=table.limits.soc_for_energy(6.0),
        limits=table.limits,
        reserve=static_reserve(FLOOR_PERCENT),
    )
    assert state is not None
    reference = simulate(state, demands, HoldPolicy().provider(), absorb_surplus=True)

    plan = solve(
        table=table,
        horizon=horizon,
        start_energy_kwh=6.0,
        terminal_floor_kwh=reference.end_energy_kwh,
        minimum_trade_gain_eur=0.10,
        permitted=IMPLEMENTED,
    )

    # The bound is the bucketed walk's endpoint...
    assert plan.terminal_floor_kwh == pytest.approx(15.0)
    # ...and the plan is held to it, so both worlds end with the same stored energy.
    assert plan.end_energy_dc_kwh >= plan.terminal_floor_kwh - 1e-9


def test_a_dark_horizon_baseline_is_unchanged_by_the_rework() -> None:
    """With nothing to absorb, the ambient walk is the frozen battery.

    The correction only ever moves a horizon that has surplus production, which is
    the honest scope of it.
    """
    table = reference_table()
    horizon = eight_interval_horizon(table)

    ambient = hold_cost(horizon=horizon, table=table, start_energy_kwh=START_KWH)

    assert ambient == pytest.approx(0.50)


# ===========================================================================
# F. terminal instrumentation -- the bound itself is unchanged
# ===========================================================================


def two_day_case(table, *, pv_scale: float = 1.0, intervals: int = 137):
    """Return a two-day horizon shaped after the live diagnostics case."""
    loads, pvs, imports, exports = [], [], [], []
    for index in range(intervals):
        hour = ((11 * 60 + 45 + index * 15) % 1440) / 60.0
        loads.append(0.25 if 6 <= hour < 23 else 0.12)
        pvs.append(
            round(1.05 * math.exp(-((hour - 13.5) ** 2) / (2 * 3.1**2)) * pv_scale, 4)
            if 6 <= hour <= 21
            else 0.0
        )
        price = (
            0.26
            if 7 <= hour < 10
            else 0.135
            if 10 <= hour < 15
            else 0.35
            if 17 <= hour < 21
            else 0.129
            if (hour >= 21 or hour < 5)
            else 0.18
        )
        imports.append(price)
        exports.append(round(max(0.0, price - 0.02), 4))
    demands = [
        IntervalDemand(index=i, baseline_kwh=loads[i], pv_kwh=pvs[i])
        for i in range(intervals)
    ]
    horizon = build_horizon(
        demands=demands,
        prices=[
            IntervalPrice(import_eur_kwh=imports[i], export_eur_kwh=exports[i])
            for i in range(intervals)
        ],
        required_reserve_kwh=[11.18] * intervals,
        table=table,
    )
    state = build_state(
        soc_percent=table.limits.soc_for_energy(17.42),
        limits=table.limits,
        reserve=static_reserve(FLOOR_PERCENT),
    )
    assert state is not None
    hold_end = simulate(
        state, demands, HoldPolicy().provider(), absorb_surplus=True
    ).end_energy_kwh
    return horizon, hold_end


def test_the_terminal_comparison_is_absent_rather_than_zero() -> None:
    """beta.18 removed the hold-end constraint, so there is nothing left to price.

    beta.16 and beta.17 ran a fourth solve with the terminal bound relaxed to the
    configured floor and published the difference. Candidate B removed the
    constraint, so the relaxed solve *is* the desired solve and the difference
    would be identically zero.

    Publishing a zero would state that a constraint costs nothing, which is a
    different claim from there being no constraint. All four figures are ``None``.
    """
    table = reference_table()
    horizon, hold_end = two_day_case(table)
    outcome = outcome_for(
        table, horizon, start_kwh=17.42, terminal_kwh=hold_end, gain=0.10
    )

    assert outcome.unbounded is None
    assert outcome.terminal_plan_cost_eur is None
    assert outcome.terminal_plan_import_kwh is None
    assert outcome.terminal_first_run_changed is None
    assert outcome.terminal_near_field_cost_eur is None


def test_the_solver_runs_three_solves_not_four() -> None:
    """Three unconditional solves, plus one that must be asked for.

    Desired, capability and the reserve-relaxed label solve run every refresh. The
    fourth beta.18 deleted priced a constraint that no longer existed, so its
    difference was identically zero; the fourth beta.31 adds solves the same inputs
    under the *previous* architecture, which is a different number and a temporary
    one. It runs only when ``compare_legacy`` is set, which only Shadow does.
    """
    tree = ast.parse(pathlib.Path(economic_module.__file__).read_text(encoding="utf-8"))
    calls = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "build_outcome":
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Name)
                    and inner.func.id == "solve"
                ):
                    calls += 1

    # The fourth is conditional; see ``compare_legacy``.
    assert calls == 4


def test_the_published_plan_is_the_only_plan() -> None:
    """There is no second terminal candidate for it to be published *instead of*.

    The distinction beta.16 drew -- bounded plan published, unbounded plan for
    instrumentation -- has no content once the bound is gone.
    """
    table = reference_table()
    horizon, hold_end = two_day_case(table)
    outcome = outcome_for(
        table, horizon, start_kwh=17.42, terminal_kwh=hold_end, gain=0.10
    )

    assert outcome.unbounded is None
    assert outcome.desired.available
    assert outcome.desired.intervals


def test_the_terminal_floor_is_a_physical_floor_and_nothing_more() -> None:
    """What ``terminal_floor_kwh`` means after beta.18, asserted not assumed.

    A physical floor the plan must still hold at the horizon's end, whose only
    production value is the user's configured minimum. Emphatically not the idle
    trajectory's endpoint: asked for the floor, the plan is free to end anywhere
    at or above it -- including well below where it started, which is exactly what
    the removed constraint forbade.
    """
    table = reference_table()
    floor = table.limits.energy_for_soc(FLOOR_PERCENT)
    horizon = eight_interval_horizon(table)
    outcome = outcome_for(table, horizon, start_kwh=START_KWH, terminal_kwh=floor)

    assert outcome.desired.terminal_floor_kwh <= floor + 1e-9
    assert outcome.desired.end_energy_dc_kwh < START_KWH
    assert outcome.terminal_plan_cost_eur is None


def test_the_publication_gap_needs_no_hedge_of_its_own() -> None:
    """A horizon holding only today's prices still ends at the hold endpoint.

    The pre-publication case, and the reason beta.16 adds no clock rule, no
    Frank-specific timer and no separate hedge: the terminal condition already
    carries energy through the gap.
    """
    table = reference_table()
    truncated, hold_end = two_day_case(table, intervals=49)
    full, _ = two_day_case(table, intervals=137)

    short = outcome_for(table, truncated, start_kwh=17.42, terminal_kwh=hold_end)
    assert short.horizon.intervals == 49
    assert short.desired.terminal_binding is True
    assert short.desired.end_energy_dc_kwh >= short.desired.terminal_floor_kwh - 1e-9
    assert full.intervals == 137


# ===========================================================================
# G. representable power, and the cost of four solves
# ===========================================================================


def test_the_representable_power_is_published_and_pinned() -> None:
    """Roughly five per cent of nameplate is unreachable, and it is visible.

    A 10 kW charge for a quarter is 2.3717 kWh DC, which is 9.487 buckets. Nine
    buckets need 9.487 kW and are reachable; ten need 10.54 kW, which the clamp
    reduces, so the move is correctly discarded.

    Quantisation, not a clamp fault and not a configured limit. beta.16 makes it
    visible rather than chasing it: refining the grid costs solve time as the
    inverse square of the bucket, and the targeted alternative breaks the
    linearity invariant the per-delta pricing table rests on.
    """
    table = reference_table()

    assert table.max_representable_power_kw == pytest.approx(9.4868, abs=1e-4)
    assert table.max_representable_power_kw < table.limits.max_charge_kw
    assert table.limits.max_charge_kw - table.max_representable_power_kw < 0.6
    # The arithmetic behind it, so the figure is explained rather than asserted.
    assert (
        pytest.approx(9.4868, abs=1e-4)
        == 10.0 * INTERVAL_HOURS * ETA / ECONOMIC_BUCKET_KWH
    )


def test_the_representable_power_is_carried_on_the_outcome() -> None:
    """So a reporting layer never rebuilds a table to describe one."""
    table = reference_table()
    outcome = outcome_for(table, eight_interval_horizon(table), start_kwh=START_KWH)

    assert outcome.max_representable_power_kw == table.max_representable_power_kw


def test_four_solves_still_fit_the_refresh_budget() -> None:
    """The fourth solve is instrumentation, and it is affordable.

    A quarter-hour refresh has 900 seconds. This runs in the executor. The guard
    is deliberately loose -- it exists to catch an order-of-magnitude regression,
    not to police a machine's mood.
    """
    table = reference_table()
    horizon = horizon_for(
        table, demands=flat_demands(96), prices=two_tier_prices(96, cheap_until=48)
    )

    started = time.perf_counter()
    outcome = outcome_for(table, horizon, start_kwh=START_KWH)
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    assert outcome.desired.intervals_evaluated == 96
    assert elapsed_ms < 5000.0, f"four solves took {elapsed_ms:.0f} ms"


# ===========================================================================
# H. two behaviours that looked wrong on the live plan and are not
# ===========================================================================
#
# Both were candidates for a new rule in beta.16 -- "reserve headroom for
# forecast production", and "charge at maximum power in the cheapest window".
# Neither was added, because the cost objective already expresses both exactly.
# A second, weaker statement of something the search proves could only ever
# disagree with the optimum. These are the experiments that decided it, kept as
# regressions so the claim stays checkable.


NO_BUYING = frozenset(
    {ECONOMIC_ACTION_DISCHARGE, ECONOMIC_ACTION_EXPORT, ECONOMIC_ACTION_CURTAIL}
)


def production_horizon(table, *, pv_total: float):
    """Cheap quarters, then a production bell, then an expensive evening.

    The shape that makes the question sharp: there is room for roughly 7 kWh, a
    cheap price now, and -- depending on ``pv_total`` -- either enough forecast
    production to fill that room for nothing, or none at all.
    """
    cheap, sunny, dear = 8, 16, 8
    total = cheap + sunny + dear
    demands = [
        IntervalDemand(
            index=index,
            baseline_kwh=0.10,
            pv_kwh=(pv_total / sunny if cheap <= index < cheap + sunny else 0.0),
        )
        for index in range(total)
    ]
    prices = []
    for index in range(total):
        if index < cheap:
            prices.append(IntervalPrice(import_eur_kwh=0.10, export_eur_kwh=0.08))
        elif index < cheap + sunny:
            prices.append(IntervalPrice(import_eur_kwh=0.25, export_eur_kwh=0.05))
        else:
            prices.append(IntervalPrice(import_eur_kwh=0.60, export_eur_kwh=0.55))
    return horizon_for(table, demands=demands, prices=prices)


def bought_in_the_cheap_block(plan) -> float:
    """The largest quarter of charging inside the first eight intervals, in AC kWh."""
    return max(
        (i.battery_charge_ac_kwh for i in plan.intervals if i.index < 8), default=0.0
    )


def test_forecast_production_makes_the_cheap_purchase_worthless() -> None:
    """20 kWh of production arriving, 7 kWh of room: it buys nothing at 0.10.

    And -- the decisive half -- **forbidding** the purchase changes the answer by
    exactly zero. Permission to buy is worth nothing here, so no rule reserving
    headroom for the sun is needed: the objective already refuses to fill a pack
    the sun is about to fill for free, and would keep refusing if the rule did
    not exist.
    """
    table = reference_table()
    horizon = production_horizon(table, pv_total=20.0)
    floor = table.limits.energy_for_soc(FLOOR_PERCENT)
    kwargs = {"start_kwh": 15.0, "terminal_kwh": floor, "gain": 0.10}

    allowed = solved(table, horizon, permitted=EVERYTHING, **kwargs)
    denied = solved(table, horizon, permitted=NO_BUYING, **kwargs)

    assert sum(i.grid_import_kwh for i in allowed.intervals) == pytest.approx(0.0)
    assert bought_in_the_cheap_block(allowed) == pytest.approx(0.0)
    assert allowed.cost_eur == pytest.approx(denied.cost_eur, abs=1e-9)

    # It did charge -- 16.87 kWh of it -- purely from production it would
    # otherwise have exported at 0.05.
    charged = sum(i.battery_charge_ac_kwh for i in allowed.intervals)
    assert charged > 16.0


def test_the_same_prices_without_the_sun_buy_at_full_power() -> None:
    """Remove the production and permission to buy is worth EUR 2.91.

    The counterfactual that makes the previous test mean something. Same prices,
    same room, same fee: the only difference is whether the sun is coming.
    """
    table = reference_table()
    horizon = production_horizon(table, pv_total=0.0)
    floor = table.limits.energy_for_soc(FLOOR_PERCENT)
    kwargs = {"start_kwh": 15.0, "terminal_kwh": floor, "gain": 0.10}

    allowed = solved(table, horizon, permitted=EVERYTHING, **kwargs)
    denied = solved(table, horizon, permitted=NO_BUYING, **kwargs)

    assert bought_in_the_cheap_block(allowed) == pytest.approx(FULL_CHARGE_AC)
    assert denied.cost_eur - allowed.cost_eur > 2.9
    assert bought_in_the_cheap_block(denied) == pytest.approx(0.0)


def test_it_buys_at_full_power_in_exactly_the_cheapest_quarters() -> None:
    """Eight candidate quarters, two of them cheapest: it takes those two.

    The live plan averaged about 2 kW over a long run, which read as a
    preference for gentle charging. It is not one. Given a near-full pack, this
    plan **sells first to make room**, then buys at the largest representable
    power in precisely the two cheapest quarters, then sells the lot into the
    peak. Energy, power and quarter selection are one joint decision, and the
    search makes it exactly -- there is no rule to add.
    """
    table = reference_table()
    floor = table.limits.energy_for_soc(FLOOR_PERCENT)
    ceiling = table.limits.energy_for_soc(100.0)
    cheapest = (2, 3)
    buy, sell = 8, 8

    prices = []
    for index in range(buy + sell):
        if index >= buy:
            prices.append(IntervalPrice(import_eur_kwh=0.65, export_eur_kwh=0.60))
        else:
            price = 0.05 if index in cheapest else 0.30
            prices.append(
                IntervalPrice(import_eur_kwh=price, export_eur_kwh=price - 0.03)
            )
    horizon = horizon_for(
        table,
        demands=[
            IntervalDemand(index=index, baseline_kwh=0.10, pv_kwh=0.0)
            for index in range(buy + sell)
        ],
        prices=prices,
    )

    # Nearly full: about 2.5 kWh of room, so buying requires selling first.
    plan = solved(
        table, horizon, start_kwh=ceiling - 2.5, terminal_kwh=floor, gain=0.10
    )

    charging = {
        i.index: i.battery_charge_ac_kwh
        for i in plan.intervals
        if i.battery_charge_ac_kwh > 0.0
    }
    assert tuple(sorted(charging)) == cheapest
    for index in cheapest:
        assert charging[index] == pytest.approx(FULL_CHARGE_AC)
        # The published figure is rounded for reporting; the move is the same one.
        assert charging[index] / INTERVAL_HOURS == pytest.approx(
            table.max_representable_power_kw, abs=5e-5
        )

    # And it made the room deliberately: a sale before the purchase, at a price
    # it would never have sold at if it were not buying back cheaper.
    assert [run.action for run in plan.runs] == [
        ECONOMIC_ACTION_EXPORT,
        ECONOMIC_ACTION_CHARGE,
        ECONOMIC_ACTION_EXPORT,
    ]
    assert plan.runs[0].end_index < min(cheapest)
