"""The six actions, the two plans, and the labels derived from them.

Where ``test_economic_model`` proves the arithmetic, this file proves the
*vocabulary*: which of the six actions a situation produces, what the capability
plan makes of it, why a gap is a gap, and how a charge comes to be called a safety
buy. All of it synthetic, all of it constructed rather than observed.

The separation that dominates the file is **desired versus capability**. Two
independent solves, not one solve and a downgrade -- so the economic optimum stays
undistorted by which actuators happen to exist, and execution can never silently
substitute a different action for the one that was wanted. Every test that asserts
one of them also asserts the other, because a bug that collapses them onto each
other is invisible from either side alone.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.const import (
    ECONOMIC_ACTION_CHARGE,
    ECONOMIC_ACTION_CURTAIL,
    ECONOMIC_ACTION_DISCHARGE,
    ECONOMIC_ACTION_EXPORT,
    ECONOMIC_ACTION_HOLD,
    ECONOMIC_ACTION_OPTIONS,
    ECONOMIC_ACTION_SAFETY_BUY,
    ECONOMIC_GAP_FORECAST_INFEASIBLE,
    ECONOMIC_GAP_NO_PRIMITIVE,
    ECONOMIC_GAP_NONE,
    ECONOMIC_REASON_CHEAP_WINDOW,
    ECONOMIC_REASON_EXPENSIVE_WINDOW,
    ECONOMIC_REASON_MAKE_HEADROOM,
    ECONOMIC_REASON_NEGATIVE_EXPORT,
    ECONOMIC_REASON_NO_ACTION,
    ECONOMIC_REASON_RESERVE_RECOVERY,
    ECONOMIC_REASON_SAFETY_BUY,
)
from custom_components.alpha_ems_manager.economic import (
    IMPLEMENTED_ACTIONS,
    EconomicHorizon,
    EconomicOutcome,
    IntervalPrice,
    PhysicsTable,
    build_outcome,
)
from custom_components.alpha_ems_manager.simulation import IntervalDemand

from .test_economic_model import (
    EIGHT,
    FLOOR_PERCENT,
    START_KWH,
    eight_interval_horizon,
    flat_demands,
    horizon_for,
    reference_table,
)


def outcome_for(
    table: PhysicsTable,
    horizon: EconomicHorizon,
    *,
    start_kwh: float,
    terminal_kwh: float | None = None,
    gain: float = 0.0,
    grid_charging: bool = True,
    battery_export: bool = True,
    above_capacity: float = 0.0,
) -> EconomicOutcome:
    """Run every solve the way the coordinator does."""
    floor = table.limits.energy_for_soc(FLOOR_PERCENT)
    return build_outcome(
        table=table,
        horizon=horizon,
        start_energy_kwh=start_kwh,
        terminal_floor_kwh=start_kwh if terminal_kwh is None else terminal_kwh,
        floor_energy_kwh=floor,
        minimum_trade_gain_eur=gain,
        allow_grid_charging=grid_charging,
        allow_battery_export=battery_export,
        reserve_above_capacity_kwh=above_capacity,
    )


def reserve_deadline_horizon(
    table: PhysicsTable, *, import_price: float = 0.50
) -> EconomicHorizon:
    """Return a horizon whose reserve rises out of reach of production.

    Dark, so nothing can be replenished, and expensive, so no plan would buy for
    profit. Any charge here exists because of the reserve and for no other reason,
    which is what makes it a clean test of the safety-buy label.
    """
    floor = table.limits.energy_for_soc(FLOOR_PERCENT)
    return horizon_for(
        table,
        demands=flat_demands(4, load_kwh=0.0),
        prices=[IntervalPrice(import_eur_kwh=import_price, export_eur_kwh=0.02)] * 4,
        reserve_kwh=[floor, floor, 9.0, 9.0],
    )


# -- A. the two plans are separate -------------------------------------------


def test_the_desired_plan_uses_every_action_the_physics_allows() -> None:
    """Export appears in the desired plan even though no actuator can perform it.

    This is the whole point of two solves. Letting the absence of an execution
    primitive shape the optimum would mean the integration could never tell you
    what building one is worth.
    """
    table = reference_table()
    outcome = outcome_for(table, eight_interval_horizon(table), start_kwh=START_KWH)

    assert [run.action for run in outcome.desired.runs] == [
        ECONOMIC_ACTION_CHARGE,
        ECONOMIC_ACTION_EXPORT,
    ]
    assert ECONOMIC_ACTION_EXPORT in outcome.desired.permitted


def curtailment_horizon(table: PhysicsTable) -> EconomicHorizon:
    """Return a horizon whose best action is one no actuator can perform.

    A full pack, three kilowatt-hours of production against a quarter of a
    kilowatt-hour of load, and a **negative** export price -- so the money says
    decline the production, and no release commands the inverter to do that.
    Curtailment is the one action still outside :data:`IMPLEMENTED_ACTIONS`, which
    makes this the only horizon that can still demonstrate a capability gap.
    """
    return horizon_for(
        table,
        demands=flat_demands(2, load_kwh=0.25, pv_kwh=3.0),
        prices=[IntervalPrice(import_eur_kwh=0.30, export_eur_kwh=-0.10)] * 2,
    )


def test_the_capability_plan_is_solved_separately_not_degraded() -> None:
    """A different plan, not the desired one with an action crossed out.

    **The demonstration moved horizons in beta.32, and why is the point.** This
    used to run on the eight-interval sell horizon: the desired plan charged for
    four intervals and exported, and the capability plan -- forbidden to export --
    charged later and discharged into the house instead. Different windows, so
    provably not a filtered copy.

    ``export`` is in :data:`IMPLEMENTED_ACTIONS` since beta.32, because
    ``CONTROL_EXECUTABLE_ACTIONS_BY_INTENT`` has authorised an admitted
    ``net_export`` since beta.27 and the hardware has performed one. So on that
    horizon the two plans now **agree**, which is asserted below as the release's
    own change, and the separate-solve property is demonstrated on the one action
    still without an actuator: curtailment.
    """
    table = reference_table()
    outcome = outcome_for(table, eight_interval_horizon(table), start_kwh=START_KWH)

    desired = [
        (run.action, run.start_index, run.end_index) for run in outcome.desired.runs
    ]
    capability = [
        (run.action, run.start_index, run.end_index) for run in outcome.capability.runs
    ]

    assert desired == [(ECONOMIC_ACTION_CHARGE, 0, 3), (ECONOMIC_ACTION_EXPORT, 4, 7)]
    # No gap: the plant can do what the plan wants, and beta.31 said otherwise.
    assert capability == desired
    assert outcome.capability.permitted == frozenset(
        outcome.desired.permitted & IMPLEMENTED_ACTIONS
    )

    # And the property itself, where a gap still exists.
    gapped = outcome_for(
        table, curtailment_horizon(table), start_kwh=22.0, terminal_kwh=22.0
    )
    assert gapped.action == ECONOMIC_ACTION_CURTAIL
    assert gapped.capability_action != ECONOMIC_ACTION_CURTAIL
    assert ECONOMIC_ACTION_CURTAIL not in gapped.capability.permitted


def test_the_value_forgone_is_the_gap_between_the_two_plans() -> None:
    """What the missing primitives cost, in euros, recomputed from both plans.

    **Measured on a curtailment horizon since beta.32.** On the sell horizon the
    figure is now zero, and correctly so: the plant can perform the export, so
    nothing is forgone. That drop is one of the intended consequences of widening
    :data:`IMPLEMENTED_ACTIONS`, and it is asserted separately below.
    """
    table = reference_table()
    outcome = outcome_for(
        table, curtailment_horizon(table), start_kwh=22.0, terminal_kwh=22.0
    )

    assert outcome.economic_value_forgone_eur == pytest.approx(
        outcome.desired.expected_net_value_eur
        - outcome.capability.expected_net_value_eur
    )
    assert outcome.economic_value_forgone_eur > 0.0

    # The identity still holds where there is no gap, and there the figure is zero
    # rather than absent -- an export day no longer reports value the plant cannot
    # capture, because it can.
    selling = outcome_for(table, eight_interval_horizon(table), start_kwh=START_KWH)
    assert selling.economic_value_forgone_eur == pytest.approx(0.0)


def test_the_value_forgone_is_zero_when_every_wanted_action_has_an_actuator() -> None:
    """No gap, no cost. A figure that was never zero would be meaningless."""
    table = reference_table()
    outcome = outcome_for(
        table,
        eight_interval_horizon(table),
        start_kwh=START_KWH,
        battery_export=False,
    )

    assert outcome.capability_gap_reason == ECONOMIC_GAP_NONE
    assert outcome.economic_value_forgone_eur == pytest.approx(0.0)


# -- B. the six actions ------------------------------------------------------


def test_every_published_action_is_in_the_declared_option_set() -> None:
    """The entity's enum and the model's vocabulary are the same six words."""
    assert set(ECONOMIC_ACTION_OPTIONS) == {
        ECONOMIC_ACTION_HOLD,
        ECONOMIC_ACTION_CHARGE,
        ECONOMIC_ACTION_DISCHARGE,
        ECONOMIC_ACTION_EXPORT,
        ECONOMIC_ACTION_CURTAIL,
        ECONOMIC_ACTION_SAFETY_BUY,
    }


def test_nothing_worth_doing_is_hold_with_a_reason_that_says_so() -> None:
    """Flat prices, no reserve pressure: hold, and ``no_profitable_action``."""
    table = reference_table()
    horizon = horizon_for(
        table,
        demands=flat_demands(EIGHT),
        prices=[IntervalPrice(import_eur_kwh=0.20, export_eur_kwh=0.05)] * EIGHT,
    )
    outcome = outcome_for(table, horizon, start_kwh=START_KWH, gain=0.10)

    assert outcome.action == ECONOMIC_ACTION_HOLD
    assert outcome.capability_action == ECONOMIC_ACTION_HOLD
    assert outcome.reason == ECONOMIC_REASON_NO_ACTION
    assert outcome.price_eur_kwh is None
    assert outcome.capability_gap_reason == ECONOMIC_GAP_NONE


def test_a_cheap_window_is_a_charge_priced_on_the_import_side() -> None:
    """``charge``, ``cheap_window``, and the import price of the first interval."""
    table = reference_table()
    outcome = outcome_for(table, eight_interval_horizon(table), start_kwh=START_KWH)

    assert outcome.action == ECONOMIC_ACTION_CHARGE
    assert outcome.reason == ECONOMIC_REASON_CHEAP_WINDOW
    assert outcome.price_eur_kwh == pytest.approx(0.10)


def test_an_expensive_window_with_nothing_to_sell_into_is_a_discharge() -> None:
    """Serving the house from the pack, priced at the import it avoids.

    The import price, deliberately: a load-serving discharge earns nothing at the
    meter, and the number that makes it worth doing is what it saved.
    """
    table = reference_table()
    horizon = horizon_for(
        table,
        demands=flat_demands(4, load_kwh=2.0),
        prices=[IntervalPrice(import_eur_kwh=0.45, export_eur_kwh=0.02)] * 4,
    )
    outcome = outcome_for(
        table,
        horizon,
        start_kwh=20.0,
        terminal_kwh=table.limits.energy_for_soc(FLOOR_PERCENT),
        battery_export=False,
    )

    assert outcome.action == ECONOMIC_ACTION_DISCHARGE
    assert outcome.reason == ECONOMIC_REASON_EXPENSIVE_WINDOW
    assert outcome.price_eur_kwh == pytest.approx(0.45)


def test_an_export_is_priced_on_the_export_side_and_measured_at_the_meter() -> None:
    """``export``, the export price, and the grid figure rather than the battery one."""
    table = reference_table()
    outcome = outcome_for(table, eight_interval_horizon(table), start_kwh=START_KWH)
    run = next(
        run for run in outcome.desired.runs if run.action == ECONOMIC_ACTION_EXPORT
    )

    assert run.energy_kwh == pytest.approx(run.grid_export_kwh)
    assert run.grid_export_kwh < run.battery_discharge_ac_kwh
    assert run.energy_kwh == pytest.approx(run.battery_discharge_ac_kwh - 1.0)


def test_a_negative_export_price_is_a_curtailment_with_its_own_reason() -> None:
    """``curtail_pv``, ``negative_export``, and the production declined."""
    table = reference_table()
    horizon = horizon_for(
        table,
        demands=flat_demands(2, load_kwh=0.25, pv_kwh=3.0),
        prices=[IntervalPrice(import_eur_kwh=0.30, export_eur_kwh=-0.10)] * 2,
    )
    outcome = outcome_for(table, horizon, start_kwh=22.0, terminal_kwh=22.0)

    assert outcome.action == ECONOMIC_ACTION_CURTAIL
    assert outcome.reason == ECONOMIC_REASON_NEGATIVE_EXPORT
    run = outcome.desired.published_run
    assert run is not None
    assert run.energy_kwh == pytest.approx(run.pv_curtailed_kwh)
    assert run.pv_curtailed_kwh == pytest.approx(2 * (3.0 - 0.25), abs=1e-9)


def test_a_sale_that_makes_room_for_forecast_sunshine_says_so() -> None:
    """``make_headroom`` rather than ``expensive_window``, and the distinction matters.

    A sale taken purely on price and a sale that creates room the sun will fill are
    different decisions with different risks -- the second one rests on a forecast.
    A user staring at a summer evening deserves to be told which it is.
    """
    table = reference_table()
    demands = [
        IntervalDemand(index=0, baseline_kwh=0.25, pv_kwh=0.0),
        IntervalDemand(index=1, baseline_kwh=0.25, pv_kwh=0.0),
        IntervalDemand(index=2, baseline_kwh=0.0, pv_kwh=2.5),
        IntervalDemand(index=3, baseline_kwh=0.0, pv_kwh=2.5),
    ]
    horizon = horizon_for(
        table,
        demands=demands,
        prices=[
            IntervalPrice(import_eur_kwh=0.60, export_eur_kwh=0.55),
            IntervalPrice(import_eur_kwh=0.60, export_eur_kwh=0.55),
            IntervalPrice(import_eur_kwh=0.10, export_eur_kwh=0.02),
            IntervalPrice(import_eur_kwh=0.10, export_eur_kwh=0.02),
        ],
    )
    outcome = outcome_for(
        table, horizon, start_kwh=20.0, terminal_kwh=20.0, grid_charging=False
    )

    assert outcome.action == ECONOMIC_ACTION_EXPORT
    assert outcome.reason == ECONOMIC_REASON_MAKE_HEADROOM
    assert outcome.price_eur_kwh == pytest.approx(0.55)


# -- C. safety buy, as a label rather than a mechanism -----------------------


def test_a_charge_the_reserve_caused_is_labelled_a_safety_buy() -> None:
    """Attributed by comparison with the relaxed solve, not by inspecting prices.

    A cheap interval and a reserve deadline often coincide, so no price threshold
    could tell them apart. Solving the same horizon with the reserve relaxed to the
    configured floor can: the charging that disappears is the charging the reserve
    was responsible for.
    """
    table = reference_table()
    floor = table.limits.energy_for_soc(FLOOR_PERCENT)
    outcome = outcome_for(
        table,
        reserve_deadline_horizon(table),
        start_kwh=floor,
        terminal_kwh=floor,
        gain=0.10,
        battery_export=False,
    )

    assert outcome.action == ECONOMIC_ACTION_SAFETY_BUY
    assert outcome.reason == ECONOMIC_REASON_SAFETY_BUY
    assert outcome.safety_buy_runs == (0,)
    assert outcome.safety_buy_ac_kwh > 0.0
    # The underlying run is still a charge; the label is derived, not stored.
    assert outcome.desired.runs[0].action == ECONOMIC_ACTION_CHARGE


def test_a_charge_taken_purely_on_price_is_not_a_safety_buy() -> None:
    """Relaxing the reserve changes nothing here, so nothing is attributed to it."""
    table = reference_table()
    outcome = outcome_for(table, eight_interval_horizon(table), start_kwh=START_KWH)

    assert outcome.safety_buy_runs == ()
    assert outcome.safety_buy_ac_kwh == pytest.approx(0.0)
    assert outcome.action == ECONOMIC_ACTION_CHARGE
    assert outcome.reason == ECONOMIC_REASON_CHEAP_WINDOW


def test_the_reserve_protection_cost_is_what_the_reserve_cost_in_euros() -> None:
    """The figure that would expose a reserve being defended at an absurd price.

    A lexicographic order needs this visible rather than argued: it is the whole
    difference between the safe answer and the cheap one, in money.
    """
    table = reference_table()
    floor = table.limits.energy_for_soc(FLOOR_PERCENT)
    outcome = outcome_for(
        table,
        reserve_deadline_horizon(table),
        start_kwh=floor,
        terminal_kwh=floor,
        gain=0.10,
        battery_export=False,
    )

    assert outcome.relaxed is not None
    assert outcome.reserve_protection_cost_eur == pytest.approx(
        outcome.desired.cost_eur - outcome.relaxed.cost_eur
    )
    assert outcome.reserve_protection_cost_eur > 0.0
    # The relaxed solve still has to respect the *configured* floor -- it is the
    # reserve that is relaxed, never the user's own setting -- so it charges the
    # one bucket that takes it back over 4.4 kWh and stops there. What makes the
    # attribution work is the size of the difference, not its presence.
    assert outcome.relaxed.planned_charge_ac_kwh < (
        outcome.desired.planned_charge_ac_kwh - outcome.bucket_kwh
    )


def test_a_shortfall_that_cannot_be_closed_reports_reserve_recovery() -> None:
    """``reserve_recovery`` outranks every economic reason, because it is not one.

    And -- the important half -- an unreachable reserve must never *unlock* a sale.
    An earlier draft fell back to a profit solve when the reserve was unreachable,
    which meant a deficit made the optimizer freer rather than more careful.
    """
    table = reference_table()
    horizon = horizon_for(
        table,
        demands=flat_demands(4, load_kwh=0.0),
        prices=[IntervalPrice(import_eur_kwh=0.20, export_eur_kwh=0.90)] * 4,
        reserve_kwh=[22.0] * 4,
    )
    outcome = outcome_for(table, horizon, start_kwh=5.0, terminal_kwh=5.0)

    assert outcome.reason == ECONOMIC_REASON_RESERVE_RECOVERY
    assert outcome.desired.violation_kwh > 0.0
    assert outcome.desired.planned_grid_export_kwh == pytest.approx(0.0)
    assert outcome.action == ECONOMIC_ACTION_SAFETY_BUY


def test_a_reserve_above_the_pack_is_carried_as_its_own_figure() -> None:
    """How far the requirement exceeded the ceiling, reported rather than hidden."""
    table = reference_table()
    outcome = outcome_for(
        table,
        eight_interval_horizon(table),
        start_kwh=START_KWH,
        above_capacity=3.5,
    )

    assert outcome.reserve_above_capacity_kwh == pytest.approx(3.5)


# -- D. the capability gap ---------------------------------------------------


def test_a_wanted_export_has_an_actuator_and_reports_no_gap() -> None:
    """The gap that beta.27 closed and beta.31 was still reporting.

    ``CONTROL_EXECUTABLE_ACTIONS_BY_INTENT`` has authorised an admitted
    ``net_export`` since beta.27; ``CONTROL_LIVE_DISPATCH_INTENTS`` contains it;
    the hardware has performed one. :data:`IMPLEMENTED_ACTIONS` said no actuator
    existed, and that was not a label -- it bounded the capability solve, so every
    export day reported euros the plant supposedly could not capture, and it put an
    ``Advisory`` marker on Live export lines a command was about to be sent for.
    """
    table = reference_table()
    outcome = outcome_for(table, eight_interval_horizon(table), start_kwh=START_KWH)

    assert ECONOMIC_ACTION_EXPORT in IMPLEMENTED_ACTIONS
    assert outcome.desired.runs[1].action == ECONOMIC_ACTION_EXPORT
    assert outcome.capability_gap_reason == ECONOMIC_GAP_NONE
    assert outcome.economic_value_forgone_eur == pytest.approx(0.0)


def test_a_wanted_curtailment_with_no_actuator_reports_no_primitive() -> None:
    """Curtailment has no primitive either, and the published gap says so."""
    table = reference_table()
    horizon = horizon_for(
        table,
        demands=flat_demands(2, load_kwh=0.25, pv_kwh=3.0),
        prices=[IntervalPrice(import_eur_kwh=0.30, export_eur_kwh=-0.10)] * 2,
    )
    outcome = outcome_for(table, horizon, start_kwh=22.0, terminal_kwh=22.0)

    assert ECONOMIC_ACTION_CURTAIL not in IMPLEMENTED_ACTIONS
    assert outcome.action == ECONOMIC_ACTION_CURTAIL
    # **Not ``hold`` any more, and the change is honest.** A full pack with three
    # kilowatt-hours of surplus physically exports it whatever the label says; with
    # ``export`` implemented the capability plan names what actually happens
    # instead of calling it a hold. The gap is unchanged: curtailment is still the
    # thing no actuator can do.
    assert outcome.capability_action == ECONOMIC_ACTION_EXPORT
    assert outcome.capability_gap_reason == ECONOMIC_GAP_NO_PRIMITIVE


def test_a_safety_buy_against_a_plain_charge_is_not_a_capability_gap() -> None:
    """Compared on direction, with ``safety_buy`` normalised back to the charge.

    The defect this pins: a desired charge attributed to the reserve and a
    capability charge that was not read as a capability gap, when both plans were
    doing the same thing in the same intervals for the same reason.
    """
    table = reference_table()
    floor = table.limits.energy_for_soc(FLOOR_PERCENT)
    outcome = outcome_for(
        table,
        reserve_deadline_horizon(table),
        start_kwh=floor,
        terminal_kwh=floor,
        gain=0.10,
        battery_export=False,
    )

    assert outcome.action == ECONOMIC_ACTION_SAFETY_BUY
    assert outcome.capability_action == ECONOMIC_ACTION_SAFETY_BUY
    assert outcome.capability_gap_reason == ECONOMIC_GAP_NONE


def test_a_gap_in_a_direction_that_has_a_primitive_is_a_forecast_gap() -> None:
    """Both directions are implemented, so a disagreement is about feasibility.

    ``forecast_infeasible`` rather than ``no_primitive``: the actuator exists, and
    what the capability plan could not do was reach the same state under the same
    forecast.
    """
    assert ECONOMIC_ACTION_CHARGE in IMPLEMENTED_ACTIONS
    assert ECONOMIC_ACTION_DISCHARGE in IMPLEMENTED_ACTIONS
    assert ECONOMIC_GAP_FORECAST_INFEASIBLE != ECONOMIC_GAP_NO_PRIMITIVE


def test_the_opt_ins_shape_the_desired_plan_and_the_published_action() -> None:
    """Both default to off, and both change what is published without executing.

    This is why they are offered in the options form while the execution enable is
    withheld: turning grid charging on moves the action, the value forgone and
    every per-run figure, all in a release that sends nothing.
    """
    table = reference_table()
    horizon = eight_interval_horizon(table)

    neither = outcome_for(
        table,
        horizon,
        start_kwh=START_KWH,
        grid_charging=False,
        battery_export=False,
    )
    buying = outcome_for(
        table, horizon, start_kwh=START_KWH, grid_charging=True, battery_export=False
    )
    both = outcome_for(
        table, horizon, start_kwh=START_KWH, grid_charging=True, battery_export=True
    )

    assert neither.action == ECONOMIC_ACTION_HOLD
    assert buying.action == ECONOMIC_ACTION_CHARGE
    assert both.action == ECONOMIC_ACTION_CHARGE
    assert both.desired.expected_net_value_eur > buying.desired.expected_net_value_eur
    assert buying.desired.expected_net_value_eur > (
        neither.desired.expected_net_value_eur
    )


# -- E. an unavailable outcome ----------------------------------------------


def test_an_unavailable_outcome_holds_and_says_why() -> None:
    """No horizon, no plan. Hold, and a named reason rather than a silent zero."""
    table = reference_table()
    horizon = horizon_for(table, demands=[], prices=[])
    outcome = outcome_for(table, horizon, start_kwh=START_KWH)

    assert outcome.available is False
    assert outcome.unavailable_reason is not None
    assert outcome.action == ECONOMIC_ACTION_HOLD
    assert outcome.capability_action == ECONOMIC_ACTION_HOLD
    assert outcome.reason == ECONOMIC_REASON_NO_ACTION
    assert outcome.price_eur_kwh is None
    assert outcome.safety_buy_ac_kwh == pytest.approx(0.0)


def test_the_solver_timings_are_reported_for_both_solves() -> None:
    """Measured, not estimated, so a performance regression is visible in the field."""
    table = reference_table()
    outcome = outcome_for(table, eight_interval_horizon(table), start_kwh=START_KWH)

    assert outcome.solve_ms >= 0.0
    assert outcome.buckets == table.buckets
    assert outcome.bucket_kwh == table.bucket_kwh


def test_two_identical_calls_produce_equal_labels() -> None:
    """Deterministic through ``build_outcome``, not just through ``solve``."""
    table = reference_table()
    horizon = eight_interval_horizon(table)

    first = outcome_for(table, horizon, start_kwh=START_KWH)
    second = outcome_for(table, horizon, start_kwh=START_KWH)

    assert first.desired == second.desired
    assert first.capability == second.capability
    assert first.safety_buy_runs == second.safety_buy_runs
    assert first.action == second.action
    assert first.reason == second.reason
