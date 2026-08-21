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

import math

import pytest

from custom_components.alpha_ems_manager.const import (
    ECONOMIC_ACTION_CHARGE,
    ECONOMIC_ACTION_DISCHARGE,
    ECONOMIC_ACTION_EXPORT,
    ECONOMIC_ACTION_HOLD,
    ECONOMIC_GAP_NO_PRIMITIVE,
    ECONOMIC_GAP_NONE,
)
from custom_components.alpha_ems_manager.economic import (
    IntervalPrice,
    build_horizon,
    fingerprint_economic,
    hold_cost,
)
from custom_components.alpha_ems_manager.simulation import IntervalDemand

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
    assert hold_cost(horizon=horizon) < hold_cost(horizon=eight_interval_horizon(table))


# --- hygiene ----------------------------------------------------------------


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
