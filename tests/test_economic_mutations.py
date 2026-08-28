"""Deliberately break each economic invariant, and prove a test notices.

A green suite is not evidence on its own. A test that would also pass against the
broken implementation it exists to protect against is decoration, and the only way
to find out which kind you have is to break the thing and watch.

Every mutation here is a *plausible* refactor rather than an absurdity -- the kind
of change someone might make in good faith while tidying up. Five are worth
singling out, and every one of the five was real:

* **the charge permission measured on direction alone.** Storing your own sunshine
  became "charging from the grid", so with the default opt-ins the model believed
  a battery never absorbs anything. On any sunny day the terminal bound then sat
  above everything the state space could reach and the plan collapsed to
  unavailable -- reported, worse, as an empty horizon. The ratified contract is
  that ``charge`` means *buying*, and absorption is ``hold``.
* **the terminal bound taken from the continuous trajectory.** The same collapse
  from the other side: a bound expressed in a resolution the state space cannot
  represent is a bound nothing can satisfy. The ratified contract reproduces it on
  the solver's own bucketed grid, reachable by construction.
* **the export permission measured on direction alone.** Unavoidable spill made
  every sunny state illegal, which silently collapsed the desired plan onto the
  capability plan and made the whole two-solve design report a gap of zero.
* **the reserve falling back to a profit solve when unreachable.** A deficit made
  the optimizer *freer* rather than more careful, which is precisely backwards.
* **the fingerprint taken over the answer instead of the inputs.** The plan's
  horizon starts at the next boundary, so it differs every quarter-hour: digesting
  it stores ninety-six documents a day.

The mutations are reimplemented locally or expressed as rearrangements of the real
inputs, so the real module is never modified and the last test proves it.
"""

from __future__ import annotations

import ast
import math
import pathlib
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.alpha_ems_manager import economic as economic_module
from custom_components.alpha_ems_manager.activity import (
    RunContent,
    RunIdentity,
    next_activity,
)
from custom_components.alpha_ems_manager.battery import (
    INTERVAL_HOURS,
    build_limits,
)
from custom_components.alpha_ems_manager.const import (
    ECONOMIC_ACTION_CHARGE,
    ECONOMIC_ACTION_DISCHARGE,
    ECONOMIC_ACTION_EXPORT,
    ECONOMIC_ACTION_HOLD,
    ECONOMIC_BUCKET_BAND_KWH,
    ECONOMIC_BUCKET_KWH,
    ECONOMIC_BUCKET_RULE_CONSTANT,
    ECONOMIC_BUCKET_STATE_BUDGET,
    ECONOMIC_GAP_NO_PRIMITIVE,
    ECONOMIC_GAP_NONE,
)
from custom_components.alpha_ems_manager.economic import (
    IntervalPrice,
    build_horizon,
    build_physics_table,
    economic_as_dict,
    fingerprint_economic,
    hold_cost,
    select_bucket_kwh,
)
from custom_components.alpha_ems_manager.economic import solve as economic_solve
from custom_components.alpha_ems_manager.reserve import build_reserve
from custom_components.alpha_ems_manager.simulation import IntervalDemand

from .live_capability import assert_charge_only_capability
from .test_economic_actions import outcome_for, reserve_deadline_horizon
from .test_economic_model import (
    EIGHT,
    ETA,
    EVERYTHING,
    FLOOR_PERCENT,
    IMPLEMENTED,
    START_KWH,
    SUNNY_HOLD_END_KWH,
    eight_interval_horizon,
    flat_demands,
    horizon_for,
    reference_table,
    solved,
    sunny_horizon,
    two_tier_prices,
)
from .test_economic_reporting import two_day_case

# --- A. quantisation ---------------------------------------------------------


def test_quantising_the_requirement_down_is_caught() -> None:
    """Mutation: ``bucket_at_or_below`` where ``bucket_at_or_above`` belongs.

    What an earlier draft of the plan specified, and it reads as the tidier
    choice. It makes a real 0.24 kWh shortfall report as zero: the requirement
    drops to the bucket *below* the true one, so a pack sitting exactly there looks
    satisfied. Protecting one bucket too much is the safe error; ignoring one
    bucket of shortfall is not.
    """
    table = reference_table()
    raw = 4.64

    honest = table.energy(table.bucket_at_or_above(raw))
    broken = table.energy(table.bucket_at_or_below(raw))

    assert honest == 4.75
    assert broken == 4.5
    assert honest > raw > broken
    # A pack holding 4.50 is one bucket short of the honest requirement, and
    # exactly at the broken one.
    assert honest - 4.5 == pytest.approx(0.25)
    assert broken - 4.5 == pytest.approx(0.0)


def test_rounding_the_requirement_to_nearest_is_caught() -> None:
    """Mutation: round to the closest bucket instead of always up.

    Half the time it rounds down, which is the unsafe half. 4.60 kWh becomes 4.50
    -- ten watt-hours of real requirement discarded -- and there is no direction
    argument that makes rounding to nearest safe in a safety figure.
    """
    table = reference_table()
    raw = 4.60

    honest = table.energy(table.bucket_at_or_above(raw))
    broken = round(raw / table.bucket_kwh) * table.bucket_kwh

    assert honest == 4.75
    assert broken == pytest.approx(4.5)
    assert broken < raw


def test_snapping_the_measured_state_of_charge_up_is_caught() -> None:
    """Mutation: ``bucket_at_or_above`` for the *start* energy.

    Symmetrically wrong to the last one. The model would assume the pack holds
    energy it might not -- 18.656 kWh becomes 18.75 -- so a reserve could read as
    satisfied purely by rounding.
    """
    table = reference_table()
    measured = 18.656

    honest = table.energy(table.bucket_at_or_below(measured))
    broken = table.energy(table.bucket_at_or_above(measured))

    assert honest == 18.5
    assert broken == 18.75
    assert honest < measured < broken


def test_quantising_the_configured_floor_is_caught() -> None:
    """Mutation: put the user's own floor on the bucket grid too.

    It looks consistent and it moves the user's setting, which Phase 7 exists to
    refuse. A 20 % floor on a 22 kWh pack is 4.4 kWh, which is not a multiple of
    0.25 -- so quantising it either raises it to 4.5 (stealing usable energy) or
    lowers it to 4.25 (breaching the setting).
    """
    table = reference_table()
    floor = table.limits.energy_for_soc(FLOOR_PERCENT)

    assert floor == 4.4
    assert table.energy(table.bucket_at_or_above(floor)) == 4.5
    assert table.energy(table.bucket_at_or_below(floor)) == 4.25
    # The real table quantises neither: the clamp enforces 4.4 exactly, which is
    # why the reachable set from 4.5 stops one bucket short of 4.25.
    assert 17 not in {move.target for move in table.moves[18]}


def test_measuring_the_violation_against_the_raw_requirement_is_caught() -> None:
    """Mutation: bucket the states but compare against the unquantised figure.

    This reintroduces sub-bucket violations -- the very thing the quantisation
    exists to make *unrepresentable* -- and with a lexicographic order that means
    the optimizer will pay real money to close a shortfall of one watt-hour.
    """
    table = reference_table()
    raw = 4.41
    requirement = table.energy(table.bucket_at_or_above(raw))

    honest = [table.energy(bucket) - requirement for bucket in range(table.buckets + 1)]
    broken = [table.energy(bucket) - raw for bucket in range(table.buckets + 1)]

    for gap in honest:
        assert abs(gap / table.bucket_kwh - round(gap / table.bucket_kwh)) < 1e-9
    assert any(
        abs(gap / table.bucket_kwh - round(gap / table.bucket_kwh)) > 1e-6
        for gap in broken
    )


# --- B. the terminal condition ----------------------------------------------


def test_a_terminal_floor_at_the_reserve_dumps_the_battery_is_caught() -> None:
    """Mutation: use the reserve at the last interval as the terminal bound.

    Equivalent to no terminal condition at all, because the reserve *is* the floor
    at the horizon's end by construction. The plan then empties the pack into the
    final priced interval, every single day, and the only thing that made it do so
    is where the data stopped.
    """
    table = reference_table()
    floor = table.limits.energy_for_soc(FLOOR_PERCENT)
    horizon = horizon_for(
        table,
        demands=flat_demands(4, load_kwh=0.0),
        prices=[IntervalPrice(import_eur_kwh=0.20, export_eur_kwh=0.55)] * 4,
    )

    honest = solved(table, horizon, start_kwh=15.0, terminal_kwh=15.0)
    broken = solved(table, horizon, start_kwh=15.0, terminal_kwh=floor)

    # The honest plan trades within the horizon -- buying at 0.20 to sell at 0.55
    # clears the round trip -- but it puts back everything it took out.
    assert honest.end_energy_dc_kwh == pytest.approx(15.0, abs=1e-9)
    assert honest.planned_grid_export_kwh > 0.0

    # The broken one ends ten kilowatt-hours lower: four intervals at the inverter
    # limit, which is what "dump" means when the power bound binds before the
    # floor does. Every one of those ten is energy the horizon's end took with it.
    assert broken.end_energy_dc_kwh == pytest.approx(5.0, abs=1e-9)
    assert honest.end_energy_dc_kwh - broken.end_energy_dc_kwh == pytest.approx(10.0)
    assert broken.planned_grid_export_kwh > honest.planned_grid_export_kwh
    assert floor < broken.end_energy_dc_kwh


def test_pricing_the_terminal_energy_at_the_horizon_minimum_still_dumps() -> None:
    """Mutation: value terminal energy at the cheapest import in the horizon.

    A replacement-cost argument that sounds principled and does not work: with a
    0.129 EUR/kWh import floor the horizon minimum is typically 0.13-0.20 while an
    evening export peak is 0.30, so the sale is still worth taking and the pack
    still empties. Using the *maximum* instead would prevent it, but nothing
    justifies choosing the maximum -- that is a magic constant wearing a formula.
    """
    horizon_min_import = 0.13
    peak_export = 0.30

    assert peak_export * ETA > horizon_min_import
    # Which is to say: selling still pays even after the round trip, so a floor
    # priced this way is not a floor.


def test_a_hold_trajectory_simulated_without_absorption_is_looser() -> None:
    """Mutation: build the terminal bound with ``absorb_surplus=False``.

    A tempting simplification -- the DP's own idle move does not absorb either --
    and it is the wrong direction: a lower endpoint is a weaker bound, so it
    permits selling the sun the moment it lands.
    """
    table = reference_table()
    horizon = sunny_horizon(table)

    honest = solved(
        table,
        horizon,
        start_kwh=6.0,
        terminal_kwh=SUNNY_HOLD_END_KWH,
        permitted=IMPLEMENTED,
    )
    broken = solved(
        table, horizon, start_kwh=6.0, terminal_kwh=6.0, permitted=IMPLEMENTED
    )

    assert broken.terminal_floor_kwh < honest.terminal_floor_kwh
    assert broken.planned_grid_export_kwh > honest.planned_grid_export_kwh
    assert broken.end_energy_dc_kwh < honest.end_energy_dc_kwh


def test_applying_the_terminal_bound_at_every_interval_is_caught() -> None:
    """Mutation: require ``E(t) >= E_hold(t)`` for all ``t``, not only at the end.

    It looks like the stronger, safer reading. It forbids *all* mid-horizon
    headroom creation -- sell into the evening peak, let tomorrow's sun refill --
    which is the single most valuable thing the optimizer can do in summer.
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
    plan = solved(table, horizon, start_kwh=12.0, terminal_kwh=12.0)

    trough = min(
        entry.start_energy_dc_kwh + entry.battery_delta_dc_kwh
        for entry in plan.intervals
    )

    # The honest plan dips below its own endpoint, which the per-interval mutation
    # would have made infeasible.
    assert trough < plan.end_energy_dc_kwh - 1e-9
    assert plan.end_energy_dc_kwh == pytest.approx(12.0, abs=1e-9)
    assert plan.planned_grid_export_kwh > 0.0


def test_leaving_the_terminal_bound_on_the_continuous_scale_collapses_it() -> None:
    """Mutation: enforce the continuous hold endpoint on the discrete state space.

    **Real, and found by this suite.** Bucketed absorption loses up to one bucket
    an interval against the continuous trajectory, so on a sunny horizon the
    requested figure sits above anything the states can express. Every state is
    then infeasible and the whole plan reports unavailable -- in the shipped
    version, under the reason ``economic_horizon_empty``, about a horizon that was
    four intervals long.
    """
    table = reference_table()
    horizon = sunny_horizon(table)
    plan = solved(
        table,
        horizon,
        start_kwh=6.0,
        terminal_kwh=SUNNY_HOLD_END_KWH,
        permitted=IMPLEMENTED,
    )

    # What the mutation would have enforced, against what the grid can reach.
    requested_bucket = table.bucket_at_or_below(SUNNY_HOLD_END_KWH)
    reachable_bucket = 24 + 4 * 9

    assert requested_bucket > reachable_bucket
    assert plan.available is True
    assert plan.terminal_floor_kwh == table.energy(reachable_bucket)


# --- C. the boundary contract ------------------------------------------------


def test_pricing_export_revenue_on_the_battery_figure_is_caught() -> None:
    """Mutation: ``export_price * battery_discharge_ac_kwh``.

    Overstates the revenue by the house load, every interval, because the meter
    only ever sees what the house could not use. On this fixture that is a full
    kilowatt-hour of phantom sales.
    """
    table = reference_table()
    plan = solved(table, eight_interval_horizon(table), start_kwh=START_KWH)

    honest = 0.35 * plan.planned_grid_export_kwh
    broken = 0.35 * plan.planned_discharge_ac_kwh

    assert broken > honest
    assert broken - honest == pytest.approx(0.35 * 1.0, abs=1e-9)


def test_pricing_import_cost_on_the_battery_figure_is_caught() -> None:
    """Mutation: ``import_price * battery_charge_ac_kwh``.

    Understates the cost by the house load, which is the same error with the sign
    reversed -- and the direction that makes a plan look better than it is.
    """
    table = reference_table()
    plan = solved(table, eight_interval_horizon(table), start_kwh=START_KWH)

    honest = 0.10 * plan.planned_grid_import_kwh
    broken = 0.10 * plan.planned_charge_ac_kwh

    assert broken < honest
    assert honest - broken == pytest.approx(0.10 * 1.0, abs=1e-9)


def test_pricing_anything_on_a_dc_quantity_is_caught() -> None:
    """Mutation: multiply a price by ``battery_delta_dc_kwh``.

    Wrong by the conversion factor in both directions at once: a charge costs
    ``1/eta`` too little and a discharge earns ``eta`` too much. Nothing in the
    payload is a DC quantity priced, and the reconciliation test would fail by
    exactly this ratio.
    """
    table = reference_table()
    plan = solved(table, eight_interval_horizon(table), start_kwh=START_KWH)

    for entry in plan.intervals:
        if not entry.moves_battery:
            continue
        dc = abs(entry.battery_delta_dc_kwh)
        ac = entry.battery_charge_ac_kwh or entry.battery_discharge_ac_kwh
        assert dc != pytest.approx(ac, abs=1e-6)
        assert dc / ac == pytest.approx(ETA if entry.battery_charge_ac_kwh else 1 / ETA)


def test_publishing_the_battery_figure_as_the_export_energy_is_caught() -> None:
    """Mutation: ``energy_kwh`` for an export returns the discharge instead.

    A user reading "export 8.5 kWh" means the grid. The commanded quantity is
    still a battery rate and it exceeds the export by the house load, so both live
    in the per-run diagnostics -- but the published figure has to be the one that
    gets paid for.
    """
    table = reference_table()
    plan = solved(table, eight_interval_horizon(table), start_kwh=START_KWH)
    run = next(run for run in plan.runs if run.action == ECONOMIC_ACTION_EXPORT)

    assert run.energy_kwh == pytest.approx(run.grid_export_kwh)
    assert run.battery_discharge_ac_kwh > run.grid_export_kwh
    assert run.battery_discharge_ac_kwh - run.grid_export_kwh == pytest.approx(1.0)


def test_stating_the_interval_examples_as_export_is_caught() -> None:
    """Mutation: read "5 kWh at 20/10/5 kW is 1/2/4 intervals" as *export*.

    An error in an earlier revision of the plan's own worked examples. The figures
    are right as **battery AC**; as export the house load gets in the way and
    5 kWh at 10 kW against 1 kW of load is 2.22 intervals rather than 2.
    """
    battery_ac_per_interval = 10.0 * 0.25
    export_per_interval = (10.0 - 1.0) * 0.25

    assert 5.0 / battery_ac_per_interval == 2.0
    assert 5.0 / export_per_interval == pytest.approx(2.222, abs=0.001)
    assert math.ceil(5.0 / export_per_interval - 1e-9) == 3


# --- D. the permissions ------------------------------------------------------


def test_gating_solar_absorption_behind_the_grid_charging_opt_in_is_caught() -> None:
    """Mutation: permit a charge on direction alone.

    **Real, and the most damaging defect this phase had.** Storing production the
    house cannot use draws nothing from the grid, so it is ambient physical
    behaviour and not a purchase -- the ratified contract is that ``charge`` and
    ``allow_grid_charging`` mean *buying* and nothing else. Gating absorption
    behind the opt-in made the model believe a battery never absorbs its own solar,
    with the default settings, on every sunny day.
    """
    table = reference_table()
    horizon = sunny_horizon(table)

    # Honest: absorption is permitted with the opt-ins off.
    honest = solved(
        table,
        horizon,
        start_kwh=6.0,
        terminal_kwh=SUNNY_HOLD_END_KWH,
        permitted=IMPLEMENTED,
    )
    # The mutation's world: the only way to absorb was to allow buying.
    with_buying = solved(
        table,
        horizon,
        start_kwh=6.0,
        terminal_kwh=SUNNY_HOLD_END_KWH,
        permitted=IMPLEMENTED | {ECONOMIC_ACTION_CHARGE},
    )

    assert honest.available is True
    assert honest.planned_charge_ac_kwh > 0.0
    assert honest.planned_grid_import_kwh == pytest.approx(0.0)
    # And permitting purchases changes nothing here, which is the proof that the
    # absorption was never a purchase.
    assert with_buying.planned_charge_ac_kwh == pytest.approx(
        honest.planned_charge_ac_kwh
    )


def test_labelling_free_absorption_as_a_charge_run_is_caught() -> None:
    """Mutation: give an ambient charge the ``charge`` action and a run.

    Two consequences, both bad. It reports the sun arriving as a trade the
    optimizer decided to make, and it charges ``minimum_trade_gain_eur`` against
    it -- so a high enough threshold would have the battery decline free energy to
    save money nobody pays.
    """
    table = reference_table()
    horizon = sunny_horizon(table)
    kwargs = {
        "start_kwh": 6.0,
        "terminal_kwh": SUNNY_HOLD_END_KWH,
        "permitted": IMPLEMENTED,
    }

    free = solved(table, horizon, gain=0.0, **kwargs)
    dear = solved(table, horizon, gain=10.0, **kwargs)

    assert free.planned_charge_ac_kwh > 0.0
    assert free.runs == ()
    assert {entry.action for entry in free.intervals} == {ECONOMIC_ACTION_HOLD}
    assert free.switching_cost_eur == pytest.approx(0.0)
    assert dear.planned_charge_ac_kwh == pytest.approx(free.planned_charge_ac_kwh)


def test_gating_unavoidable_spill_behind_the_export_opt_in_is_caught() -> None:
    """Mutation: permit a discharge on direction alone and refuse any export.

    **Real.** Production exceeding house load spills whatever the battery does, so
    forbidding a state because of a spill the battery did not cause makes the
    interval infeasible rather than safe -- and collapses the desired plan onto
    the capability plan, reporting a value forgone of zero for every sunny day.
    """
    table = reference_table()
    horizon = horizon_for(
        table,
        demands=flat_demands(2, load_kwh=0.0, pv_kwh=3.0),
        prices=[IntervalPrice(import_eur_kwh=0.30, export_eur_kwh=0.10)] * 2,
    )
    plan = solved(
        table, horizon, start_kwh=22.0, terminal_kwh=22.0, permitted=IMPLEMENTED
    )

    assert plan.available is True
    assert plan.planned_grid_export_kwh > 0.0
    assert ECONOMIC_ACTION_EXPORT not in plan.permitted


def test_producing_the_capability_plan_by_filtering_is_caught() -> None:
    """Mutation: take the desired plan and cross out the unimplemented runs.

    Cheaper than a second solve and it answers a different question. A filtered
    plan keeps the desired plan's charge *window* -- chosen to feed a sale that no
    longer happens -- so it reports a capability the actuators do not have.
    """
    table = reference_table()
    outcome = outcome_for(table, eight_interval_horizon(table), start_kwh=START_KWH)

    desired = [(run.action, run.start_index) for run in outcome.desired.runs]
    capability = [(run.action, run.start_index) for run in outcome.capability.runs]

    assert desired == [(ECONOMIC_ACTION_CHARGE, 0), (ECONOMIC_ACTION_EXPORT, 4)]
    assert capability == [
        (ECONOMIC_ACTION_CHARGE, 2),
        (ECONOMIC_ACTION_DISCHARGE, 4),
    ]
    # A filtered copy would have kept the four-interval charge window.
    assert capability[0][1] != desired[0][1]


def test_measuring_caused_export_against_zero_is_caught() -> None:
    """Mutation: any export at all counts as one the optimizer chose.

    The same failure as gating on direction, expressed as a threshold rather than
    as a branch, and it is worth pinning separately because it is the shape a
    reviewer is most likely to write while "simplifying" the baseline away.
    """
    table = reference_table()
    horizon = horizon_for(
        table,
        demands=flat_demands(2, load_kwh=0.0, pv_kwh=3.0),
        prices=[IntervalPrice(import_eur_kwh=0.30, export_eur_kwh=0.10)] * 2,
    )
    plan = solved(
        table, horizon, start_kwh=22.0, terminal_kwh=22.0, permitted=IMPLEMENTED
    )

    # Every interval exports, and none of it is the battery's doing.
    for entry in plan.intervals:
        assert entry.grid_export_kwh > 0.0
        assert entry.battery_discharge_ac_kwh == pytest.approx(0.0)


# --- E. the ordering ---------------------------------------------------------


def test_replacing_the_lexicographic_order_with_a_weight_is_caught() -> None:
    """Mutation: minimise ``violation * W + cost`` for some large ``W``.

    Any finite weight is a price, and a price can be paid. The honest order avoids
    a shortfall at a cost of nine hundred euros here, so a weight of a hundred --
    or a thousand, or any constant a reviewer would write -- lets a big enough
    price buy the violation. That is why the ordering is lexicographic and not
    merely steep.
    """
    table = reference_table()
    floor = table.limits.energy_for_soc(FLOOR_PERCENT)
    horizon = horizon_for(
        table,
        demands=flat_demands(4, load_kwh=0.0),
        prices=[IntervalPrice(import_eur_kwh=999.0, export_eur_kwh=0.0)] * 4,
        reserve_kwh=[floor, floor, 8.0, 8.0],
    )
    plan = solved(table, horizon, start_kwh=floor, terminal_kwh=floor)

    assert plan.violation_kwh == pytest.approx(0.0)
    # What the honest order was willing to pay. Any weight below this loses.
    assert plan.cost_eur > 1000.0


def test_falling_back_to_a_profit_solve_when_the_reserve_is_unreachable() -> None:
    """Mutation: drop the reserve term once the shortfall is irreducible.

    **Real, in an earlier revision of the plan.** It is exactly backwards: a
    deficit would *unlock* the export, so the emptier the battery the freer the
    optimizer. The honest answer keeps both terms and lets the first one tie.
    """
    table = reference_table()
    horizon = horizon_for(
        table,
        demands=flat_demands(4, load_kwh=0.0),
        prices=[IntervalPrice(import_eur_kwh=0.20, export_eur_kwh=0.90)] * 4,
        reserve_kwh=[22.0] * 4,
    )
    honest = solved(table, horizon, start_kwh=5.0, terminal_kwh=5.0)
    # The fallback's world: the reserve term dropped entirely once it could not be
    # met, leaving a plain profit solve over the same prices.
    relaxed = horizon_for(
        table,
        demands=flat_demands(4, load_kwh=0.0),
        prices=[IntervalPrice(import_eur_kwh=0.20, export_eur_kwh=0.90)] * 4,
        reserve_kwh=[4.4] * 4,
    )
    broken = solved(table, relaxed, start_kwh=5.0, terminal_kwh=4.4)

    assert honest.violation_kwh > 0.0
    assert honest.planned_grid_export_kwh == pytest.approx(0.0)
    assert broken.planned_grid_export_kwh > 0.0
    assert broken.cost_eur < honest.cost_eur


def test_exempting_the_reserve_from_the_threshold_by_special_case_is_caught() -> None:
    """Mutation: ``if reserve_short: ignore minimum_trade_gain_eur``.

    The obvious implementation, and a special case is a thing that can be applied
    in the wrong place. None is needed: reserve feasibility already outranks cost,
    so a protective charge happens below the threshold as a *consequence* of the
    ordering rather than as an exception to it.
    """
    table = reference_table()
    floor = table.limits.energy_for_soc(FLOOR_PERCENT)
    horizon = horizon_for(
        table,
        demands=flat_demands(4, load_kwh=0.0),
        prices=[IntervalPrice(import_eur_kwh=0.20, export_eur_kwh=0.02)] * 4,
        reserve_kwh=[floor, floor, 8.0, 8.0],
    )
    plan = solved(table, horizon, start_kwh=floor, terminal_kwh=floor, gain=999.0)

    assert plan.violation_kwh == pytest.approx(0.0)
    assert any(run.action == ECONOMIC_ACTION_CHARGE for run in plan.runs)
    # And the threshold was charged, not waived: it is a cost, not a veto.
    assert plan.switching_cost_eur == pytest.approx(999.0)


def test_reporting_only_the_first_violation_is_caught() -> None:
    """Mutation: report the first shortfall rather than the sum over the horizon.

    Both are published, and for different reasons: the worst single shortfall says
    how bad it gets, and the sum says how long it lasts. Reporting only one would
    make a four-interval brush with the reserve indistinguishable from a
    four-interval breach of it.
    """
    table = reference_table()
    horizon = horizon_for(
        table,
        demands=flat_demands(4, load_kwh=0.0),
        prices=[IntervalPrice(import_eur_kwh=0.20, export_eur_kwh=0.02)] * 4,
        reserve_kwh=[6.0] * 4,
    )
    plan = solved(
        table, horizon, start_kwh=4.5, terminal_kwh=4.4, permitted=IMPLEMENTED
    )

    assert plan.worst_shortfall_kwh == pytest.approx(1.5)
    assert plan.violation_kwh == pytest.approx(6.0)
    assert plan.first_violation_index == 0


# --- F. runs and the switching cost -----------------------------------------


def test_charging_the_switching_cost_per_interval_is_caught() -> None:
    """Mutation: add ``minimum_trade_gain_eur`` to every acting interval.

    It suppresses exactly the wrong trades. A long profitable window pays the fee
    once per interval and dies, while a single-interval micro-cycle pays it once
    and survives -- the opposite of what the threshold is for.
    """
    table = reference_table()
    horizon = eight_interval_horizon(table)
    gain = 0.30
    plan = solved(table, horizon, start_kwh=START_KWH, gain=gain)

    intervals = sum(1 for entry in plan.intervals if entry.moves_battery)
    gross = plan.hold_cost_eur - plan.cost_eur

    assert len(plan.runs) == 2
    assert intervals == 8
    assert plan.switching_cost_eur == pytest.approx(2 * gain)
    # Per run the trade clears the bar by more than a euro; per interval it would
    # be charged 2.40 against a gross gain of 2.09 and would not happen at all.
    assert gross > 2 * gain
    assert intervals * gain > gross


def test_taking_the_run_state_from_the_physical_mode_is_caught() -> None:
    """Mutation: derive the run from ``move.mode`` instead of the classification.

    The physics table knows a move charges; only the interval's economics know
    whether that charge was a purchase or the sun arriving. Reading the mode makes
    every absorbed kilowatt-hour the start of a trade.
    """
    table = reference_table()
    plan = solved(
        table,
        sunny_horizon(table),
        start_kwh=6.0,
        terminal_kwh=SUNNY_HOLD_END_KWH,
        gain=0.10,
        permitted=IMPLEMENTED,
    )

    charging = [entry for entry in plan.intervals if entry.battery_charge_ac_kwh > 0.0]

    assert charging, "the fixture must actually absorb something"
    assert all(entry.action == ECONOMIC_ACTION_HOLD for entry in charging)
    assert all(not entry.run_start for entry in charging)
    assert plan.switching_cost_eur == pytest.approx(0.0)


def test_reporting_the_value_net_of_the_switching_cost_is_caught() -> None:
    """Mutation: ``hold - cost`` without adding the switching cost back.

    The switching cost is a notional device for suppressing pointless action;
    nobody pays it. Reporting a gain net of it understates what the plan actually
    earns -- by ten cents a run, which on a busy day is most of the gain.
    """
    table = reference_table()
    horizon = eight_interval_horizon(table)
    plan = solved(table, horizon, start_kwh=START_KWH, gain=0.25)

    honest = (plan.hold_cost_eur - plan.cost_eur) - plan.switching_cost_eur
    broken = plan.hold_cost_eur - plan.cost_eur

    assert plan.expected_net_value_eur == pytest.approx(honest)
    assert broken - honest == pytest.approx(0.50)
    assert plan.switching_cost_eur == pytest.approx(0.50)


# --- G. labels, curtailment and evidence ------------------------------------


def test_attributing_a_safety_buy_by_a_price_threshold_is_caught() -> None:
    """Mutation: call a charge a safety buy when the price is above some figure.

    A cheap interval and a reserve deadline coincide constantly, so no price
    threshold can separate them. The honest attribution solves the same horizon
    with the reserve relaxed to the configured floor: the charging that disappears
    is the charging the reserve was responsible for.
    """
    table = reference_table()
    floor = table.limits.energy_for_soc(FLOOR_PERCENT)

    # A reserve-driven charge at a *cheap* price, which any threshold misses.
    cheap = outcome_for(
        table,
        reserve_deadline_horizon(table, import_price=0.05),
        start_kwh=floor,
        terminal_kwh=floor,
        gain=0.10,
        battery_export=False,
    )
    # And an economically-driven charge, which any threshold would also catch.
    economic = outcome_for(table, eight_interval_horizon(table), start_kwh=START_KWH)

    assert cheap.safety_buy_runs == (0,)
    assert cheap.price_eur_kwh == pytest.approx(0.05)
    assert economic.safety_buy_runs == ()
    assert economic.price_eur_kwh == pytest.approx(0.10)
    # Both charge at a low price, and only one of them is a safety buy.
    assert cheap.price_eur_kwh < economic.price_eur_kwh


def test_comparing_the_capability_gap_on_the_raw_action_is_caught() -> None:
    """Mutation: report a gap whenever the two action words differ.

    **Real, and caught during implementation.** A desired charge attributed to the
    reserve reads ``safety_buy`` while the capability charge reads ``charge``, so
    the words differ while both plans do the same thing in the same intervals for
    the same reason. Normalising to the underlying direction is what fixes it.
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

    assert outcome.action != ECONOMIC_ACTION_CHARGE
    assert outcome.capability_gap_reason == ECONOMIC_GAP_NONE
    # And a genuine gap still reports one, so the normaliser is not a blanket.
    curtail = outcome_for(
        table,
        horizon_for(
            table,
            demands=flat_demands(2, load_kwh=0.25, pv_kwh=3.0),
            prices=[IntervalPrice(import_eur_kwh=0.30, export_eur_kwh=-0.10)] * 2,
        ),
        start_kwh=22.0,
        terminal_kwh=22.0,
    )
    assert curtail.capability_gap_reason == ECONOMIC_GAP_NO_PRIMITIVE


def test_curtailing_more_than_the_would_be_export_is_caught() -> None:
    """Mutation: decline all production while the export price is negative.

    Over-curtailment forces import at a positive price to cover load that free
    production was about to meet. The closed form declines exactly the energy that
    would otherwise have been exported, and no more.
    """
    table = reference_table()
    horizon = horizon_for(
        table,
        demands=flat_demands(2, load_kwh=0.25, pv_kwh=3.0),
        prices=[IntervalPrice(import_eur_kwh=0.30, export_eur_kwh=-0.10)] * 2,
    )
    plan = solved(table, horizon, start_kwh=22.0, terminal_kwh=22.0)

    for entry in plan.intervals:
        assert entry.pv_curtailed_kwh == pytest.approx(3.0 - 0.25, abs=1e-9)
        assert entry.grid_import_kwh == pytest.approx(0.0)
        assert entry.grid_export_kwh == pytest.approx(0.0)
    # Declining all 3.0 kWh would have bought the load back at 0.30.
    assert plan.cost_eur == pytest.approx(0.0, abs=1e-9)
    assert plan.planned_curtailed_kwh < 2 * 3.0


def test_fingerprinting_the_answer_instead_of_the_inputs_is_caught() -> None:
    """Mutation: digest the plan rather than what produced it.

    **Real, in Phase 7.** The plan's horizon starts at the next boundary, so the
    answer differs every quarter-hour even when nothing has changed: digesting it
    stores ninety-six documents a day and breaks the rule that an unchanged
    refresh costs no I/O.
    """
    inputs = {
        "price_fingerprint": "p",
        "load_fingerprint": "l",
        "pv_fingerprint": "v",
        "reserve_fingerprint": "r",
        "config_fingerprint": "c",
        "settings_fingerprint": "s",
    }
    table = reference_table()
    horizon = eight_interval_horizon(table)

    one = solved(table, horizon, start_kwh=START_KWH)
    other = solved(table, horizon, start_kwh=20.0, terminal_kwh=4.4)

    assert one.cost_eur != pytest.approx(other.cost_eur)
    assert fingerprint_economic(**inputs) == fingerprint_economic(**inputs)
    # And it does move when an input does, so it is not a constant.
    assert fingerprint_economic(**{**inputs, "price_fingerprint": "q"}) != (
        fingerprint_economic(**inputs)
    )


def test_treating_an_unforecast_interval_as_an_idle_house_is_caught() -> None:
    """Mutation: read a ``None`` baseline as zero load.

    The rule every layer of this integration keeps: learned nothing must never
    read as zero. An unpredicted interval is not a predicted idle house, and a
    plan that spanned one would be planning over invented data.
    """
    table = reference_table()
    demands = flat_demands(4)
    demands[2] = IntervalDemand(index=2, baseline_kwh=None, pv_kwh=None)
    prices = two_tier_prices(4, cheap_until=2)

    honest = build_horizon(
        demands=demands,
        prices=prices,
        required_reserve_kwh=[4.4] * 4,
        table=table,
    )
    zeroed = build_horizon(
        demands=[
            IntervalDemand(index=index, baseline_kwh=0.0, pv_kwh=0.0)
            if entry.baseline_kwh is None
            else entry
            for index, entry in enumerate(demands)
        ],
        prices=prices,
        required_reserve_kwh=[4.4] * 4,
        table=table,
    )

    assert honest.intervals == 2
    assert honest.limited_by == "load_forecast"
    assert zeroed.intervals == 4
    assert zeroed.limited_by == "complete"


def test_spanning_a_price_gap_instead_of_stopping_at_it_is_caught() -> None:
    """Mutation: skip an unpriced interval and carry on.

    Knowing prices either side of a hole is not knowing them continuously, and a
    plan that spanned the hole would price the gap at whatever came next.
    """
    table = reference_table()
    prices = two_tier_prices(EIGHT, cheap_until=4)
    prices[5] = IntervalPrice(import_eur_kwh=0.20, export_eur_kwh=None)
    horizon = horizon_for(table, demands=flat_demands(EIGHT), prices=prices)

    assert horizon.intervals == 5
    assert horizon.limited_by == "prices"
    assert hold_cost(
        horizon=horizon, table=table, start_energy_kwh=START_KWH
    ) < hold_cost(
        horizon=eight_interval_horizon(table),
        table=table,
        start_energy_kwh=START_KWH,
    )


# --- hygiene ----------------------------------------------------------------


# --- H. the beta.16 reporting and model corrections --------------------------
#
# beta.16 changed what the plan says about itself far more than what it decides,
# so most of these mutations break a *number a person reads* rather than a
# decision. That makes them easy to reintroduce by accident and hard to notice --
# which is exactly why each one is pinned.


def beta16_arbitrage(table, *, pv_kwh=0.0, pv_only_at=None, load_kwh=0.10):
    """Return a cheap-then-dear plan starting at the floor.

    Local copy rather than an import, so a change to the reporting file's helper
    cannot quietly change what these mutations are measured against.
    """
    from .test_economic_model import FLOOR_PERCENT, horizon_for

    floor = table.limits.energy_for_soc(FLOOR_PERCENT)
    total = 6
    pvs = [
        pv_kwh if (pv_only_at is None or i == pv_only_at) and i < 3 else 0.0
        for i in range(total)
    ]
    horizon = horizon_for(
        table,
        demands=[
            IntervalDemand(index=i, baseline_kwh=load_kwh, pv_kwh=pvs[i])
            for i in range(total)
        ],
        prices=[
            IntervalPrice(import_eur_kwh=0.05, export_eur_kwh=0.02)
            if i < 3
            else IntervalPrice(import_eur_kwh=0.60, export_eur_kwh=0.55)
            for i in range(total)
        ],
    )
    return economic_solve(
        table=table,
        horizon=horizon,
        start_energy_kwh=floor,
        terminal_floor_kwh=floor,
        minimum_trade_gain_eur=0.10,
        permitted=EVERYTHING,
    )


def test_letting_absorption_reset_a_charge_run_is_caught() -> None:
    """Mutation: classify ambient absorption as plain idle again.

    The beta.15 behaviour, and the reason it was wrong: a sunny quarter inside a
    paid charging window broke the run, so the next purchasing quarter started a
    second campaign and paid ``minimum_trade_gain_eur`` again. One decision, two
    fees, on any partly-sunny cheap afternoon.
    """
    from custom_components.alpha_ems_manager.economic import (
        _RUN_ABSORB,
        _RUN_CHARGE,
        _RUN_IDLE,
        _resolved_run_state,
    )

    from .test_economic_model import reference_table

    table = reference_table()
    sunny = beta16_arbitrage(table, pv_kwh=2.5, pv_only_at=1)
    dark = beta16_arbitrage(table, pv_kwh=0.0)

    # Honest: the sun changes the cost, never the number of decisions charged.
    assert sunny.switching_cost_eur == pytest.approx(dark.switching_cost_eur)
    assert sunny.intervals[1].absorbing is True
    assert sunny.intervals[2].run_start is False

    # The mutation, applied to the rule itself.
    assert _resolved_run_state(_RUN_ABSORB, _RUN_CHARGE) == _RUN_CHARGE
    assert _RUN_IDLE != _RUN_CHARGE


def test_letting_absorption_pay_a_fee_of_its_own_is_caught() -> None:
    """Mutation: treat absorption as a charge that starts a run.

    Then the sun arriving would be billed as a trade the optimizer chose, and a
    high enough threshold would have the battery decline free energy.
    """
    from .test_economic_model import reference_table

    table = reference_table()
    plan = beta16_arbitrage(table, pv_kwh=2.5, pv_only_at=1)
    absorbing = [e for e in plan.intervals if e.absorbing]

    assert absorbing, "the fixture must actually absorb something"
    for entry in absorbing:
        assert entry.run_start is False
        assert entry.marginal_grid_import_kwh == pytest.approx(0.0)


def test_letting_absorption_continue_a_discharge_run_is_caught() -> None:
    """Mutation: make absorption transparent to *any* run, not just a charge.

    The bug my own first implementation had, caught by an existing test. Absorption
    is a charge; letting it continue a discharge would claim the battery kept
    discharging while it charged, and would suppress the fee a real reversal owes.
    """
    from custom_components.alpha_ems_manager.economic import (
        _RUN_ABSORB,
        _RUN_DISCHARGE,
        _RUN_IDLE,
        _resolved_run_state,
    )

    assert _resolved_run_state(_RUN_ABSORB, _RUN_DISCHARGE) == _RUN_IDLE
    assert _resolved_run_state(_RUN_ABSORB, _RUN_DISCHARGE) != _RUN_DISCHARGE


def test_pricing_the_hold_baseline_on_a_frozen_battery_is_caught() -> None:
    """Mutation: revert the baseline to a battery that never moves.

    Then the baseline *sells* every kilowatt-hour of surplus while the plan --
    held to the ambient endpoint by the terminal bound -- banks it and is credited
    nothing. The published gain is understated by the export value of everything
    absorbed, which on a sunny horizon is most of it.
    """
    from .test_economic_model import (
        flat_demands,
        horizon_for,
        reference_table,
    )

    table = reference_table()
    pv, export_price = 2.5, 0.10
    horizon = horizon_for(
        table,
        demands=flat_demands(8, load_kwh=0.0, pv_kwh=pv),
        prices=[IntervalPrice(import_eur_kwh=0.20, export_eur_kwh=export_price)] * 8,
    )

    honest = hold_cost(horizon=horizon, table=table, start_energy_kwh=6.0)
    frozen = -8 * pv * export_price

    assert honest > frozen
    assert honest - frozen > 1.0


def test_reporting_the_cash_flow_as_the_marginal_cost_is_caught() -> None:
    """Mutation: let ``marginal_cost_eur`` be the negated cash flow again.

    They are different quantities and they disagree most where it matters most: a
    discharge sized to house load has a cash flow of zero and has avoided the
    whole import bill.
    """
    from .test_economic_model import (
        FLOOR_PERCENT,
        IMPLEMENTED,
        flat_demands,
        horizon_for,
        reference_table,
    )

    table = reference_table()
    per_quarter = 2.25 / ETA
    horizon = horizon_for(
        table,
        demands=flat_demands(4, load_kwh=per_quarter),
        prices=[IntervalPrice(import_eur_kwh=0.50, export_eur_kwh=0.45)] * 4,
    )
    plan = economic_solve(
        table=table,
        horizon=horizon,
        start_energy_kwh=20.0,
        terminal_floor_kwh=table.limits.energy_for_soc(FLOOR_PERCENT),
        minimum_trade_gain_eur=0.10,
        permitted=IMPLEMENTED,
    )
    run = next(r for r in plan.runs if r.battery_discharge_ac_kwh > 0.0)

    assert run.net_cash_flow_eur == pytest.approx(0.0, abs=0.01)
    assert run.marginal_cost_eur < -4.0
    assert run.marginal_cost_eur != pytest.approx(-run.net_cash_flow_eur, abs=0.5)


def test_attributing_site_import_to_the_battery_run_is_caught() -> None:
    """Mutation: report ``grid_import_kwh`` as what the run bought.

    Over-claims by the house load over the run, every time. This is the field that
    made "charged 4.48 kWh" read as "bought 4.48 kWh" on the live installation,
    where the site figure beside it was 1.55 kWh and the run's own share smaller
    still.
    """
    from .test_economic_model import reference_table

    table = reference_table()
    load = 0.25
    plan = beta16_arbitrage(table, pv_kwh=0.0, load_kwh=load)
    run = next(r for r in plan.runs if r.battery_charge_ac_kwh > 0.0)

    assert run.grid_import_kwh > run.marginal_grid_import_kwh
    assert run.grid_import_kwh - run.marginal_grid_import_kwh == pytest.approx(
        run.interval_count * load, abs=1e-9
    )


def test_fabricating_a_zero_terminal_cost_is_caught() -> None:
    """**The beta.18 honesty mutation.** No comparison means no number.

    The tempting mutation is to keep the fields and return ``0.0`` and ``False``
    now that the fourth solve is gone -- it keeps every consumer working and reads
    as "the terminal condition costs nothing". It is a different claim from "there
    is no terminal condition", and the second is the true one.
    """
    table = reference_table()
    horizon, hold_end = two_day_case(table)
    outcome = outcome_for(
        table, horizon, start_kwh=17.42, terminal_kwh=hold_end, gain=0.10
    )

    assert outcome.unbounded is None
    for value in (
        outcome.terminal_plan_cost_eur,
        outcome.terminal_plan_import_kwh,
        outcome.terminal_near_field_cost_eur,
    ):
        assert value is None
        assert value != 0.0
    assert outcome.terminal_first_run_changed is None
    assert outcome.terminal_first_run_changed is not False


def test_relaxing_the_production_terminal_bound_is_caught() -> None:
    """Mutation: hand the caller the unbounded plan.

    beta.16 instruments the bound and does not change it. Serving the relaxed solve
    would be a silent change to safety-relevant behaviour while claiming otherwise.
    """
    from .test_economic_actions import outcome_for
    from .test_economic_model import reference_table
    from .test_economic_reporting import two_day_case

    table = reference_table()
    horizon, hold_end = two_day_case(table)
    outcome = outcome_for(
        table, horizon, start_kwh=17.42, terminal_kwh=hold_end, gain=0.10
    )

    assert outcome.desired.terminal_binding is True
    assert outcome.desired.end_energy_dc_kwh == pytest.approx(
        outcome.desired.terminal_floor_kwh, abs=0.25
    )
    assert outcome.action == outcome.desired.published_run.action


def test_keying_the_activity_identity_on_an_index_again_is_caught() -> None:
    """Mutation: identify a run by its horizon index.

    The beta.15 design and the root cause of the spam. An index advances every
    refresh while a run is under way and rebases at midnight; an absolute instant
    does neither.
    """
    import inspect

    from custom_components.alpha_ems_manager import activity

    fields = set(activity.RunIdentity.__dataclass_fields__)
    assert fields == {"direction", "start_utc"}
    # The prose explains why an index is wrong, so the check is on the *code*: no
    # executable line may mention one.
    tree = ast.parse(inspect.getsource(activity))
    names = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    } | {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "start_index" not in names
    assert "end_index" not in names


def test_returning_to_bucket_and_hash_comparison_is_caught() -> None:
    """Mutation: bucket the figures and hash them to decide whether to speak.

    A bucket boundary fires on a hundredth of a kilowatt while a fifth inside one
    stays silent. beta.16 replaced that with a deadband measured from the announced
    value, which cannot flap; beta.31 went further and made the *identity* decide,
    so there is no comparison of figures left to get wrong.

    **The forbidden term list had to change, and the reason is worth stating.**
    beta.31 does hash -- ``plan_id_for`` digests the plan identity to produce the
    six characters a user reads. That is the opposite of the fault: it hashes what
    two refreshes must agree *on*, not what they must be compared *by*. So the
    mutation is now asserted where it lives: no energy, power or price figure may
    appear in anything the id is derived from.
    """
    import inspect

    from custom_components.alpha_ems_manager import activity

    source = inspect.getsource(activity)
    assert "fingerprint" not in source
    # The identity is a category and an instant. Nothing measured in kilowatt-hours
    # or euros may enter it, or a revision would mint a new plan.
    identity_source = inspect.getsource(activity.plan_id_for)
    for forbidden in ("energy", "kwh", "price", "power", "eur"):
        assert forbidden not in identity_source.lower(), forbidden
    assert set(activity.PlanIdentity.__dataclass_fields__) == {"category", "end_utc"}
    # And the deadband is still what absorbs a revision, rather than a bucket.
    assert "ECONOMIC_DEADBAND_ENERGY_KWH" in source


def test_announcing_a_distant_run_is_caught() -> None:
    """Mutation: announce whenever a run exists.

    Exactly the reported symptom: an entry every quarter about a run eighteen
    hours out, whose far end moves every time the plan is rebuilt.
    """

    from .test_activity_announcements import NOW, make_run

    assert (
        next_activity(previous=None, runs=(make_run(start_minutes=180),), now=NOW)
        is None
    )
    assert (
        next_activity(previous=None, runs=(make_run(start_minutes=10),), now=NOW)
        is not None
    )


def test_back_dating_an_elapsed_run_is_caught() -> None:
    """Mutation: announce a run whose window has already closed.

    A line describing a decision nobody could act on, written after the fact.
    """

    from .test_activity_announcements import NOW, make_run

    finished = make_run(start_minutes=-120, duration_minutes=60)
    running = make_run(start_minutes=-30, duration_minutes=120)

    assert next_activity(previous=None, runs=(finished,), now=NOW) is None
    # But one still under way is announced, once.
    assert next_activity(previous=None, runs=(running,), now=NOW) is not None


def test_a_shadow_refresh_can_never_emit_an_execution_kind() -> None:
    """Mutation: let the Activity surface claim the battery began, in Shadow.

    Through beta.23 the guard was the release barrier and this asserted a refusal.
    beta.24 executes a charge, so the kind itself is legitimate -- and the property
    worth protecting moved with it: **Shadow** must still never emit it, whatever
    the barrier says, because a shadow line indistinguishable from a live one is
    the one thing this surface cannot produce.

    **beta.31 makes it structural instead of verbal.** Shadow used to emit
    ``would_start`` -- a distinct kind, worded carefully. It now emits *nothing*
    for a start, because ``_started_entry`` returns early while ``executed`` is
    false. A line that does not exist cannot be mistaken for a live one, and that
    is a stronger guarantee than any wording.
    """
    from custom_components.alpha_ems_manager.activity import (
        ActivityEntry,
        ActivityState,
        ExecutionView,
        _started_entry,
        _terminal_entry,
        logbook_payload,
    )
    from custom_components.alpha_ems_manager.const import (
        ECONOMIC_DIRECTION_CHARGE,
        ECONOMIC_EVENT_ERROR,
        ECONOMIC_EVENT_FINISHED,
        ECONOMIC_EVENT_STARTED,
        ECONOMIC_EVENT_STOPPED,
        ECONOMIC_EXECUTION_EVENT_KINDS,
        EXECUTION_STOP_TARGET_REACHED,
    )

    # beta.19 added ``stopped`` as ``started``'s counterpart; beta.31 added the
    # two terminals, because a success and a failure are also claims about the
    # battery.
    assert set(ECONOMIC_EXECUTION_EVENT_KINDS) == {
        ECONOMIC_EVENT_STARTED,
        ECONOMIC_EVENT_STOPPED,
        ECONOMIC_EVENT_FINISHED,
        ECONOMIC_EVENT_ERROR,
    }

    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    end = now + timedelta(hours=1)
    shadow_view = ExecutionView(
        identity=RunIdentity(direction=ECONOMIC_DIRECTION_CHARGE, start_utc=now),
        end_utc=end,
        running=True,
        executed=False,
        intent="grid_charge",
        activation_confirmed=True,
        run_id="shadow-run",
    )

    # Nothing at all, in either direction, however complete the evidence.
    assert _started_entry(ActivityState(), shadow_view, now=now) is None
    assert (
        _terminal_entry(
            ActivityState(),
            ExecutionView(
                identity=shadow_view.identity,
                end_utc=end,
                executed=False,
                running=False,
                intent="grid_charge",
                stop_reason=EXECUTION_STOP_TARGET_REACHED,
                run_id="shadow-run",
            ),
            now=now,
        )
        is None
    )

    # And the payload accepts the live kind, because this release does execute.
    payload = logbook_payload(
        ActivityEntry(kind=ECONOMIC_EVENT_STARTED, message="x", state=ActivityState()),
        domain="alpha_ems_manager",
        entity_id="sensor.x",
    )
    assert payload["message"] == "x"


def test_the_structural_contracts_are_unchanged_by_beta16() -> None:
    """Mutation: any of the release barriers moving.

    Entity count, the execution flag, the service set and the helper families.
    Grouped because a change to any one of them invalidates the whole release.
    """
    from custom_components.alpha_ems_manager.alphaess_device import (
        FAMILIES,
        PERMITTED_SERVICES,
    )
    from custom_components.alpha_ems_manager.const import (
        ACTION_CHARGE,
        ACTION_DISCHARGE,
    )

    from .test_entity_contract import CONTRACT

    assert len(CONTRACT) == 13
    assert_charge_only_capability()
    assert len(PERMITTED_SERVICES) == 4
    assert set(FAMILIES) == {ACTION_DISCHARGE, ACTION_CHARGE}
    for forbidden in ("force_export", "force_import", "pv_switch"):
        assert forbidden not in str(sorted(FAMILIES))


def test_every_mutation_in_this_file_is_reverted() -> None:
    """The real module is untouched, and the mutations live only in this file.

    Every break above is a local reimplementation or a rearrangement of the real
    inputs, so there is nothing to undo -- but saying so is cheap and the
    alternative is a monkeypatch that outlives its test.
    """
    table = reference_table()
    outcome = outcome_for(table, eight_interval_horizon(table), start_kwh=START_KWH)

    assert table.bucket_kwh == 0.25
    assert table.buckets == 88
    assert table.limits.energy_for_soc(FLOOR_PERCENT) == 4.4
    assert outcome.action == ECONOMIC_ACTION_CHARGE
    assert outcome.desired.permitted == EVERYTHING
    assert outcome.desired.terminal_floor_kwh == pytest.approx(START_KWH)
    assert outcome.desired.cost_eur == pytest.approx(-1.5897, abs=5e-5)


# ===========================================================================
# I. beta.17: the lattice, the honest terminal figure, and the Activity sentence
# ===========================================================================
#
# Four of these guard the bucket selector, which is the only change in beta.17
# that alters what the optimizer *decides*. The guarantee it ships with -- an
# installation is left as it was or improved, never traded off -- is the sort of
# claim that rots quietly, so each way of breaking it is written down as a
# mutation rather than trusted to review.


def test_reverting_to_the_beta16_bucket_is_caught() -> None:
    """A selector that always returns the constant bucket has stopped working.

    The mutation is the entire beta.16 behaviour, so nothing crashes and nothing
    looks wrong -- the reference installation simply goes back to leaving five per
    cent of its inverter unreachable in both directions.
    """
    limits = reference_table().limits
    floor = limits.energy_for_soc(FLOOR_PERCENT)

    def always_constant(_limits, *, floor_energy_kwh):
        return ECONOMIC_BUCKET_KWH, ECONOMIC_BUCKET_RULE_CONSTANT

    honest = select_bucket_kwh(limits, floor_energy_kwh=floor)
    mutated = always_constant(limits, floor_energy_kwh=floor)

    assert honest != mutated
    real = build_physics_table(limits, floor_energy_kwh=floor, bucket_kwh=honest[0])
    broken = build_physics_table(limits, floor_energy_kwh=floor, bucket_kwh=mutated[0])
    assert real.max_representable_charge_kw > broken.max_representable_charge_kw
    assert real.max_representable_discharge_kw > broken.max_representable_discharge_kw


def test_a_bucket_that_lets_a_move_exceed_the_configured_power_is_caught() -> None:
    """Representable is not the same as permitted, and the clamp decides.

    The mutation: pick the bucket that makes the *nameplate* power land on a
    lattice point while ignoring what ``apply_request`` will actually allow. If a
    realigned lattice could smuggle a move past the clamp, this change would be
    unsafe rather than merely wrong -- so the table is checked against the
    configured limits directly.
    """
    limits = reference_table().limits
    floor = limits.energy_for_soc(FLOOR_PERCENT)
    bucket, _rule = select_bucket_kwh(limits, floor_energy_kwh=floor)
    table = build_physics_table(limits, floor_energy_kwh=floor, bucket_kwh=bucket)

    for source, row in enumerate(table.moves):
        for move in row:
            delta = move.target - source
            allowed = limits.max_charge_kw if delta > 0 else limits.max_discharge_kw
            assert move.power_kw <= allowed + 1e-9


def test_improving_one_direction_while_degrading_the_other_is_caught() -> None:
    """The no-regression rule is two-sided, and one-sided is the tempting bug.

    ``quarter_dc / k`` is the obvious alignment and it is **not** safe on its own:
    on a 22 kWh / 5 kW pack it takes the charge side to exactly 5 kW and the
    discharge side from 5.1 % short to 10.0 % short. The released selector
    declines that trade and keeps the beta.16 lattice; this mutation accepts it.
    """
    limits, why = build_limits(
        capacity_kwh=22.0,
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
        round_trip_efficiency_percent=90.0,
    )
    assert limits is not None, why
    floor = limits.energy_for_soc(FLOOR_PERCENT)
    base = build_physics_table(
        limits, floor_energy_kwh=floor, bucket_kwh=ECONOMIC_BUCKET_KWH
    )

    # The naive rule: align the charge quarter and accept whatever discharge does.
    naive = (5.0 * INTERVAL_HOURS * limits.charge_efficiency) / 5
    mutated = build_physics_table(limits, floor_energy_kwh=floor, bucket_kwh=naive)
    assert mutated.max_representable_charge_kw > base.max_representable_charge_kw
    assert mutated.max_representable_discharge_kw < base.max_representable_discharge_kw

    # The released selector refuses it.
    chosen, rule = select_bucket_kwh(limits, floor_energy_kwh=floor)
    assert rule == ECONOMIC_BUCKET_RULE_CONSTANT
    assert chosen == ECONOMIC_BUCKET_KWH


def test_buying_representable_power_with_unbounded_complexity_is_caught() -> None:
    """Exact power at any price is not a bargain.

    Unconstrained, the search will take a lattice of ten states for a 22 kWh pack:
    peak power exact, state of charge resolved to 2.4 kWh, and every energy and
    reserve figure ruined. The band and the state budget are what stop it, and
    this is the mutation they stop.
    """
    limits = reference_table().limits
    floor = limits.energy_for_soc(FLOOR_PERCENT)
    base = build_physics_table(
        limits, floor_energy_kwh=floor, bucket_kwh=ECONOMIC_BUCKET_KWH
    )

    # k = 1: one bucket per maximum-power quarter. Power becomes exact.
    reckless = 10.0 * INTERVAL_HOURS * limits.charge_efficiency
    mutated = build_physics_table(limits, floor_energy_kwh=floor, bucket_kwh=reckless)
    assert mutated.max_representable_charge_kw == pytest.approx(10.0, abs=5e-5)
    assert mutated.buckets < 15

    low, high = ECONOMIC_BUCKET_BAND_KWH
    assert not low <= reckless <= high
    chosen, _rule = select_bucket_kwh(limits, floor_energy_kwh=floor)
    assert low <= chosen <= high
    real = build_physics_table(limits, floor_energy_kwh=floor, bucket_kwh=chosen)
    assert real.buckets <= int(base.buckets * (1.0 + ECONOMIC_BUCKET_STATE_BUDGET)) + 1


def test_reporting_only_the_larger_directional_power_is_caught() -> None:
    """beta.16's single figure, which hid an asymmetry reaching thirty per cent.

    The mutation is publishing ``max(charge, discharge)`` alone. On a 15 kWh /
    7.5 kW pack that reads 7.4620 kW -- half a per cent short of nameplate, and
    entirely reassuring -- while the discharge side reaches only 6.5666 kW.
    """
    limits, why = build_limits(
        capacity_kwh=15.0,
        max_charge_kw=7.5,
        max_discharge_kw=7.5,
        round_trip_efficiency_percent=88.0,
    )
    assert limits is not None, why
    floor = limits.energy_for_soc(FLOOR_PERCENT)
    table = build_physics_table(
        limits, floor_energy_kwh=floor, bucket_kwh=ECONOMIC_BUCKET_KWH
    )

    headline = table.max_representable_power_kw
    assert headline == pytest.approx(7.4620, abs=5e-4)
    assert (7.5 - headline) / 7.5 < 0.01
    # And the figure it conceals.
    assert (7.5 - table.max_representable_discharge_kw) / 7.5 > 0.12


def test_the_payload_says_the_terminal_figures_are_gone_not_free() -> None:
    """A reader of the download must be able to tell absent from zero.

    beta.16 published a whole-horizon plan difference that read as realised money;
    beta.17 renamed and qualified it; beta.18 removed the constraint it priced. The
    payload has to say which of those it is doing.
    """
    table = reference_table()
    horizon, hold_end = two_day_case(table)
    outcome = outcome_for(
        table, horizon, start_kwh=17.42, terminal_kwh=hold_end, gain=0.10
    )
    payload = economic_as_dict(
        outcome, execution_blocked_reason="execution_unavailable"
    )["terminal"]

    assert payload["plan_cost_eur"] is None
    assert payload["first_run_changed"] is None
    assert "no longer exists" in payload["plan_cost_rule"]
    assert "ratcheted" in payload["plan_cost_rule"]


def test_an_activity_sentence_that_multiplies_out_wrongly_is_caught() -> None:
    """First-interval battery power beside whole-run grid energy was the bug.

    The live beta.16 line read ``0.95 kW, 0.27 kWh``: a battery power at one
    boundary and a meter energy at another, in one sentence, with nothing saying
    so. A reader who multiplied got nonsense and a reader who did not still could
    not tell which quantity was which.

    **beta.31 removes the class of fault rather than the instance.** Activity
    carries one energy and no power at all, so there is no pair of figures at
    different boundaries left to put side by side -- and the mutation is not
    "quote the wrong pair" but "give this surface the figures to quote", which the
    dataclass refuses.
    """
    from custom_components.alpha_ems_manager.activity import ExecutionView

    fields = set(RunContent.__dataclass_fields__)
    assert fields == {"category", "energy_kwh", "end_utc", "window", "executable"}
    # One energy, so the ambiguity is not expressible.
    assert [f for f in fields if "energy" in f] == ["energy_kwh"]
    for forbidden in (
        "power_kw",
        "average_power_kw",
        "peak_power_kw",
        "battery_energy_kwh",
        "price_eur_kwh",
        "value_eur",
        "charge_source",
    ):
        assert forbidden not in fields, forbidden
        assert forbidden not in ExecutionView.__dataclass_fields__, forbidden


def test_inventing_a_price_for_an_absent_tomorrow_is_caught() -> None:
    """A horizon that cannot see tomorrow must plan on today, not on a guess.

    The mutation is extending the price series past what the source published --
    with the last known price, an average, anything. The guard is structural:
    ``build_horizon`` prices only the intervals it is given, so an unpriced
    interval is *excluded* rather than filled, and the horizon reports how far it
    actually reaches.
    """
    table = reference_table()
    horizon = horizon_for(
        table,
        demands=flat_demands(48),
        prices=[
            IntervalPrice(import_eur_kwh=0.20, export_eur_kwh=0.10)
            if index < 24
            else IntervalPrice(import_eur_kwh=None, export_eur_kwh=None)
            for index in range(48)
        ],
    )

    # Half the day is unpriced, so half the day is not planned. A mutation that
    # invented a price would give 48.
    assert horizon.intervals == 24
    assert horizon.limited_by is not None


# ===========================================================================
# J. beta.18: the margin, the realised layer, and the execution contract
# ===========================================================================


def _margin_plan(
    *, spread, margin, gain=0.10, production=0.0, reserve=None, start=None, load=0.10
):
    """Return an arbitrage plan under a per-kWh grid-charge margin."""
    table = reference_table()
    floor = table.limits.energy_for_soc(FLOOR_PERCENT)
    count = 12
    horizon = horizon_for(
        table,
        demands=[
            IntervalDemand(
                index=index,
                baseline_kwh=load,
                pv_kwh=production if index < 6 else 0.0,
            )
            for index in range(count)
        ],
        prices=[
            IntervalPrice(
                import_eur_kwh=0.20 if index < 6 else 0.20 + spread,
                export_eur_kwh=(0.20 if index < 6 else 0.20 + spread) * 0.9,
            )
            for index in range(count)
        ],
        reserve_kwh=[floor if reserve is None else reserve] * count,
    )
    return economic_solve(
        table=table,
        horizon=horizon,
        start_energy_kwh=floor if start is None else start,
        terminal_floor_kwh=floor,
        minimum_trade_gain_eur=gain,
        permitted=EVERYTHING,
        grid_charge_margin_eur_per_kwh=margin,
    )


def test_charging_the_margin_once_per_run_instead_of_per_kwh_is_caught() -> None:
    """The mutation is the bug the margin exists to fix, reintroduced.

    A fixed amount per run does not scale with volume: it is cleared once and then
    an arbitrary quantity rides through. The released margin scales, so the same
    thin spread that survives a fixed charge dies under a per-kWh one.
    """
    thin = _margin_plan(spread=0.10, margin=0.0, gain=0.10)
    per_kwh = _margin_plan(spread=0.10, margin=0.10, gain=0.10)
    # A fixed charge of the same size, expressed the only way the DP can express
    # one: as a larger per-run gain.
    per_run = _margin_plan(spread=0.10, margin=0.0, gain=1.50)

    assert thin.marginal_grid_charge_kwh > 10.0
    assert per_kwh.marginal_grid_charge_kwh < 1.0
    # The per-run form cannot reproduce it: a fixed 1.50 still lets volume ride
    # through once cleared, or blocks small honest trades. Either way it is not
    # the same instrument.
    assert (
        per_run.marginal_grid_charge_kwh
        != pytest.approx(per_kwh.marginal_grid_charge_kwh, abs=0.5)
        or per_run.marginal_grid_charge_kwh < 1.0
    )


def test_charging_the_margin_on_total_battery_charge_is_caught() -> None:
    """The basis is marginal grid import, not battery movement.

    On a mixed quarter the two differ by the production term. A margin on total
    charge would tax the sun -- and the reported basis proves it does not.
    """
    plan = _margin_plan(spread=0.40, margin=0.02, production=0.8)

    charged = sum(entry.battery_charge_ac_kwh for entry in plan.intervals[:6])
    assert charged > 0.0
    # Strictly less: the mutation would make these equal.
    assert plan.marginal_grid_charge_kwh < charged
    assert plan.grid_charge_margin_eur == pytest.approx(
        0.02 * plan.marginal_grid_charge_kwh, abs=1e-9
    )


def test_charging_the_margin_on_ambient_absorption_is_caught() -> None:
    """Absorption causes no marginal import, so it must cost nothing.

    An absurd margin with plenty of production: the battery still fills, and the
    basis stays at zero. The mutation -- charging every charged kilowatt-hour --
    would empty the plan of free energy.
    """
    plan = _margin_plan(spread=0.10, margin=5.0, production=2.0)

    absorbed = sum(entry.battery_charge_ac_kwh for entry in plan.intervals[:6])
    assert absorbed > 1.0
    assert plan.marginal_grid_charge_kwh < 0.3


def test_a_margin_that_blocks_the_reserve_is_caught() -> None:
    """**The safety mutation.** No cost may outrank keeping the house supplied.

    Lexicographic ordering is what guarantees it, so this holds at any margin --
    including one four orders of magnitude beyond anything sane.
    """
    for margin in (1.0, 99.0, 10_000.0):
        plan = _margin_plan(spread=0.0, margin=margin, reserve=15.5, start=5.0)
        peak = max(
            entry.start_energy_dc_kwh + entry.battery_delta_dc_kwh
            for entry in plan.intervals
        )
        assert plan.available, margin
        assert peak == pytest.approx(15.5, abs=1e-9), margin


def test_a_margin_that_taxes_load_avoidance_is_caught() -> None:
    """A discharge is not charging, so supplying the house is never charged."""
    plan = _margin_plan(spread=0.0, margin=99.0, load=1.0, start=11.0)

    discharged = sum(entry.battery_discharge_ac_kwh for entry in plan.intervals)
    assert discharged > 1.0
    assert plan.grid_charge_margin_eur == pytest.approx(0.0, abs=1e-9)


def test_folding_the_margin_into_the_reported_cost_is_caught() -> None:
    """``cost_eur`` must stay reconcilable to grid energy at the interval's prices.

    The margin is notional -- nobody pays it. Folding it in would break the
    invariant that every euro in the payload can be checked against the flows
    printed beside it, which is the same reason the switching cost is kept out.
    """
    plan = _margin_plan(spread=0.40, margin=0.05)

    assert plan.grid_charge_margin_eur > 0.0
    for entry in plan.intervals:
        expected = entry.grid_import_kwh * (
            entry.import_price_eur_kwh or 0.0
        ) - entry.grid_export_kwh * (entry.export_price_eur_kwh or 0.0)
        assert entry.cost_eur == pytest.approx(expected, abs=1e-9)


def test_inverting_the_margin_boundary_is_caught() -> None:
    """Above the threshold must refuse and below must allow, not the other way."""
    below = _margin_plan(spread=0.10, margin=0.02)
    above = _margin_plan(spread=0.10, margin=0.10)

    assert below.marginal_grid_charge_kwh > above.marginal_grid_charge_kwh


def test_a_realised_figure_reaching_the_objective_is_caught() -> None:
    """**The sunk-cost mutation.** A cost basis must never enter the search.

    Structural: no module that decides anything may import the realised layer, and
    inside the optimizer only the payload builder may even name it. A rule like
    "never sell below what this cost" would forbid correct decisions -- selling at
    0.18 energy that cost 0.20 is right when it makes room for something cheaper.
    """
    package = pathlib.Path(economic_module.__file__).parent
    for name in ("economic.py", "reserve.py", "policy.py", "safety.py", "battery.py"):
        tree = ast.parse((package / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "realized" not in node.module, name
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert "realized" not in alias.name, name

    touching = set()
    tree = ast.parse((package / "economic.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            names |= {a.arg for a in ast.walk(node) if isinstance(a, ast.arg)}
            if any("realized" in name for name in names):
                touching.add(node.name)
    assert touching == {"economic_as_dict"}, touching


def test_keying_the_execution_target_on_a_horizon_index_is_caught() -> None:
    """The beta.16 Activity defect, refused in the new contract.

    An index moves every quarter as the horizon advances. Two targets for the same
    run at different horizon positions must share an identifier; the mutation --
    hashing the index -- would give them different ones and make a future Stage B
    abandon its own dispatch every fifteen minutes.
    """
    from custom_components.alpha_ems_manager.economic import execution_target

    opens = datetime(2026, 8, 22, 18, 30, tzinfo=UTC)
    closes = opens + timedelta(minutes=60)
    early = _target_run(start_index=42, charge=6.0)
    late = _target_run(start_index=41, charge=4.0)

    first = execution_target(
        early,
        window_start=opens,
        window_end=closes,
        reserve_floor_kwh=4.4,
        issued_at=opens,
        stale_after=closes,
    )
    second = execution_target(
        late,
        window_start=opens,
        window_end=closes,
        reserve_floor_kwh=4.4,
        issued_at=opens,
        stale_after=closes,
    )

    assert first["plan_id"] == second["plan_id"]
    assert "42" not in first["plan_id"]


def test_revision_churn_on_insignificant_noise_is_caught() -> None:
    """A revision that moved on floating-point drift would cause control jitter."""
    from custom_components.alpha_ems_manager.economic import (
        execution_revision,
        execution_target,
    )

    opens = datetime(2026, 8, 22, 18, 30, tzinfo=UTC)
    closes = opens + timedelta(minutes=60)
    base = execution_target(
        _target_run(start_index=1, charge=6.0),
        window_start=opens,
        window_end=closes,
        reserve_floor_kwh=4.4,
        issued_at=opens,
        stale_after=closes,
    )
    base["revision"] = 5
    nudged = execution_target(
        _target_run(start_index=1, charge=6.0 + 1e-9),
        window_start=opens,
        window_end=closes,
        reserve_floor_kwh=4.4,
        issued_at=opens,
        stale_after=closes,
    )

    assert execution_revision(base, nudged) == 5


def test_using_the_grid_target_as_a_battery_setpoint_is_caught() -> None:
    """**The boundary mutation, and the most dangerous one here.**

    On the live installation 1.3 kW of net export needed 2.2 kW of battery. A
    Stage B that took the grid figure as a battery command would deliver 0.4 kW.
    The contract keeps them in separate, separately named fields, and the grid
    field is absent entirely unless the meter is what the plan aims at.
    """
    from custom_components.alpha_ems_manager.economic import execution_target

    opens = datetime(2026, 8, 22, 18, 30, tzinfo=UTC)
    exporting = execution_target(
        _target_run(
            start_index=1, discharge=2.2, grid_export=1.3, action=ECONOMIC_ACTION_EXPORT
        ),
        window_start=opens,
        window_end=opens + timedelta(minutes=60),
        reserve_floor_kwh=4.4,
        issued_at=opens,
        stale_after=opens,
    )
    charging = execution_target(
        _target_run(start_index=1, charge=5.0, grid_import=4.2),
        window_start=opens,
        window_end=opens + timedelta(minutes=60),
        reserve_floor_kwh=4.4,
        issued_at=opens,
        stale_after=opens,
    )

    assert exporting["battery_target_kwh"] == pytest.approx(2.2)
    assert exporting["grid_target_kwh"] == pytest.approx(1.3)
    assert exporting["battery_target_kwh"] != pytest.approx(
        exporting["grid_target_kwh"]
    )
    # A charge has no meter-side target at all, so it cannot be confused for one.
    assert charging["grid_target_kwh"] is None
    assert charging["battery_target_kwh"] == pytest.approx(5.0)


def test_a_missing_staleness_stamp_is_caught() -> None:
    """Every target carries the instant beyond which it must not be trusted."""
    from custom_components.alpha_ems_manager.economic import execution_target

    opens = datetime(2026, 8, 22, 18, 30, tzinfo=UTC)
    target = execution_target(
        _target_run(start_index=1, charge=1.0),
        window_start=opens,
        window_end=opens + timedelta(minutes=60),
        reserve_floor_kwh=4.4,
        issued_at=opens,
        stale_after=opens + timedelta(minutes=30),
    )

    assert target["stale_after"]
    assert target["stale_after"] > target["window_start"]


def _target_run(
    *,
    start_index,
    charge=0.0,
    discharge=0.0,
    grid_import=0.0,
    grid_export=0.0,
    action=ECONOMIC_ACTION_CHARGE,
):
    """Return one run for the execution-contract mutations."""
    from custom_components.alpha_ems_manager.economic import EconomicRun

    return EconomicRun(
        action=action,
        start_index=start_index,
        end_index=start_index + 3,
        interval_count=4,
        battery_charge_ac_kwh=charge,
        battery_discharge_ac_kwh=discharge,
        grid_import_kwh=grid_import,
        grid_export_kwh=grid_export,
        pv_curtailed_kwh=0.0,
        first_power_kw=(charge + discharge) * 4.0,
        net_cash_flow_eur=0.0,
        min_price_eur_kwh=0.1,
        max_price_eur_kwh=0.4,
        average_price_eur_kwh=0.25,
        marginal_grid_import_kwh=grid_import,
        marginal_grid_export_kwh=grid_export,
        marginal_cost_eur=-1.0,
        direction="charge" if charge else "discharge",
        charged_switching_fee=True,
    )


# --- K. beta.18: the reserve is the only physical floor ----------------------


def _cb_world(pv_total=30.0, load=0.15, tail=1.20, days=2):
    """Return a shaped world: production tomorrow, dear quarters at the end today."""
    production, price, demand = [], [], []
    for index in range(96 * days):
        quarter = index % 96
        day = index // 96
        arc = 0.0
        if 32 <= quarter < 80 and pv_total > 0.0:
            arc = max(0.0, pv_total * math.sin(math.pi * (quarter - 32) / 48) / 30.55)
        production.append(arc)
        demand.append(load)
        value = 0.10 if quarter < 24 else (0.40 if 78 <= quarter < 84 else 0.22)
        if day == 0 and quarter >= 92:
            value = tail
        price.append(value)
    return production, price, demand


def _cb_horizon(step=80, priced_end=96, flatten_reserve=False, **kw):
    """Return a horizon whose reserve outlives its prices, as production does.

    The physical forecast runs a further civil day past the last priced interval,
    because that is how much forecast exists: production covers today and
    tomorrow, and the learned load baseline is defined for any interval. Every
    mutation below depends on that asymmetry being present, since it is what makes
    the reserve substantial exactly where the prices stop.
    """
    table = reference_table()
    floor = table.limits.energy_for_soc(FLOOR_PERCENT)
    production, price, demand = _cb_world(**kw)
    window = range(step + 1, min(len(price), priced_end + 96))
    demands = tuple(
        IntervalDemand(
            index=i - (step + 1), baseline_kwh=demand[i], pv_kwh=production[i]
        )
        for i in window
    )
    if flatten_reserve:
        required = tuple(floor for _ in demands)
    else:
        projection = build_reserve(
            limits=table.limits, floor_energy_kwh=floor, demands=demands
        )
        required = tuple(entry.required_dc_kwh for entry in projection.intervals)
    prices = tuple(
        IntervalPrice(
            import_eur_kwh=price[i] if i < priced_end else None,
            export_eur_kwh=price[i] * 0.55 if i < priced_end else None,
        )
        for i in window
    )
    return build_horizon(
        demands=demands, prices=prices, required_reserve_kwh=required, table=table
    )


def _cb_solve(horizon, terminal, start=19.5):
    """Solve one case, varying only the terminal floor the caller asks for."""
    return economic_solve(
        table=reference_table(),
        horizon=horizon,
        start_energy_kwh=start,
        terminal_floor_kwh=terminal,
        minimum_trade_gain_eur=0.10,
        permitted=EVERYTHING,
    )


def test_restoring_the_hold_end_terminal_floor_is_caught() -> None:
    """**The headline mutation.** Asking for the idle endpoint restores the defect.

    The solver still honours whatever terminal floor it is handed -- the internal
    clamp is a reachability guard, not a policy -- so the removed defect is one
    caller change away. Handed the idle endpoint it holds everything it has;
    handed the configured floor it spends down to the reserve's requirement.
    """
    table = reference_table()
    floor = table.limits.energy_for_soc(FLOOR_PERCENT)
    horizon = _cb_horizon()

    mutated = _cb_solve(horizon, table.limits.energy_for_soc(100.0) * 2.0)
    released = _cb_solve(horizon, floor)

    # The idle walk across an evening with no surplus is flat, so the floor is
    # the starting charge and the plan ends exactly where it began.
    assert mutated.terminal_floor_kwh == pytest.approx(19.5, abs=0.4)
    assert mutated.end_energy_dc_kwh == pytest.approx(19.5, abs=0.4)
    assert released.terminal_floor_kwh <= floor + 1e-9
    assert released.end_energy_dc_kwh < mutated.end_energy_dc_kwh - 5.0


def test_using_the_current_charge_as_the_terminal_floor_is_caught() -> None:
    """The same defect stated plainly, since a flat walk makes the two identical."""
    table = reference_table()
    horizon = _cb_horizon()

    as_current = _cb_solve(horizon, 19.5)
    released = _cb_solve(horizon, table.limits.energy_for_soc(FLOOR_PERCENT))

    assert as_current.end_energy_dc_kwh > released.end_energy_dc_kwh + 5.0


def test_adding_the_boundary_reserve_as_a_second_floor_is_caught() -> None:
    """Redundant, and provably so: the reserve already binds at that interval.

    A boundary bridge was a serious candidate. It turns out bit-identical to
    having no terminal floor at all, so adding it would be a second constraint
    expressing something already enforced pointwise -- and a second place to have
    to keep correct.
    """
    table = reference_table()
    horizon = _cb_horizon()

    released = _cb_solve(horizon, table.limits.energy_for_soc(FLOOR_PERCENT))
    bridged = _cb_solve(horizon, horizon.planning_reserve_kwh[-1])

    assert bridged.cost_eur == pytest.approx(released.cost_eur, abs=1e-9)
    assert bridged.end_energy_dc_kwh == pytest.approx(released.end_energy_dc_kwh)
    assert bridged.violation_kwh == pytest.approx(released.violation_kwh)


def test_flattening_the_reserve_to_the_configured_floor_is_caught() -> None:
    """**The safety mutation.** With no terminal floor the reserve is the only guard.

    This is also the harness mistake that made an earlier investigation reach the
    wrong conclusion: passing a constant reserve invents dumping that the real
    pointwise profile prevents, and so appears to justify a terminal floor.
    """
    table = reference_table()
    floor = table.limits.energy_for_soc(FLOOR_PERCENT)
    real = _cb_horizon()
    flattened = _cb_horizon(flatten_reserve=True)

    guarded = _cb_solve(real, floor)
    unguarded = _cb_solve(flattened, floor)

    assert real.planning_reserve_kwh[-1] > floor + 3.0
    assert guarded.end_energy_dc_kwh >= real.planning_reserve_kwh[-1] - 1e-6
    assert unguarded.end_energy_dc_kwh < guarded.end_energy_dc_kwh - 3.0


def test_spending_below_the_requirement_at_any_interval_is_caught() -> None:
    """The requirement holds at every interval, not merely at the horizon's end."""
    table = reference_table()
    horizon = _cb_horizon()
    plan = _cb_solve(horizon, table.limits.energy_for_soc(FLOOR_PERCENT))

    assert plan.violation_kwh == pytest.approx(0.0)
    for index, entry in enumerate(plan.intervals):
        landed = entry.start_energy_dc_kwh + entry.battery_delta_dc_kwh
        assert landed >= horizon.planning_reserve_kwh[index] - 1e-6


def test_a_terminal_floor_that_ratchets_across_refreshes_is_caught() -> None:
    """Under rolling execution the floor must be one number, refresh after refresh.

    The mutation is the released beta.17 caller, and this is the mechanism that
    made it worse over time rather than merely suboptimal once: the floor is
    recomputed from the *current* charge, so a charge raises it, the next refresh
    inherits the raised value, and the pack is locked out of late value for good.
    Measured here it climbs; the configured floor does not move.
    """
    table = reference_table()
    floor = table.limits.energy_for_soc(FLOOR_PERCENT)
    ceiling = table.limits.energy_for_soc(100.0)

    def walk(*, hold_end):
        charge, floors = 19.5, []
        for offset in range(12):
            horizon = _cb_horizon(step=80 + offset)
            if not horizon.intervals:
                break
            plan = _cb_solve(
                horizon, ceiling * 2.0 if hold_end else floor, start=charge
            )
            if not plan.available or not plan.intervals:
                break
            floors.append(round(plan.terminal_floor_kwh, 2))
            entry = plan.intervals[0]
            charge = min(
                ceiling,
                max(floor, entry.start_energy_dc_kwh + entry.battery_delta_dc_kwh),
            )
        return floors

    ratcheting = walk(hold_end=True)
    fixed = walk(hold_end=False)

    assert len(ratcheting) > 4
    assert len(set(ratcheting)) > 1
    assert max(ratcheting) > min(ratcheting) + 1.0
    assert ratcheting == sorted(ratcheting)
    assert len(set(fixed)) == 1


def test_fabricating_a_false_first_run_comparison_is_caught() -> None:
    """``False`` claims the bound left the executing run alone. Nothing compared it.

    The tempting shape is ``return False`` for "no difference found". There was no
    comparison, so there is no difference to have found, and a boolean here would
    be read by the dashboard as a measurement.
    """
    table = reference_table()
    outcome = outcome_for(
        table,
        _cb_horizon(),
        start_kwh=19.5,
        terminal_kwh=table.limits.energy_for_soc(FLOOR_PERCENT),
        gain=0.10,
    )

    assert outcome.unbounded is None
    assert outcome.terminal_first_run_changed is None
    assert outcome.terminal_first_run_changed is not False
    # And the run it would have compared does exist, so the absence is about the
    # missing comparison rather than a missing plan.
    assert outcome.desired.available
    assert outcome.desired.intervals


def test_running_a_fourth_solve_to_price_nothing_is_caught() -> None:
    """The comparison solve that priced nothing is still gone. A fourth now exists
    for a different reason, and it is bounded.

    beta.18 removed a fourth solve because its difference from the third was
    *identically zero by construction* -- it re-solved the same problem and
    published a zero as though a constraint cost nothing. That guard stands, and
    this test still enforces it: nothing here may re-solve an identical problem.

    beta.31 adds a fourth that solves a **different** problem: the same inputs
    under the previous architecture, so a change of economics can be watched
    against live data before it is trusted with money. Three things keep it from
    becoming the fault beta.18 removed -- it is gated behind ``compare_legacy``, it
    is requested in Shadow alone, and it is documented as temporary.
    """
    source = pathlib.Path(economic_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
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

    # Three unconditional, plus one that only runs when a caller asks for the
    # architecture comparison. Asserted as an exact count so a fifth is a visible
    # decision rather than a quiet cost.
    assert calls == 4

    # And the fourth is genuinely conditional: it cannot run unless asked.
    guarded = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If) and "compare_legacy" in ast.dump(node.test)
    ]
    assert guarded, "the legacy comparison solve must be behind compare_legacy"
