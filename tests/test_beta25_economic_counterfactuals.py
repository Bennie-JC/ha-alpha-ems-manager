"""Prove the economics before changing them, which is the whole point of this file.

Every scenario below was *suspected* of being a defect. The rule the beta.25 plan
holds to is that Stage A changes only where a harness demonstrates one -- so each
test states the suspicion, runs it against the real solver, and records what the
objective actually does.

**Why these are answerable at all**, and it is worth stating once rather than in
every docstring. Three facts about ``economic.solve``:

1. it is backward induction over ``(interval, bucket, run_state)`` with a value of
   ``(violation, cost)`` compared lexicographically and summed per interval;
2. ``cost_eur`` is one expression -- ``import_price * import_kwh - export_price *
   export_kwh`` -- so avoided import and export revenue are the same term;
3. the **bucket axis is the only channel** by which energy travels between
   intervals, so a buy decision and a sell decision are coupled through it by
   construction and cannot be optimised independently even in principle.

That is why "sell at the highest price" and "buy at the cheapest quarter" are not
implemented anywhere: each is a weaker restatement of something the search already
proves exactly, and a weaker restatement can only ever disagree with the optimum.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.economic import (
    IntervalPrice,
    _safety_buy_attribution,
    _safety_buy_runs,
    solve,
)
from custom_components.alpha_ems_manager.simulation import IntervalDemand

from .test_economic_model import (
    EVERYTHING,
    FLOOR_PERCENT,
    horizon_for,
    reference_table,
)

#: The reference installation's configured minimum, resolved once. Scenarios
#: express a reserve requirement relative to it, and needed it before the solve.
FLOOR = reference_table().limits.energy_for_soc(FLOOR_PERCENT)

#: A requirement comfortably below the starting energy, for the intervals a
#: scenario means to leave unconstrained.
#:
#: **Not** ``FLOOR`` itself, and finding that out cost two wrong tests.
#: ``build_horizon`` quantises the requirement **up** to a bucket boundary -- the
#: safe direction, since protecting at most one bucket too much beats ignoring a
#: shortfall -- so passing the floor asks for the bucket *above* it, which is
#: above the start. Every interval then carries a real shortfall and the plan
#: charges immediately, which looks exactly like the defect these tests exist to
#: rule out.
SLACK = FLOOR - 1.0


def demands(house: list[float], pv: list[float] | None = None) -> list[IntervalDemand]:
    """Return one interval per house figure, with optional production."""
    return [
        IntervalDemand(
            index=index,
            baseline_kwh=load,
            pv_kwh=None if pv is None else pv[index],
        )
        for index, load in enumerate(house)
    ]


def prices(pairs: list[tuple[float, float]]) -> list[IntervalPrice]:
    """Return prices as (import, export) pairs, never one signed number."""
    return [
        IntervalPrice(import_eur_kwh=buy, export_eur_kwh=sell) for buy, sell in pairs
    ]


def run_plan(
    *,
    house: list[float],
    price_pairs: list[tuple[float, float]],
    pv: list[float] | None = None,
    reserve: list[float | None] | None = None,
    start_offset_kwh: float = 0.0,
    terminal_offset_kwh: float = 0.0,
    max_charge_kw: float | None = None,
):
    """Solve one scenario and return ``(plan, table, floor_kwh)``."""
    overrides = {} if max_charge_kw is None else {"max_charge_kw": max_charge_kw}
    table = reference_table(**overrides)
    floor = table.limits.energy_for_soc(FLOOR_PERCENT)
    horizon = horizon_for(
        table,
        demands=demands(house, pv),
        prices=prices(price_pairs),
        reserve_kwh=None if reserve is None else reserve,
    )
    plan = solve(
        table=table,
        horizon=horizon,
        start_energy_kwh=floor + start_offset_kwh,
        terminal_floor_kwh=floor + terminal_offset_kwh,
        minimum_trade_gain_eur=0.0,
        permitted=EVERYTHING,
    )
    return plan, table, floor


def charging_indices(plan) -> list[int]:
    """Return the intervals in which the battery bought from the grid."""
    return [
        entry.index
        for entry in plan.intervals
        if entry.battery_charge_ac_kwh > 0.0 and entry.marginal_grid_import_kwh > 1e-6
    ]


def exporting_indices(plan) -> list[int]:
    """Return the intervals in which the battery caused grid export."""
    return [
        entry.index for entry in plan.intervals if entry.marginal_grid_export_kwh > 1e-6
    ]


def discharging_indices(plan) -> list[int]:
    """Return the intervals in which the battery discharged at all."""
    return [
        entry.index for entry in plan.intervals if entry.battery_discharge_ac_kwh > 0.0
    ]


# == Safety Buy: cheapest feasible path to the reserve ========================
#
# The invariant under test is *not* "buy when reserve risk appears". It is
# "cheapest currently feasible path to guarantee the future reserve" -- and the
# difference between those two is entirely in whether waiting is checked.


def test_safety_buy_waits_for_the_cheapest_feasible_quarter() -> None:
    """**Scenario 1.** 0.28, 0.23, 0.17, and the reserve reachable from each.

    Suspected defect: buying as soon as a reserve shortfall is visible. The
    objective has no such trigger -- the reserve enters as the first lexicographic
    term on where each transition *lands*, so among the paths that reach the
    requirement the order is pure cost.
    """
    # Export compensation is held negligible throughout on purpose, and the dear
    # quarter is *last*. An attractive export price -- or a dear interval to
    # discharge into -- makes every earlier quarter worth buying for resale, which
    # is a correct answer to a different question and would hide this one.
    plan, table, _floor = run_plan(
        house=[1.0, 1.0, 1.0],
        price_pairs=[(0.28, 0.01), (0.23, 0.01), (0.17, 0.01)],
        reserve=[SLACK, SLACK, FLOOR + 0.5],
    )

    assert plan.available, plan.unavailable_reason
    bought = charging_indices(plan)
    assert bought == [2], (
        f"bought at {bought}: the requirement fits in one quarter, and the "
        "cheapest feasible one is index 2"
    )
    # And it buys the minimum: exactly up to the quantised requirement, no more.
    delivered = sum(entry.battery_delta_dc_kwh for entry in plan.intervals)
    assert delivered == pytest.approx(0.75, abs=table.bucket_kwh)


def test_safety_buy_moves_early_only_when_waiting_is_infeasible() -> None:
    """**Scenario 2.** Same prices, but one quarter cannot deliver the shortfall.

    With the charge power limited, the requirement is larger than a single
    interval can supply, so waiting for the cheapest quarter alone is physically
    infeasible and the plan must start earlier. It should still buy the *minimum*
    early: reserve feasibility is a constraint, not an excuse.
    """
    plan, _table, _floor = run_plan(
        house=[1.0, 1.0, 1.0],
        price_pairs=[(0.28, 0.01), (0.23, 0.01), (0.17, 0.01)],
        reserve=[SLACK, SLACK, FLOOR + 1.0],
        max_charge_kw=3.0,
    )

    assert plan.available, plan.unavailable_reason
    charged = {
        entry.index: entry.battery_delta_dc_kwh
        for entry in plan.intervals
        if entry.battery_delta_dc_kwh > 0.0
    }
    assert set(charged) == {0, 1, 2}, (
        f"charged {charged}: at 3 kW no two quarters can supply the shortfall, "
        "so feasibility forces the plan to start at the first"
    )
    # **And the dearest quarter carries the least.** This is the assertion that
    # distinguishes "buy early because you have to" from "buy early because a
    # shortfall was visible": the 0.28 quarter supplies only the remainder the two
    # cheaper ones physically cannot, and the cheap quarters run at their limit.
    assert charged[0] < charged[2], charged
    assert charged[1] == pytest.approx(charged[2]), charged


def test_a_reserve_independent_profitable_charge_is_not_reserve_attributed() -> None:
    """**Scenario 5.** Cheap now, dear later, and the reserve flat throughout.

    The charge exists because it is profitable, so the reserve-relaxed
    counterfactual buys the same energy and the attribution is zero. A price
    threshold could never make this distinction -- a cheap interval and a reserve
    deadline often coincide.
    """
    house = [1.0, 1.0, 1.0, 1.0]
    price_pairs = [(0.10, 0.08), (0.10, 0.08), (0.40, 0.35), (0.40, 0.35)]
    desired, table, _floor = run_plan(house=house, price_pairs=price_pairs)
    relaxed, _table, _floor = run_plan(house=house, price_pairs=price_pairs)

    assert desired.available
    flagged = _safety_buy_runs(desired, relaxed, table.bucket_kwh)
    assert flagged == (), "a profitable charge is not a safety buy"

    attribution = _safety_buy_attribution(desired, relaxed)
    for safety, economic in attribution.values():
        assert safety == pytest.approx(0.0, abs=1e-6)
        assert economic >= 0.0


def test_the_attribution_always_sums_to_the_run_charge() -> None:
    """The split is exhaustive, so a reader is never shown a missing remainder."""
    house = [1.0] * 4
    price_pairs = [(0.10, 0.08), (0.12, 0.09), (0.40, 0.35), (0.40, 0.35)]
    desired, _table, _floor = run_plan(
        house=house,
        price_pairs=price_pairs,
        reserve=[SLACK, SLACK, SLACK, FLOOR + 1.0],
    )
    relaxed, _t, _f = run_plan(house=house, price_pairs=price_pairs)

    attribution = _safety_buy_attribution(desired, relaxed)
    by_start = {run.start_index: run for run in desired.runs}
    for start, (safety, economic) in attribution.items():
        assert safety + economic == pytest.approx(
            by_start[start].battery_charge_ac_kwh, abs=1e-6
        )


# == Sell side: highest-value use, which is not the highest price =============


def test_energy_is_retained_for_a_dearer_avoided_import() -> None:
    """**Structural, as the review claimed: 0.40 saved beats 0.30 earned.**

    One kilowatt-hour of discretionary energy. Exporting it now earns 0.30;
    holding it covers house load later that would otherwise be imported at 0.40.
    Both flow through the *same* cost term -- serving load reduces ``import_kwh``,
    exporting raises ``export_kwh`` -- so this is arithmetic rather than a rule
    that could be written the wrong way round.
    """
    # **Exactly one kilowatt-hour of headroom above the floor**, which is the
    # whole design of the scenario: with two the plan can do both and the
    # comparison is never made.
    plan, _table, _floor = run_plan(
        house=[0.0, 1.0],
        price_pairs=[(0.31, 0.30), (0.40, 0.05)],
        start_offset_kwh=1.0,
    )

    assert plan.available, plan.unavailable_reason
    assert exporting_indices(plan) == [], "the early export is the worse use"
    assert 1 in discharging_indices(plan), "the later house load is the better use"


def test_energy_is_exported_when_export_really_is_the_better_use() -> None:
    """The inverse, so the previous test is not passing for a structural reason.

    Now the early export pays 0.50 and the later import is only 0.20, so selling
    is the higher-value choice and the objective takes it.
    """
    plan, _table, _floor = run_plan(
        house=[0.0, 1.0],
        price_pairs=[(0.51, 0.50), (0.20, 0.05)],
        start_offset_kwh=1.0,
    )

    assert plan.available, plan.unavailable_reason
    assert exporting_indices(plan), "selling at 0.50 is the better use here"


def test_the_battery_is_not_exhausted_in_the_first_sell_window() -> None:
    """**Multiple sell windows, and the mechanism that couples them.**

    0.34, then 0.40, then 0.31, with only enough discretionary energy for one.
    Discharging in the first window lands on a *lower bucket*, and the second
    window's value is read at that lower bucket -- so the 0.40 it forgoes is
    already priced into the 0.34 decision. Premature exhaustion is only ever
    chosen when it wins the whole sum.
    """
    plan, _table, _floor = run_plan(
        house=[0.0, 0.0, 0.0],
        price_pairs=[(0.36, 0.34), (0.42, 0.40), (0.33, 0.31)],
        start_offset_kwh=1.0,
    )

    assert plan.available, plan.unavailable_reason
    sold = exporting_indices(plan)
    assert sold, "there is discretionary energy and a profitable window"
    assert 1 in sold, f"exported at {sold}: the 0.40 window is the valuable one"


def test_the_reserve_is_never_traded_away_for_export_revenue() -> None:
    """**Lexicographic priority, asserted where money would tempt it.**

    A very high export price against a reserve requirement the discharge would
    breach. No amount of revenue may unlock a violation, because the first term of
    the value ties before the second is ever compared.
    """
    plan, _table, _floor = run_plan(
        house=[0.0, 0.0],
        price_pairs=[(1.30, 1.20), (1.30, 1.20)],
        start_offset_kwh=1.0,
        reserve=None,
    )
    protected, _t, _f = run_plan(
        house=[0.0, 0.0],
        price_pairs=[(1.30, 1.20), (1.30, 1.20)],
        start_offset_kwh=1.0,
        reserve=[FLOOR + 1.0, FLOOR + 1.0],
    )

    assert plan.available and protected.available
    assert exporting_indices(plan), "unprotected, the revenue is worth taking"
    assert protected.end_energy_dc_kwh >= FLOOR + 1.0 - 1e-6, (
        "the reserve must survive any export price"
    )


def test_energy_above_the_reserve_stays_economically_discretionary() -> None:
    """And the inverse failure: the reserve must not become "keep the SoC high".

    Energy above the requirement is tradeable, and a plan that refused to touch it
    would be over-reserving -- the exact ratchet beta.18 removed when it stopped
    passing the hold trajectory's endpoint as the terminal floor.
    """
    plan, _table, _floor = run_plan(
        house=[0.0, 0.0],
        price_pairs=[(0.60, 0.55), (0.60, 0.55)],
        start_offset_kwh=3.0,
        reserve=[FLOOR + 1.0, FLOOR + 1.0],
    )

    assert plan.available
    assert exporting_indices(plan), "the surplus above the reserve is discretionary"
    assert plan.end_energy_dc_kwh >= FLOOR + 1.0 - 1e-6


# == Negative prices: the same objective, not a special rule set ==============


def test_a_negative_import_price_makes_charging_profitable() -> None:
    """**Case A.** Being paid to import is an ordinary negative cost term.

    Nothing reads the sign of a price anywhere. ``import_price * import_kwh``
    simply goes negative, so the search takes the move because it reduces the
    total -- and the later avoided import is a *separate interval* in the same
    sum, so both are counted once each.
    """
    plan, _table, _floor = run_plan(
        house=[0.5, 0.5],
        price_pairs=[(-0.05, -0.06), (0.40, 0.35)],
    )

    assert plan.available, plan.unavailable_reason
    assert 0 in charging_indices(plan), "being paid to fill the battery is free money"


def test_headroom_is_preserved_for_an_even_more_negative_quarter() -> None:
    """**Case B.** A better opportunity later makes waiting correct.

    The same coupling as the multiple-sell-window case, in the other direction:
    filling now lands on a higher bucket, and the later interval's value is read
    from there, so the cheaper acquisition it forgoes is already priced in.
    """
    plan, _table, _floor = run_plan(
        house=[0.5, 0.5, 0.5],
        price_pairs=[(-0.05, -0.06), (-0.40, -0.45), (0.60, 0.55)],
    )

    assert plan.available, plan.unavailable_reason
    bought = charging_indices(plan)
    assert bought, "there is money to be made importing here"
    charged = {
        entry.index: entry.battery_charge_ac_kwh
        for entry in plan.intervals
        if entry.battery_charge_ac_kwh > 0.0
    }
    assert charged.get(1, 0.0) >= charged.get(0, 0.0), (
        f"charged {charged}: the -0.40 quarter is the better acquisition"
    )


def test_a_negative_export_price_makes_exporting_destroy_value() -> None:
    """**Case C.** ``- export_price * export_kwh`` becomes a positive cost.

    So the objective avoids export where it feasibly can, with no rule saying so.
    """
    plan, _table, _floor = run_plan(
        house=[1.0, 1.0],
        price_pairs=[(0.20, -0.30), (0.20, -0.30)],
        pv=[3.0, 0.0],
    )

    assert plan.available, plan.unavailable_reason
    # The surplus production is absorbed or declined rather than sold at a loss.
    sold = sum(entry.grid_export_kwh for entry in plan.intervals)
    idle_sold = sum(entry.idle_export_kwh for entry in plan.intervals)
    assert sold <= idle_sold + 1e-6, "the battery must not add export at a loss"


def test_a_full_battery_facing_a_negative_export_price_declines_production() -> None:
    """**Case D.** Curtailment is a modelled action, evaluated on the same basis.

    With no headroom left and export paying a negative price, declining production
    is the correct outcome -- and the closed form declines exactly the energy that
    would have been exported and no more, because going further would force import
    at a positive price.
    """
    plan, _table, _floor = run_plan(
        house=[0.5],
        price_pairs=[(0.20, -0.50)],
        pv=[4.0],
        start_offset_kwh=table_headroom(),
    )

    assert plan.available, plan.unavailable_reason
    curtailed = sum(entry.pv_curtailed_kwh for entry in plan.intervals)
    exported = sum(entry.grid_export_kwh for entry in plan.intervals)
    assert curtailed > 0.0, "production sold at -0.50 is worse than not producing it"
    assert exported == pytest.approx(0.0, abs=1e-6)


def test_cheap_acquisition_now_is_valued_against_expensive_use_later() -> None:
    """**Case E.** The whole point: acquisition and future value in one sum.

    Efficiency, the switching fee, the reserve and both prices each enter at
    exactly one place, so the comparison is made once rather than assembled from
    parts that might disagree.
    """
    plan, _table, _floor = run_plan(
        house=[1.0, 1.0, 1.0, 1.0],
        price_pairs=[(0.05, 0.03), (0.05, 0.03), (0.80, 0.70), (0.80, 0.70)],
    )

    assert plan.available, plan.unavailable_reason
    assert charging_indices(plan), "0.05 to buy against 0.80 to avoid is worth it"
    assert discharging_indices(plan), "and the stored energy is actually used"
    early = [index for index in charging_indices(plan) if index <= 1]
    late = [index for index in discharging_indices(plan) if index >= 2]
    assert early and late, (charging_indices(plan), discharging_indices(plan))


def test_no_module_selects_a_mode_from_a_price_sign() -> None:
    """Structural: negative-price economics must not become a special rule set.

    The planner values a negative price like any other number. What must never
    appear is a rule that reads ``price < 0`` and reaches for an actuator mode --
    that would be an economic decision taken in the execution layer.
    """
    import ast
    from pathlib import Path

    component = Path("custom_components/alpha_ems_manager")
    for path in sorted(component.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            rendered = ast.unparse(node)
            if "price" not in rendered.lower():
                continue
            # A price comparison is legitimate in the objective; what is not is a
            # price comparison in a module that builds commands.
            assert path.name not in {"dispatch.py", "alphaess_device.py"}, (
                f"{path.name}: {rendered[:70]}"
            )


def table_headroom() -> float:
    """Return an offset that starts the pack effectively full.

    Expressed as a helper rather than a literal so the reference installation's
    capacity stays in one place.
    """
    table = reference_table()
    floor = table.limits.energy_for_soc(FLOOR_PERCENT)
    return table.energy(table.buckets) - floor
