"""What the optimizer decides, and the arithmetic that proves it decided right.

Every case here is **synthetic and says so**. The live installation supplied the
battery configuration and nothing else: it cannot supply a negative export price,
a horizon that stops in a hole, a reserve above the pack or a winter evening in
August. Those are constructed, and none of them is claimed as live-verified.

Three things dominate this file.

**The euros are recomputed by hand.** A plan that costs the right amount for the
wrong reason is the failure mode a total-only assertion cannot see, so the
load-bearing tests state the grid energy per interval, multiply it by the price
themselves, and assert the sum at exact values. The eight-interval fixture is
sized so that arithmetic is doable on paper.

**The boundary is asserted, not assumed.** Six energies per interval -- one DC,
two battery AC, two grid AC and one curtailment -- and a euro figure is only
meaningful against the one it was measured at. There is a structural test that no
price is ever multiplied by a DC or battery-side quantity, and a
round-trip-efficiency test that would catch a boundary slip even if every total
happened to look plausible.

**The safety ordering is proved to be lexicographic, not weighted.** A violation
must lose to nothing, at any price, and the way to show that is to make the price
of avoiding it absurd and check it is still avoided.

Arithmetic is asserted at exact values wherever the arithmetic is exact.
"""

from __future__ import annotations

import math
import time
from collections.abc import Sequence

import pytest

from custom_components.alpha_ems_manager.battery import (
    INTERVAL_HOURS,
    BatteryLimits,
    build_limits,
)
from custom_components.alpha_ems_manager.const import (
    ECONOMIC_ACTION_CHARGE,
    ECONOMIC_ACTION_CURTAIL,
    ECONOMIC_ACTION_DISCHARGE,
    ECONOMIC_ACTION_EXPORT,
    ECONOMIC_ACTION_HOLD,
    ECONOMIC_ACTION_SAFETY_BUY,
    ECONOMIC_BUCKET_KWH,
    ECONOMIC_UNAVAILABLE_HORIZON_EMPTY,
)
from custom_components.alpha_ems_manager.economic import (
    EconomicHorizon,
    EconomicPlan,
    IntervalPrice,
    PhysicsTable,
    build_horizon,
    build_physics_table,
    hold_cost,
    solve,
)
from custom_components.alpha_ems_manager.simulation import IntervalDemand

#: The reference installation, as read from live diagnostics on 2026-08-21:
#: 22 kWh DC usable, a 20 % floor, 10 kW each way, 90 % round trip.
REFERENCE = {
    "capacity_kwh": 22.0,
    "max_charge_kw": 10.0,
    "max_discharge_kw": 10.0,
    "round_trip_efficiency_percent": 90.0,
}
FLOOR_PERCENT = 20.0
#: One boundary crossing at the reference efficiency. The clamp applies the
#: square root once per direction, which is what makes a round trip cost 10 %.
ETA = math.sqrt(0.9)

#: Every action the physics allows. Handed to the desired solve in tests that are
#: about the model rather than about what an actuator exists for.
EVERYTHING = frozenset(
    {
        ECONOMIC_ACTION_CHARGE,
        ECONOMIC_ACTION_DISCHARGE,
        ECONOMIC_ACTION_EXPORT,
        ECONOMIC_ACTION_CURTAIL,
    }
)
#: What this release can actually command: a discharge into the house, and
#: declining production. Neither buys nor sells.
IMPLEMENTED = frozenset({ECONOMIC_ACTION_DISCHARGE, ECONOMIC_ACTION_CURTAIL})


def reference_limits(**overrides) -> BatteryLimits:
    """Return the live installation's limits, asserting they were accepted."""
    limits, reason = build_limits(**{**REFERENCE, **overrides})
    assert limits is not None, reason
    return limits


def reference_table(**overrides) -> PhysicsTable:
    """Return the precomputed transitions for the reference installation."""
    limits = reference_limits(**overrides)
    table = build_physics_table(
        limits, floor_energy_kwh=limits.energy_for_soc(FLOOR_PERCENT)
    )
    assert table is not None
    return table


def flat_demands(count: int, *, load_kwh: float = 0.25, pv_kwh: float = 0.0):
    """Return ``count`` intervals of identical load and production."""
    return [
        IntervalDemand(index=index, baseline_kwh=load_kwh, pv_kwh=pv_kwh)
        for index in range(count)
    ]


def two_tier_prices(
    count: int,
    *,
    cheap_until: int,
    cheap: tuple[float, float] = (0.10, 0.08),
    dear: tuple[float, float] = (0.40, 0.35),
):
    """Return a cheap block followed by an expensive one."""
    return [
        IntervalPrice(
            import_eur_kwh=(cheap if index < cheap_until else dear)[0],
            export_eur_kwh=(cheap if index < cheap_until else dear)[1],
        )
        for index in range(count)
    ]


def horizon_for(
    table: PhysicsTable,
    *,
    demands: Sequence[IntervalDemand],
    prices: Sequence[IntervalPrice],
    reserve_kwh: Sequence[float | None] | None = None,
) -> EconomicHorizon:
    """Return the horizon, defaulting the reserve to the configured floor."""
    floor = table.limits.energy_for_soc(FLOOR_PERCENT)
    return build_horizon(
        demands=demands,
        prices=prices,
        required_reserve_kwh=(
            [floor] * len(demands) if reserve_kwh is None else reserve_kwh
        ),
        table=table,
    )


def solved(
    table: PhysicsTable,
    horizon: EconomicHorizon,
    *,
    start_kwh: float,
    terminal_kwh: float | None = None,
    gain: float = 0.0,
    permitted: frozenset[str] = EVERYTHING,
) -> EconomicPlan:
    """Return one solve, defaulting the terminal floor to the start energy."""
    return solve(
        table=table,
        horizon=horizon,
        start_energy_kwh=start_kwh,
        terminal_floor_kwh=start_kwh if terminal_kwh is None else terminal_kwh,
        minimum_trade_gain_eur=gain,
        permitted=permitted,
    )


#: The eight-interval fixture every hand-computed euro figure below rests on.
#:
#: One kilowatt of house load throughout, no production, four cheap intervals then
#: four expensive ones. Eight intervals rather than ninety-six because the whole
#: point is that the arithmetic can be done on paper and checked.
EIGHT = 8
CHEAP_IMPORT, CHEAP_EXPORT = 0.10, 0.08
DEAR_IMPORT, DEAR_EXPORT = 0.40, 0.35
#: Mid-pack, and deliberately not on a bucket boundary's edge: 11.0 kWh is 44
#: buckets exactly, so the start does not itself exercise the snapping rule.
START_KWH = 11.0


def eight_interval_horizon(table: PhysicsTable) -> EconomicHorizon:
    """Return the fixture horizon: four cheap intervals, then four expensive."""
    return horizon_for(
        table,
        demands=flat_demands(EIGHT),
        prices=two_tier_prices(EIGHT, cheap_until=4),
    )


# -- A. the physics table ----------------------------------------------------


def test_the_table_is_built_by_asking_the_clamp_not_by_reading_the_limits() -> None:
    """The measured conversion ratios match the clamp to floating-point noise.

    Load-bearing: reading ``round_trip_efficiency_percent`` off the limits and
    taking its square root would agree here by luck, and would stop agreeing the
    moment the clamp gained a second consideration. Measuring means the model
    cannot drift away from the simulator without this failing.
    """
    table = reference_table()

    assert table.charge_dc_per_ac == pytest.approx(ETA, abs=1e-14)
    assert table.discharge_dc_per_ac == pytest.approx(1.0 / ETA, abs=1e-14)


def test_the_grid_spans_the_pack_and_the_top_state_is_the_ceiling_exactly() -> None:
    """88 buckets of 0.25 kWh, and the last one is 22.0 rather than 22.25."""
    table = reference_table()

    assert table.bucket_kwh == ECONOMIC_BUCKET_KWH == 0.25
    assert table.buckets == 88
    assert table.ceiling_kwh == 22.0
    assert table.energy(88) == 22.0
    assert table.energy(89) == 22.0
    assert table.energy(17) == 4.25


def test_a_measured_state_of_charge_snaps_down_never_up() -> None:
    """18.656 kWh becomes 18.50, not 18.75.

    Down, because the model must never assume the pack holds energy it might not.
    Up would make a reserve look satisfied by rounding.
    """
    table = reference_table()

    assert table.bucket_at_or_below(18.656) == 74
    assert table.energy(74) == 18.5
    assert table.bucket_at_or_below(18.5) == 74
    assert table.bucket_at_or_below(0.0) == 0


def test_the_power_limits_bound_the_reachable_deltas_in_both_directions() -> None:
    """A 10 kW inverter moves at most 9 buckets up and 10 down per interval.

    Asymmetric, and correctly so: 2.5 kWh of AC charge is 2.372 kWh DC (nine and a
    half buckets, so nine) while 2.5 kWh of AC discharge draws 2.635 kWh DC (ten
    and a half, so ten). The asymmetry *is* the round-trip loss.
    """
    table = reference_table()
    deltas = sorted({move.target - 50 for move in table.moves[50]})

    assert deltas == list(range(-10, 10))
    assert pytest.approx(9.4868, abs=1e-4) == 2.5 * ETA / 0.25
    assert pytest.approx(10.5409, abs=1e-4) == 2.5 / ETA / 0.25


def test_the_configured_floor_makes_lower_buckets_unreachable_by_discharge() -> None:
    """A 20 % floor is 4.4 kWh, and nothing above it may discharge below it.

    The floor is emphatically **not** quantised -- doing so would move the user's
    own setting. It is enforced by the clamp instead, which is why the reachable
    set stops where it does rather than at a bucket boundary: 4.5 kWh can shed
    0.10 kWh and no more, so the move to 4.25 does not exist.

    Buckets *below* the floor do exist, and must: a measured state of charge can
    legitimately be under the user's setting, and a state space that could not
    represent that would have nowhere to start from. What the floor forbids is
    crossing it, not being beneath it.
    """
    table = reference_table()
    floor = table.limits.energy_for_soc(FLOOR_PERCENT)

    assert floor == 4.4
    for source in range(table.buckets + 1):
        if table.energy(source) < floor:
            continue
        for move in table.moves[source]:
            assert table.energy(move.target) >= floor - 1e-9, (source, move)

    # The boundary case, stated explicitly rather than left to the sweep.
    assert table.energy(18) == 4.5
    assert 17 not in {move.target for move in table.moves[18]}
    assert 18 in {move.target for move in table.moves[18]}


def test_a_zero_or_negative_bucket_is_refused_rather_than_guessed() -> None:
    """An impossible discretisation returns ``None``, never a one-state table."""
    limits = reference_limits()
    floor = limits.energy_for_soc(FLOOR_PERCENT)

    assert build_physics_table(limits, floor_energy_kwh=floor, bucket_kwh=0.0) is None
    assert build_physics_table(limits, floor_energy_kwh=floor, bucket_kwh=-1.0) is None


# -- B. reserve quantisation -------------------------------------------------


def test_the_reserve_requirement_quantises_up_to_a_bucket() -> None:
    """4.41 kWh becomes 4.50, never 4.25.

    Up, so the worst error is protecting one bucket too much. Down would let a
    0.24 kWh shortfall on a 0.25 kWh grid become invisible, which is the one
    direction that trades safety for tidiness.
    """
    table = reference_table()
    horizon = horizon_for(
        table,
        demands=flat_demands(2),
        prices=two_tier_prices(2, cheap_until=2),
        reserve_kwh=[4.41, 4.26],
    )

    assert horizon.planning_reserve_kwh == (4.5, 4.5)


def test_a_requirement_already_on_a_boundary_is_left_alone() -> None:
    """Quantisation is idempotent: 4.50 stays 4.50."""
    table = reference_table()
    horizon = horizon_for(
        table,
        demands=flat_demands(1),
        prices=two_tier_prices(1, cheap_until=1),
        reserve_kwh=[4.5],
    )

    assert horizon.planning_reserve_kwh == (4.5,)


def test_a_requirement_above_the_pack_is_capped_at_what_it_can_reach() -> None:
    """Asking for 30 kWh from a 22 kWh pack quantises to 21.75, not 30.0.

    Capping rather than refusing, because a requirement above the pack is a real
    situation -- a winter evening the battery cannot cover -- and the honest plan
    is "hold everything", not "no plan".

    **One bucket below the ceiling since beta.41, and that is a correction rather
    than a loosening.** Household service is a state transition now, so a pack
    that is serving the house is never quite full: a floor demanding a
    *completely* full pack is one no state can occupy, and it produced a permanent
    violation that suppressed the published economic value entirely. Capping at
    the highest level a serving pack can actually reach keeps the requirement as
    demanding as it can be while remaining a requirement.
    """
    table = reference_table()
    horizon = horizon_for(
        table,
        demands=flat_demands(1),
        prices=two_tier_prices(1, cheap_until=1),
        reserve_kwh=[30.0],
    )

    ceiling = table.energy(table.buckets)
    assert horizon.planning_reserve_kwh == (21.75,)
    # Reachable by construction: a state exists at or above it.
    assert horizon.planning_reserve_kwh[0] < ceiling
    assert horizon.planning_reserve_kwh[0] == table.energy(table.buckets - 1)


def test_every_violation_is_an_exact_multiple_of_the_bucket() -> None:
    """The property that makes sub-bucket violations *unrepresentable*.

    Both sides of ``energy - requirement`` land on the grid, so a 0.001 kWh
    shortfall cannot be expressed. That is what bounds the lexicographic
    objective's worst over-payment to one bucket of energy, and it is a property
    of the state space rather than of a rounding rule.
    """
    table = reference_table()
    for raw in (4.41, 6.13, 9.999, 12.5, 17.26, 21.87):
        horizon = horizon_for(
            table,
            demands=flat_demands(1),
            prices=two_tier_prices(1, cheap_until=1),
            reserve_kwh=[raw],
        )
        requirement = horizon.planning_reserve_kwh[0]
        for bucket in range(table.buckets + 1):
            gap = table.energy(bucket) - requirement
            assert abs(gap / table.bucket_kwh - round(gap / table.bucket_kwh)) < 1e-9


def test_a_real_sub_bucket_shortfall_is_visible_as_one_bucket() -> None:
    """A 0.24 kWh shortfall reports 0.25, not 0.00.

    The case an earlier draft lost. Quantising the *violation* down reported zero
    here, which is exactly the shortfall a person needs to be told about.
    """
    table = reference_table()
    floor = table.limits.energy_for_soc(FLOOR_PERCENT)
    horizon = horizon_for(
        table,
        demands=flat_demands(2),
        prices=two_tier_prices(2, cheap_until=2),
        # 4.64 quantises up to 4.75, so a pack holding 4.50 is one bucket short.
        reserve_kwh=[4.64, 4.64],
    )
    plan = solved(table, horizon, start_kwh=4.5, permitted=IMPLEMENTED)

    assert horizon.planning_reserve_kwh == (4.75, 4.75)
    assert floor == 4.4
    assert plan.violation_kwh == pytest.approx(0.5)  # one bucket, both intervals
    assert plan.worst_shortfall_kwh == pytest.approx(0.25)
    assert plan.first_violation_index == 0


# -- C. the horizon ----------------------------------------------------------


def test_the_horizon_is_the_contiguous_prefix_every_input_agrees_on() -> None:
    """A price gap at interval five stops the horizon at five, not at eight."""
    table = reference_table()
    prices = two_tier_prices(EIGHT, cheap_until=4)
    prices[5] = IntervalPrice(import_eur_kwh=0.20, export_eur_kwh=None)
    horizon = horizon_for(table, demands=flat_demands(EIGHT), prices=prices)

    assert horizon.intervals == 5
    assert horizon.limited_by == "prices"


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        ("load", "load_forecast"),
        ("reserve", "reserve"),
        ("prices_short", "prices"),
    ],
)
def test_the_horizon_names_what_stopped_it(mutate: str, expected: str) -> None:
    """Each input can truncate the horizon, and each says so by name."""
    table = reference_table()
    demands = flat_demands(EIGHT)
    prices = two_tier_prices(EIGHT, cheap_until=4)
    reserve: list[float | None] = [4.4] * EIGHT

    if mutate == "load":
        demands[3] = IntervalDemand(index=3, baseline_kwh=None, pv_kwh=0.0)
    elif mutate == "reserve":
        reserve[3] = None
    else:
        prices = prices[:3]

    horizon = horizon_for(table, demands=demands, prices=prices, reserve_kwh=reserve)

    assert horizon.intervals == 3
    assert horizon.limited_by == expected


def test_a_complete_horizon_says_complete() -> None:
    """Nothing truncated it, and the plan covers every interval offered."""
    table = reference_table()
    horizon = eight_interval_horizon(table)

    assert horizon.intervals == EIGHT
    assert horizon.limited_by == "complete"


def test_an_empty_horizon_produces_an_unavailable_plan_not_a_zero_one() -> None:
    """No intervals means no plan. Not a plan that costs nothing."""
    table = reference_table()
    horizon = horizon_for(table, demands=[], prices=[])
    plan = solved(table, horizon, start_kwh=START_KWH)

    assert horizon.intervals == 0
    assert plan.available is False
    assert plan.unavailable_reason == ECONOMIC_UNAVAILABLE_HORIZON_EMPTY
    assert plan.runs == ()
    assert plan.intervals == ()


# -- D. the objective, recomputed by hand ------------------------------------


def test_doing_nothing_costs_exactly_the_house_load_at_the_prices() -> None:
    """Four intervals at 10 cents and four at 40, a quarter kilowatt-hour each."""
    table = reference_table()
    horizon = eight_interval_horizon(table)

    expected = 4 * 0.25 * CHEAP_IMPORT + 4 * 0.25 * DEAR_IMPORT

    assert expected == pytest.approx(0.50)
    assert hold_cost(
        horizon=horizon, table=table, start_energy_kwh=START_KWH
    ) == pytest.approx(0.50)


def test_load_shifting_without_a_grid_purchase_is_worth_nothing_here() -> None:
    """With buying forbidden and the terminal floor at the start, hold wins.

    Not a defect and worth pinning: every kilowatt-hour discharged into the cheap
    evening has to come back from somewhere, and if nothing may buy it and the
    plan must end where it started, there is nowhere for it to come from.
    """
    table = reference_table()
    horizon = eight_interval_horizon(table)
    plan = solved(table, horizon, start_kwh=START_KWH, permitted=IMPLEMENTED)

    assert plan.runs == ()
    assert plan.cost_eur == pytest.approx(0.50)
    assert plan.expected_net_value_eur == pytest.approx(0.0)


def test_buying_cheap_and_discharging_dear_costs_exactly_the_hand_figure() -> None:
    """Every euro recomputed from grid AC energy, interval by interval.

    The plan moves four buckets -- 1.0 kWh DC -- because that is all the terminal
    condition and the bucket grid between them allow. Four buckets is 0.5271 kWh
    of AC charge and 0.9487 kWh of AC discharge, and the difference is the round
    trip.
    """
    table = reference_table()
    horizon = eight_interval_horizon(table)
    plan = solved(
        table,
        horizon,
        start_kwh=START_KWH,
        permitted=IMPLEMENTED | {ECONOMIC_ACTION_CHARGE},
    )

    charge = plan.planned_charge_ac_kwh
    discharge = plan.planned_discharge_ac_kwh
    assert charge == pytest.approx(1.0 / ETA, abs=1e-9)
    assert discharge == pytest.approx(1.0 * ETA, abs=1e-9)

    # Cheap block: two idle intervals of pure load, two that also charge.
    cheap_import = 4 * 0.25 + charge
    # Expensive block: load less what the battery supplied, floored at zero.
    dear_import = 4 * 0.25 - discharge
    expected = cheap_import * CHEAP_IMPORT + dear_import * DEAR_IMPORT

    assert expected == pytest.approx(0.2259, abs=5e-5)
    assert plan.cost_eur == pytest.approx(expected, abs=1e-9)
    assert plan.planned_grid_import_kwh == pytest.approx(cheap_import + dear_import)
    assert plan.planned_grid_export_kwh == pytest.approx(0.0)


def test_selling_into_the_peak_is_bounded_by_the_power_limit_and_the_terminal() -> None:
    """Nine kilowatt-hours DC in, nine out, and the export is lower by the load.

    Two bounds bite at once and both are checked: four intervals at nine buckets
    is thirty-six buckets of charge, and the terminal condition forces every one
    of them back out. The export is the discharge *minus the house load*, which is
    the signature that grid energy is coming from ``split_grid_energy`` rather than
    from the battery figure.
    """
    table = reference_table()
    horizon = eight_interval_horizon(table)
    plan = solved(table, horizon, start_kwh=START_KWH, permitted=EVERYTHING)

    moved_dc = 36 * table.bucket_kwh
    assert moved_dc == 9.0
    assert plan.planned_charge_ac_kwh == pytest.approx(moved_dc / ETA, abs=1e-9)
    assert plan.planned_discharge_ac_kwh == pytest.approx(moved_dc * ETA, abs=1e-9)

    load = EIGHT * 0.25
    export = plan.planned_discharge_ac_kwh - 4 * 0.25
    assert plan.planned_grid_export_kwh == pytest.approx(export, abs=1e-9)
    assert export == pytest.approx(9.0 * ETA - 1.0, abs=1e-12)
    assert export == pytest.approx(7.5381, abs=1e-4)

    expected = (4 * 0.25 + plan.planned_charge_ac_kwh) * CHEAP_IMPORT - (
        export * DEAR_EXPORT
    )
    assert plan.cost_eur == pytest.approx(expected, abs=1e-9)
    assert plan.cost_eur == pytest.approx(-1.5897, abs=5e-5)
    assert plan.planned_grid_import_kwh == pytest.approx(
        4 * 0.25 + plan.planned_charge_ac_kwh, abs=1e-9
    )
    assert load == 2.0


def test_the_expected_net_value_is_the_gain_over_holding_before_switching() -> None:
    """Value is ``hold - cost``, less the switching cost, and never net of it.

    The switching cost is a notional device for suppressing pointless action.
    Nobody pays it, so reporting a gain net of it would understate what the plan
    earns -- but it still has to be visible, which is why it is a separate term.
    """
    table = reference_table()
    horizon = eight_interval_horizon(table)
    plan = solved(table, horizon, start_kwh=START_KWH, gain=0.25)

    assert plan.switching_cost_eur == pytest.approx(0.50)  # two runs at 0.25
    assert plan.hold_cost_eur == pytest.approx(0.50)
    assert plan.expected_net_value_eur == pytest.approx(
        (plan.hold_cost_eur - plan.cost_eur) - plan.switching_cost_eur
    )


# -- E. the terminal condition -----------------------------------------------


def test_the_last_interval_does_not_dump_the_battery() -> None:
    """The highest export price is last, and the plan still ends at the floor set.

    Terminal value zero plus a terminal floor at the reserve would sell everything
    above the reserve in the final priced interval, every single day. That is not
    economics, it is an artefact of where the data stops.
    """
    table = reference_table()
    horizon = horizon_for(
        table,
        demands=flat_demands(4),
        prices=[
            IntervalPrice(import_eur_kwh=0.20, export_eur_kwh=0.05),
            IntervalPrice(import_eur_kwh=0.20, export_eur_kwh=0.05),
            IntervalPrice(import_eur_kwh=0.20, export_eur_kwh=0.05),
            IntervalPrice(import_eur_kwh=0.60, export_eur_kwh=0.55),
        ],
    )
    plan = solved(table, horizon, start_kwh=15.0, terminal_kwh=15.0)

    assert plan.end_energy_dc_kwh == pytest.approx(15.0, abs=1e-9)
    assert plan.terminal_binding is True
    assert plan.terminal_floor_kwh == pytest.approx(15.0)


def test_a_low_terminal_floor_lets_the_evening_sale_happen_bounded() -> None:
    """The condition is a bound, not a prohibition.

    The action space is continuous in buckets, so the optimizer sells *down to*
    the terminal floor rather than choosing between everything and nothing. An
    earlier draft rejected this constraint on a counterexample that assumed the
    all-or-nothing reading, and the counterexample dissolves here.
    """
    table = reference_table()
    horizon = horizon_for(
        table,
        demands=flat_demands(4),
        prices=[IntervalPrice(import_eur_kwh=0.60, export_eur_kwh=0.55)] * 4,
    )
    plan = solved(table, horizon, start_kwh=15.0, terminal_kwh=8.0)

    assert plan.end_energy_dc_kwh == pytest.approx(8.0, abs=1e-9)
    assert plan.terminal_binding is True
    assert plan.planned_grid_export_kwh > 0.0
    # Sold down to the bound and no further: the floor is 4.4, not 8.0.
    assert plan.end_energy_dc_kwh > table.limits.energy_for_soc(FLOOR_PERCENT)


def test_mid_horizon_headroom_creation_is_entirely_unconstrained() -> None:
    """The condition applies at the endpoint only.

    A plan may empty the battery in the middle of the horizon and refill it, and
    must be allowed to: that is exactly the evening-peak-then-tomorrow's-sun trade.
    Applying the bound at every interval would forbid all of it.
    """
    table = reference_table()
    # Expensive middle, and a late surplus that refills the pack for free.
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
    assert plan.end_energy_dc_kwh == pytest.approx(12.0, abs=1e-9)
    assert trough < 12.0 - 1e-9


def test_a_terminal_floor_the_plan_clears_anyway_is_not_binding() -> None:
    """``terminal_binding`` is false when the constraint was not what stopped it."""
    table = reference_table()
    horizon = horizon_for(
        table,
        demands=flat_demands(4),
        prices=[IntervalPrice(import_eur_kwh=0.20, export_eur_kwh=0.02)] * 4,
    )
    plan = solved(table, horizon, start_kwh=15.0, terminal_kwh=4.4)

    assert plan.end_energy_dc_kwh > 4.4 + 1e-9
    assert plan.terminal_binding is False


# -- F. the lexicographic ordering -------------------------------------------


def test_a_violation_loses_to_nothing_at_any_price() -> None:
    """Make avoiding the shortfall absurdly expensive; it is still avoided.

    This is the test that distinguishes a lexicographic order from a weighted
    penalty. A weight, however large, is a price -- and a test that only checked a
    moderate price would pass against either.
    """
    table = reference_table()
    floor = table.limits.energy_for_soc(FLOOR_PERCENT)
    horizon = horizon_for(
        table,
        demands=flat_demands(4, load_kwh=0.0),
        # Buying is ruinous; the reserve still has to be met.
        prices=[IntervalPrice(import_eur_kwh=999.0, export_eur_kwh=0.0)] * 4,
        reserve_kwh=[floor, floor, 8.0, 8.0],
    )
    plan = solved(table, horizon, start_kwh=floor, terminal_kwh=floor)

    assert plan.violation_kwh == pytest.approx(0.0)
    assert plan.cost_eur > 100.0
    assert plan.runs[0].action == ECONOMIC_ACTION_CHARGE


def test_when_no_violation_is_avoidable_the_order_degenerates_to_cost() -> None:
    """An impossible reserve must not switch the optimizer into a safety mode.

    Both terms still apply; the first simply ties. The plan then minimises cost
    *while* holding the shortfall at its unavoidable minimum -- which is why an
    unreachable reserve must never unlock a sale.
    """
    table = reference_table()
    horizon = horizon_for(
        table,
        demands=flat_demands(4, load_kwh=0.0),
        prices=[IntervalPrice(import_eur_kwh=0.20, export_eur_kwh=0.90)] * 4,
        # 22 kWh is the whole pack, so starting at 5.0 can never satisfy it.
        reserve_kwh=[22.0] * 4,
    )
    plan = solved(table, horizon, start_kwh=5.0, terminal_kwh=5.0)

    assert plan.violation_kwh > 0.0
    # A tempting export at 0.90 that a fallback-to-profit design would have taken.
    assert plan.planned_grid_export_kwh == pytest.approx(0.0)
    assert plan.end_energy_dc_kwh >= 5.0 - 1e-9


def test_the_worst_over_payment_for_the_last_bucket_stays_proportionate() -> None:
    """Bounded by one bucket of energy plus one switching cost, not unbounded.

    Roughly twenty-one cents at a peak price of 0.45 and a gain threshold of 0.10.
    The reason it is bounded at all is that the action space is continuous in
    buckets: the optimizer sells down to the reserve rather than choosing between
    selling everything and selling nothing.
    """
    bound = ECONOMIC_BUCKET_KWH * 0.45 + 0.10

    assert bound == pytest.approx(0.2125)
    assert bound < 0.25


# -- G. the boundary contract ------------------------------------------------


def test_every_interval_carries_all_six_energies_and_they_reconcile() -> None:
    """DC delta, two battery AC figures, two grid AC figures, one curtailment.

    The reconciliation is what makes them a contract rather than six fields: the
    DC movement must equal the AC figure through the measured ratio, and at most
    one direction may be non-zero in an interval.
    """
    table = reference_table()
    horizon = eight_interval_horizon(table)
    plan = solved(table, horizon, start_kwh=START_KWH)

    for entry in plan.intervals:
        assert not (entry.battery_charge_ac_kwh and entry.battery_discharge_ac_kwh)
        assert not (entry.grid_import_kwh and entry.grid_export_kwh)
        if entry.battery_charge_ac_kwh:
            assert entry.battery_delta_dc_kwh == pytest.approx(
                entry.battery_charge_ac_kwh * table.charge_dc_per_ac, abs=1e-9
            )
        if entry.battery_discharge_ac_kwh:
            assert -entry.battery_delta_dc_kwh == pytest.approx(
                entry.battery_discharge_ac_kwh * table.discharge_dc_per_ac, abs=1e-9
            )


def test_the_priced_quantity_is_grid_energy_and_only_grid_energy() -> None:
    """Each interval's cost is recomputed from its own two grid figures.

    A structural check as much as an arithmetic one: if a euro figure were ever
    taken from a DC or battery-side quantity, this reconstruction would miss by
    exactly the round-trip loss or by the house load.
    """
    table = reference_table()
    horizon = eight_interval_horizon(table)
    plan = solved(table, horizon, start_kwh=START_KWH)

    total = 0.0
    for entry in plan.intervals:
        assert entry.import_price_eur_kwh is not None
        assert entry.export_price_eur_kwh is not None
        expected = (
            entry.import_price_eur_kwh * entry.grid_import_kwh
            - entry.export_price_eur_kwh * entry.grid_export_kwh
        )
        assert entry.cost_eur == pytest.approx(expected, abs=1e-12)
        total += expected

    assert plan.cost_eur == pytest.approx(total, abs=1e-9)


def test_changing_the_round_trip_moves_the_dc_figure_and_not_the_priced_one() -> None:
    """The boundary test a plausible-looking total cannot catch.

    Commanded AC energy is what a command sets and what the meter sees. The DC
    movement behind it depends on the efficiency; the grid figure for the same
    commanded AC energy does not. If the two were ever confused, this would fail
    at 80 % while passing at 90 %.
    """
    for percent in (90.0, 80.0):
        table = reference_table(round_trip_efficiency_percent=percent)
        eta = math.sqrt(percent / 100.0)
        assert table.discharge_dc_per_ac == pytest.approx(1.0 / eta, abs=1e-12)

        # One interval, one interval's worth of forced discharge into a big load.
        horizon = horizon_for(
            table,
            demands=flat_demands(1, load_kwh=2.5),
            prices=[IntervalPrice(import_eur_kwh=1.0, export_eur_kwh=0.0)],
            reserve_kwh=[4.4],
        )
        plan = solved(table, horizon, start_kwh=15.0, terminal_kwh=4.4)
        entry = plan.intervals[0]

        # The grid pays for what the house could not take from the battery.
        assert entry.cost_eur == pytest.approx(
            2.5 - entry.battery_discharge_ac_kwh, abs=1e-9
        )
        assert -entry.battery_delta_dc_kwh == pytest.approx(
            entry.battery_discharge_ac_kwh / eta, abs=1e-9
        )


@pytest.mark.parametrize(
    ("power_kw", "intervals"),
    [(20.0, 1), (10.0, 2), (5.0, 4)],
)
def test_five_kilowatt_hours_of_battery_ac_occupies_the_stated_intervals(
    power_kw: float, intervals: int
) -> None:
    """5 kWh of battery **AC** discharge at 20/10/5 kW is 1/2/4 intervals.

    Stated in battery AC, which is the unit that makes the arithmetic exact. An
    earlier draft stated the same figures as *export*, where the house load gets
    in the way and 5 kWh at 10 kW is 2.22 intervals rather than 2. The companion
    test below pins that difference rather than papering over it.
    """
    per_interval = power_kw * INTERVAL_HOURS

    assert math.ceil(5.0 / per_interval - 1e-9) == intervals


def test_grid_export_for_the_same_runs_is_lower_by_exactly_the_house_load() -> None:
    """A 10 kW discharge against 1 kW of load exports 2.25 kWh, not 2.5.

    Computed through the model rather than by a formula of its own, because the
    residual split is ``split_grid_energy``'s job and a second formula for it is
    a second thing that can be wrong.
    """
    table = reference_table()
    horizon = horizon_for(
        table,
        demands=flat_demands(2, load_kwh=0.25),
        prices=[IntervalPrice(import_eur_kwh=0.05, export_eur_kwh=0.90)] * 2,
    )
    plan = solved(table, horizon, start_kwh=20.0, terminal_kwh=4.4)

    for entry in plan.intervals:
        if entry.battery_discharge_ac_kwh:
            assert entry.grid_export_kwh == pytest.approx(
                entry.battery_discharge_ac_kwh - 0.25, abs=1e-9
            )


# -- H. runs, and the switching cost -----------------------------------------


def test_a_run_is_a_maximal_contiguous_stretch_of_one_action() -> None:
    """Two runs here, and each one covers its whole block."""
    table = reference_table()
    horizon = eight_interval_horizon(table)
    plan = solved(table, horizon, start_kwh=START_KWH)

    assert [run.action for run in plan.runs] == [
        ECONOMIC_ACTION_CHARGE,
        ECONOMIC_ACTION_EXPORT,
    ]
    assert [(run.start_index, run.end_index) for run in plan.runs] == [(0, 3), (4, 7)]
    assert [run.interval_count for run in plan.runs] == [4, 4]


def test_the_switching_cost_is_charged_once_per_run_not_per_interval() -> None:
    """Two runs at ten cents is twenty cents, however long the runs are."""
    table = reference_table()
    horizon = eight_interval_horizon(table)
    plan = solved(table, horizon, start_kwh=START_KWH, gain=0.10)

    assert len(plan.runs) == 2
    assert plan.switching_cost_eur == pytest.approx(0.20)


def test_a_high_enough_gain_threshold_suppresses_the_trade_entirely() -> None:
    """Raise the bar above what the trade earns and the plan holds.

    The point of a per-run cost rather than a per-kilowatt-hour one: a tenth of a
    kilowatt-hour at a wide margin still earns only a few cents, and no per-kWh
    figure suppresses that without also suppressing the trades worth making.
    """
    table = reference_table()
    horizon = eight_interval_horizon(table)

    cheap = solved(
        table,
        horizon,
        start_kwh=START_KWH,
        gain=0.0,
        permitted=IMPLEMENTED | {ECONOMIC_ACTION_CHARGE},
    )
    dear = solved(
        table,
        horizon,
        start_kwh=START_KWH,
        gain=5.0,
        permitted=IMPLEMENTED | {ECONOMIC_ACTION_CHARGE},
    )

    assert cheap.runs != ()
    assert dear.runs == ()
    assert dear.cost_eur == pytest.approx(
        hold_cost(horizon=horizon, table=table, start_energy_kwh=START_KWH)
    )


def test_reserve_protection_charging_happens_below_the_gain_threshold() -> None:
    """No exemption rule, and none needed: the reserve has priority in the order.

    Worth pinning because the obvious implementation is a special case ("ignore
    the threshold when the reserve is short"), and a special case is a thing that
    can be applied in the wrong place.
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


def test_the_published_run_is_the_current_one_else_the_next() -> None:
    """A run at interval zero is current; a later one is next, never current."""
    table = reference_table()
    # Cheap first, then dear: the charge starts immediately.
    immediate = solved(table, eight_interval_horizon(table), start_kwh=START_KWH)
    assert immediate.current_run is not None
    assert immediate.current_run.start_index == 0
    assert immediate.published_run is immediate.current_run

    # Dear first, then cheap: nothing happens until the cheap block.
    later = solved(
        table,
        horizon_for(
            table,
            demands=flat_demands(EIGHT),
            prices=two_tier_prices(EIGHT, cheap_until=0)[:4]
            + two_tier_prices(EIGHT, cheap_until=EIGHT)[4:],
        ),
        start_kwh=START_KWH,
    )
    if later.runs:
        assert later.current_run is None or later.current_run.start_index == 0
        if later.current_run is None:
            assert later.published_run is later.next_run
            assert later.published_run.start_index > 0


@pytest.mark.parametrize(
    ("action", "field"),
    [
        (ECONOMIC_ACTION_CHARGE, "battery_charge_ac_kwh"),
        (ECONOMIC_ACTION_SAFETY_BUY, "battery_charge_ac_kwh"),
        (ECONOMIC_ACTION_DISCHARGE, "battery_discharge_ac_kwh"),
        (ECONOMIC_ACTION_EXPORT, "grid_export_kwh"),
        (ECONOMIC_ACTION_CURTAIL, "pv_curtailed_kwh"),
    ],
)
def test_the_published_energy_is_the_flow_the_action_controls(
    action: str, field: str
) -> None:
    """One number per action, and a different one for each, deliberately.

    A charge sets a battery rate and the pack gains that energy; an export is paid
    for at the meter and the battery movement behind it is larger by the house
    load; a curtailment is production declined. Reporting one figure for all of
    them would be wrong for three.
    """
    from custom_components.alpha_ems_manager.economic import EconomicRun

    run = EconomicRun(
        action=action,
        start_index=0,
        end_index=0,
        interval_count=1,
        battery_charge_ac_kwh=1.0,
        battery_discharge_ac_kwh=2.0,
        grid_import_kwh=3.0,
        grid_export_kwh=4.0,
        pv_curtailed_kwh=5.0,
        first_power_kw=6.0,
        net_cash_flow_eur=0.0,
        min_price_eur_kwh=None,
        max_price_eur_kwh=None,
        average_price_eur_kwh=None,
    )

    assert run.energy_kwh == getattr(run, field)


def test_a_hold_reports_a_known_zero_rather_than_an_unknown() -> None:
    """``hold`` is 0.0 kWh, which is a fact, not a missing value."""
    from custom_components.alpha_ems_manager.economic import EconomicRun

    run = EconomicRun(
        action=ECONOMIC_ACTION_HOLD,
        start_index=0,
        end_index=0,
        interval_count=1,
        battery_charge_ac_kwh=1.0,
        battery_discharge_ac_kwh=1.0,
        grid_import_kwh=1.0,
        grid_export_kwh=1.0,
        pv_curtailed_kwh=1.0,
        first_power_kw=0.0,
        net_cash_flow_eur=0.0,
        min_price_eur_kwh=None,
        max_price_eur_kwh=None,
        average_price_eur_kwh=None,
    )

    assert run.energy_kwh == 0.0


# -- I. production, curtailment and negative prices --------------------------


def test_a_negative_export_price_declines_exactly_the_would_be_export() -> None:
    """Curtail the spill and not one kilowatt-hour more.

    Closed form rather than a search: the amount worth declining is precisely the
    export that would otherwise happen, because production the house or the
    battery can absorb still has value.
    """
    table = reference_table()
    horizon = horizon_for(
        table,
        # 3 kWh of production against 0.25 kWh of load, and a full battery.
        demands=flat_demands(2, load_kwh=0.25, pv_kwh=3.0),
        prices=[IntervalPrice(import_eur_kwh=0.30, export_eur_kwh=-0.10)] * 2,
    )
    plan = solved(table, horizon, start_kwh=22.0, terminal_kwh=22.0)

    assert plan.planned_grid_export_kwh == pytest.approx(0.0)
    assert plan.planned_curtailed_kwh == pytest.approx(2 * (3.0 - 0.25), abs=1e-9)
    assert plan.cost_eur == pytest.approx(0.0, abs=1e-9)


def test_a_positive_export_price_never_curtails() -> None:
    """Production that pays is not declined, however small the payment."""
    table = reference_table()
    horizon = horizon_for(
        table,
        demands=flat_demands(2, load_kwh=0.25, pv_kwh=3.0),
        prices=[IntervalPrice(import_eur_kwh=0.30, export_eur_kwh=0.001)] * 2,
    )
    plan = solved(table, horizon, start_kwh=22.0, terminal_kwh=22.0)

    assert plan.planned_curtailed_kwh == pytest.approx(0.0)
    assert plan.planned_grid_export_kwh > 0.0


def test_unavoidable_spill_does_not_make_a_state_infeasible() -> None:
    """A full battery under bright sun exports, and that is not the battery's doing.

    The permission for export is measured against the **idle baseline**: what would
    have spilled anyway. An earlier draft tested the direction alone, which made
    every state during a sunny midday illegal and collapsed the desired plan onto
    the capability plan.
    """
    table = reference_table()
    horizon = horizon_for(
        table,
        demands=flat_demands(2, load_kwh=0.0, pv_kwh=3.0),
        prices=[IntervalPrice(import_eur_kwh=0.30, export_eur_kwh=0.10)] * 2,
    )
    # Export is *not* permitted, and the plan must still be available.
    plan = solved(
        table, horizon, start_kwh=22.0, terminal_kwh=22.0, permitted=IMPLEMENTED
    )

    assert plan.available is True
    assert plan.planned_grid_export_kwh > 0.0
    assert plan.planned_curtailed_kwh == pytest.approx(0.0)


def test_production_is_absorbed_before_anything_is_bought() -> None:
    """A surplus interval charges the battery for free, with no grid import."""
    table = reference_table()
    horizon = horizon_for(
        table,
        demands=flat_demands(2, load_kwh=0.0, pv_kwh=2.0),
        prices=[IntervalPrice(import_eur_kwh=0.30, export_eur_kwh=0.05)] * 2,
    )
    plan = solved(table, horizon, start_kwh=10.0, terminal_kwh=10.0)

    assert plan.planned_grid_import_kwh == pytest.approx(0.0)


def test_an_unforecast_interval_advances_the_battery_but_prices_nothing() -> None:
    """``None`` load is not a predicted idle house.

    It truncates the horizon rather than being treated as zero, which is the rule
    every layer of this integration keeps: learned nothing must never read as zero.
    """
    table = reference_table()
    demands = flat_demands(4)
    demands[2] = IntervalDemand(index=2, baseline_kwh=None, pv_kwh=None)
    horizon = horizon_for(
        table, demands=demands, prices=two_tier_prices(4, cheap_until=2)
    )

    assert horizon.intervals == 2
    assert horizon.limited_by == "load_forecast"


# -- J. purity and cost ------------------------------------------------------


def test_the_solver_is_deterministic() -> None:
    """The same inputs give a bit-identical plan, twice."""
    table = reference_table()
    horizon = eight_interval_horizon(table)

    first = solved(table, horizon, start_kwh=START_KWH)
    second = solved(table, horizon, start_kwh=START_KWH)

    assert first == second


def test_the_solver_never_raises_on_absurd_inputs() -> None:
    """Total, by contract. A refresh must not die on a strange number."""
    table = reference_table()
    horizon = eight_interval_horizon(table)

    for start, terminal in ((-5.0, 0.0), (1e9, 1e9), (0.0, 1e9), (11.0, -1.0)):
        plan = solved(table, horizon, start_kwh=start, terminal_kwh=terminal)
        assert isinstance(plan, EconomicPlan)
        assert math.isfinite(plan.cost_eur)
        assert math.isfinite(plan.violation_kwh)


def test_a_full_day_horizon_solves_inside_the_refresh_budget() -> None:
    """Ninety-six intervals, four solves' worth of work, well under a second.

    The guard exists because the first working version took 670 ms by calling the
    grid split once per transition. Precomputing the per-interval outcomes by
    bucket delta cut it to a sixth, and this is what stops that regressing
    silently into a refresh that misses its quarter-hour.
    """
    table = reference_table()
    horizon = horizon_for(
        table,
        demands=flat_demands(96),
        prices=two_tier_prices(96, cheap_until=48),
    )

    started = time.perf_counter()
    plan = solved(table, horizon, start_kwh=START_KWH)
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    assert plan.intervals_evaluated == 96
    assert elapsed_ms < 1500.0, f"one solve took {elapsed_ms:.0f} ms"


# -- K. the two ratified contracts -------------------------------------------
#
# Both were found by this file during Phase-8 Stage A, and both are refinements of
# the approved plan that were reviewed and ratified before release. They are pinned
# here because either one read the other way makes the release non-functional in
# its **default** configuration:
#
# 1. ``charge`` means *buying from the grid*. Ambient production absorption is not
#    an economic charge action: it is published as ``hold``, earns no run, pays no
#    switching cost, and needs no opt-in.
# 2. The terminal bound is the idle-with-absorption endpoint reproduced on the
#    solver's own bucketed grid, reachable by construction. ``HoldPolicy`` remains
#    the conceptual counterfactual; the continuous reference value is never
#    confused with the enforced constraint.


#: The hold trajectory's endpoint for the sunny fixture below, taken from the
#: Phase-3 simulator by the collapse regression further down. This is the number
#: the coordinator passes in production, and it is the only thing that makes the
#: battery store its own sunshine rather than spill it.
SUNNY_HOLD_END_KWH = 15.486832980505142


def sunny_horizon(table: PhysicsTable) -> EconomicHorizon:
    """Return four intervals of pure surplus at a modest positive export price."""
    return horizon_for(
        table,
        demands=flat_demands(4, load_kwh=0.0, pv_kwh=2.5),
        prices=[IntervalPrice(import_eur_kwh=0.20, export_eur_kwh=0.05)] * 4,
    )


def test_storing_your_own_surplus_is_not_charging_from_the_grid() -> None:
    """Absorption happens with both opt-ins off, and is labelled ``hold``.

    The ratified contract:

        physical battery charging from ambient production
            is not
        Alpha EMS economically choosing to buy energy from the grid

    ``charge`` and ``allow_grid_charging`` mean the second thing only. Permission
    is measured against the **idle baseline** -- the exact mirror of the export
    rule already in place -- so a charge that draws nothing extra from the grid is
    ambient physical behaviour rather than a purchase.

    Read the other way, as the approved plan originally stated it, the only way to
    model a battery storing its own sunshine was to turn on the grid-charging
    opt-in. With it off -- the default -- the model believed the pack never absorbs
    anything, which is false about every inverter ever built.
    """
    table = reference_table()
    plan = solved(
        table,
        sunny_horizon(table),
        start_kwh=6.0,
        terminal_kwh=SUNNY_HOLD_END_KWH,
        permitted=IMPLEMENTED,
    )

    assert plan.available is True
    assert plan.planned_charge_ac_kwh > 0.0
    assert plan.planned_grid_import_kwh == pytest.approx(0.0)
    assert plan.end_energy_dc_kwh > 6.0
    # Ambient, so it is not a run and it is not a trade.
    assert plan.runs == ()
    assert plan.switching_cost_eur == pytest.approx(0.0)
    assert {entry.action for entry in plan.intervals} == {ECONOMIC_ACTION_HOLD}


def test_stored_energy_carries_no_price_so_a_slack_terminal_floor_spills() -> None:
    """Deliberate, and the reason the terminal condition exists at all.

    Terminal energy is assigned **no value** -- that is what keeps the objective
    free of any claim about prices after the horizon. The consequence is that a
    plan with a slack endpoint prefers a penny of export revenue to a
    kilowatt-hour in the pack, and the *only* thing that stops it is the bound
    from the hold trajectory. Pinning it here means the bound cannot be quietly
    weakened without this failing first.
    """
    table = reference_table()
    horizon = sunny_horizon(table)

    slack = solved(
        table, horizon, start_kwh=6.0, terminal_kwh=6.0, permitted=IMPLEMENTED
    )
    bound = solved(
        table,
        horizon,
        start_kwh=6.0,
        terminal_kwh=SUNNY_HOLD_END_KWH,
        permitted=IMPLEMENTED,
    )

    assert slack.planned_charge_ac_kwh == pytest.approx(0.0)
    assert slack.planned_grid_export_kwh > bound.planned_grid_export_kwh
    assert bound.end_energy_dc_kwh > slack.end_energy_dc_kwh


def test_absorption_costs_no_switching_fee_however_high_the_threshold() -> None:
    """A ten-euro gain threshold does not make the battery decline free energy.

    Follows from the labelling, and worth pinning separately: charging a notional
    per-run fee against ambient behaviour would have the optimizer refuse
    sunshine to save money nobody pays -- or, worse, report the sun arriving as a
    trade it had decided to make.
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
    assert free.planned_charge_ac_kwh == pytest.approx(dear.planned_charge_ac_kwh)
    assert dear.switching_cost_eur == pytest.approx(0.0)


def test_a_sunny_horizon_produces_a_plan_rather_than_collapsing() -> None:
    """The regression that matters: default settings, real August weather.

    Read literally, as the approved plan stated it, this returned
    ``available=False`` with the reason ``economic_horizon_empty`` -- about a
    four-interval horizon -- because the terminal floor came from a continuous hold
    trajectory that absorbs while the state space had no way to absorb. Two wrongs:
    a bound nobody could satisfy, reported under a name that described a different
    failure.

    Retained by explicit instruction as the regression that proves both halves: the
    literal bound collapses the sunny default configuration, and the ratified
    bucketed bound produces a valid plan.
    """
    from custom_components.alpha_ems_manager.battery import build_state, static_reserve
    from custom_components.alpha_ems_manager.policy import HoldPolicy
    from custom_components.alpha_ems_manager.simulation import simulate

    table = reference_table()
    demands = flat_demands(4, load_kwh=0.0, pv_kwh=2.5)
    horizon = sunny_horizon(table)
    state = build_state(
        soc_percent=table.limits.soc_for_energy(6.0),
        limits=table.limits,
        reserve=static_reserve(FLOOR_PERCENT),
    )
    assert state is not None
    reference = simulate(state, demands, HoldPolicy().provider(), absorb_surplus=True)

    plan = solved(
        table,
        horizon,
        start_kwh=6.0,
        terminal_kwh=reference.end_energy_kwh,
        permitted=IMPLEMENTED,
    )

    assert reference.end_energy_kwh == pytest.approx(SUNNY_HOLD_END_KWH, abs=1e-12)
    assert plan.available is True
    assert plan.unavailable_reason is None
    assert plan.intervals_evaluated == 4


def test_the_enforced_terminal_floor_never_exceeds_what_the_grid_can_reach() -> None:
    """The requested bound is continuous; the enforced one is on the solver's grid.

    The ratified contract: ``HoldPolicy`` stays the conceptual counterfactual, the
    economic horizon is the horizon being optimised, and terminal inventory
    protection is reproduced on the bucketed state space so it is reachable by
    construction. Bucketed absorption loses up to one bucket per interval against
    the continuous trajectory, so the requested figure can sit above anything the
    state space can express.

    What is *published* is what is *enforced*: publishing a bound the solver
    quietly relaxed would be the worst of both, and the continuous reference value
    must never be silently confused with the bucketed constraint.
    """
    table = reference_table()
    horizon = sunny_horizon(table)
    requested = SUNNY_HOLD_END_KWH
    plan = solved(
        table, horizon, start_kwh=6.0, terminal_kwh=requested, permitted=IMPLEMENTED
    )

    assert plan.terminal_floor_kwh <= requested
    assert plan.terminal_floor_kwh == pytest.approx(15.0)
    assert plan.end_energy_dc_kwh >= plan.terminal_floor_kwh - 1e-9
    # On the grid, exactly: 24 buckets at the start plus nine per interval.
    assert plan.terminal_floor_kwh == table.energy(24 + 4 * 9)


def test_a_terminal_floor_the_grid_can_express_is_left_alone() -> None:
    """No surplus, so the ambient walk stays put and the bound is the request."""
    table = reference_table()
    horizon = eight_interval_horizon(table)
    plan = solved(table, horizon, start_kwh=START_KWH, terminal_kwh=START_KWH)

    assert plan.terminal_floor_kwh == pytest.approx(START_KWH)


def test_the_terminal_bound_is_the_same_for_every_solve() -> None:
    """Every compared solve must be bounded identically, or the comparison is
    unfair.

    The terminal-protection solve is the one deliberate exception, and it is
    not compared against these: relaxing the bound is the whole point of it.

    It follows from the ambient walk consulting only the idle-run deltas, which no
    permission set can remove: absorbing production the house cannot use needs no
    opt-in. If the bound moved with the permitted set, the desired plan and the
    capability plan would be answering different questions and
    ``economic_value_forgone_eur`` would be measuring the difference between two
    problems rather than between two plans.
    """
    table = reference_table()
    horizon = sunny_horizon(table)
    kwargs = {"start_kwh": 6.0, "terminal_kwh": SUNNY_HOLD_END_KWH}

    everything = solved(table, horizon, permitted=EVERYTHING, **kwargs)
    implemented = solved(table, horizon, permitted=IMPLEMENTED, **kwargs)
    bare = solved(
        table, horizon, permitted=frozenset({ECONOMIC_ACTION_DISCHARGE}), **kwargs
    )

    assert everything.terminal_floor_kwh == implemented.terminal_floor_kwh
    assert implemented.terminal_floor_kwh == bare.terminal_floor_kwh
    assert bare.terminal_floor_kwh == pytest.approx(15.0)


def test_curtailment_never_turns_free_absorption_into_a_purchase() -> None:
    """The closed form declines only what would have been exported.

    Which is why the ambient walk survives it: after curtailing, the remaining
    production still covers the load and the charge exactly, so a charge that drew
    nothing from the grid before still draws nothing. Declining *all* production
    would have created import at a positive price and made absorption a purchase.
    """
    table = reference_table()
    horizon = horizon_for(
        table,
        demands=flat_demands(4, load_kwh=0.25, pv_kwh=2.5),
        prices=[IntervalPrice(import_eur_kwh=0.30, export_eur_kwh=-0.10)] * 4,
    )
    plan = solved(
        table, horizon, start_kwh=6.0, terminal_kwh=12.0, permitted=IMPLEMENTED
    )

    assert plan.available is True
    assert plan.planned_grid_import_kwh == pytest.approx(0.0)
    assert plan.planned_charge_ac_kwh > 0.0
    assert plan.end_energy_dc_kwh >= 12.0 - 1e-9


def test_ambient_absorption_is_reported_as_hold_and_needs_no_opt_in() -> None:
    """The user-facing contract, asserted in one place as a whole.

    Six claims, because the distinction is the sort a later refactor erodes one
    clause at a time: when production naturally enters the battery while Alpha EMS
    takes no economic action, that absorption

    * is published as ``hold``, never ``charge``;
    * creates no economic action run;
    * pays no ``minimum_trade_gain_eur``;
    * does not require ``allow_grid_charging``;
    * remains part of the physical trajectory;
    * and can still create future economic value through the energy it stored.

    The last one is what makes the distinction honest rather than a technicality:
    the absorbed energy is worth something, and the plan that follows it is free to
    spend it. What did not happen is a decision.
    """
    table = reference_table()
    horizon = sunny_horizon(table)
    kwargs = {"start_kwh": 6.0, "terminal_kwh": SUNNY_HOLD_END_KWH}

    # No opt-in of any kind, and a gain threshold high enough to veto any trade.
    absorbing = solved(table, horizon, gain=10.0, permitted=IMPLEMENTED, **kwargs)

    assert ECONOMIC_ACTION_CHARGE not in absorbing.permitted
    assert {entry.action for entry in absorbing.intervals} == {ECONOMIC_ACTION_HOLD}
    assert absorbing.runs == ()
    assert absorbing.switching_cost_eur == pytest.approx(0.0)
    assert absorbing.planned_charge_ac_kwh > 0.0
    assert absorbing.planned_grid_import_kwh == pytest.approx(0.0)

    # Part of the physical trajectory: the pack really did gain the energy.
    assert absorbing.end_energy_dc_kwh > 6.0

    # And that energy is worth something. The same horizon, extended by an
    # expensive evening interval, spends it -- which it could not do if the
    # absorption had been refused for want of a permission.
    extended = horizon_for(
        table,
        demands=[
            *flat_demands(4, load_kwh=0.0, pv_kwh=2.5),
            IntervalDemand(index=4, baseline_kwh=2.5, pv_kwh=0.0),
        ],
        prices=[
            *[IntervalPrice(import_eur_kwh=0.20, export_eur_kwh=0.05)] * 4,
            IntervalPrice(import_eur_kwh=0.80, export_eur_kwh=0.02),
        ],
    )
    spending = solved(
        table,
        extended,
        start_kwh=6.0,
        terminal_kwh=table.limits.energy_for_soc(FLOOR_PERCENT),
        permitted=IMPLEMENTED,
    )

    assert spending.planned_discharge_ac_kwh > 0.0
    assert spending.runs, "the stored energy has to be spendable"
    assert spending.runs[-1].action == ECONOMIC_ACTION_DISCHARGE
