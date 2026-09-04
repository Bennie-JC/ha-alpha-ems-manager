"""The frozen execution claim carries the curve the plan actually obeyed.

``reserve_floor_kwh`` is documented on the execution target as *"Stage-A physical
limits Stage B must honour, frozen with the schedule"*. Before beta.41 it was
filled from ``plan.reserve_projection`` -- the **autonomy** counterfactual, the
figure that answers "what would this pack need if the grid vanished". On the live
2026-09-03 horizon that read 21.93 kWh against a 21.6 kWh pack: a requirement
nothing can satisfy, frozen into a claim that says Stage B must honour it.

It was inert. All ten references were verified one at a time and every one is a
declaration, a pass-through or a serialisation -- no comparison, no clamp, no
``min``, no ``max``, no conditional -- and ``dispatch.py`` does not mention it at
all. What actually enforces the floor is the configured state of charge in
``safety.py``, which never reads this field.

So this is a **provenance correction made before something starts trusting it**,
and these tests are what stop the autonomy figure coming back. They assert the
property rather than the number: the frozen floor is the enforced reachability
curve the recursion obeyed, it fits inside the pack, and it never sits below the
hard floor.
"""

from __future__ import annotations

from .beta34_shape import CAPACITY, solve_at

HEAD = 56
END = 152
SHAPES = (5.0, 9.936, 18.0)


def enforced(stored: float):
    """Return one solved horizon's enforced reachability curve and its plan."""
    outcome = solve_at(head=HEAD, end=END, stored=stored).outcome
    assert outcome.available, stored
    return outcome


def test_the_frozen_floor_is_the_curve_the_recursion_obeyed() -> None:
    """**Provenance, asserted structurally rather than by value.**

    The enforced reachability curve is the one the backward induction scored
    ``violations`` against, so it is the only curve a downstream reader could act
    on without contradicting the plan in front of it. It is published aligned with
    ``horizon.demands``, and that alignment is what makes the per-interval zip in
    ``_execution_targets`` well defined rather than merely plausible.
    """
    outcome = enforced(9.936)
    curve = outcome.horizon.planning_reserve_kwh

    assert len(curve) == len(outcome.horizon.demands)
    assert all(value >= 0.0 for value in curve)


def test_the_frozen_floor_fits_inside_the_pack() -> None:
    """**The autonomy figure's signature failure, and the reason for the change.**

    21.93 kWh against a 21.6 kWh pack is not a floor, it is an impossibility -- and
    frozen into a claim it would be an instruction Stage B could never satisfy. The
    enforced curve is quantised up to one bucket and capped at the ceiling, so it
    cannot say this whatever the horizon looks like.
    """
    for stored in SHAPES:
        curve = enforced(stored).horizon.planning_reserve_kwh
        assert max(curve) <= CAPACITY + 1e-9, (stored, max(curve), CAPACITY)


def test_the_frozen_floor_never_sits_below_the_hard_floor() -> None:
    """**The direction that matters for safety is retained.**

    ``max(floor, hard_floor)`` was kept when the provenance changed. The hard floor
    alone would have been worse -- it discards the reason the plan chose a level,
    which is exactly what a frozen claim is for -- but it remains the lower bound,
    and the enforced curve is at or above it before that maximum is ever applied.
    """
    for stored in SHAPES:
        outcome = enforced(stored)
        hard = outcome.desired.terminal_floor_kwh
        for index, value in enumerate(outcome.horizon.planning_reserve_kwh):
            assert value >= hard - 1e-9, (stored, index, value, hard)


def test_the_frozen_floor_is_not_simply_the_hard_floor_repeated() -> None:
    """**The vacuity gate: an observable claim, not a tautology.**

    If the enforced curve merely echoed the configured floor, "carries the enforced
    curve" and "carries the hard floor" would be the same statement and every test
    above would hold against either. It does not: the curve is quantised up to the
    next whole bucket and carries the uncertainty margin, so it stands measurably
    clear of the floor -- 4.480 against 4.216 on this shape.

    Recorded as a strict inequality rather than as that pair of numbers, because
    what must survive is that the two are distinguishable, not how far apart a
    particular lattice happens to put them.
    """
    outcome = enforced(9.936)
    hard = outcome.desired.terminal_floor_kwh
    curve = outcome.horizon.planning_reserve_kwh

    assert min(curve) > hard + 1e-6, (min(curve), hard)


def test_the_deciding_module_cannot_reach_the_autonomy_figure_at_all() -> None:
    """**Why the correction is small: the autonomy curve was never in this path.**

    beta.31 removed it from every decision after measuring it immobilising 96.9 %
    of the pack, and what is left is a diagnostic. The solver's own outcome exposes
    no autonomy projection -- there is nothing here to read by accident -- so the
    only place the figure could re-enter is the coordinator's claim assembly, which
    is the one line this release moved.

    Asserted on the public surface, so it fails if a future change re-exposes it.
    """
    outcome = enforced(9.936)

    assert not hasattr(outcome.desired, "reserve_projection")
    assert not hasattr(outcome, "reserve_projection")
    # The one autonomy-derived figure Stage A still publishes is explicitly named
    # as an excess over the pack and reaches no decision.
    assert outcome.reserve_above_capacity_kwh >= 0.0


# == the coordinator, which is the one line this release moved ==============


async def test_the_published_claim_carries_the_enforced_curve(
    hass, setup_integration, source_entities: None, frank
) -> None:
    """**6a through production, because that is where the field is filled.**

    Everything above is a property of the solver's own curve. The correction was in
    the coordinator: it read ``plan.reserve_projection`` -- the autonomy
    counterfactual -- into the per-interval map that becomes ``reserve_floor_kwh``
    on a frozen execution target, a field whose own contract reads *"Stage-A
    physical limits Stage B must honour, frozen with the schedule"*.

    So the assertion is made on the published payload: every target's floor must be
    a value the enforced curve actually contains, floored at the hard floor, and
    must not be a value only the autonomy curve could have produced. Reading the
    autonomy figure back in fails this even though nothing downstream compares it,
    which is the point of asserting provenance before something starts to.
    """
    from .test_beta33_campaign_wiring import planning_coordinator

    coordinator = await planning_coordinator(hass, setup_integration, frank)
    targets = list(coordinator.execution_targets)
    assert targets, "the fixture must publish targets for this to mean anything"

    economic = coordinator.data["economic"]
    enforced = {round(value, 2) for value in economic.horizon.planning_reserve_kwh}
    # The same two objects ``_execution_targets`` itself reads: the solve's horizon,
    # and the Stage-B plan that carries the autonomy projection.
    projection = coordinator.data["battery_plan"].reserve_projection
    hard_floor = projection.floor_energy_kwh if projection is not None else 0.0

    autonomy = {
        round(entry.required_dc_kwh, 2)
        for entry in (projection.intervals if projection is not None else ())
        if entry.required_dc_kwh is not None
    }
    # The vacuity gate: if the two curves agreed, this test would prove nothing.
    assert autonomy - enforced, (sorted(autonomy)[:4], sorted(enforced)[:4])

    # Two decimals, because that is the resolution the claim is serialised at.
    permitted = {round(max(value, hard_floor), 2) for value in enforced}
    permitted.add(round(hard_floor, 2))
    for target in targets:
        floor = target.get("reserve_floor_kwh")
        assert floor is not None, target.get("plan_id")
        assert round(floor, 2) in permitted, (
            target.get("plan_id"),
            floor,
            sorted(permitted)[:4],
        )
        assert floor >= hard_floor - 1e-9, (target.get("plan_id"), floor, hard_floor)
