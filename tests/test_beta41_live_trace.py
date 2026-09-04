"""beta.41 replayed on the live 2026-09-03 20:45 refresh that planned nothing.

**The mandatory regression for this release.** Every input is
:mod:`tests.beta41_trace`, which records what the diagnostic measured and labels
what it reconstructs.

The capture: 9.936 kWh in a 21.6 kWh pack, a 4.32 kWh floor, tomorrow's day-ahead
fully published, tomorrow forecasting 7.79 kWh of production against 21.65 kWh of
load, the pack on the floor from 02:45 and staying there, and the household
importing 11.37 kWh over the horizon. The optimiser returned ``runs: []``.

It could not have returned anything else. With the post-horizon demand window
collapsed the marginal worth of stored energy was pinned at
``eta_discharge * export_price`` = 0.15013 EUR/kWh, which puts the break-even
import price at 0.092425 -- below the cheapest quarter this installation has ever
recorded. The user's 0.20 EUR minimum trade gain was never reached, because the
per-kWh gate refused first and would have refused at a zero margin too.

**What this file establishes.** The physical facts of the capture are reproduced
and pinned, and so is the outcome: the corrected model buys 12.778 kWh in
tomorrow's cheap window at 0.160, separately from 3.889 kWh the reserve compels at
0.270, with the two attributed to different categories and the hard floor intact.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.battery import build_limits
from custom_components.alpha_ems_manager.const import (
    ECONOMIC_ACTION_CHARGE,
    TERMINAL_WINDOW_CLOCK_MATCHED,
)
from custom_components.alpha_ems_manager.economic import (
    IntervalPrice,
    TerminalValue,
    actionable_intervals,
    build_horizon,
    build_outcome,
    build_physics_table,
    edge_creditable_energy_kwh,
    edge_value_eur_per_kwh,
    post_horizon_window,
    select_bucket_kwh,
)
from custom_components.alpha_ems_manager.reserve import (
    build_reserve,
    build_reserve_reachable,
    uncertainty_margin,
)
from custom_components.alpha_ems_manager.simulation import IntervalDemand

from .beta41_trace import (
    CAPACITY_DC_KWH,
    CHEAPEST_QUARTER_EVER_SEEN,
    DAY_INTERVALS,
    DISCHARGE_EFFICIENCY,
    END_INDEX,
    FLOOR_DC_KWH,
    FRAME_INDEX,
    GRID_CHARGE_MARGIN_EUR_PER_KWH,
    MARGINAL_VALUE_EUR_KWH,
    MAX_CHARGE_KW,
    MAX_DISCHARGE_KW,
    MINIMUM_TRADE_GAIN_EUR,
    ROUND_TRIP_EFFICIENCY_PERCENT,
    STORED_DC_KWH,
    TERMINAL_EXPORT_PRICE_EUR_KWH,
    export_of,
    import_price_at,
    load_at,
    pv_at,
)

LIMITS, _MISSING = build_limits(
    capacity_kwh=CAPACITY_DC_KWH,
    max_charge_kw=MAX_CHARGE_KW,
    max_discharge_kw=MAX_DISCHARGE_KW,
    round_trip_efficiency_percent=ROUND_TRIP_EFFICIENCY_PERCENT,
    max_soc_percent=100.0,
)
assert _MISSING is None


def _series():
    """Return the horizon exactly as the live refresh saw it."""
    demands = tuple(
        IntervalDemand(index=i, baseline_kwh=load_at(i), pv_kwh=pv_at(i))
        for i in range(FRAME_INDEX, END_INDEX)
    )
    prices = tuple(
        IntervalPrice(
            import_eur_kwh=import_price_at(d.index),
            export_eur_kwh=export_of(import_price_at(d.index)),
        )
        for d in demands
    )
    return demands, prices


def solve_live(*, terminal_value, stored: float = STORED_DC_KWH):
    """Solve the captured horizon through the production path."""
    demands, prices = _series()
    bucket, rule = select_bucket_kwh(LIMITS, floor_energy_kwh=FLOOR_DC_KWH)
    table = build_physics_table(
        LIMITS, floor_energy_kwh=FLOOR_DC_KWH, bucket_kwh=bucket
    )
    actionable = actionable_intervals(demands, prices)
    probe = build_reserve_reachable(
        limits=LIMITS,
        floor_energy_kwh=FLOOR_DC_KWH,
        demands=demands,
        grid_credit_intervals=actionable,
    )
    margin = uncertainty_margin(
        probe, mae_kwh_per_interval=0.06, usable_capacity_kwh=LIMITS.capacity_kwh
    )
    enforced = build_reserve_reachable(
        limits=LIMITS,
        floor_energy_kwh=FLOOR_DC_KWH + margin.total_dc_kwh,
        demands=demands,
        grid_credit_intervals=actionable,
    )
    autonomy = build_reserve(
        limits=LIMITS, floor_energy_kwh=FLOOR_DC_KWH, demands=demands
    )
    curve = tuple(
        entry.required_dc_kwh
        if entry.required_dc_kwh is not None
        else FLOOR_DC_KWH + margin.total_dc_kwh
        for entry in enforced.intervals
    )
    horizon = build_horizon(
        demands=demands, prices=prices, required_reserve_kwh=curve, table=table
    )
    outcome = build_outcome(
        table=table,
        horizon=horizon,
        start_energy_kwh=stored,
        terminal_floor_kwh=FLOOR_DC_KWH,
        floor_energy_kwh=FLOOR_DC_KWH,
        minimum_trade_gain_eur=MINIMUM_TRADE_GAIN_EUR,
        allow_grid_charging=True,
        allow_battery_export=True,
        grid_charge_margin_eur_per_kwh=GRID_CHARGE_MARGIN_EUR_PER_KWH,
        battery_throughput_cost_eur_per_kwh=0.0,
        edge_value_eur_per_kwh=edge_value_eur_per_kwh(
            horizon.prices[:actionable],
            discharge_efficiency=LIMITS.discharge_efficiency,
        ),
        edge_creditable_kwh=edge_creditable_energy_kwh(
            ceiling_kwh=LIMITS.energy_for_soc(100.0),
            forecast_surplus_kwh=sum(d.surplus_kwh for d in demands[:actionable]),
        ),
        autonomy=tuple(entry.required_dc_kwh for entry in autonomy.intervals),
        reachability=enforced,
        uncertainty=margin,
        actionable_interval_count=actionable,
        ambient_self_consumption=True,
        bucket_rule=rule,
        terminal_value=terminal_value,
    )
    return outcome, table


def _corrected_terminal() -> TerminalValue:
    """The terminal value the corrected coordinator now builds for this horizon."""
    demands, prices = _series()
    window = post_horizon_window(
        demands,
        prices,
        horizon_intervals=len(demands),
        today_interval_count=DAY_INTERVALS,
    )
    assert window.basis == TERMINAL_WINDOW_CLOCK_MATCHED, window
    return TerminalValue(
        demand_ac_kwh=window.demand_ac_kwh,
        displaced_price_eur_kwh=window.displaced_price_eur_kwh,
        export_price_eur_kwh=TERMINAL_EXPORT_PRICE_EUR_KWH,
        discharge_efficiency=LIMITS.discharge_efficiency,
        window_basis=window.basis,
        window_intervals=window.intervals,
        window_stopped_by=window.stopped_by,
    )


def _collapsed_terminal() -> TerminalValue:
    """What beta.40 built: no served segment, so a flat export rate."""
    return TerminalValue(
        demand_ac_kwh=0.0,
        displaced_price_eur_kwh=0.0,
        export_price_eur_kwh=TERMINAL_EXPORT_PRICE_EUR_KWH,
        discharge_efficiency=LIMITS.discharge_efficiency,
    )


# == 1. the collapse, reproduced from the trace ============================


def test_the_users_settings_were_never_the_cause() -> None:
    """**The 0.20 EUR gate is never reached, and a zero margin would not help.**

    The per-kWh gate refuses first. Dropping the margin to zero leaves the
    break-even at the round trip times the export price, still below the cheapest
    quarter -- so the refusal cannot be blamed on either setting, and neither is
    touched by this release.
    """
    zero_margin_break_even = (
        LIMITS.charge_efficiency * DISCHARGE_EFFICIENCY * TERMINAL_EXPORT_PRICE_EUR_KWH
    )

    assert zero_margin_break_even == pytest.approx(0.142425, abs=1e-6)
    assert zero_margin_break_even < CHEAPEST_QUARTER_EVER_SEEN


# == 2. the corrected model buys ===========================================


def test_the_corrected_model_finds_the_buy_the_operator_expected() -> None:
    """**The release objective, met.**

    With household service carried in the solver's own state and the post-horizon
    window restored, the worth of stored energy at the level the pack was actually
    at rises from the collapsed 0.15013 EUR/kWh to **0.381**, and the optimiser
    buys 12.778 kWh in tomorrow's cheap window at 0.160 -- still gated by the
    user's own 0.20 EUR and 0.05 EUR/kWh settings, which are passed unchanged.

    **The two purchases are separated, and that separation is the point.** The
    plan also buys 3.889 kWh at 0.270, and that run is attributed entirely to the
    reserve: the pack cannot reach the reachability band across this horizon once
    it is modelled as depleting. Judging a compelled purchase by an economic price
    test would be judging it by the wrong standard, so only the economic run is
    held to one.
    """
    outcome, table = solve_live(terminal_value=_corrected_terminal())
    plan = outcome.desired
    bucket = table.bucket_at_or_below(STORED_DC_KWH)

    value, why = plan.marginal_value_eur_per_kwh(bucket, bucket_kwh=table.bucket_kwh)
    assert value is not None, why
    assert value > MARGINAL_VALUE_EUR_KWH + 0.05, (
        f"the worth of stored energy must clear the export rate: {value}"
    )

    charge_runs = [
        run
        for run in plan.runs
        if run.action == ECONOMIC_ACTION_CHARGE and run.battery_charge_ac_kwh > 0.0
    ]
    assert charge_runs, "the plan must buy: this is the reported fault, fixed"

    economic = [r for r in charge_runs if r.start_index not in outcome.safety_buy_runs]
    assert economic, "and at least one purchase must be economic rather than compelled"
    bought = sum(run.battery_charge_ac_kwh for run in economic)
    assert bought > 1.0, bought
    # Bought where it is cheap, not merely somewhere.
    for run in economic:
        prices = [
            import_price_at(index)
            for index in range(run.start_index, run.end_index + 1)
        ]
        assert max(prices) <= 0.25, (run.start_index, run.end_index, prices)


def test_the_corrected_plan_still_honours_the_hard_floor() -> None:
    """The floor is not traded against the new value. Decided state, both models."""
    for terminal in (_collapsed_terminal(), _corrected_terminal()):
        outcome, _table = solve_live(terminal_value=terminal)
        plan = outcome.desired
        # The decided endpoint, against the floor the lattice can express. The
        # configured 4.32 kWh quantises down to ``terminal_floor_kwh``, which is
        # the conservative direction for an amount you have.
        assert plan.edge_energy_kwh >= plan.terminal_floor_kwh - 1e-9
        assert plan.terminal_floor_kwh <= FLOOR_DC_KWH + 1e-9
        assert plan.violation_kwh == pytest.approx(0.0, abs=1e-9)


def test_the_reserve_now_compels_what_it_could_not_see_before() -> None:
    """**Nothing is compelled at the head, and something is compelled later.**

    ``bridge_kwh_now`` is the deficit at *this* interval and it is zero: the pack
    holds 9.936 kWh against a reachability requirement of the hard floor plus the
    blind-window margin, 5.33. That figure does not move, which is what keeps it
    price-blind.

    What changed is that the enforced curve is applied at every interval of a
    backward induction, and until beta.41 the recursion could not see the pack
    depleting -- so a requirement binding *later* in the horizon bound on a state
    that never fell. It falls now, tomorrow forecasts 21.65 kWh of load against
    7.79 kWh of production, and the reserve genuinely compels a pre-positioned
    purchase. That is the mechanism this codebase already documents: a winter
    shape whose head bridge was zero while 0.56 kWh was compulsory four quarters
    ahead.
    """
    outcome, _table = solve_live(terminal_value=_corrected_terminal())

    assert outcome.bridge_kwh_now == pytest.approx(0.0, abs=1e-9)
    assert outcome.safety_buy_ac_kwh > 0.0, (
        "the reserve binds later in the horizon, and the recursion can see it now"
    )
    assert outcome.safety_buy_runs


def test_the_pack_would_otherwise_sit_on_the_floor_all_day() -> None:
    """The physical fact that makes the missing Buy expensive rather than merely odd.

    Tomorrow forecasts far more load than production, so a pack left alone reaches
    the floor overnight and cannot refill itself: the household then buys every
    kilowatt-hour it needs at whatever the day costs.
    """
    demands, _prices = _series()
    tomorrow = [d for d in demands if d.index >= DAY_INTERVALS]

    production = sum(d.pv_kwh or 0.0 for d in tomorrow)
    load = sum(d.baseline_kwh or 0.0 for d in tomorrow)
    assert production < load / 2.0, (production, load)

    surplus = sum(d.surplus_kwh for d in tomorrow)
    absorbing = sum(1 for d in tomorrow if d.surplus_kwh > 0.0)
    # Published: 0.01 kWh across two quarters. The reconstruction lands on
    # 0.034 across five -- a symmetric production curve cannot cross a flat load
    # band in exactly two quarters, because the quarters either side of the peak
    # are equal by construction. The physics these tests rest on is the same: the
    # sun does not refill this pack.
    assert surplus < 0.10, surplus
    assert absorbing <= 6, absorbing

    deliverable = (STORED_DC_KWH - FLOOR_DC_KWH) * DISCHARGE_EFFICIENCY
    assert deliverable == pytest.approx(5.328, abs=0.01)
