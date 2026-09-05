"""beta.41 checked against quantities computed outside the model under test.

**Why this file exists.** Mutation testing found the limit of the sweeps in
``test_beta41_physical_energy.py``: almost every property of a solved plan is a
*self-consistency* property, and self-consistency survives breaking the state model.
Replace :func:`_physical_energy_kwh` with the identity and the recursion, the
forward walk, the terminal credit and both published endpoints all move together --
back to the beta.40 model exactly -- while every assertion of the form "the walk
closes onto the endpoint" still passes, because both sides moved.

So the assertions here are anchored on the one quantity the carry axis cannot
change: **``ambient_self_consumption_ac_kwh``, the exact household energy at the
meter**, computed per interval from load, production and price and never from the
lattice. A trajectory reconstructed from it is what the pack really does, and the
published trajectory has to match it.

The bound is one number and it is measured, not chosen: across all seven neutrality
shapes at six starting states apiece, the largest divergence between the published
trajectory and the exact meter-side walk is **1.72 carry steps (0.0568 kWh)**. It is
not zero because the pack suspends service when it has nothing left to give while
the meter-side figure is a function of the interval alone -- which is precisely what
``battery_state_quantisation_residual_kwh`` is published to reconcile. Two steps is
therefore a tight guard: the beta.40 model diverges by the whole of household
consumption, up to 4.5 kWh, which is over a hundred steps.
"""

from __future__ import annotations

from custom_components.alpha_ems_manager.const import (
    AMBIENT_CARRY_STEPS,
    ECONOMIC_ACTION_CHARGE,
)

from .beta34_shape import LIMITS
from .solve_cache import cached_solve_at
from .test_beta39_neutrality import SHAPES

#: Starting states swept per shape: empty, near-floor, mid, and full.
STARTS = (0.3, 4.5, 8.0, 12.0, 18.0, 21.0)

#: The measured divergence is 1.72 carry steps; two is the guard.
TOLERANCE_STEPS = 2.0


def plans():
    """Yield ``(label, outcome)`` across every shape and starting state.

    Availability is asserted rather than filtered: skipping unavailable solves is
    how an earlier draft of this suite gave false comfort, when excluding
    below-floor states made every survival horizon report
    ``economic_terminal_unreachable`` and the sweep quietly dropped them.
    """
    for name in sorted(SHAPES):
        kwargs = dict(SHAPES[name][0])
        for stored in STARTS:
            kwargs["stored"] = stored
            outcome = cached_solve_at(**kwargs).outcome
            assert outcome.available, f"{name}@{stored}: {outcome.unavailable_reason}"
            yield f"{name}@{stored}", outcome


def meter_side_dc(interval) -> float:
    """Return the exact household service at the pack, from the meter figure.

    The external anchor. ``ambient_self_consumption_ac_kwh`` is what the inverter
    delivered to the house, and every grid figure on the interval is split against
    it, so it is arithmetic rather than an estimate -- and it is computed from the
    interval's own load, production and price, never from the solver's state.
    """
    return interval.ambient_self_consumption_ac_kwh / LIMITS.discharge_efficiency


def exact_walk(plan) -> list[float]:
    """Return the trajectory the meter says the pack followed, interval by interval.

    ``count + 1`` entries: the level entering each interval, then the endpoint.
    """
    level = plan.intervals[0].start_energy_dc_kwh
    walked = [level]
    for entry in plan.intervals:
        level += entry.battery_delta_dc_kwh - meter_side_dc(entry)
        walked.append(level)
    return walked


def step_of(outcome) -> float:
    """Return one carry step for this solve."""
    return outcome.bucket_kwh / AMBIENT_CARRY_STEPS


# == 1. the published trajectory is the one the meter describes =============


def test_the_published_trajectory_matches_the_meter_side_walk() -> None:
    """**The invariant beta.41 delivers, stated against something external.**

    Until this release the recursion decided on a lattice level while the pack's
    real content was that level minus household service the lattice could not
    express, and on the live horizon the two were 5.4 kWh apart. They are one
    quantity now, and this is what says so: the trajectory the solver publishes and
    the trajectory the meter implies never separate by more than the quantisation
    the release documents.

    This is the assertion that fails first if the state ever splits again, and it
    fails loudly -- the old model's divergence is the whole of consumption.
    """
    for label, outcome in plans():
        plan = outcome.desired
        bound = TOLERANCE_STEPS * step_of(outcome)
        walked = exact_walk(plan)
        for position, entry in enumerate(plan.intervals):
            assert abs(entry.start_energy_dc_kwh - walked[position]) <= bound, (
                label,
                entry.index,
                entry.start_energy_dc_kwh,
                walked[position],
            )
        assert abs(plan.end_energy_dc_kwh - walked[-1]) <= bound, (
            label,
            plan.end_energy_dc_kwh,
            walked[-1],
        )


def test_the_meter_side_walk_never_goes_below_nothing() -> None:
    """A pack cannot deliver energy it does not hold, and the meter would say so.

    The rejected drain-corrected-terminal implementation projected an endpoint of
    -0.158 kWh on the live horizon while reporting no violation, which is the
    clearest possible statement that a trajectory is not physical.
    """
    for label, outcome in plans():
        bound = TOLERANCE_STEPS * step_of(outcome)
        for position, level in enumerate(exact_walk(outcome.desired)):
            assert level >= -bound, (label, position, level)


def test_a_zero_violation_plan_keeps_the_meter_side_walk_above_the_floor() -> None:
    """**Violations are scored on physical energy, so this is one statement.**

    beta.40 could publish ``violation_kwh: 0.0`` beside a reconstruction that ended
    below the floor, because the two were measured on different quantities. Scoring
    the reserve on the lattice level again would restore exactly that, and it would
    not disturb any self-consistency property -- it shows up here and nowhere else.

    Trajectories already below the floor when the horizon opens are exempt: they
    are there, and the reserve cannot retroactively forbid it.
    """
    for label, outcome in plans():
        plan = outcome.desired
        if plan.violation_kwh > 1e-9:
            continue
        bound = TOLERANCE_STEPS * step_of(outcome)
        start = plan.intervals[0].start_energy_dc_kwh
        floor = min(plan.terminal_floor_kwh, start)
        for position, level in enumerate(exact_walk(plan)):
            assert level >= floor - bound, (label, position, level, floor)


def test_the_endpoint_respects_the_floor_it_was_required_to_reach() -> None:
    """**The terminal condition, which is not the same as the in-horizon reserve.**

    Dropping it from the seed was measured to make an eight-interval fixture
    discharge from 11.0 to 9.0 kWh against an 11.0 floor while still reporting no
    violation, because ``violations`` penalises states *inside* the horizon and the
    endpoint is the state after the last one.
    """
    for label, outcome in plans():
        plan = outcome.desired
        if plan.violation_kwh > 1e-9:
            continue
        start = plan.intervals[0].start_energy_dc_kwh
        if start < plan.terminal_floor_kwh - 1e-9:
            continue
        bound = TOLERANCE_STEPS * step_of(outcome)
        assert plan.end_energy_dc_kwh >= plan.terminal_floor_kwh - bound, (
            label,
            plan.end_energy_dc_kwh,
            plan.terminal_floor_kwh,
        )
        assert exact_walk(plan)[-1] >= plan.terminal_floor_kwh - bound, label


def test_household_service_never_exceeds_what_the_pack_had_to_give() -> None:
    """The meter figure is bounded by the pack, not merely consistent with it.

    Total service over the horizon cannot exceed what the pack started with above
    nothing plus everything it took in, both measured at the DC side. An
    over-serving rounding rule shows up here as energy from nowhere.
    """
    for label, outcome in plans():
        plan = outcome.desired
        served = sum(meter_side_dc(entry) for entry in plan.intervals)
        gained = sum(max(0.0, entry.battery_delta_dc_kwh) for entry in plan.intervals)
        available = plan.intervals[0].start_energy_dc_kwh + gained
        assert served <= available + TOLERANCE_STEPS * step_of(outcome), (
            label,
            served,
            available,
        )


# == 2. coverage, bounded by what discretion did and what the house needs ===


def test_coverage_is_exactly_what_the_executed_plan_buys_beyond_discretion() -> None:
    """**Requirement 15, as arithmetic against a published baseline.**

    ``coverage_baseline_charge_ac_kwh`` is what the discretionary plan would have
    bought, published so this is checkable from outside. Coverage is the difference
    and nothing more: an attribution that claimed energy discretion would have
    bought anyway would exceed it, and one that ignored the reserve's precedence
    would too.
    """
    for label, outcome in plans():
        covered = sum(outcome.coverage_buy_attribution.values())
        extra = (
            outcome.desired.planned_charge_ac_kwh
            - outcome.coverage_baseline_charge_ac_kwh
        )
        assert covered <= max(0.0, extra) + 1e-9, (label, covered, extra)


def test_the_three_shares_never_overlap_on_any_run() -> None:
    """**Requirement 14. Precedence is settled before the arithmetic closes.**

    Safety is settled first because its quantity is not a matter of price, and
    coverage may claim only what is left. Checked as a bound rather than as a sum,
    because a sum can be satisfied by two shares that both grew while the third was
    clamped to zero -- which is what a broken precedence looks like.
    """
    for label, outcome in plans():
        for run in outcome.desired.runs:
            if run.action != ECONOMIC_ACTION_CHARGE:
                continue
            compelled = outcome.safety_buy_attribution.get(run.start_index, (0.0, 0.0))[
                0
            ]
            covered = outcome.coverage_buy_attribution.get(run.start_index, 0.0)
            assert compelled + covered <= run.battery_charge_ac_kwh + 1e-9, (
                label,
                run.start_index,
                compelled,
                covered,
                run.battery_charge_ac_kwh,
            )


def test_coverage_never_buys_more_than_the_household_will_consume() -> None:
    """**Requirement 2, and the guard against buying in order to hold.**

    Coverage exists to displace household import. The energy it buys therefore
    cannot exceed the household's own residual consumption over the horizon --
    computed here from the demand series itself, not from anything the solver
    published. Crediting terminal inventory inside the counterfactual would break
    this at once: the pack would fill and be paid for holding.
    """
    for label, outcome in plans():
        residual = sum(
            max(
                0.0,
                (demand.baseline_kwh or 0.0) - (demand.pv_kwh or 0.0),
            )
            for demand in outcome.horizon.demands
        )
        covered = sum(outcome.coverage_buy_attribution.values())
        assert covered <= residual + 1e-9, (label, covered, residual)


def test_a_promoted_plan_always_saved_the_household_money() -> None:
    """**Requirement: coverage only where the cash saving is genuinely positive.**

    Promotion is not free -- it replaces the plan the user's own settings chose --
    so wherever a run is labelled coverage there must be a saving to show for it,
    measured in cash including the inventory each plan ends with.
    """
    for label, outcome in plans():
        if not outcome.coverage_buy_runs:
            continue
        assert outcome.coverage_saving_eur > 0.0, (label, outcome.coverage_saving_eur)
        assert (
            outcome.desired.planned_charge_ac_kwh
            > outcome.coverage_baseline_charge_ac_kwh + outcome.bucket_kwh
        ), label
