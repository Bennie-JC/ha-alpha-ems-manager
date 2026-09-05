"""beta.40 asked which endpoint was real. beta.41 made there be only one.

**The apparent contradiction, from the 2026-09-03 beta.39 diagnostic.** Three
figures appeared together and could not all be describing one trajectory:

    configured hard floor          4.32 kWh DC   (20 % of 21.6)
    an economic-plan end energy    3.51 kWh DC   (below it)
    terminal / edge energy         4.74 kWh DC   (above it)
    reserve violation              0.00 kWh      (nothing wrong)

beta.40 answered that they were two different quantities and published both with
their basis named: `end_energy_dc_kwh` was the ambient-corrected *reported* walk,
`edge_energy_kwh` was the *decided* lattice state, and violations were evaluated
on the decided one, so the 0.00 was correct.

**That answer was true and the situation it described was not tenable.** A
solver whose state disagrees with the pack by kilowatt-hours can price a terminal
credit on inventory the household has already eaten, and can sell the same
kilowatt-hour twice -- which is exactly what a later implementation did, exporting
3.94 kWh while projecting an endpoint of -0.158 kWh with a violation of 0.00.

So beta.41 carries household service in the solver's own state, and the two
figures become one. This file keeps the sweeps that prove the hard floor holds and
replaces the three tests that measured the gap with the one that says it is gone.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.const import (
    PLAN_END_ENERGY_BASES,
    PLAN_END_ENERGY_BASIS_PHYSICAL_STATE,
)

from .beta34_shape import solve_at
from .solve_cache import cached_solve_at
from .test_beta39_neutrality import (
    cheap_everywhere,
    dear_everywhere,
    no_production,
)

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
    """Return the physical energy the pack stands at, interval by interval.

    Reconstructed rather than read off ``start_energy_dc_kwh``, so the two have to
    agree: the decided move **and** the household service the state took, which
    since beta.41 are both state transitions. Before that this function summed the
    lattice moves alone and was deliberately a *different* quantity from the
    reported walk -- that difference is what this file used to be about.
    """
    if not plan.intervals:
        return []
    energy = plan.intervals[0].start_energy_dc_kwh
    walk = [energy]
    for interval in plan.intervals:
        energy += interval.battery_delta_dc_kwh
        energy -= interval.battery_state_service_dc_kwh
        walk.append(energy)
    return walk


# == 1. there is one endpoint, and everybody reaches it =====================


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_decided_endpoint_is_where_the_physical_walk_ends(shape: str) -> None:
    """**The identity that replaced the distinction.**

    ``edge_energy_kwh`` is exactly where the physical trajectory ends, in every
    shape -- and the trajectory is reconstructed here from the interval fields
    rather than read off the plan, so this is agreement between two derivations
    and not a restatement.

    *Mutation: leave household service out of the state transition and this
    fails, because the walk then ends where beta.40 said it did.*
    """
    plan = solve_at(**SHAPES[shape]).outcome.desired
    walk = lattice_walk(plan)

    assert walk, "a shape with no intervals proves nothing"
    assert plan.edge_energy_kwh == pytest.approx(walk[-1], abs=1e-9)


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_two_endpoint_names_report_one_number(shape: str) -> None:
    """``end_energy_dc_kwh`` and ``edge_energy_kwh`` are the same quantity now.

    Both names are kept because two names are cheaper than a migration, and this
    is what stops them drifting apart again. beta.40 measured the gap at
    kilowatt-hours -- 9.75 against 4.32 on the live horizon -- and had to publish a
    basis with each figure to say which was which.
    """
    plan = solve_at(**SHAPES[shape]).outcome.desired

    assert plan.end_energy_dc_kwh == pytest.approx(plan.edge_energy_kwh, abs=1e-12)


def test_the_gap_that_used_to_cross_the_floor_is_closed() -> None:
    """**The vacuity gate, inverted.**

    The no-production horizon is the one that showed the gap: beta.40 held one
    bucket for 76 intervals while the projection walked down past the configured
    floor to 1.5421 kWh, reporting no violation, because violations were evaluated
    on a state that never moved.

    Now the household service moves it, so the trajectory that is reported is the
    trajectory that was decided, and the floor test applies to both because they
    are one thing.
    """
    plan = solve_at(**SHAPES["zero_pv"]).outcome.desired
    walk = lattice_walk(plan)

    assert walk
    assert plan.edge_energy_kwh - plan.end_energy_dc_kwh == pytest.approx(
        0.0, abs=1e-12
    )
    # The floor holds on the physical trajectory, which is the only one there is.
    assert min(walk) >= plan.terminal_floor_kwh - BUCKET_DC_KWH - 1e-9, min(walk)
    assert plan.violation_kwh == pytest.approx(0.0)


# == 2. the invariant: the decided trajectory honours the hard floor =========


#: The five variations the floor sweep runs each horizon under. Named rather than
#: inline so :func:`tests.solve_cache.cached_solve_at` can key them -- an anonymous
#: lambda has no identity a cache can use, and the cache refuses one rather than risk
#: serving one price curve's plan to a test asking about another.
FLOOR_SWEEP_VARIANTS: tuple[dict, ...] = (
    {},
    {"allow_export": False},
    {"pv_fn": no_production},
    {"price_fn": dear_everywhere},
    {"price_fn": cheap_everywhere},
)

#: The starting energies swept, from the configured floor to a nearly full pack.
FLOOR_SWEEP_STORED: tuple[float, ...] = (
    4.32,
    4.4,
    4.5,
    5.0,
    6.0,
    8.294,
    11.0,
    14.0,
    17.0,
    20.0,
)


@pytest.mark.parametrize("head", [28, 36, 20, 8, 68])
def test_the_decided_trajectory_never_discharges_below_the_hard_floor(
    head: int,
) -> None:
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

    **Split by head in beta.42, and the grid is unchanged.** This was one test
    solving 250 horizons in 14.2 minutes -- eleven per cent of the whole suite's
    processor time, and irreducible by caching because every one of those horizons
    is distinct. Splitting it lets the workers share them. The same fifty
    combinations run per head, and the coverage floor is now asserted per head
    (``>= 40``, five heads, the same two hundred in total) so a head that
    contributed nothing fails instead of hiding inside an aggregate.

    *Mutation: relax the terminal feasibility test, or let the violation term
    ignore the reserve, and a shape digs below its seed.*
    """
    dug_below_seed = []
    deepest = None
    checked = 0
    for stored in FLOOR_SWEEP_STORED:
        for extra in FLOOR_SWEEP_VARIANTS:
            outcome = cached_solve_at(head=head, end=96, stored=stored, **extra).outcome
            walk = lattice_walk(outcome.desired)
            if not walk:
                continue
            checked += 1
            low, seed = min(walk), walk[0]
            if deepest is None or low < deepest:
                deepest = low
            if low < seed - 1e-9 and low < CONFIGURED_FLOOR_DC_KWH - 1e-9:
                dug_below_seed.append((head, stored, tuple(extra), low, seed))

    assert checked >= 40, checked
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

    assert totals["end_energy_basis"] == PLAN_END_ENERGY_BASIS_PHYSICAL_STATE
    assert totals["planned_end_energy_basis"] == PLAN_END_ENERGY_BASIS_PHYSICAL_STATE
    assert totals["end_energy_basis"] in PLAN_END_ENERGY_BASES
    assert totals["planned_end_energy_basis"] in PLAN_END_ENERGY_BASES
    # **Both keys report the pack's physical endpoint, and the same one.**
    # Asserted as an identity against ``edge_energy_kwh``, so publishing anything
    # else under either name cannot pass.
    plan = solve_at(**SHAPES["zero_pv"]).outcome.desired
    assert totals["planned_end_energy_dc_kwh"] == pytest.approx(
        round(plan.edge_energy_kwh, 2), abs=1e-9
    )
    assert totals["planned_end_energy_dc_kwh"] == pytest.approx(
        totals["end_energy_dc_kwh"], abs=1e-9
    ), totals
    # And the rule says what they are and what they are not.
    rule = totals["end_energy_rule"]
    assert "the same quantity" in rule
    assert "neither is an executable target" in rule


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

    assert totals["end_energy_basis"] == PLAN_END_ENERGY_BASIS_PHYSICAL_STATE
    assert totals["planned_end_energy_basis"] == PLAN_END_ENERGY_BASIS_PHYSICAL_STATE
