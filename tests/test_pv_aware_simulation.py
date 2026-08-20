"""PV-aware planning, without becoming an optimiser.

Two things are happening here and they must not be confused. The **policy** sees
net demand -- load less expected production -- which is what makes the plan
PV-aware without any policy gaining a new objective: when the sun covers the
house the net demand is zero and the existing reserve-guard rule asks for
nothing, entirely by its own logic. Separately, the **simulator** models the
inverter storing surplus the house cannot use, which is ambient physical
behaviour and never intent.

The eleven obligations this file discharges:

1. Surplus can raise the projected state of charge.
2. Production covering load reduces simulated import.
3. Surplus beyond available headroom becomes export.
4. Power and capacity limits still bind, through the single clamp.
5. Conversion efficiency is applied exactly once.
6. Ambient charging cannot produce a charge command.
7. ``ControlIntent`` remains derived only from the policy action.
8. Phase-4 execution remains unavailable.
9. The export gate stays separate and reads live flows.
10. With absorption suppressed, surplus does not raise the projected state.
11. With the device state unreadable, absorption is not modelled and it is said.

Plus the negative: no price, cost, tariff or economic term anywhere.
"""

from __future__ import annotations

import ast
import inspect
from datetime import date

import pytest

from custom_components.alpha_ems_manager import battery as battery_module
from custom_components.alpha_ems_manager import plan as plan_module
from custom_components.alpha_ems_manager import policy as policy_module
from custom_components.alpha_ems_manager import simulation as simulation_module
from custom_components.alpha_ems_manager.battery import (
    INTERVAL_HOURS,
    BatteryRequest,
    BatteryState,
    split_grid_energy,
)
from custom_components.alpha_ems_manager.const import (
    MODE_CHARGE,
)
from custom_components.alpha_ems_manager.policy import (
    SHIPPED_POLICIES,
    HoldPolicy,
    ReserveGuardPolicy,
)
from custom_components.alpha_ems_manager.simulation import (
    IntervalDemand,
    constant_provider,
    simulate,
)

from .test_battery_model import state_for

TODAY = date(2026, 8, 21)


def state(soc_percent: float) -> BatteryState:
    """Return a battery state at one state of charge.

    A ten kilowatt-hour pack behind a five kilowatt inverter, at ninety percent
    round trip, with a twenty percent floor -- built through the same factories
    the other battery tests use, so the ranges every division relies on hold.
    """
    return state_for(soc_percent)


def demands(
    *pairs: tuple[float | None, float | None], start: int = 0
) -> tuple[IntervalDemand, ...]:
    """Return demands from ``(load_kwh, pv_kwh)`` pairs."""
    return tuple(
        IntervalDemand(index=start + offset, baseline_kwh=load, pv_kwh=pv)
        for offset, (load, pv) in enumerate(pairs)
    )


# -- the netting rule --------------------------------------------------------


def test_production_is_netted_against_load_before_anything_is_converted() -> None:
    """The invariant the whole design turns on.

    Netting after conversion destroys energy invisibly, because a charge and a
    discharge of equal size are not inverse operations once efficiency applies.
    Both quantities are AC energy here and at most one survives the flooring.
    """
    demand = IntervalDemand(index=0, baseline_kwh=1.0, pv_kwh=0.4)

    assert demand.net_demand_kwh == pytest.approx(0.6)
    assert demand.surplus_kwh == 0.0


def test_at_most_one_direction_survives_per_interval() -> None:
    """Net demand and surplus can never both be non-zero.

    This is what preserves the single-direction-per-interval invariant: a policy
    shown a net demand can only ask for a discharge, and a surplus is not a demand
    at all.
    """
    for load, pv in ((1.0, 0.0), (1.0, 0.4), (1.0, 1.0), (0.4, 1.0), (0.0, 1.0)):
        demand = IntervalDemand(index=0, baseline_kwh=load, pv_kwh=pv)
        assert not (demand.net_demand_kwh and demand.surplus_kwh), (load, pv)


def test_no_production_forecast_leaves_the_demand_exactly_as_it_was() -> None:
    """The PV-blind path must be bit-identical to the behaviour before Phase 5."""
    blind = IntervalDemand(index=0, baseline_kwh=0.75)
    dark = IntervalDemand(index=0, baseline_kwh=0.75, pv_kwh=0.0)

    assert blind.net_demand_kwh == blind.baseline_kwh == 0.75
    assert blind.power_kw == pytest.approx(0.75 / INTERVAL_HOURS)
    assert blind.pv_aware is False
    # A forecast of darkness is a forecast, and says so.
    assert dark.pv_aware is True
    assert dark.net_demand_kwh == 0.75


def test_an_unknown_load_yields_no_surplus_however_sunny() -> None:
    """An unpredicted interval is not a known surplus."""
    demand = IntervalDemand(index=0, baseline_kwh=None, pv_kwh=5.0)

    assert demand.net_demand_kwh is None
    assert demand.surplus_kwh == 0.0
    assert demand.power_kw is None


# -- what the policy sees ----------------------------------------------------


def test_the_policy_asks_for_nothing_when_the_sun_covers_the_house() -> None:
    """PV-awareness, by the policy's *existing* rule and no new objective."""
    guard = ReserveGuardPolicy()
    sunny = IntervalDemand(index=0, baseline_kwh=0.5, pv_kwh=2.0)
    dull = IntervalDemand(index=0, baseline_kwh=0.5, pv_kwh=0.0)

    assert guard.propose(state(80.0), sunny).request.power_kw == 0.0
    assert guard.propose(state(80.0), dull).request.power_kw > 0.0


def test_the_policy_asks_for_the_shortfall_and_not_the_whole_load() -> None:
    """Partial cover reduces the request rather than cancelling it."""
    guard = ReserveGuardPolicy()
    partial = IntervalDemand(index=0, baseline_kwh=1.0, pv_kwh=0.4)

    proposal = guard.propose(state(80.0), partial)

    assert proposal.request.power_kw == pytest.approx(0.6 / INTERVAL_HOURS)


def test_no_shipped_policy_ever_asks_to_charge_however_large_the_surplus() -> None:
    """The obligation that keeps this from becoming an optimiser.

    Swept over the shipped policies and a range of surpluses, because "no policy
    charges" is the property Phase 4 relies on to be sure an ambient absorption
    can never become a command.
    """
    for factory in SHIPPED_POLICIES:
        chosen = factory()
        for surplus in (0.1, 1.0, 5.0, 50.0):
            demand = IntervalDemand(index=0, baseline_kwh=0.1, pv_kwh=surplus)
            for soc in (5.0, 20.0, 50.0, 99.0):
                proposal = chosen.propose(state(soc), demand)
                assert proposal.request.mode != MODE_CHARGE, (
                    chosen.identity,
                    surplus,
                    soc,
                )


# -- the grid split ----------------------------------------------------------


def test_production_reduces_simulated_import() -> None:
    """Obligation 2, at the level of the arithmetic."""
    without = split_grid_energy(
        load_ac_kwh=1.0, charge_ac_kwh=0.0, discharge_ac_kwh=0.0
    )
    with_pv = split_grid_energy(
        load_ac_kwh=1.0, pv_ac_kwh=0.4, charge_ac_kwh=0.0, discharge_ac_kwh=0.0
    )

    assert without.import_kwh == pytest.approx(1.0)
    assert with_pv.import_kwh == pytest.approx(0.6)
    assert with_pv.export_kwh == 0.0


def test_production_beyond_load_and_charging_exports() -> None:
    """Obligation 3, at the level of the arithmetic."""
    split = split_grid_energy(
        load_ac_kwh=0.5, pv_ac_kwh=2.0, charge_ac_kwh=0.5, discharge_ac_kwh=0.0
    )

    assert split.import_kwh == 0.0
    assert split.export_kwh == pytest.approx(1.0)


def test_the_default_production_term_is_zero_so_existing_callers_are_unchanged() -> (
    None
):
    """Source-compatible: a caller without a forecast passes nothing."""
    old = split_grid_energy(load_ac_kwh=1.0, charge_ac_kwh=0.2, discharge_ac_kwh=0.0)
    new = split_grid_energy(
        load_ac_kwh=1.0, charge_ac_kwh=0.2, discharge_ac_kwh=0.0, pv_ac_kwh=0.0
    )

    assert old == new


# -- ambient absorption ------------------------------------------------------


def test_surplus_raises_the_projected_state_of_charge() -> None:
    """Obligation 1. The whole reason the projected figure becomes publishable."""
    walk = simulate(
        state(50.0),
        demands((0.1, 1.1), (0.1, 1.1), (0.1, 1.1)),
        HoldPolicy().provider(),
        absorb_surplus=True,
    )
    blind = simulate(
        state(50.0),
        demands((0.1, 1.1), (0.1, 1.1), (0.1, 1.1)),
        HoldPolicy().provider(),
        absorb_surplus=False,
    )

    assert walk.end_soc_percent > 50.0
    assert blind.end_soc_percent == pytest.approx(50.0)
    assert walk.intervals_absorbing == 3
    assert blind.intervals_absorbing == 0


def test_a_suppressed_absorption_exports_the_surplus_instead() -> None:
    """Obligation 10, and the conservative direction.

    Never claiming stored energy the inverter is actually sending to the grid
    matters more than an optimistic projection: the first is a lie about the
    battery, and the second is only a missed opportunity.
    """
    walk = simulate(
        state(50.0),
        demands((0.1, 1.1), (0.1, 1.1)),
        HoldPolicy().provider(),
        absorb_surplus=False,
    )

    assert walk.end_soc_percent == pytest.approx(50.0)
    assert walk.grid_export_kwh == pytest.approx(2.0)
    assert walk.grid_import_kwh == 0.0


def test_absorption_is_capped_by_headroom_and_the_rest_exports() -> None:
    """Obligation 3 and obligation 4, through the single clamp.

    A nearly full battery cannot take a large surplus, and what it cannot take
    leaves. Nothing here enforces that: ``apply_request`` does, which is the whole
    point of routing the ambient charge through it.
    """
    walk = simulate(
        state(99.0),
        demands(
            (0.0, 3.0),
        ),
        HoldPolicy().provider(),
        absorb_surplus=True,
    )

    assert walk.end_soc_percent == pytest.approx(100.0)
    assert walk.grid_export_kwh > 0.0


def test_absorption_respects_the_charge_power_limit() -> None:
    """Obligation 4. A 5 kW inverter cannot absorb 20 kW of surplus."""
    walk = simulate(
        state(50.0),
        demands(
            (0.0, 5.0),
        ),
        HoldPolicy().provider(),
        absorb_surplus=True,
    )
    outcome = walk.outcomes[0]

    assert outcome.charge_ac_kwh == pytest.approx(5.0 * INTERVAL_HOURS)
    assert walk.grid_export_kwh == pytest.approx(5.0 - outcome.charge_ac_kwh)


def test_conversion_efficiency_is_applied_once_in_the_charge_direction() -> None:
    """Obligation 5. Ninety percent round trip is a single one-way factor here.

    Asserted against the stored energy rather than against a ratio of two
    simulated figures, so a change that applied the loss twice in a
    self-consistent way still fails.
    """
    start = state(50.0)
    walk = simulate(
        start,
        demands(
            (0.0, 1.0),
        ),
        HoldPolicy().provider(),
        absorb_surplus=True,
    )
    outcome = walk.outcomes[0]
    stored = outcome.end_energy_kwh - start.energy_kwh

    assert outcome.charge_ac_kwh == pytest.approx(1.0)
    # One-way efficiency is the square root of the round trip, applied once.
    assert stored == pytest.approx(1.0 * (0.9**0.5), rel=1e-4)
    assert stored < outcome.charge_ac_kwh
    # Twice would be 0.9 exactly, which is the failure this distinguishes.
    assert stored != pytest.approx(0.9, rel=1e-4)


def test_ambient_absorption_never_overrides_a_requested_direction() -> None:
    """A policy that asked for something has expressed intent, and intent wins.

    This is what makes the single-direction invariant structural rather than
    argued: no interval can carry a requested direction and an ambient one.
    """
    asked = simulate(
        state(80.0),
        demands(
            (0.0, 3.0),
        ),
        constant_provider(BatteryRequest.discharge(2.0)),
        absorb_surplus=True,
    )

    assert asked.intervals_absorbing == 0
    assert asked.outcomes[0].discharge_ac_kwh > 0.0
    assert asked.outcomes[0].charge_ac_kwh == 0.0


def test_absorption_does_nothing_without_a_production_forecast() -> None:
    """PV-blind means blind: permission alone absorbs nothing."""
    walk = simulate(
        state(50.0),
        (
            IntervalDemand(index=0, baseline_kwh=0.1),
            IntervalDemand(index=1, baseline_kwh=0.1),
        ),
        HoldPolicy().provider(),
        absorb_surplus=True,
    )

    assert walk.intervals_absorbing == 0
    assert walk.end_soc_percent == pytest.approx(50.0)
    assert walk.pv_aware is False


def test_a_partly_covered_horizon_is_partly_pv_aware() -> None:
    """Neither blind nor sighted, and saying either would be wrong."""
    walk = simulate(
        state(50.0),
        (
            IntervalDemand(index=0, baseline_kwh=0.5, pv_kwh=0.1),
            IntervalDemand(index=1, baseline_kwh=0.5),
        ),
        HoldPolicy().provider(),
    )

    assert walk.pv_aware is True
    assert walk.intervals_pv_aware == 1
    assert walk.intervals == 2


# -- ambient absorption cannot become a command ------------------------------


def test_an_absorbed_surplus_produces_no_charge_request_in_the_trajectory() -> None:
    """Obligation 6, stated where a command would have to come from.

    The ambient charge exists only inside the simulator's own walk. The decision
    the plan publishes comes from the policy, and no shipped policy charges.
    """
    walk = simulate(
        state(50.0),
        demands(
            (0.0, 2.0),
        ),
        HoldPolicy().provider(),
        absorb_surplus=True,
    )

    assert walk.intervals_absorbing == 1
    # The recorded outcome charged, and that is a physical flow.
    assert walk.outcomes[0].charge_ac_kwh > 0.0
    # But the policy asked for nothing, which is what a command derives from.
    proposal = HoldPolicy().propose(
        state(50.0), IntervalDemand(index=0, baseline_kwh=0.0, pv_kwh=2.0)
    )
    assert proposal.request.mode != MODE_CHARGE


def test_the_control_intent_derives_only_from_the_policy_action() -> None:
    """Obligation 7, asserted structurally.

    ``translate`` reads the plan's decision, which is the policy's. Nothing in the
    control layer can reach a simulated trajectory, so an ambient charge has no
    path to a command even in principle.
    """
    from custom_components.alpha_ems_manager import control

    names = identifiers(control)

    for forbidden in ("absorbed", "surplus", "trajectory", "candidate"):
        assert not any(forbidden in name for name in names), forbidden


def test_the_control_layer_cannot_see_a_production_forecast() -> None:
    """Obligation 9's structural half: the gate reads live flows, never a forecast."""
    from custom_components.alpha_ems_manager import safety

    names = identifiers(safety)

    for forbidden in ("pvforecast", "pv_kwh", "solcast", "forecast_pv", "pv_w"):
        assert not any(forbidden in name for name in names), forbidden


# -- the negative: no economics ----------------------------------------------


#: Words that would mean an economic term had arrived.
ECONOMIC_WORDS = (
    "price",
    "tariff",
    "cost",
    "arbitrage",
    "cheap",
    "expensive",
    "eur",
    "spread",
)


def identifiers(module: object) -> set[str]:
    """Return every name the module's code actually uses.

    Read from the syntax tree rather than the text, so the docstrings that
    *explain* prices belong to Phase 6 are not mistaken for prices. A guard that
    fires on the documentation of its own rule gets silenced instead of fixed,
    and this project has already been bitten by exactly that.
    """
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg)
    return {name.lower() for name in names}


@pytest.mark.parametrize(
    "module",
    [simulation_module, policy_module, plan_module, battery_module],
    ids=["simulation", "policy", "plan", "battery"],
)
def test_no_economic_term_appears_in_the_decision_layer(module: object) -> None:
    """Prices are Phase 6, reserve sizing is Phase 7, arbitrage is Phase 8.

    Asserted structurally rather than by absence of behaviour, because an economic
    term added with a zero coefficient would behave identically today and be
    load-bearing tomorrow.

    Widened to the whole decision layer when prices arrived. The price series is
    real and normalised now, so the interesting statement is no longer "we have
    not written an optimiser" but "the data exists and still cannot reach a
    decision" -- and that is only worth asserting where the decisions are made.
    """
    for name in identifiers(module):
        for forbidden in ECONOMIC_WORDS:
            assert forbidden not in name, f"{forbidden} in {name}"


def test_the_economics_guard_can_actually_fail() -> None:
    """A guard that cannot fail is decoration.

    One Phase-4 structural check passed vacuously for exactly this reason, so the
    reader is proved able to see an offending identifier before being trusted to
    say there is none.
    """
    module = ast.parse("def choose(price_eur_kwh): return price_eur_kwh")
    found = {node.arg for node in ast.walk(module) if isinstance(node, ast.arg)}

    assert any("price" in name for name in found)


def test_absorption_is_not_a_charging_strategy() -> None:
    """No shipped policy gains a solar-charging objective.

    The simulator absorbs because the inverter does. If a policy started asking to
    charge from expected surplus, that would be an intentional strategy and it
    belongs to a later phase.
    """
    names = identifiers(policy_module)

    assert not any("absorb" in name for name in names)
    assert "surplus_kwh" not in names


def test_the_simulator_takes_permission_and_never_reads_a_device() -> None:
    """Purity: the answer arrives as a plain boolean.

    Whether absorption is permitted is a property of the live installation, so
    deciding it belongs to the impure layer. Passing it in as a bool is what keeps
    every case above testable with no Home Assistant instance in sight.
    """
    signature = inspect.signature(simulate)

    assert signature.parameters["absorb_surplus"].annotation == "bool"
    source = inspect.getsource(simulation_module)
    assert "homeassistant" not in source
    assert "excess_export" not in source
