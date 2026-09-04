"""beta.34: the export permission, in the frame it is actually consumed in.

Three defects, one solve pass, and every one of them invisible to the beta.32
suite because that suite starts every horizon at index zero. At index zero an
absolute day index and an offset from the head are the same number.

The live installation ran two days of real horizon on 2026-08-29 and published
``survival_window_quarters: 132`` on a 135-interval horizon whose head was
interval 57, and ``export_floor_dc_kwh: 23.09`` on a **21.6 kWh pack** -- a
requirement 6.9 % above the largest energy the battery can ever hold, applied as
a hard energy test whose maximum *is* that ceiling. ``export_free`` read
``[false] * 12``: not a protection, a prohibition.

What is asserted here:

* the window is an **offset**, never an index, on a horizon that can tell them
  apart;
* the published floor is **clamped** to capacity, and the raw requirement
  survives beside it as evidence;
* an unsatisfiable requirement leaves the **price** test standing alone rather
  than vetoing unconditionally;
* the gate cost is a **cash** figure a reader can recompute, and its sign is
  reported rather than assumed;
* two complete cycles are selected on a two-day horizon where both clear;
* extending the horizon does not change what the plan does **today**.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.const import (
    SURVIVAL_WINDOW_ACTIONABLE_PREFIX,
    SURVIVAL_WINDOW_PLAN_CAMPAIGN,
)
from custom_components.alpha_ems_manager.economic import survival_window_end

from .beta34_shape import CAPACITY, solve_at

# The two states the live captures were taken in, in absolute quarter indices.
HEAD_1300 = 52
HEAD_1400 = 57
STORED_1345 = 10.97
STORED_1400 = 11.75


# ===========================================================================
# 1. the frame: an offset, not an index
# ===========================================================================


def test_the_window_is_an_offset_from_the_head_not_a_day_index() -> None:
    """**The bug, on the only horizon that can see it.**

    ``EconomicInterval.index`` is the civil-day index -- 0-95 today, 96-191
    tomorrow -- and ``survival_curves`` consumes the window as ``range(position,
    stop)`` into a curve of length ``horizon.intervals``. Returning
    ``campaign.start_index`` unconverted therefore returned the campaign's
    *clock position* where an *offset* was expected.

    On a two-day horizon headed at 57 with a refill at 132 the correct offset is
    75 and the beta.33 answer was 132: a 33-hour protection window instead of a
    19-hour one.

    *Mutation: restore ``return campaign.start_index`` and this fails.*
    """
    solved = solve_at(head=HEAD_1400, end=192, stored=STORED_1400)
    outcome = solved.outcome

    window = outcome.survival_window_end
    # The whole point: bounded by the actionable prefix, in the prefix's own
    # units. 132 is not a legal answer to this question on any horizon.
    assert window <= outcome.actionable_interval_count
    assert window <= len(outcome.export_floor_kwh)
    assert window >= 0


def test_the_offset_is_measured_from_the_head_that_is_passed_in() -> None:
    """Directly on the function, so the conversion is not merely implied.

    Solved through production, then re-asked with the head moved. A campaign at
    absolute 132 is 75 intervals away from a head at 57 and 32 away from a head
    at 100; an implementation returning the index answers 132 to both.
    """
    solved = solve_at(head=HEAD_1400, end=192, stored=STORED_1400)
    ungated = solved.outcome.ungated
    assert ungated is not None and ungated.available

    charge = [
        campaign
        for campaign in ungated.campaigns
        if campaign.direction == "charge" and campaign.sell_announcement_material
    ]
    if not charge:  # pragma: no cover - the shape is pinned to produce one
        pytest.skip("this shape produced no material charge campaign")
    start = charge[0].start_index

    near, near_basis = survival_window_end(
        ungated, actionable_intervals=200, head_index=start - 10
    )
    far, far_basis = survival_window_end(
        ungated, actionable_intervals=200, head_index=start - 40
    )

    assert near == 10
    assert far == 40
    assert near_basis == far_basis == SURVIVAL_WINDOW_PLAN_CAMPAIGN
    # And a head at or past the campaign is a zero-length window, never negative
    # and never the index.
    at, _ = survival_window_end(ungated, actionable_intervals=200, head_index=start)
    past, _ = survival_window_end(
        ungated, actionable_intervals=200, head_index=start + 5
    )
    assert at == 0
    assert past == 0


def test_the_actionable_prefix_branch_is_a_count_and_stays_one() -> None:
    """The other branch was always a count. It must not become an index either."""
    solved = solve_at(head=HEAD_1300, end=96, stored=18.0)
    outcome = solved.outcome

    assert outcome.survival_window_basis == SURVIVAL_WINDOW_ACTIONABLE_PREFIX
    assert outcome.survival_window_end == outcome.actionable_interval_count


# ===========================================================================
# 2. the clamp: a requirement above capacity is evidence, not a veto
# ===========================================================================


@pytest.mark.parametrize("stored", [6.0, 10.97, 14.0, 18.0])
def test_the_published_floor_never_exceeds_what_the_pack_can_hold(
    stored: float,
) -> None:
    """A floor above the ceiling is a test no bucket in the lattice can pass.

    ``max(energies) == ceiling_kwh``, so through beta.33 an over-capacity floor
    forbade every caused export at that interval for every reachable bucket, in
    every direction, permanently.

    *Mutation: drop the ``ceiling_kwh`` argument to ``survival_curves`` and this
    fails.*
    """
    outcome = solve_at(head=HEAD_1400, end=192, stored=stored).outcome

    assert outcome.export_floor_kwh
    for index, value in enumerate(outcome.export_floor_kwh):
        assert value <= CAPACITY + 1e-9, index


def test_the_raw_requirement_survives_beside_the_clamped_one() -> None:
    """Clamping must not destroy the evidence that the requirement was absurd.

    A reader looking at ``21.60`` alone cannot tell a pack that genuinely needs
    all of itself from one that was asked for half as much again. Both are
    published, and the clamp is exactly ``min``.
    """
    outcome = solve_at(head=HEAD_1400, end=192, stored=STORED_1400).outcome

    raw = outcome.export_floor_raw_kwh
    clamped = outcome.export_floor_kwh
    assert len(raw) == len(clamped)
    for index, (source, value) in enumerate(zip(raw, clamped, strict=True)):
        assert value == pytest.approx(min(source, CAPACITY)), index
        assert source >= value - 1e-9, index


# ===========================================================================
# 3. the priced fallback
# ===========================================================================


def test_an_unsatisfiable_requirement_leaves_the_price_test_standing() -> None:
    """**The requirement stops being evidence when it cannot be met.**

    The energy test asks "would this export leave the pack below what it needs".
    When what it needs is more than the pack can ever hold, the answer is yes for
    every reachable state and the test has stopped discriminating. What is left
    is the question that still means something: is this export worth more than
    the energy it spends.

    Deliberately **not** a loosening. Where the floor is reachable the energy
    test binds exactly as it did, which is what the parametrised sweep above and
    the beta.32 suite between them pin.
    """
    outcome = solve_at(head=HEAD_1400, end=192, stored=STORED_1400).outcome

    # The clamp makes every published floor reachable, so the fallback branch is
    # reached through the raw curve rather than the clamped one.
    unsatisfiable = [
        index
        for index, value in enumerate(outcome.export_floor_raw_kwh)
        if value >= CAPACITY
    ]
    if not unsatisfiable:
        pytest.skip("this shape produced no unsatisfiable requirement")
    # Whatever the energy test would have said, the plan is still allowed to
    # reach a decision: the gate must not have vetoed the whole horizon.
    assert outcome.desired.available


# ===========================================================================
# 4. the gate cost is a cost
# ===========================================================================


@pytest.mark.parametrize("stored", [6.0, 10.0, 11.75, 14.0, 18.0])
def test_the_gate_cost_is_cash_and_says_which_cash(stored: float) -> None:
    """**It reported a benefit exactly where it cost most.**

    ``objective_eur`` subtracts the terminal edge credit. A gated plan cannot
    sell, so it ends with more stored energy and a larger credit -- and the
    difference of two objectives came out **negative**, in contradiction of the
    invariant its own docstring asserted. Measured at stored 11.0 on this shape:
    the gated plan spent 1.46 EUR more cash and was published as -0.28, "a
    benefit", because it was credited 1.74 for the 7.93 kWh it was left holding.

    On a cash basis the two are comparable, and the inventory it retained is
    published beside the figure rather than inside it.

    *Mutation: restore ``desired.objective_eur - ungated.objective_eur`` and this
    fails.*
    """
    outcome = solve_at(head=HEAD_1400, end=192, stored=stored).outcome

    cost = outcome.export_gate_cost_eur
    if cost is None:
        pytest.skip("no ungated baseline was solved for this state")

    # The figure is the cash difference and nothing else, so it is reproducible
    # from the two plans by hand. That is the whole basis change: an audit number
    # a reader cannot recompute is not an audit number.
    def cash(plan):
        return (
            plan.cost_eur
            + plan.switching_cost_eur
            + plan.grid_charge_margin_eur
            + plan.battery_throughput_cost_eur
        )

    assert cost == pytest.approx(cash(outcome.desired) - cash(outcome.ungated))
    # And both inventory halves are published, because cash alone is not the cost
    # of the permission -- see the property's own docstring for the two measured
    # shapes where the cash figure is negative.
    assert outcome.export_gate_withheld_kwh is not None
    assert outcome.export_gate_withheld_kwh >= -1e-9
    assert outcome.export_gate_retained_kwh is not None


def test_the_basis_change_is_what_stops_it_reporting_a_benefit() -> None:
    """**The live contradiction, reproduced on both bases at once.**

    The state is the 13:00 one with no refill left ahead, which is where the
    permission actually binds. On this horizon:

    * the **cash** basis says the permission cost **+0.136 EUR** -- it withheld
      0.56 kWh of export and the household kept the money it would have earned;
    * the **objective** basis says **-0.024 EUR**, a benefit, because the gated
      plan ends holding more energy and the terminal edge credit pays for it.

    Both are arithmetic. Only one of them answers "what did the protection cost
    me", which is the question the field is named for.

    *Mutation: restore ``desired.objective_eur - ungated.objective_eur`` and this
    fails.*
    """
    outcome = solve_at(
        head=HEAD_1300, end=96, stored=STORED_1345, allow_charge=False
    ).outcome

    assert outcome.export_gate_cost_eur == pytest.approx(0.1355, abs=0.01)
    assert outcome.export_gate_cost_eur > 0.0
    # And the permission really did withhold something, so the figure is not
    # reporting on a gate that never fired.
    assert outcome.export_gate_withheld_kwh > 0.1

    # **beta.41: the basis still matters and the contradiction is gone.** The
    # objective basis reported a *benefit* because the gated plan ended holding
    # more energy and the terminal credit paid for it -- credit on energy the
    # household would have eaten before the horizon ended. Priced on inventory the
    # pack will really hold, the two answers no longer point in opposite
    # directions: the permission costs cash, and the plan that keeps more energy
    # is credited only for energy it will actually keep.
    #
    # The endpoint may still move -- it does here, by 0.395 kWh -- which is why
    # both halves are published rather than netted. What is asserted is the pair,
    # not a coincidence between them.
    assert outcome.export_gate_retained_kwh > 0.0

    # The basis is cash, recomputed here from the intervals rather than read back
    # from the property, so this is a cross-check and not a tautology.
    def metered(plan):
        return (
            sum(entry.cost_eur for entry in plan.intervals)
            + plan.switching_cost_eur
            + plan.grid_charge_margin_eur
            + plan.battery_throughput_cost_eur
        )

    assert outcome.export_gate_cost_eur == pytest.approx(
        metered(outcome.desired) - metered(outcome.ungated), abs=1e-6
    )


def test_a_negative_cash_figure_is_always_explained_by_the_inventory() -> None:
    """**beta.41 removed both counterexamples, and this is that visible decision.**

    beta.32 asserted the gate cost can never be negative; beta.34 disproved it and
    pinned two counterexamples here, asking that any later change which quietly
    made the figure non-negative be *a visible decision rather than a
    coincidence*. beta.41 is that change, and both counterexamples turn out to
    have been artefacts of the two defects it fixes:

    * **stored 18.0 kWh, two-day: cash -0.67, ending 3.69 kWh emptier.** Ending
      emptier was cheap because the terminal credit was priced on the *undepleted*
      lattice bucket -- energy the household would have self-consumed was still
      being paid for. Priced on the inventory the pack will really hold, the
      permission stops moving the endpoint: ``retained`` is now exactly 0.0 and
      the cash is +0.25.
    * **stored 17.0 kWh, today-only: cash -0.0046 with 0.29 kWh *more* retained.**
      beta.34 called this "half a cent of genuine free lunch, and the direct
      fingerprint of the ``_walk_forward`` discrepancy" -- holding cost almost no
      import while leaving the bucket untouched, so a restriction could come out
      ahead on both counts at once. It is now +0.030 with ``retained`` at 0.0.

    **Non-negativity is still not claimed.** That was beta.32's mistake and a
    sweep that merely lacks a counterexample proves nothing. What is pinned is the
    mechanism: the figure was negative *because* the endpoint moved and was
    mispriced, and it is the endpoint no longer moving that removes it. If a future
    change lets the endpoint move again, ``retained`` becomes non-zero first and
    this test says so before the sign does.
    """
    outcome = solve_at(head=HEAD_1400, end=192, stored=18.0).outcome

    assert outcome.export_gate_cost_eur is not None
    assert outcome.export_gate_withheld_kwh > 0.1
    assert outcome.export_gate_cost_eur > 0.0

    # The second counterexample, on its own horizon, checked the same way.
    today_only = solve_at(head=HEAD_1400, end=96, stored=17.0).outcome
    assert today_only.export_gate_withheld_kwh > 0.1
    assert today_only.export_gate_cost_eur > 0.0


# ===========================================================================
# 5. two cycles, and the horizon-extension invariant
# ===========================================================================


def test_two_profitable_cycles_are_both_selected_on_a_two_day_horizon() -> None:
    """Go/no-go 4. Buy, sell, buy, sell -- in that order, over thirty hours.

    Nothing in the objective forbids a second cycle; what forbade it in practice
    was the frame bug, which protected the pack all the way to the end of the
    horizon and left nothing to sell with.
    """
    plan = solve_at(head=HEAD_1400, end=192, stored=STORED_1400).outcome.desired

    directions = [campaign.direction for campaign in plan.campaigns]
    assert directions == ["charge", "discharge", "charge", "discharge"], directions
    # Chronological and disjoint, which is what makes them two cycles rather than
    # one campaign the grouping split.
    starts = [campaign.start_index for campaign in plan.campaigns]
    assert starts == sorted(starts)
    for earlier, later in zip(plan.campaigns, plan.campaigns[1:], strict=False):
        assert earlier.end_index < later.start_index


def test_seeing_tomorrow_does_not_change_what_the_plan_does_today() -> None:
    """**Go/no-go 5, and the invariant the live 14:00 refresh broke.**

    A today-only plan is a feasible policy for the two-day problem, so the
    two-day optimum can never be worse over the shared prefix. On 2026-08-29 it
    plainly was: the horizon grew from 40 intervals to 135 and the same-day sale
    disappeared, because the longer horizon moved the protection window to the
    *following* morning's refill and the floor to 23.09 kWh.

    Asserted structurally rather than on a scalar: the campaigns the plan makes
    inside today must be the same ones, starting in the same intervals.

    **beta.41 stopped requiring the same *end* index, and the reason is the point
    of the release.** Once holding depletes the pack honestly and the terminal
    credit is priced on inventory the pack will really hold, the two-day plan
    knows what the today-only plan cannot: tomorrow forecasts 21.65 kWh of load
    against 7.79 kWh of production. So it stops selling three quarters earlier and
    carries **5.27 kWh more** into tomorrow, spending 0.84 EUR more over the shared
    prefix to do it -- inventory worth around 1.58 EUR at the prices it then
    displaces.

    That is the two-day optimum being *better*, not worse, and the stated invariant
    is about the objective rather than about cash alone. Requiring identical end
    indices would forbid exactly the improvement this release exists to make. What
    still catches the original frame bug is unchanged: it made the same-day sale
    **disappear**, and a missing campaign fails the list below.
    """
    today = solve_at(head=HEAD_1400, end=96, stored=STORED_1400).outcome.desired
    both = solve_at(head=HEAD_1400, end=192, stored=STORED_1400).outcome.desired

    def campaigns_of(plan):
        return [
            (campaign.direction, campaign.start_index)
            for campaign in plan.campaigns
            if campaign.end_index < 96
        ]

    assert campaigns_of(both) == campaigns_of(today)
    # And the sale is still there, at the size it was.
    assert both.planned_grid_export_kwh >= today.planned_grid_export_kwh - 1e-6

    def ends_of(plan):
        return {
            campaign.start_index: campaign.end_index
            for campaign in plan.campaigns
            if campaign.end_index < 96
        }

    # A campaign may end *earlier* on the longer horizon -- carrying energy forward
    # can only be motivated by what the longer horizon can see -- but never later,
    # which would be the longer horizon selling into today what it cannot value.
    for start, end in ends_of(both).items():
        assert end <= ends_of(today)[start], (start, end)
