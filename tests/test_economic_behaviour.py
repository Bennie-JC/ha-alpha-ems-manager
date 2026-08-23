"""What the optimizer does with a price curve, proved rather than argued.

Every claim beta.17's review rests on that is not about the lattice or the rolling
horizon lives here. The point is not that these behaviours are new -- **none of
them is**. The point is that each one was suspected of being a defect on the live
installation, each was checked against the real solver, and each turned out to be
the objective doing its job. Without these tests the next person to look at a
diagnostics download reopens the same four questions from scratch.

The rule this file is built to defend: **do not add a rule the objective already
expresses.** "Sell at the highest price", "charge at maximum power", "keep headroom
for the sun", "one trade a day" -- each is a weaker restatement of something the
search proves exactly, and a weaker restatement can only ever disagree with the
optimum. Every test below is therefore written to fail if such a rule is ever
introduced, not merely to record today's answer.

Physics comes from the solver's transition table, which is built by probing
``apply_request``. Prices are asymmetric throughout -- import is an all-in price
and export is a compensation, never the same number -- because a single-price
model would make most of these questions trivial and all of them wrong.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.battery import (
    build_limits,
    split_grid_energy,
)
from custom_components.alpha_ems_manager.const import (
    ECONOMIC_ACTION_CHARGE,
    ECONOMIC_ACTION_DISCHARGE,
    ECONOMIC_ACTION_EXPORT,
)
from custom_components.alpha_ems_manager.economic import (
    IntervalPrice,
    build_outcome,
    build_physics_table,
    select_bucket_kwh,
    solve,
)
from custom_components.alpha_ems_manager.simulation import IntervalDemand

from .test_economic_model import (
    EVERYTHING,
    FLOOR_PERCENT,
    horizon_for,
    reference_table,
)

TABLE = reference_table()
FLOOR = TABLE.limits.energy_for_soc(FLOOR_PERCENT)
CEILING = TABLE.limits.energy_for_soc(100.0)
QUARTER_HOURS = 0.25


def planned(
    *,
    imports,
    exports,
    load,
    production=None,
    start_kwh,
    reserve_kwh=None,
    terminal_kwh=None,
    gain=0.0,
    table=None,
):
    """Return one plan over an explicit per-quarter curve."""
    table = table or TABLE
    count = len(imports)
    floor = table.limits.energy_for_soc(FLOOR_PERCENT)
    production = production or [0.0] * count
    horizon = horizon_for(
        table,
        demands=[
            IntervalDemand(index=i, baseline_kwh=load[i], pv_kwh=production[i])
            for i in range(count)
        ],
        prices=[
            IntervalPrice(import_eur_kwh=imports[i], export_eur_kwh=exports[i])
            for i in range(count)
        ],
        reserve_kwh=[floor if reserve_kwh is None else reserve_kwh] * count,
    )
    return solve(
        table=table,
        horizon=horizon,
        start_energy_kwh=start_kwh,
        terminal_floor_kwh=floor if terminal_kwh is None else terminal_kwh,
        minimum_trade_gain_eur=gain,
        permitted=EVERYTHING,
    )


def discharge_kw(plan, index: int) -> float:
    """Return the battery discharge power in one interval."""
    return plan.intervals[index].battery_discharge_ac_kwh / QUARTER_HOURS


def charge_kw(plan, index: int) -> float:
    """Return the battery charge power in one interval."""
    return plan.intervals[index].battery_charge_ac_kwh / QUARTER_HOURS


# A moderate early block, a cheap middle, then four very dear quarters.
DEAR_LATE_IMPORT = [0.25] * 4 + [0.10] * 4 + [0.60] * 4
DEAR_LATE_EXPORT = [0.20] * 4 + [0.05] * 4 + [0.55] * 4
LOAD_1KW = [0.25] * 12


# ===========================================================================
# A. sale timing: quantity, power, quarter and household avoidance, jointly
# ===========================================================================


def test_the_later_dearer_window_is_filled_before_the_earlier_one() -> None:
    """Plenty to sell and four dear quarters that can absorb it: they get it.

    The behaviour "sell only at the single highest quarter" would fail this, and
    so would "sell as soon as the price is above average". What the search
    actually does is allocate to the quarters of highest *marginal* value until
    power or energy runs out -- so all four dear quarters run at the largest
    representable power, and the earlier moderate block is used only because the
    cheap middle lets it buy the energy back for less than it sold it for.
    """
    plan = planned(
        imports=DEAR_LATE_IMPORT,
        exports=DEAR_LATE_EXPORT,
        load=LOAD_1KW,
        start_kwh=FLOOR + 10.0,
    )

    peak = TABLE.max_representable_discharge_kw
    for index in (8, 9, 10, 11):
        assert discharge_kw(plan, index) == pytest.approx(peak, abs=5e-4)
    # And the early sale was a *round trip*, not a mistake: it refilled cheaply.
    assert sum(charge_kw(plan, i) for i in (4, 5, 6, 7)) > 0.0


def test_a_small_saleable_amount_serves_the_house_instead_of_exporting() -> None:
    """Two kilowatt-hours, and avoided import beats export revenue for them.

    ``0.25`` avoided against ``0.20`` earned is the whole test: the margin is
    five cents, the two are different boundaries, and a model with one price
    cannot see the difference. The plan discharges into the house early and keeps
    the dear window for what it can refill cheaply.
    """
    plan = planned(
        imports=DEAR_LATE_IMPORT,
        exports=DEAR_LATE_EXPORT,
        load=LOAD_1KW,
        start_kwh=FLOOR + 2.0,
    )

    early = plan.intervals[1:4]
    assert all(entry.battery_discharge_ac_kwh > 0.0 for entry in early)
    assert all(entry.grid_export_kwh == pytest.approx(0.0) for entry in early)
    assert all(entry.action == ECONOMIC_ACTION_DISCHARGE for entry in early)


def test_an_earlier_sale_is_rational_when_the_peak_cannot_absorb_it_all() -> None:
    """One dear quarter, ten kilowatt-hours to sell: power is the constraint.

    The case that makes "always wait for the best price" wrong. A single quarter
    can carry at most one quarter-hour at peak power, so the rest has to be sold
    somewhere -- and the earlier moderate block is where. Selling early here is
    not impatience, it is arithmetic.
    """
    imports = [0.25] * 4 + [0.10] * 7 + [0.60]
    exports = [0.20] * 4 + [0.05] * 7 + [0.55]
    plan = planned(
        imports=imports, exports=exports, load=LOAD_1KW, start_kwh=FLOOR + 10.0
    )

    assert sum(discharge_kw(plan, i) for i in range(4)) > 0.0
    assert discharge_kw(plan, 11) == pytest.approx(
        TABLE.max_representable_discharge_kw, abs=5e-4
    )


def test_house_load_avoidance_outranks_a_poor_export_price() -> None:
    """Import 0.50, export 0.08: the kilowatt-hour is worth six times more indoors.

    A test that fails the moment marginal value stops accounting for avoided
    import -- which is the single most valuable thing the battery does and the
    thing a revenue-only objective cannot see.
    """
    imports = [0.15] * 4 + [0.10] * 4 + [0.50] * 4
    exports = [0.10] * 4 + [0.05] * 4 + [0.08] * 4
    plan = planned(
        imports=imports, exports=exports, load=[1.0] * 12, start_kwh=FLOOR + 4.0
    )

    dear = plan.intervals[8:12]
    served = sum(entry.battery_discharge_ac_kwh for entry in dear)
    exported = sum(entry.grid_export_kwh for entry in dear)
    assert served > 0.0
    # Most of what the battery gave up went to the house, not to the grid.
    assert exported < served / 2.0
    assert all(entry.grid_import_kwh == pytest.approx(0.0, abs=1e-6) for entry in dear)


def test_production_displaces_the_grid_purchase_not_the_sale() -> None:
    """Production coming later changes *what it buys*, not *when it sells*.

    Worth stating precisely, because the intuitive version of this is wrong.
    On this shape the sale is identical with and without the sun -- the dear
    window is dear either way, and the energy to fill it is already in the
    pack. What the production changes is the **refill**: 10.49 kWh bought from
    the grid becomes 2.49 kWh, and the same starting energy earns 0.80 EUR
    more.

    So the emergent summer behaviour is 'stop buying', not 'sell sooner', and
    a rule written to reserve headroom for the sun would be aiming at the
    wrong quantity.
    """
    production = [0.0] * 4 + [2.0] * 4 + [0.0] * 4
    with_sun = planned(
        imports=DEAR_LATE_IMPORT,
        exports=DEAR_LATE_EXPORT,
        load=LOAD_1KW,
        production=production,
        start_kwh=FLOOR + 6.0,
    )
    without = planned(
        imports=DEAR_LATE_IMPORT,
        exports=DEAR_LATE_EXPORT,
        load=LOAD_1KW,
        start_kwh=FLOOR + 6.0,
    )

    bought_with = sum(entry.grid_import_kwh for entry in with_sun.intervals)
    bought_without = sum(entry.grid_import_kwh for entry in without.intervals)
    assert bought_with < bought_without - 5.0
    assert with_sun.cost_eur < without.cost_eur - 0.5
    # The sale itself is untouched, which is the part that surprises.
    sold_with = sum(entry.grid_export_kwh for entry in with_sun.intervals)
    sold_without = sum(entry.grid_export_kwh for entry in without.intervals)
    assert sold_with == pytest.approx(sold_without, abs=1e-6)


# ===========================================================================
# B. asymmetric pricing: a round trip has to actually pay
# ===========================================================================


def test_a_losing_round_trip_is_refused() -> None:
    """Buy at 0.20, sell at 0.15, rebuy at 0.25 -- never.

    The arithmetic that makes it a loss is the efficiency: selling a stored
    kilowatt-hour at 0.15 and replacing it at 0.25 costs 0.278 after conversion.
    A market-price-only model, or one that used the import price on both sides,
    would find a trade here.
    """
    plan = planned(
        imports=[0.20] * 4 + [0.20] * 4 + [0.25] * 4,
        exports=[0.15] * 12,
        load=LOAD_1KW,
        start_kwh=FLOOR,
    )

    assert sum(entry.grid_export_kwh for entry in plan.intervals) == pytest.approx(0.0)


def test_the_same_shape_with_a_real_peak_does_trade() -> None:
    """The counterexample that stops the previous test passing vacuously.

    If the refusal above came from timidity rather than arithmetic, this would be
    refused too.
    """
    plan = planned(
        imports=[0.20] * 4 + [0.60] * 4 + [0.25] * 4,
        exports=[0.15] * 4 + [0.55] * 4 + [0.20] * 4,
        load=LOAD_1KW,
        start_kwh=FLOOR,
    )

    assert sum(entry.battery_charge_ac_kwh for entry in plan.intervals) > 5.0
    assert sum(entry.grid_export_kwh for entry in plan.intervals) > 5.0
    assert plan.cost_eur < 0.0


def test_export_is_never_priced_at_the_import_rate() -> None:
    """Every euro in the payload reconciles against the asymmetric pair.

    A guard on the accounting itself rather than on a decision: if the two prices
    were ever crossed, or one used for both sides, the reported cost would stop
    matching the flows it is supposedly computed from.
    """
    imports = [0.30] * 6 + [0.12] * 6
    exports = [0.11] * 6 + [0.04] * 6
    plan = planned(
        imports=imports, exports=exports, load=LOAD_1KW, start_kwh=FLOOR + 6.0
    )

    for entry in plan.intervals:
        expected = (
            entry.grid_import_kwh * imports[entry.index]
            - entry.grid_export_kwh * exports[entry.index]
        )
        assert entry.cost_eur == pytest.approx(expected, abs=1e-9)


# ===========================================================================
# C. buying: minimal for safety, opportunistic for profit
# ===========================================================================


@pytest.mark.parametrize("reserve", [8.0, 12.0, 15.5, 19.0])
def test_a_reserve_driven_buy_stops_at_the_requirement(reserve: float) -> None:
    """It buys what the reserve needs and **not one bucket more**.

    Flat prices, so there is no economic reason to hold anything: every
    kilowatt-hour bought is bought for the requirement. "Fill the pack when the
    reserve binds" would overshoot by up to 14 kWh here, and would be invisible
    on any day where filling happened to be profitable anyway.
    """
    plan = planned(
        imports=[0.25] * 32,
        exports=[0.15] * 32,
        load=[0.30] * 32,
        start_kwh=5.0,
        reserve_kwh=reserve,
    )

    peak = max(
        entry.start_energy_dc_kwh + entry.battery_delta_dc_kwh
        for entry in plan.intervals
    )
    assert peak == pytest.approx(reserve, abs=1e-9)
    assert peak < CEILING - 1e-9


def test_the_reserve_is_obeyed_at_every_interval_not_only_at_the_end() -> None:
    """A requirement spike in the middle of the horizon is met exactly.

    A plan that checked its requirement only at the horizon's end would sail
    straight through this and look perfectly healthy.
    """
    spike = [FLOOR] * 10 + [18.0] * 4 + [FLOOR] * 10
    horizon = horizon_for(
        TABLE,
        demands=[
            IntervalDemand(index=i, baseline_kwh=0.30, pv_kwh=0.0) for i in range(24)
        ],
        prices=[
            IntervalPrice(
                import_eur_kwh=0.25 if i < 10 else 0.05,
                export_eur_kwh=0.15 if i < 10 else 0.03,
            )
            for i in range(24)
        ],
        reserve_kwh=spike,
    )
    plan = solve(
        table=TABLE,
        horizon=horizon,
        start_energy_kwh=19.0,
        terminal_floor_kwh=FLOOR,
        minimum_trade_gain_eur=0.0,
        permitted=EVERYTHING,
    )

    landed = [
        entry.start_energy_dc_kwh + entry.battery_delta_dc_kwh
        for entry in plan.intervals
    ]
    assert min(landed[10:14]) >= 18.0 - 1e-9
    assert plan.violation_kwh == pytest.approx(0.0)


def test_safety_buy_is_a_label_the_comparison_produces() -> None:
    """There is no separate safety-buy mechanism, and this is how we know.

    The run is identified by re-solving with the reserve relaxed to the
    configured floor and seeing which charging disappears. So the label is a
    *consequence* of the lexicographic objective rather than a rule beside it --
    which is why no exemption from the trade threshold is needed anywhere.
    """
    horizon = horizon_for(
        TABLE,
        demands=[
            IntervalDemand(index=i, baseline_kwh=0.30, pv_kwh=0.0) for i in range(24)
        ],
        prices=[IntervalPrice(import_eur_kwh=0.25, export_eur_kwh=0.15)] * 24,
        reserve_kwh=[15.5] * 24,
    )
    outcome = build_outcome(
        table=TABLE,
        horizon=horizon,
        start_energy_kwh=5.0,
        terminal_floor_kwh=FLOOR,
        floor_energy_kwh=FLOOR,
        minimum_trade_gain_eur=0.10,
        allow_grid_charging=True,
        allow_battery_export=True,
    )

    assert outcome.safety_buy_runs == (0,)
    assert outcome.relaxed is not None
    relaxed_charge = sum(e.battery_charge_ac_kwh for e in outcome.relaxed.intervals)
    desired_charge = sum(e.battery_charge_ac_kwh for e in outcome.desired.intervals)
    # Flat prices, so with the requirement relaxed there is nothing to buy at all.
    assert relaxed_charge == pytest.approx(0.0)
    assert desired_charge > 5.0


def test_several_cheap_windows_are_used_when_one_cannot_carry_the_energy() -> None:
    """Winter behaviour, and no rule limits the number of trades in a day.

    Three separated cheap quarters and more demand than any one of them can
    supply: the plan takes all three. "One charge a day" or "one window" would
    fail this, and would quietly leave a winter installation short.
    """
    imports = [0.40] * 20
    for cheap in (1, 8, 15):
        imports[cheap] = 0.05
    plan = planned(
        imports=imports,
        exports=[value * 0.5 for value in imports],
        load=[0.30] * 20,
        start_kwh=FLOOR + 1.0,
        reserve_kwh=FLOOR,
        gain=0.0,
    )

    charged = {i for i in range(20) if charge_kw(plan, i) > 0.1}
    assert charged == {1, 8, 15}
    for cheap in (1, 8, 15):
        assert charge_kw(plan, cheap) == pytest.approx(
            TABLE.max_representable_charge_kw, abs=5e-4
        )


# ===========================================================================
# D. the price curve responds to the configured power
# ===========================================================================


@pytest.mark.parametrize("power", [5.0, 10.0, 20.0])
def test_the_chosen_curve_follows_the_configured_power(power: float) -> None:
    """More power, fewer quarters, same energy: the shape follows the hardware.

    Not "always maximum power" and not "always spread out" -- the number of
    quarters a plan needs is the energy divided by what the inverter can move,
    and it must fall as the inverter grows.
    """
    limits, why = build_limits(
        capacity_kwh=22.0,
        max_charge_kw=power,
        max_discharge_kw=power,
        round_trip_efficiency_percent=90.0,
    )
    assert limits is not None, why
    floor = limits.energy_for_soc(FLOOR_PERCENT)
    bucket, _rule = select_bucket_kwh(limits, floor_energy_kwh=floor)
    table = build_physics_table(limits, floor_energy_kwh=floor, bucket_kwh=bucket)
    assert table is not None

    # Two dear quarters and far more energy than they can carry, so **power** is
    # what binds. Given a window it cannot fill, the plan must use every watt the
    # inverter has -- and how many watts that is follows the configuration.
    plan = planned(
        imports=[0.10] * 4 + [0.70] * 2,
        exports=[0.05] * 4 + [0.65] * 2,
        load=[0.25] * 6,
        start_kwh=floor + 17.0,
        table=table,
    )
    peak = max(entry.battery_discharge_ac_kwh for entry in plan.intervals)

    assert peak / QUARTER_HOURS == pytest.approx(
        table.max_representable_discharge_kw, abs=5e-4
    )
    # And the peak scales with the hardware rather than with a preference.
    assert peak / QUARTER_HOURS > power * 0.9


def test_the_horizon_length_no_longer_changes_the_terminal_requirement() -> None:
    """Extending the forecast must not move the floor, because the floor is fixed.

    This test used to measure how far the hold-end constraint reached into the
    next interval, and how that changed between a one-day and a two-day horizon.
    beta.18 removed that constraint, and the property worth pinning is the
    stronger one that replaced it: the terminal requirement is the configured
    physical floor, so it is **the same number** whatever the horizon length --
    there is nothing left for a longer forecast to loosen or tighten.
    """
    floors = set()
    for count in (48, 96, 192):
        imports, production = [], []
        for index in range(count):
            quarter = index % 96
            imports.append(0.10 if quarter < 24 else (0.40 if quarter >= 78 else 0.22))
            production.append(1.0 if 40 <= quarter < 72 else 0.0)
        horizon = horizon_for(
            TABLE,
            demands=[
                IntervalDemand(index=i, baseline_kwh=0.30, pv_kwh=production[i])
                for i in range(count)
            ],
            prices=[
                IntervalPrice(import_eur_kwh=v, export_eur_kwh=v * 0.55)
                for v in imports
            ],
            reserve_kwh=[15.5] * count,
        )
        outcome = build_outcome(
            table=TABLE,
            horizon=horizon,
            start_energy_kwh=19.0,
            terminal_floor_kwh=FLOOR,
            floor_energy_kwh=FLOOR,
            minimum_trade_gain_eur=0.10,
            allow_grid_charging=True,
            allow_battery_export=True,
        )
        floors.add(round(outcome.desired.terminal_floor_kwh, 6))
        # And no comparison is published, at any horizon length.
        assert outcome.terminal_plan_cost_eur is None
        assert outcome.terminal_first_run_changed is None

    assert len(floors) == 1, floors
    assert floors.pop() <= FLOOR + 1e-9


def test_the_published_actions_stay_within_the_documented_vocabulary() -> None:
    """No new action was invented by any of the above."""
    plan = planned(
        imports=DEAR_LATE_IMPORT,
        exports=DEAR_LATE_EXPORT,
        load=LOAD_1KW,
        start_kwh=FLOOR + 8.0,
    )

    allowed = {
        ECONOMIC_ACTION_CHARGE,
        ECONOMIC_ACTION_DISCHARGE,
        ECONOMIC_ACTION_EXPORT,
    }
    assert {run.action for run in plan.runs} <= allowed


# ===========================================================================
# E. the live installation, pinned
# ===========================================================================
#
# Measured on the real AlphaESS around beta.17. Both cases are about the same
# thing: **a battery figure and a grid figure are different numbers**, and a
# future Stage B that confused them would command the wrong quantity. Stage A
# already models this correctly -- these tests exist so it stays that way while
# the execution contract is built on top of it.


def test_the_live_charge_case_reproduces_the_measured_grid_import() -> None:
    """3.7 kW of battery charging drew 4.17 kW from the meter, not 3.7.

    House 1.1 kW, production 0.63 kW, battery charging 3.7 kW. The meter showed
    about 4.2 kW. The identity is ``load - production + charge``, netted in AC
    before any conversion, and it lands on 4.170.

    **The Stage-B consequence:** the dispatch setpoint is the *battery* figure.
    House load must not be added to it -- the house is already in the residual.
    """
    flows = split_grid_energy(
        load_ac_kwh=1.1 * QUARTER_HOURS,
        charge_ac_kwh=3.7 * QUARTER_HOURS,
        discharge_ac_kwh=0.0,
        pv_ac_kwh=0.63 * QUARTER_HOURS,
    )

    assert flows.import_kwh / QUARTER_HOURS == pytest.approx(4.17, abs=0.005)
    assert flows.export_kwh == pytest.approx(0.0)
    # The two figures differ by production less load, which is the whole point.
    assert abs(flows.import_kwh / QUARTER_HOURS - 3.7) == pytest.approx(0.47, abs=0.005)


def test_the_live_export_case_needs_more_battery_than_it_delivers() -> None:
    """1.3 kW of net export needs 2.2 kW of battery against 0.9 kW of house.

    And the converse, which is the dangerous one: discharging *at* the export
    figure delivers well under half of it, because the house takes its share
    first.
    """
    wanted = split_grid_energy(
        load_ac_kwh=0.9 * QUARTER_HOURS,
        charge_ac_kwh=0.0,
        discharge_ac_kwh=2.2 * QUARTER_HOURS,
        pv_ac_kwh=0.0,
    )
    naive = split_grid_energy(
        load_ac_kwh=0.9 * QUARTER_HOURS,
        charge_ac_kwh=0.0,
        discharge_ac_kwh=1.3 * QUARTER_HOURS,
        pv_ac_kwh=0.0,
    )

    assert wanted.export_kwh / QUARTER_HOURS == pytest.approx(1.3, abs=0.005)
    assert naive.export_kwh / QUARTER_HOURS == pytest.approx(0.4, abs=0.005)


def test_the_optimizer_prices_the_grid_consequence_not_the_battery_movement() -> None:
    """A charge under production costs what the *meter* saw, not what the pack took.

    The same live shape put through the solver: the interval's cost is the grid
    import at the import price, and the battery movement is larger than the import.
    A model that priced battery energy would overcharge this interval by the whole
    production term.
    """
    plan = planned(
        imports=[0.30] * 4,
        exports=[0.10] * 4,
        load=[1.1 * QUARTER_HOURS] * 4,
        production=[0.63 * QUARTER_HOURS] * 4,
        start_kwh=FLOOR + 2.0,
        gain=0.0,
    )

    for entry in plan.intervals:
        expected = entry.grid_import_kwh * 0.30 - entry.grid_export_kwh * 0.10
        assert entry.cost_eur == pytest.approx(expected, abs=1e-9)
        if entry.battery_charge_ac_kwh > 0.0:
            # Production covered part of it, so the meter saw less than the pack.
            assert entry.grid_import_kwh < entry.battery_charge_ac_kwh + 1e-9


# ===========================================================================
# F. production coverage requested for beta.18
# ===========================================================================


def test_limited_headroom_makes_a_sale_before_the_sun_rational() -> None:
    """A nearly full pack with production coming sells first to make room.

    Not a rule -- an outcome. The alternative is curtailing free energy, and the
    objective can see that because the forgone export appears in the residual.
    """
    # Sell into a decent price now, absorb the sun, spend it on a dear evening.
    # Without the early sale there is no room for the sun at all.
    plan = planned(
        imports=[0.22] * 4 + [0.22] * 4 + [0.60] * 4,
        exports=[0.18] * 4 + [0.02] * 4 + [0.55] * 4,
        load=[0.10] * 8 + [0.30] * 4,
        production=[0.0] * 4 + [2.5] * 4 + [0.0] * 4,
        start_kwh=CEILING - 1.0,
        gain=0.0,
    )

    sold_early = sum(entry.grid_export_kwh for entry in plan.intervals[:4])
    absorbed = sum(entry.battery_charge_ac_kwh for entry in plan.intervals[4:8])
    spent_late = sum(entry.battery_discharge_ac_kwh for entry in plan.intervals[8:])

    assert sold_early > 0.5
    assert absorbed > 0.5
    assert spent_late > 0.5


def test_a_high_export_price_sends_production_to_the_grid_instead() -> None:
    """When selling now beats storing for later, it sells. The crossover exists.

    Measured at 0.18 EUR/kWh against a 0.40 evening on the reference pack: below
    it the sun is stored, above it the sun is sold. No rule decides this; the
    forgone revenue is simply part of the cost.
    """
    stored = planned(
        imports=[0.22] * 4 + [0.40] * 4,
        exports=[0.05] * 4 + [0.22] * 4,
        load=[0.10] * 8,
        production=[2.0] * 4 + [0.0] * 4,
        start_kwh=FLOOR + 2.0,
        gain=0.0,
    )
    sold = planned(
        imports=[0.22] * 4 + [0.40] * 4,
        exports=[0.30] * 4 + [0.22] * 4,
        load=[0.10] * 8,
        production=[2.0] * 4 + [0.0] * 4,
        start_kwh=FLOOR + 2.0,
        gain=0.0,
    )

    absorbed_cheap = sum(e.battery_charge_ac_kwh for e in stored.intervals[:4])
    absorbed_dear = sum(e.battery_charge_ac_kwh for e in sold.intervals[:4])
    assert absorbed_cheap > absorbed_dear
    assert sum(e.grid_export_kwh for e in sold.intervals[:4]) > sum(
        e.grid_export_kwh for e in stored.intervals[:4]
    )


def test_a_negative_export_price_stores_the_sun_rather_than_paying_to_export() -> None:
    """Being charged to export makes storing free energy strictly better.

    The curtailment-equivalent case: with no actuator to decline production, the
    battery is the only place for it to go, and the objective wants it there.
    """
    plan = planned(
        imports=[0.22] * 4 + [0.40] * 4,
        exports=[-0.05] * 4 + [0.22] * 4,
        load=[0.10] * 8,
        production=[2.0] * 4 + [0.0] * 8,
        start_kwh=FLOOR + 2.0,
        gain=0.0,
    )

    assert sum(e.battery_charge_ac_kwh for e in plan.intervals[:4]) > 1.0


def test_a_mixed_production_and_grid_charge_is_reported_as_mixed() -> None:
    """Sun and meter in the same campaign, and the attribution says so.

    The figure a per-kWh margin is charged on, and the figure a reader needs in
    order to believe "charged 8 kWh" without assuming 8 kWh was bought.
    """
    plan = planned(
        imports=[0.05] * 4 + [0.60] * 4,
        exports=[0.02] * 4 + [0.55] * 4,
        load=[0.10] * 8,
        production=[0.8] * 4 + [0.0] * 4,
        start_kwh=FLOOR,
        gain=0.0,
    )

    charge_runs = [run for run in plan.runs if run.battery_charge_ac_kwh > 0.0]
    assert charge_runs
    run = charge_runs[0]
    assert 0.0 < run.marginal_grid_import_kwh < run.battery_charge_ac_kwh


# ===========================================================================
# G. safety buy, re-pinned under the new margin
# ===========================================================================


def test_safety_buy_still_buys_only_what_the_reserve_needs() -> None:
    """Four requirements, four exact landings, and never the ceiling."""
    for reserve in (8.0, 12.0, 15.5, 19.0):
        plan = planned(
            imports=[0.25] * 32,
            exports=[0.15] * 32,
            load=[0.30] * 32,
            start_kwh=5.0,
            reserve_kwh=reserve,
            gain=0.10,
        )
        peak = max(
            entry.start_energy_dc_kwh + entry.battery_delta_dc_kwh
            for entry in plan.intervals
        )
        assert peak == pytest.approx(reserve, abs=1e-9), reserve
        assert peak < CEILING


def test_safety_buy_works_with_only_today_priced() -> None:
    """Tomorrow absent must not stop the reserve being met.

    The pre-publication state: a short horizon and a requirement above the current
    state of charge. Reserve feasibility outranks cost, so it recovers regardless
    of how far ahead the prices reach.
    """
    plan = planned(
        imports=[0.25] * 20,
        exports=[0.15] * 20,
        load=[0.30] * 20,
        start_kwh=6.0,
        reserve_kwh=15.5,
        gain=0.10,
    )

    ended = plan.intervals[-1]
    landed = ended.start_energy_dc_kwh + ended.battery_delta_dc_kwh
    assert landed == pytest.approx(15.5, abs=1e-9)
    assert plan.available
