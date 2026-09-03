"""beta.40: the hard floor, and the two endpoints a reader must not confuse.

**The apparent contradiction, from the 2026-09-03 beta.39 diagnostic.** Three
figures appeared together and could not all be describing one trajectory:

    configured hard floor          4.32 kWh DC   (20 % of 21.6)
    an economic-plan end energy    3.51 kWh DC   (below it)
    terminal / edge energy         4.74 kWh DC   (above it)
    reserve violation              0.00 kWh      (nothing wrong)

They are not describing one trajectory. `end_energy_dc_kwh` is the
**ambient-corrected reported walk**: `start_energy_dc_kwh` is reduced every
interval by `ambient_self_consumption_ac_kwh` -- the pack feeding the house with
no decision involved -- while `battery_delta_dc_kwh`, the lattice move the
recursion chose, stays put. `edge_energy_kwh` is the **decided lattice state**,
and this file proves it is exactly where the recursion ends.

So the violation of 0.00 is correct: violations are evaluated on the decided
state, which never went below the reserve. And the invariant that matters --
*the binding Stage-A economic trajectory may never plan below the configured
physical hard floor* -- holds, which the sweep below establishes rather than
assumes.

The measured gap is not small, and that is why the diagnostics now name the
basis: on a no-production horizon the recursion holds one bucket for 76 intervals
while the reported walk falls 5.7975 to 1.5421, below the floor, deciding nothing.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.const import (
    PLAN_END_ENERGY_BASES,
    PLAN_END_ENERGY_BASIS_AMBIENT_WALK,
    PLAN_END_ENERGY_BASIS_LATTICE_STATE,
)

from .beta34_shape import solve_at

#: The reference installation: 21.6 kWh usable DC at a configured 20 % minimum.
CAPACITY_DC_KWH = 21.6
MIN_SOC_PERCENT = 20.0
CONFIGURED_FLOOR_DC_KWH = CAPACITY_DC_KWH * MIN_SOC_PERCENT / 100.0

#: One lattice step on the reference site, from the diagnostic. The seed is
#: quantised down by at most this much, which is the only sub-floor level that
#: ever appears.
BUCKET_DC_KWH = 0.263523

SHAPES: dict[str, dict] = {
    "sell": {"head": 28, "end": 96, "stored": 8.294},
    "buy": {"head": 8, "end": 96, "stored": 1.2, "allow_export": False},
    "mixed": {"head": 36, "end": 96, "stored": 4.0},
    "zero_pv": {"head": 20, "end": 96, "stored": 6.0, "pv_fn": lambda i: 0.0},
    "survival": {"head": 68, "end": 96, "stored": 0.3},
    "survival_dear": {"head": 68, "end": 96, "stored": 0.3, "price_fn": lambda i: 0.90},
    "survival_cheap": {
        "head": 68,
        "end": 96,
        "stored": 0.3,
        "price_fn": lambda i: 0.02,
    },
}


def lattice_walk(plan) -> list[float]:
    """Return the energy the **decided** state stands at, interval by interval.

    Reconstructed from ``battery_delta_dc_kwh`` -- the lattice moves -- seeded at
    the plan's own first reported energy. Deliberately *not* read off
    ``start_energy_dc_kwh``, which carries the ambient correction and is therefore
    a different quantity; conflating the two is the whole confusion this file
    exists to settle.
    """
    if not plan.intervals:
        return []
    energy = plan.intervals[0].start_energy_dc_kwh
    walk = [energy]
    for interval in plan.intervals:
        energy += interval.battery_delta_dc_kwh
        walk.append(energy)
    return walk


# == 1. the two endpoints are different quantities, and one is the decision ==


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_decided_endpoint_is_the_edge_energy_and_not_the_reported_walk(
    shape: str,
) -> None:
    """**The identity that resolves the contradiction.**

    ``edge_energy_kwh`` is exactly where the lattice walk ends, in every shape. So
    it -- and not ``end_energy_dc_kwh`` -- is the endpoint the plan decided, and it
    is the figure the terminal credit was priced on.

    *Mutation: seed the terminal credit from ``end_energy_dc_kwh`` and this
    fails.*
    """
    plan = solve_at(**SHAPES[shape]).outcome.desired
    walk = lattice_walk(plan)

    assert walk, "a shape with no intervals proves nothing"
    assert plan.edge_energy_kwh == pytest.approx(walk[-1], abs=1e-9)


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_reported_walk_never_exceeds_the_decided_state(shape: str) -> None:
    """Ambient self-consumption can only *reduce* the projection.

    It is discharge, so the reported walk is a lower bound on the decided state --
    which is the direction that makes the projection safe to publish and unsafe to
    act on.
    """
    plan = solve_at(**SHAPES[shape]).outcome.desired

    assert plan.end_energy_dc_kwh <= plan.edge_energy_kwh + 1e-9


def test_the_gap_is_real_and_large_enough_to_cross_the_floor() -> None:
    """**The vacuity gate.** Without a measurable gap none of this would matter.

    The no-production horizon is the one that shows it: the recursion holds while
    the projection walks down past the configured floor.
    """
    plan = solve_at(**SHAPES["zero_pv"]).outcome.desired
    walk = lattice_walk(plan)

    # The decided state never went below the floor, wherever else it went.
    assert min(walk) > CONFIGURED_FLOOR_DC_KWH, min(walk)
    # The projection did not, and reported no violation for it.
    assert plan.end_energy_dc_kwh < CONFIGURED_FLOOR_DC_KWH
    assert plan.violation_kwh == pytest.approx(0.0)
    # And the gap is kilowatt-hours, not rounding.
    assert plan.edge_energy_kwh - plan.end_energy_dc_kwh > 2.0


# == 2. the invariant: the decided trajectory honours the hard floor =========


def test_the_decided_trajectory_never_discharges_below_the_hard_floor() -> None:
    """**The release gate, swept rather than argued.**

    For every horizon shape and every starting energy at or above the configured
    floor: the decided lattice walk never goes below the floor, and never below
    where it started.

    The distinction matters because the *seed* is quantised down --
    ``bucket_at_or_below`` models a measurement as slightly less energy than the
    pack holds, which is the conservative direction for an amount you have -- so a
    pack measured at 4.32 seeds at 4.2164. The plan then **holds** there. It does
    not discharge deeper, and that is the invariant: no decision takes the pack
    below the floor.

    *Mutation: relax the terminal feasibility test, or let the violation term
    ignore the reserve, and a shape digs below its seed.*
    """
    dug_below_seed = []
    deepest = None
    checked = 0
    for head in (28, 36, 20, 8, 68):
        for stored in (4.32, 4.4, 4.5, 5.0, 6.0, 8.294, 11.0, 14.0, 17.0, 20.0):
            for extra in (
                {},
                {"allow_export": False},
                {"pv_fn": lambda i: 0.0},
                {"price_fn": lambda i: 0.90},
                {"price_fn": lambda i: 0.02},
            ):
                outcome = solve_at(head=head, end=96, stored=stored, **extra).outcome
                walk = lattice_walk(outcome.desired)
                if not walk:
                    continue
                checked += 1
                low, seed = min(walk), walk[0]
                if deepest is None or low < deepest:
                    deepest = low
                if low < seed - 1e-9 and low < CONFIGURED_FLOOR_DC_KWH - 1e-9:
                    dug_below_seed.append((head, stored, tuple(extra), low, seed))

    assert checked >= 200, checked
    assert dug_below_seed == [], dug_below_seed[:5]
    # The deepest level anywhere is the seed quantisation, at most one bucket
    # below the configured floor -- never a decision to discharge past it.
    assert deepest >= CONFIGURED_FLOOR_DC_KWH - BUCKET_DC_KWH - 1e-9, deepest


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_a_sub_floor_lattice_level_is_only_ever_a_sub_floor_start(shape: str) -> None:
    """Where the decided state is below the floor, it began below the floor.

    ``buy`` and the three ``survival`` shapes start at 1.2 and 0.3 kWh. A plan
    cannot be required to end above a floor it began below without compelling a
    purchase, and compelling a purchase is what physical reachability does through
    Safety Buy -- never something the terminal condition may do on its own.
    """
    plan = solve_at(**SHAPES[shape]).outcome.desired
    walk = lattice_walk(plan)
    if not walk or min(walk) >= CONFIGURED_FLOOR_DC_KWH:
        return

    assert walk[0] < CONFIGURED_FLOOR_DC_KWH, walk[0]
    assert min(walk) >= walk[0] - 1e-9, (min(walk), walk[0])


# == 3. the diagnostics cannot be misread again ==============================


def test_both_endpoints_are_published_with_their_basis_named() -> None:
    """**The fix for the misleading diagnostic, asserted on the payload.**

    A future reader -- or a future release -- must not be able to take
    ``end_energy_dc_kwh`` for the executable trajectory. Both figures are
    published, each carries its basis from a closed vocabulary, and a rule string
    states which is which.

    *Mutation: drop ``end_energy_basis`` or ``planned_end_energy_dc_kwh`` and this
    fails.*
    """
    from custom_components.alpha_ems_manager.economic import economic_as_dict

    report = economic_as_dict(
        solve_at(**SHAPES["zero_pv"]).outcome, execution_blocked_reason="none"
    )
    totals = report["desired"]["totals"]

    assert totals["end_energy_basis"] == PLAN_END_ENERGY_BASIS_AMBIENT_WALK
    assert totals["planned_end_energy_basis"] == PLAN_END_ENERGY_BASIS_LATTICE_STATE
    assert totals["end_energy_basis"] in PLAN_END_ENERGY_BASES
    assert totals["planned_end_energy_basis"] in PLAN_END_ENERGY_BASES
    # **The published decided endpoint is the lattice state, not the projection.**
    # Asserted as an identity against ``edge_energy_kwh`` and as a strict gap, so
    # publishing the projection under both names cannot pass.
    plan = solve_at(**SHAPES["zero_pv"]).outcome.desired
    assert totals["planned_end_energy_dc_kwh"] == pytest.approx(
        round(plan.edge_energy_kwh, 2), abs=1e-9
    )
    assert totals["planned_end_energy_dc_kwh"] - totals["end_energy_dc_kwh"] > 2.0, (
        totals
    )
    # And the rule says the projection is not an executable target.
    rule = totals["end_energy_rule"]
    assert "diagnostic projection" in rule
    assert "neither figure is an executable target" in rule


def test_the_capability_plan_publishes_the_same_distinction() -> None:
    """The counterfactual is where the live 3.51 was read, so it needs it too.

    ``capability`` describes what implemented primitives could achieve. Its end
    energy is doubly non-binding -- a projection of a plan that is itself a
    counterfactual -- and it was published with no basis at all.
    """
    from custom_components.alpha_ems_manager.economic import economic_as_dict

    totals = economic_as_dict(
        solve_at(**SHAPES["zero_pv"]).outcome, execution_blocked_reason="none"
    )["capability"]["totals"]

    assert totals["end_energy_basis"] == PLAN_END_ENERGY_BASIS_AMBIENT_WALK
    assert totals["planned_end_energy_basis"] == PLAN_END_ENERGY_BASIS_LATTICE_STATE
