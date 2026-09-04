"""beta.41: one physical energy, swept rather than asserted on one fixture.

**What this file is for.** Until beta.41 the recursion transitioned on a lattice
bucket while the pack's real content was that bucket *minus* household service the
lattice could not express. The two were allowed to disagree by kilowatt-hours: on
the live 2026-09-03 horizon the optimiser decided on 9.75 kWh while the published
trajectory ended at 4.32, and because violations were evaluated on the level rather
than on the energy, a plan could report ``violation_kwh: 0.0`` while its own
reconstruction ended **below zero** and exported 3.94 kWh it did not have.

The recursion now carries that service in its state, so there is one physical
energy and every physical constraint reads it. These are the properties that
follow, checked across shapes and starting states rather than on a single replay --
a single fixture is how the old split survived three releases.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from custom_components.alpha_ems_manager.const import (
    AMBIENT_CARRY_STEPS,
    ECONOMIC_ACTION_CHARGE,
)

from .beta34_shape import LIMITS, solve_at
from .test_beta39_neutrality import SHAPES

#: Starting states swept per shape, spanning empty, near-floor, mid and full.
STARTS = (0.3, 4.5, 8.0, 12.0, 18.0, 21.0)

#: One carry step, the resolution at which household service is carried.
STEP = 0.2635230352303523 / AMBIENT_CARRY_STEPS


def plans():
    """Yield ``(label, plan)`` across every shape and starting state.

    **Availability is asserted, not filtered.** Skipping unavailable solves is how
    an earlier draft of this file gave false comfort: excluding below-floor states
    from the recursion made every survival horizon report
    ``economic_terminal_unreachable``, and a sweep that quietly dropped them said
    nothing was wrong.
    """
    for name in sorted(SHAPES):
        kwargs = dict(SHAPES[name][0])
        for stored in STARTS:
            kwargs["stored"] = stored
            outcome = solve_at(**kwargs).outcome
            assert outcome.available, f"{name}@{stored}: {outcome.unavailable_reason}"
            yield f"{name}@{stored}", outcome


def ambient_dc(interval) -> float:
    """Return the household service at the pack, **exact, meter-side**."""
    return interval.ambient_self_consumption_ac_kwh / LIMITS.discharge_efficiency


def state_dc(interval) -> float:
    """Return the DC energy the solver's state actually moved by."""
    return interval.battery_state_service_dc_kwh


# == 1-2. the energy is real, and it respects the floor =====================


def test_physical_energy_is_never_negative() -> None:
    """**Invariant 1.** A pack cannot hold less than nothing.

    The rejected drain-corrected-terminal implementation reported an endpoint of
    -0.158 kWh on the live horizon, which is the clearest possible statement that
    the trajectory was not physical.
    """
    for label, outcome in plans():
        worst = min(e.start_energy_dc_kwh for e in outcome.desired.intervals)
        assert worst >= -1e-9, (label, worst)
        assert outcome.desired.end_energy_dc_kwh >= -1e-9, label


def test_a_feasible_plan_never_plans_below_the_hard_floor() -> None:
    """**Invariant 2, and it is now a property of the state.**

    States below the floor are unreachable in the recursion rather than merely
    discouraged, so a plan reported as feasible cannot contain one. Trajectories
    that *start* below the floor are exempt: they are already there, and the
    quantised seed may sit one bucket lower still.
    """
    for label, outcome in plans():
        plan = outcome.desired
        if plan.violation_kwh > 0.0:
            continue
        start = plan.intervals[0].start_energy_dc_kwh
        if start < plan.terminal_floor_kwh - 1e-9:
            continue
        worst = min(e.start_energy_dc_kwh for e in plan.intervals)
        assert worst >= plan.terminal_floor_kwh - STEP - 1e-9, (label, worst)


def test_no_zero_violation_plan_hides_a_floor_breach() -> None:
    """**Invariant 3, and this is the one beta.40 could not have passed.**

    Violations are computed on physical energy now, so ``violation_kwh == 0`` and
    "the trajectory stayed above the floor" are the same statement rather than two
    that happened to be published side by side.
    """
    for label, outcome in plans():
        plan = outcome.desired
        if plan.violation_kwh > 1e-9:
            continue
        start = plan.intervals[0].start_energy_dc_kwh
        floor = min(plan.terminal_floor_kwh, start)
        for entry in plan.intervals:
            assert entry.start_energy_dc_kwh >= floor - STEP - 1e-9, (
                label,
                entry.index,
                entry.start_energy_dc_kwh,
            )


# == 4-6. nothing is credited, served or sold that is not there =============


def test_the_terminal_credit_is_bounded_by_the_energy_that_is_there() -> None:
    """**Invariant 4.** The credit prices inventory, so it cannot exceed it.

    Checked as a bound on energy rather than on euros: whatever rate the terminal
    rule applies, it may only apply it to energy the pack actually ends holding
    above the floor.
    """
    for label, outcome in plans():
        plan = outcome.desired
        terminal = plan.terminal_value
        if terminal is None or plan.edge_value_eur <= 0.0:
            continue
        above = max(0.0, plan.end_energy_dc_kwh - plan.terminal_floor_kwh)
        ceiling = terminal.credit_eur(plan.end_energy_dc_kwh, plan.terminal_floor_kwh)
        assert plan.edge_value_eur <= ceiling + 1e-9, (label, plan.edge_value_eur)
        if above <= 0.0:
            assert plan.edge_value_eur <= 1e-9, (label, above, plan.edge_value_eur)


def test_household_service_never_exceeds_what_the_pack_can_give() -> None:
    """**Invariant 5.** Service is bounded by the room above the floor.

    It was not: while service cost almost nothing and moved nothing, the same
    kilowatt-hour could be served every interval for ever.
    """
    for label, outcome in plans():
        plan = outcome.desired
        for entry in plan.intervals:
            served = state_dc(entry)
            assert served <= max(0.0, entry.start_energy_dc_kwh) + STEP + 1e-9, (
                label,
                entry.index,
                served,
            )


def test_export_cannot_draw_on_energy_the_pack_does_not_hold() -> None:
    """**Invariant 6.** Everything leaving the pack comes out of its inventory.

    Summed over the horizon rather than per interval, because production absorbed
    in one interval may legitimately be exported in another, and stated wholly in
    **DC** using the plan's own lattice deltas.

    Reconstructing the DC side from the published AC energies would need an
    efficiency, and the one to use is not the configured figure:
    ``PhysicsTable`` records ``charge_dc_per_ac`` as *measured* from the clamp
    rather than derived, so the two differ. An earlier draft of this test divided
    by the configured value and reported a 0.05 kWh conservation breach that was
    entirely its own arithmetic.
    """
    for label, outcome in plans():
        plan = outcome.desired
        start = plan.intervals[0].start_energy_dc_kwh
        put_in = sum(max(0.0, e.battery_delta_dc_kwh) for e in plan.intervals)
        took_out = sum(-min(0.0, e.battery_delta_dc_kwh) for e in plan.intervals)
        served = sum(state_dc(e) for e in plan.intervals)
        assert took_out + served <= start + put_in + 1e-6, (
            label,
            took_out,
            served,
            start,
            put_in,
        )
        # And the closing balance is exact, which is the same statement said
        # forwards.
        assert start + put_in - took_out - served == pytest.approx(
            plan.end_energy_dc_kwh, abs=1e-9
        ), label


# == 7-9. one quantity, and everybody agrees on it =========================


def test_the_energy_balance_closes_quarter_by_quarter() -> None:
    """**Invariant 7, and it is the load-bearing one.**

        start + decided delta - household service = next start

    to machine precision. The published service is what the state actually moved,
    not the unrounded figure the model asked for, which is what makes this exact
    rather than approximate.
    """
    for label, outcome in plans():
        entries = outcome.desired.intervals
        for earlier, later in pairwise(entries):
            closing = (
                earlier.start_energy_dc_kwh
                + earlier.battery_delta_dc_kwh
                - state_dc(earlier)
            )
            assert closing == pytest.approx(later.start_energy_dc_kwh, abs=1e-9), (
                label,
                earlier.index,
                closing,
                later.start_energy_dc_kwh,
            )


def test_the_walk_ends_where_the_recursion_decided_to_end() -> None:
    """**Invariant 8, and the last interval closes onto the endpoint too.**"""
    for label, outcome in plans():
        plan = outcome.desired
        last = plan.intervals[-1]
        closing = last.start_energy_dc_kwh + last.battery_delta_dc_kwh - state_dc(last)
        assert closing == pytest.approx(plan.end_energy_dc_kwh, abs=1e-9), label


def test_the_two_endpoint_names_are_one_quantity() -> None:
    """**Invariant 9. The 9.75-against-4.32 split is gone by construction.**

    beta.40 had to publish both with a basis apiece because they were genuinely
    different: the level the recursion decided, and the energy the pack would hold.
    They are the same number now, and this is what would fail first if the state
    ever split again.
    """
    for label, outcome in plans():
        plan = outcome.desired
        assert plan.edge_energy_kwh == pytest.approx(
            plan.end_energy_dc_kwh, abs=1e-12
        ), label


# == 10. economics may not paper over physics ==============================


def test_no_economic_term_can_buy_its_way_past_a_violation() -> None:
    """**Invariant 10, restated as the lexicographic ordering it relies on.**

    Between two plans on the same horizon the one with the smaller violation wins
    whatever it costs. Checked by making energy absurdly expensive and requiring
    the violation not to rise.
    """
    for name in sorted(SHAPES):
        kwargs = dict(SHAPES[name][0])
        kwargs["stored"] = 0.3
        cheap = solve_at(**{**kwargs, "price_fn": lambda index: 0.02}).outcome
        dear = solve_at(**{**kwargs, "price_fn": lambda index: 9.00}).outcome
        if not (cheap.available and dear.available):
            continue
        assert dear.desired.violation_kwh <= cheap.desired.violation_kwh + 1e-9, name


def test_a_charge_run_always_raises_the_physical_trajectory() -> None:
    """Charging puts energy in the pack, and the state says so.

    Trivial to state and it was not true before: a charge raised the lattice level
    while the reported walk could fall through it, because the two were different
    quantities moving for different reasons.
    """
    for label, outcome in plans():
        plan = outcome.desired
        for run in plan.runs:
            if run.action != ECONOMIC_ACTION_CHARGE:
                continue
            if run.battery_charge_ac_kwh <= 0.0:
                continue
            inside = [
                e for e in plan.intervals if run.start_index <= e.index <= run.end_index
            ]
            assert inside, label
            gained = sum(e.battery_delta_dc_kwh for e in inside)
            assert gained > 0.0, (label, run.start_index, gained)


# == 11-13. the two service quantities, and what reconciles them ===========


def test_the_meter_side_service_is_exact_and_not_quantised() -> None:
    """**The beta.39 accounting guarantee, kept whole.**

    ``ambient_self_consumption_ac_kwh`` is household energy at the meter. Every
    grid figure on the interval is split against it, so the no-battery
    counterfactual is exact arithmetic rather than an estimate -- which is what
    beta.39 claimed and what beta.41 must not quietly weaken by substituting the
    solver's quantised state movement for it.
    """
    for label, outcome in plans():
        for entry in outcome.desired.intervals:
            assert entry.no_battery_import_kwh == pytest.approx(
                entry.idle_import_kwh + entry.ambient_self_consumption_ac_kwh,
                abs=1e-12,
            ), (label, entry.index)


def test_the_two_service_quantities_reconcile_through_the_residual() -> None:
    """**Invariant: exact household service = state movement + residual.**

    The pack's state moves in whole carry steps and the household does not, so the
    difference is real and is published rather than absorbed. This is the identity
    that makes the two figures one model instead of two accounts.
    """
    for label, outcome in plans():
        for entry in outcome.desired.intervals:
            assert ambient_dc(entry) == pytest.approx(
                entry.battery_state_service_dc_kwh
                + entry.battery_state_quantisation_residual_kwh,
                abs=1e-12,
            ), (label, entry.index)


def test_the_quantisation_residual_is_bounded_by_one_carry_step() -> None:
    """**Bounded, and that bound is the whole claim.**

    An unbounded residual would be the beta.40 defect again in miniature: a
    difference between what the solver believes and what the pack holds, growing
    without anything reporting it. One carry step is the most a single interval
    can be out by, and this measures it rather than asserting it.

    **Signed, and both signs are meaningful.** Positive is service the interval
    incurred that the state could not express and deferred; negative is an earlier
    interval's arrears being paid off now, because the quantisation is cumulative.
    That is why the outstanding total does not drift, which a same-signed residual
    would.

    **The running bound is one step plus one interval's service, not one step.**
    Two different things end up in this term. Rounding defers at most a step. But
    where the pack cannot reach the floor and still serve, the service does not
    happen at all -- the household imports instead -- and that whole interval's
    worth is deferred rather than a fraction of it. Both are bounded and neither
    accumulates, which is the property that matters; claiming the tighter bound
    would have been claiming the second case does not exist.
    """
    for label, outcome in plans():
        entries = outcome.desired.intervals
        largest = max(
            (ambient_dc(entry) for entry in entries),
            default=0.0,
        )
        outstanding = 0.0
        for entry in entries:
            residual = entry.battery_state_quantisation_residual_kwh
            assert abs(residual) <= STEP + 1e-12, (label, entry.index, residual)
            outstanding += residual
            assert abs(outstanding) <= STEP + largest + 1e-9, (
                label,
                entry.index,
                outstanding,
            )


def test_the_residual_reaches_no_decision() -> None:
    """It is observability. Nothing in the solver may read it back.

    Asserted structurally rather than by inspection: the term appears in the
    interval it is measured on and nowhere the recursion, the reserve or the
    export permission can see.
    """
    import pathlib

    source = pathlib.Path("custom_components/alpha_ems_manager/economic.py").read_text(
        encoding="utf-8"
    )
    solver = source[source.index("def solve(") : source.index("def _ambient_walk(")]
    assert "battery_state_quantisation_residual_kwh" not in solver
