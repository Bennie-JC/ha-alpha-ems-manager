"""Tomorrow's prices are absent, and the planner says so rather than inventing them.

**Captured from the reference installation at 01:00, beta.41 in Live.** Today's
prices were complete, tomorrow's had not been published, and the plan reported a
91-interval horizon ``limited_by: prices`` with a ``truncated`` reserve basis, no
bridge requirement, no Safety Buy and no reserve violation. That is correct
behaviour and this file freezes it.

**It is a regression invariant, not a planner change.** Nothing in the DP, the
reserve, the categories or the terminal value is touched here; these tests read a
solve and assert what it did.

The failure this guards against is not a crash. It is the family of quiet repairs
that look helpful: copying today onto tomorrow, extrapolating the evening curve,
zero-filling, or treating unknown energy as free. Every one of them would let the
optimiser plan across data nobody published, and the resulting plan would be
confidently wrong in a way no downstream figure could reveal -- the prices would
look real because they *are* real numbers.

**What is deliberately not asserted**: that the economic quantities stay the same
once tomorrow arrives. A newly published day is new information, and a planner that
ignored it would be a worse planner. What must hold across that boundary is that the
horizon *extends* rather than being rewritten, and that nothing already executed is
revisited -- which is the opened-row authority ``test_beta38_opened_row_authority``
holds, and the no-catch-up rule ``test_beta29_quarter_authority_lifecycle`` holds.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.const import RESERVE_HORIZON_TRUNCATED
from custom_components.alpha_ems_manager.economic import IntervalDemand
from custom_components.alpha_ems_manager.reserve import build_reserve

from .beta34_shape import (
    FLOOR,
    LIMITS,
    export_of,
    load_29aug,
    price_29aug,
    pv_29aug,
    solve_at,
)

#: The captured refresh: 01:00, so the quarter in flight is index 4 and the head is
#: 5. Today runs to 96, tomorrow is unpublished, and the demand forecast -- which is
#: a learned diurnal profile and needs no price -- runs on to 192.
HEAD = 5
TODAY_END = 96
TOMORROW_END = 192

#: The published figure, and the reason this file exists rather than a comment.
CAPTURED_HORIZON_INTERVALS = 91


def unpriced_tomorrow(index: int) -> float | None:
    """Return today's import price, and ``None`` for every unpublished interval."""
    return None if index >= TODAY_END else price_29aug(index)


def unpriced_tomorrow_export(index: int) -> float | None:
    """Return today's export price, and ``None`` for every unpublished interval."""
    return None if index >= TODAY_END else export_of(price_29aug(index))


def _truncated(stored: float = 8.0):
    """Solve the captured shape with tomorrow unpublished."""
    return solve_at(
        head=HEAD,
        end=TOMORROW_END,
        stored=stored,
        price_fn=unpriced_tomorrow,
        export_fn=unpriced_tomorrow_export,
    ).outcome


def _complete(stored: float = 8.0):
    """Solve the same shape with tomorrow published, for comparison."""
    return solve_at(head=HEAD, end=TOMORROW_END, stored=stored).outcome


# ===========================================================================
# the captured case, figure for figure
# ===========================================================================


def test_the_captured_refresh_reproduces_exactly() -> None:
    """**Every number the 01:00 payload published, from the production path.**

    Asserted together rather than one per test: they were captured together, and a
    fixture that reproduced four of the five would be describing a different
    refresh.
    """
    outcome = _truncated()

    assert outcome.horizon.intervals == CAPTURED_HORIZON_INTERVALS
    assert outcome.horizon.limited_by == "prices"
    assert outcome.bridge_kwh_now == 0.0
    assert outcome.safety_buy_ac_kwh == 0
    assert outcome.desired.violation_kwh == 0.0


def test_the_horizon_stops_at_the_last_published_interval() -> None:
    """The end of the last continuously-known priced civil day, and not one past it.

    ``91`` is not a magic number: it is ``96 - 5``, the head to the end of today.
    Both ends are asserted, because a horizon of the right *length* starting in the
    wrong place would pass a count alone.
    """
    outcome = _truncated()
    intervals = outcome.desired.intervals

    assert intervals[0].index == HEAD
    assert intervals[-1].index == TODAY_END - 1
    assert len(intervals) == TODAY_END - HEAD == CAPTURED_HORIZON_INTERVALS


def test_no_interval_beyond_the_published_day_is_planned() -> None:
    """**The Stage-A half of "no catch-up".**

    An opportunity outside the known horizon is not an opportunity the planner has
    seen, so no row may exist for it. The demand forecast runs a further 96
    intervals and is deliberately *not* enough on its own: energy without a price
    cannot be traded, only survived.
    """
    outcome = _truncated()

    assert not [i for i in outcome.desired.intervals if i.index >= TODAY_END]
    assert not [r for r in outcome.desired.runs if r.end_index >= TODAY_END]


def test_the_reserve_basis_is_truncated_and_says_why_it_is_not_evidence() -> None:
    """The captured payload reported ``truncated``, and it did so for its own reason.

    **This field is not a price signal, and reading it as one would be a mistake
    the second assertion here exists to prevent.** The basis is ``truncated``
    whenever the final drawdown window does not close inside the horizon, which is
    true of the complete two-day shape as well -- the evening draw simply continues
    past the last interval either way. ``limited_by`` is the field that says prices
    ran out; this one says the reserve figure is a lower bound.
    """
    for end in (TODAY_END, TOMORROW_END):
        demands = tuple(
            IntervalDemand(index=i, baseline_kwh=load_29aug(i), pv_kwh=pv_29aug(i))
            for i in range(HEAD, end)
        )
        projection = build_reserve(
            limits=LIMITS, floor_energy_kwh=FLOOR, demands=demands
        )
        assert projection.horizon_basis == RESERVE_HORIZON_TRUNCATED, end


# ===========================================================================
# nothing is invented
# ===========================================================================


def test_every_priced_interval_carries_the_price_that_was_published_for_it() -> None:
    """**Not synthesised, not extrapolated, not copied from today.**

    Each of those would produce a horizon full of plausible numbers, and a plan
    built on them would look exactly like a good plan. So the check is not that the
    prices are "reasonable" -- it is that every one of them is the value the source
    published for that interval, compared against the fixture's own price function,
    which the production code never sees.
    """
    outcome = _truncated()

    for interval in outcome.desired.intervals:
        assert interval.import_price_eur_kwh == pytest.approx(
            price_29aug(interval.index), abs=1e-12
        ), interval.index
        assert interval.export_price_eur_kwh == pytest.approx(
            export_of(price_29aug(interval.index)), abs=1e-12
        ), interval.index


def test_an_unpublished_interval_is_never_valued_at_zero() -> None:
    """Zero is the most dangerous fill of all, and it is worth its own test.

    A zero import price makes buying free, so a zero-filled tomorrow would not
    merely be wrong -- it would look like the best buying opportunity the horizon
    has ever contained, and the plan would defer everything to it.

    Proved structurally: the horizon simply has no interval there. There is nothing
    to value, which is the only honest answer.
    """
    outcome = _truncated()
    planned = {interval.index for interval in outcome.desired.intervals}

    assert planned.isdisjoint(range(TODAY_END, TOMORROW_END))
    assert outcome.actionable_interval_count == CAPTURED_HORIZON_INTERVALS


# ===========================================================================
# the reserve is unmoved -- missing prices are not a hazard
# ===========================================================================


#: Starting states spanning the whole range the reserve responds over: the first
#: three are infeasible at this floor and the last four are comfortable. Both ends
#: matter, because an invariant proved only where the answer is zero is not proved.
SWEEP = (0.3, 1.2, 2.5, 4.0, 6.0, 8.0, 12.0, 18.0)


@pytest.mark.parametrize("stored", SWEEP)
def test_a_missing_tomorrow_never_moves_the_safety_buy(stored: float) -> None:
    """**The invariant that matters most, and it holds exactly rather than
    approximately.**

    A Safety Buy is compelled energy: the amount the reserve leaves no discretion
    over. If truncating the horizon could inflate it, then every evening before the
    next day's prices are published would buy energy the plant did not need -- at
    whatever the evening peak happened to cost -- and the purchase would be filed
    under the one category that is exempt from every economic gate.

    It cannot, and the reason is structural: the compelled quantity comes from the
    reachability recursion, which is bounded by ``grid_credit_intervals`` -- the
    count of jointly-known intervals. Truncation reduces what may be *credited*, and
    a shorter credit window can only ever leave the requirement unchanged or lower
    it. The two solves agree to the last decimal across the sweep.
    """
    truncated, complete = _truncated(stored), _complete(stored)

    assert truncated.safety_buy_ac_kwh == pytest.approx(
        complete.safety_buy_ac_kwh, abs=1e-9
    )
    assert truncated.desired.violation_kwh == pytest.approx(
        complete.desired.violation_kwh, abs=1e-9
    )


def test_the_sweep_actually_reaches_a_compelled_purchase() -> None:
    """The witness, so the parametrised test above is not eight assertions of zero.

    An invariant demonstrated only where both sides are zero is a statement about
    the fixture, not about the code.
    """
    bought = [s for s in SWEEP if _truncated(s).safety_buy_ac_kwh > 0.0]
    violated = [s for s in SWEEP if _truncated(s).desired.violation_kwh > 0.0]

    assert len(bought) >= 4, bought
    assert violated, "no starting state exercises an infeasible reserve"


def test_reserve_feasibility_still_outranks_economics_inside_the_known_horizon() -> (
    None
):
    """Truncation does not demote the lexicographic pair.

    At 0.3 kWh the reserve cannot be met from the known horizon at any price, and
    the objective is ``(violation, cost)`` -- so the plan buys what it is compelled
    to buy and reports the residual violation rather than pricing its way out of it.
    A truncated horizon must not turn that into an economic decision.
    """
    outcome = _truncated(0.3)

    assert outcome.desired.violation_kwh > 0.0
    assert outcome.safety_buy_ac_kwh > 0.0
    # Compelled, not chosen: the purchase exists because the reserve demanded it,
    # and it is reported under the category that says so.
    assert outcome.safety_buy_ac_kwh == pytest.approx(_complete(0.3).safety_buy_ac_kwh)


# ===========================================================================
# planning continues, and the terminal value moves with the edge
# ===========================================================================


def test_economics_stay_active_over_the_horizon_that_is_known() -> None:
    """**A truncated horizon is a shorter plan, not a suspended one.**

    Refusing to trade until tomorrow is published would be the other way to be
    wrong, and it would be invisible: an installation that simply did nothing on
    the evenings before a late publication would look like an installation with
    nothing worth doing.

    The known day contains both a charge and an export at prices that justify them,
    and both survive the truncation.
    """
    outcome = _truncated()
    desired = outcome.desired

    assert sum(1 for run in desired.runs if run.battery_charge_ac_kwh > 0.0) >= 1
    assert sum(1 for run in desired.runs if run.grid_export_kwh > 0.0) >= 1
    assert sum(i.grid_import_kwh for i in desired.intervals) > 0.0
    assert sum(i.grid_export_kwh for i in desired.intervals) > 0.0


def test_the_terminal_value_is_evaluated_at_the_edge_the_horizon_actually_has() -> None:
    """The credit belongs to the last *known* interval, not to a nominal midnight.

    Valuing the pack at an edge the plan does not reach would price energy against
    a moment the optimiser cannot act in -- and it would do so silently, because a
    terminal credit is a single number with nothing beside it to disagree with.

    The two horizons end at different instants and therefore carry different
    creditable quantities, which is the observable consequence.
    """
    truncated, complete = _truncated(), _complete()

    assert truncated.edge_creditable_kwh != pytest.approx(
        complete.edge_creditable_kwh
    ), "the two edges must differ, or this test is comparing one horizon with itself"
    assert truncated.edge_value_eur_per_kwh is not None
    assert truncated.edge_creditable_kwh > 0.0


# ===========================================================================
# and when tomorrow arrives
# ===========================================================================


def test_a_published_tomorrow_extends_the_horizon_rather_than_replacing_it() -> None:
    """Stage A may legitimately replan the future once it can see further.

    **What is asserted is the extension, not the economics.** A newly published day
    is new information and the plan is allowed -- expected -- to change over it. What
    may not change is the *span*: the intervals the truncated horizon covered are
    still covered, in the same positions, and the longer horizon is a superset.
    Anything else would mean the arrival of tomorrow's prices had moved today.
    """
    truncated, complete = _truncated(), _complete()

    short = [interval.index for interval in truncated.desired.intervals]
    long = [interval.index for interval in complete.desired.intervals]

    assert long[: len(short)] == short
    assert len(long) > len(short)
    assert complete.horizon.limited_by == "complete"


# ===========================================================================
# a hole, not merely a missing tail
# ===========================================================================


def partial_publication(index: int) -> float | None:
    """Return prices with a ten-interval hole, and today's prices either side."""
    return None if 50 <= index < 60 else price_29aug(index)


def partial_publication_export(index: int) -> float | None:
    """Return the matching export prices, with the same hole."""
    return None if 50 <= index < 60 else export_of(price_29aug(index))


def test_a_hole_ends_the_horizon_rather_than_being_stepped_over() -> None:
    """**The distinction the module's own docstring turns on, and the mutation table
    is what showed it was untested here.**

    ``H1`` replaces the break with a continue -- the horizon skips an unpriced
    interval and carries on. Against a missing *tail* that is indistinguishable from
    stopping, because everything after the hole is missing too, so the mutation
    survived while every assertion above passed.

    A partial publication is the case that separates them, and it is a real one: a
    source that returns some of a day is more likely than one that returns none of
    it. Knowing the first and third hour of an evening is not knowing the evening,
    and a plan that bridged the gap would be optimising a discharge across a price
    nobody quoted.

    So the horizon is the contiguous *prefix*, and it ends where the prefix does --
    at 45 intervals here, not at the 85 that are individually priced.
    """
    outcome = solve_at(
        head=HEAD,
        end=TOMORROW_END,
        stored=8.0,
        price_fn=partial_publication,
        export_fn=partial_publication_export,
    ).outcome
    indices = [interval.index for interval in outcome.desired.intervals]

    assert outcome.horizon.limited_by == "prices"
    assert indices == list(range(HEAD, 50))
    assert len(indices) == 45
    # Contiguous by construction, and asserted rather than assumed: a horizon that
    # had stepped over the hole would still be sorted and would still start here.
    assert indices == list(range(indices[0], indices[-1] + 1))
    assert outcome.actionable_interval_count == 45


def test_the_hole_and_the_missing_tail_are_the_same_rule() -> None:
    """One rule, two shapes, and neither is a special case of the other in the code.

    ``build_horizon`` stops at the first interval any input cannot answer for. That
    the reference installation happens to lose a whole trailing day is a property of
    its price source, not of the planner.
    """
    tail = solve_at(
        head=HEAD,
        end=TOMORROW_END,
        stored=8.0,
        price_fn=unpriced_tomorrow,
        export_fn=unpriced_tomorrow_export,
    ).outcome
    hole = solve_at(
        head=HEAD,
        end=TOMORROW_END,
        stored=8.0,
        price_fn=partial_publication,
        export_fn=partial_publication_export,
    ).outcome

    assert tail.horizon.limited_by == hole.horizon.limited_by == "prices"
    assert tail.horizon.intervals == 91
    assert hole.horizon.intervals == 45
