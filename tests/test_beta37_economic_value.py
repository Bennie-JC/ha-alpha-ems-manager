"""beta.37: what the optimiser thinks the plan is worth, and when it will not say.

**Gate 1 of the release.** One entity now publishes the expected advantage of the
selected plan over doing nothing economically active. Everything here is read from
the solve that already happened, so none of it can change a decision -- and the tests
that hold *that* are in ``test_beta37_neutrality``.

The distinction this module spends most of its assertions on is ``None`` versus
``0.0``:

* ``None`` means **no valid comparison could be formed** -- no plan, no horizon, no
  actionable interval, or a reserve violation, which under the lexicographic
  objective means no monetary alternative was ever ranked at all;
* ``0.0`` means **a valid comparison that came out equal**, which is a real result.

Both failure directions are forbidden. ``sensor.py`` already states the first
half -- *"Zero here would mean a perfect forecast, so 'no data' must never be allowed
to render as one"* -- and beta.37 adds the converse: a genuine zero must never render
as no-data either.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from custom_components.alpha_ems_manager.const import (
    ADVANTAGE_BASIS_METERED_CASH,
    DAY_SPLIT_BASIS_INTERVAL_IDLE,
    ECONOMIC_VALUE_UNAVAILABLE_EMPTY_HORIZON,
    ECONOMIC_VALUE_UNAVAILABLE_NO_PLAN,
    ECONOMIC_VALUE_UNAVAILABLE_NOT_ACTIONABLE,
    ECONOMIC_VALUE_UNAVAILABLE_REASONS,
    ECONOMIC_VALUE_UNAVAILABLE_VIOLATION,
    MARGINAL_VALUE_BASIS_RETENTION,
    REASON_CODE_AWAITING_PLANNED_ACTION,
    REASON_CODE_EXPORT_NOW_DOMINATES,
    REASON_CODE_PHYSICAL_SAFETY_BUY,
    REASON_CODES,
    STORED_VALUE_UNDEFINED_BOTTOM_BUCKET,
    STORED_VALUE_UNDEFINED_REASONS,
    STORED_VALUE_UNDEFINED_TOP_BUCKET,
    STORED_VALUE_UNDEFINED_VIOLATION,
)
from custom_components.alpha_ems_manager.economic import (
    bucket_at_or_below_kwh,
    economic_value_summary,
)

from .beta34_shape import solve_at

#: The reference horizon: 07:00 today through midnight, pack at 8.294 kWh.
HEAD, END, STORED = 28, 96, 8.294


def summary(**overrides):
    """Return the Economic Value payload for one solved horizon."""
    solve_kwargs = {
        key: overrides.pop(key)
        for key in ("head", "end", "stored", "price_fn", "load_fn", "pv_fn", "gain")
        if key in overrides
    }
    solve_kwargs.setdefault("head", HEAD)
    solve_kwargs.setdefault("end", END)
    solve_kwargs.setdefault("stored", STORED)
    payload = {
        "today_interval_count": 96,
        "import_price_eur_kwh": 0.32,
        "export_price_eur_kwh": 0.21,
    }
    payload.update(overrides)
    return economic_value_summary(solve_at(**solve_kwargs).outcome, **payload)


# ===========================================================================
# 1-3. the state means one thing
# ===========================================================================


def test_the_state_is_the_plan_against_the_passive_counterfactual() -> None:
    """**The definition, asserted against the plan it was read from.**

    ``hold_cost_eur - cost_eur``. Not the plan's cost, not the objective, not
    ``expected_net_value_eur`` -- each of which is a different number that moves for
    different reasons, and one of which (the objective) can move the *opposite* way
    to ``cost_eur`` when a switching fee changes.

    *Mutation: return ``cost_eur``, or flip the sign, and this fails.*
    """
    plan = solve_at(head=HEAD, end=END, stored=STORED).outcome.desired
    state = summary()["state"]

    assert state == pytest.approx(plan.hold_cost_eur - plan.cost_eur, abs=1e-4)
    # **Materially, not merely positively.** A flattened price series still leaves a
    # small residual -- the dynamic programme's idle trajectory and the ambient walk
    # are not the same walk -- so ``> 0.0`` would pass on a fixture with no spread in
    # it at all, and the whole shape would stop being load-bearing.
    assert state > 1.0, (
        f"the witness: this plan materially beats doing nothing: {state}"
    )


def test_the_state_is_none_of_the_other_euro_figures() -> None:
    """Named individually, so a rename cannot quietly substitute one for another."""
    plan = solve_at(head=HEAD, end=END, stored=STORED).outcome.desired
    state = summary()["state"]

    # **At the published precision, deliberately.** The state is rounded to four
    # decimals, so ``!= approx(plan.cost_eur)`` with the default relative tolerance is
    # satisfied by the rounding alone -- and a mutation that published ``cost_eur``
    # directly went unnoticed. The tolerance has to be looser than the rounding for
    # the inequality to mean anything.
    assert state != pytest.approx(plan.cost_eur, abs=1e-3)
    assert state != pytest.approx(plan.objective_eur, abs=1e-3)
    assert state != pytest.approx(plan.expected_net_value_eur, abs=1e-3)
    assert state != pytest.approx(plan.hold_cost_eur, abs=1e-3)


def test_the_advantage_carries_no_model_term() -> None:
    """**The cash figure equals the state, and publishing both is the proof.**

    An earlier draft of this release assumed the advantage contained the terminal
    credit and the notional margins. It does not: ``cost_eur`` and ``hold_cost_eur``
    are both metered cash -- every euro in them reconciles to grid energy at the
    interval's own prices -- and the switching fee, the margin, the throughput cost
    and the terminal credit all live in ``objective_eur`` instead. So the two lines
    coincide, and their coinciding is what tells a reader nothing notional leaked in.

    Asserted with the model terms shown to be non-zero, or the equality would hold
    vacuously on a plan that happened to have none.
    """
    plan = solve_at(head=HEAD, end=END, stored=STORED).outcome.desired
    payload = summary()

    terms = (
        plan.switching_cost_eur
        + plan.grid_charge_margin_eur
        + plan.battery_throughput_cost_eur
        + plan.edge_value_eur
    )
    assert terms > 0.1, "the witness: this plan has material model terms"
    assert payload["advantage_cash_eur"] == payload["state"]
    assert payload["advantage_basis"] == ADVANTAGE_BASIS_METERED_CASH
    assert payload["plan"]["model_terms_are_cash"] is False
    # And the objective genuinely differs from the cost, so the two bases are real.
    assert plan.objective_eur != pytest.approx(plan.cost_eur)


# ===========================================================================
# 4-9. none versus zero
# ===========================================================================


def test_a_valid_comparison_that_comes_out_equal_publishes_zero() -> None:
    """**A real result, and it must not be suppressed into ``unknown``.**

    **The equality is injected, and the reason is worth stating.** A flat price
    series makes the optimiser choose no runs at all -- measured: zero runs -- and
    yet ``cost_eur`` and ``hold_cost_eur`` still differ by about 0.07, because the
    dynamic programme's own idle trajectory and ``_ambient_walk``'s
    highest-reachable-target trajectory are not the same walk. So an exactly equal
    pair is not something this solver produces, and constructing one by hunting for
    a price series that happens to yield it would be a fixture nobody could read.

    What is under test is the **publication rule**, not the solver: given a valid
    plan whose advantage is exactly zero, the sensor must publish ``0.0`` and not
    ``unknown``. The plan handed in is a real solved plan with one field replaced, so
    every other precondition -- available, non-empty, actionable, no violation -- is
    genuinely satisfied rather than asserted.

    *Mutation: return ``None`` when the advantage rounds to zero and this fails.*
    """
    solved = solve_at(head=HEAD, end=END, stored=STORED).outcome
    equal = replace(
        solved, desired=replace(solved.desired, hold_cost_eur=solved.desired.cost_eur)
    )
    payload = economic_value_summary(
        equal, today_interval_count=96, import_price_eur_kwh=0.32
    )

    assert payload["available"] is True
    assert payload["unavailable_reason"] is None
    assert payload["state"] == 0.0
    assert payload["state"] is not None
    # The witness: valid in every other respect, so zero is the comparison's answer
    # and not a stand-in for a missing one.
    assert payload["horizon_intervals"] > 0
    assert payload["actionable_intervals"] > 0
    assert equal.desired.available is True
    assert equal.desired.violation_kwh == 0.0


def test_no_outcome_at_all_is_unavailable_and_not_zero() -> None:
    """The plainest unavailability, and the one a restart produces."""
    payload = economic_value_summary(None, today_interval_count=96)

    assert payload["available"] is False
    assert payload["state"] is None
    assert payload["unavailable_reason"] == ECONOMIC_VALUE_UNAVAILABLE_NO_PLAN


def test_an_empty_horizon_is_unavailable_and_not_zero() -> None:
    """A horizon with no intervals has no comparison in it, and says so."""
    payload = summary(head=95, end=95)

    assert payload["available"] is False
    assert payload["state"] is None
    assert payload["unavailable_reason"] in (
        ECONOMIC_VALUE_UNAVAILABLE_EMPTY_HORIZON,
        ECONOMIC_VALUE_UNAVAILABLE_NOT_ACTIONABLE,
        ECONOMIC_VALUE_UNAVAILABLE_NO_PLAN,
    )


def test_a_reserve_violation_is_unavailable_and_not_zero() -> None:
    """**The subtlest of the four, and the one a naive reading gets wrong.**

    An empty pack cannot reach the reserve curve, so the solve carries a violation --
    measured at 4.22 kWh here. The objective is lexicographic ``(violation, cost)``,
    so with a violation present *no monetary alternative was ever ranked*: the money
    terms of two states that disagree about feasibility were never compared, and
    their difference is not the price of anything. A plan can be perfectly
    ``available`` and still have nothing economic to say, which is why this is its own
    cause rather than folded into "no plan".

    *Mutation: drop the violation check and this publishes a number that means
    nothing.*
    """
    payload = summary(stored=0.0)

    assert payload["available"] is False
    assert payload["state"] is None
    assert payload["unavailable_reason"] == ECONOMIC_VALUE_UNAVAILABLE_VIOLATION
    # The witness: the plan itself is available, so this is not the no-plan case.
    plan = solve_at(head=HEAD, end=END, stored=0.0).outcome.desired
    assert plan.available is True
    assert plan.violation_kwh > 1.0


def test_every_unavailable_reason_is_from_the_closed_vocabulary() -> None:
    """A sixth cause cannot appear without being named."""
    for payload in (
        economic_value_summary(None, today_interval_count=96),
        summary(head=95, end=95),
    ):
        reason = payload["unavailable_reason"]
        assert reason in ECONOMIC_VALUE_UNAVAILABLE_REASONS, reason
    assert ECONOMIC_VALUE_UNAVAILABLE_VIOLATION in ECONOMIC_VALUE_UNAVAILABLE_REASONS


# ===========================================================================
# 10-11. tomorrow's prices
# ===========================================================================


def test_a_today_only_horizon_still_has_a_state() -> None:
    """**Every morning before the day-ahead auction, and the headline must survive.**

    A sensor that went ``unknown`` for half of every day would be useless, and
    "tomorrow is not priced yet" is not a failure to compare -- it is a shorter
    horizon. The headline is computed over what *is* known; only the
    tomorrow-specific figures are absent.

    *Mutation: null the state when ``tomorrow_prices_known`` is false and this
    fails.*
    """
    payload = summary(end=96, tomorrow_prices_known=False)

    assert payload["available"] is True
    assert isinstance(payload["state"], float)
    assert payload["tomorrow_prices_known"] is False
    assert payload["tomorrow_interval_value_eur"] is None
    assert payload["tomorrow"]["intervals"] == 0
    assert payload["tomorrow"]["grid_import_cost_eur"] is None
    # And today is fully populated, so the absence is tomorrow's and not the sum's.
    assert payload["today_interval_value_eur"] is not None
    assert payload["today"]["intervals"] > 0


def test_tomorrow_arriving_extends_the_horizon_without_a_gap() -> None:
    """The step is in the figures, never through ``unknown``."""
    today_only = summary(end=96, tomorrow_prices_known=False)
    both = summary(end=192, tomorrow_prices_known=True)

    assert today_only["available"] is both["available"] is True
    assert both["horizon_intervals"] > today_only["horizon_intervals"]
    assert both["tomorrow_prices_known"] is True
    assert both["tomorrow_interval_value_eur"] is not None
    assert both["tomorrow"]["intervals"] > 0


# ===========================================================================
# 16-21. the marginal worth of stored energy
# ===========================================================================


def test_the_headline_marginal_value_is_the_retention_side() -> None:
    """**``V(b-1) - V(b)``, computed independently here.**

    The alternative to holding stored energy is giving it up, so the slope that
    answers "why am I holding instead of exporting" is the downward one. The upward
    difference -- what one *more* kWh would be worth -- is published beside it as a
    diagnostic and is a different number at every kink.

    *Mutation: publish the upward side as the headline and this fails.*
    """
    solved = solve_at(head=HEAD, end=END, stored=STORED)
    plan, bucket_kwh = solved.outcome.desired, solved.outcome.bucket_kwh
    bucket = bucket_at_or_below_kwh(
        plan.intervals[0].start_energy_dc_kwh, bucket_kwh=bucket_kwh
    )
    expected = (
        plan.head_value[bucket - 1][plan.head_run_state][1]
        - plan.head_value[bucket][plan.head_run_state][1]
    ) / bucket_kwh

    payload = summary()
    stored = payload["stored_value"]

    assert payload["stored_energy_marginal_value_eur_kwh"] == pytest.approx(
        expected, abs=1e-4
    )
    assert payload["marginal_value_basis"] == MARGINAL_VALUE_BASIS_RETENTION
    assert stored["marginal_value_down_eur_kwh"] == pytest.approx(expected, abs=1e-4)
    assert stored["current_bucket"] == bucket
    assert stored["head_run_state"] == plan.head_run_state
    # The resolution is the width the slope was actually divided by, at four
    # decimals -- two would report 0.26 for a 0.2635 kWh bucket.
    assert stored["marginal_value_resolution_kwh"] == pytest.approx(
        bucket_kwh, abs=1e-4
    )


def test_the_retention_side_is_discriminable_from_the_upward_side() -> None:
    """**A state where the two slopes disagree, or "which side" proves nothing.**

    At the reference state the upward and downward differences happen to be equal --
    0.1876 both -- so a test asserting only "the headline is the retention side" there
    passes whichever side is published. Measured on this horizon, bucket 63 is kinked:
    the upward slope is 0.018 and the downward 0.212, an order of magnitude apart.

    *Mutation: publish the upward side as the headline and this fails; the version of
    this test that used the reference state did not notice.*
    """
    # **The witness moved in beta.41, and it had to be found again rather than
    # assumed.** Bucket 63 was the kinked one while household service moved no
    # state; with the pack modelled as depleting, the curve there is locally
    # straight -- its two slopes agree to 9e-16, which is a stronger statement
    # that the old witness is gone than any tolerance would be.
    #
    # Measured across the range, 6.0 kWh is the sharply kinked state now:
    # bucket 22, 0.204 down against 0.447 up. Relocated rather than relaxed,
    # because a smaller threshold would have let a genuinely straight curve pass.
    solved = solve_at(head=HEAD, end=END, stored=6.0)
    plan, bucket_kwh = solved.outcome.desired, solved.outcome.bucket_kwh
    bucket = bucket_at_or_below_kwh(
        plan.intervals[0].start_energy_dc_kwh, bucket_kwh=bucket_kwh
    )
    row = plan.head_run_state
    down = (
        plan.head_value[bucket - 1][row][1] - plan.head_value[bucket][row][1]
    ) / bucket_kwh
    up = (
        plan.head_value[bucket][row][1] - plan.head_value[bucket + 1][row][1]
    ) / bucket_kwh
    assert abs(down - up) > 0.05, (down, up, "the witness: this state is kinked")

    payload = economic_value_summary(
        solved.outcome, today_interval_count=96, import_price_eur_kwh=0.32
    )

    assert payload["stored_energy_marginal_value_eur_kwh"] == pytest.approx(
        down, abs=1e-4
    )
    assert payload["stored_value"]["marginal_value_up_eur_kwh"] == pytest.approx(
        up, abs=1e-4
    )
    assert payload["stored_value"]["marginal_value_kinked"] is True


def test_the_marginal_value_is_read_from_the_head_run_state_row() -> None:
    """**The physical row, and beta.36 is what made it truthful.**

    The value function has one row per run state, and they differ by the switching fee
    the head no longer has to pay. Reading row zero unconditionally would price a
    running export as though the inverter were idle.

    Discriminated by solving with a discharging head, where the rows genuinely differ
    -- at the reference state rows 0 and 1 are identical, so a test there proves
    nothing about which row was read.
    """
    from custom_components.alpha_ems_manager.economic import RUN_STATE_DISCHARGE

    # **5.8 kWh, not the reference state, and the difference is the whole test.** At
    # the reference bucket the idle and discharge rows differ in *level* -- by the
    # switching fee -- but the offset is constant across neighbouring buckets, so
    # their *slopes* are identical and reading the wrong row is undetectable. Measured
    # across the lattice, bucket 22 with a discharging head is where the slopes
    # genuinely part: 0.188 on the idle row against 0.261 on the physical one.
    solved = solve_at(
        head=HEAD, end=END, stored=5.8, head_run_state=RUN_STATE_DISCHARGE
    )
    plan, bucket_kwh = solved.outcome.desired, solved.outcome.bucket_kwh
    bucket = bucket_at_or_below_kwh(
        plan.intervals[0].start_energy_dc_kwh, bucket_kwh=bucket_kwh
    )
    assert plan.head_run_state == RUN_STATE_DISCHARGE
    idle_slope = (
        plan.head_value[bucket - 1][0][1] - plan.head_value[bucket][0][1]
    ) / bucket_kwh
    live_row = plan.head_value[bucket][plan.head_run_state][1]
    live_slope = (
        plan.head_value[bucket - 1][plan.head_run_state][1] - live_row
    ) / bucket_kwh
    assert abs(idle_slope - live_slope) > 0.02, (
        idle_slope,
        live_slope,
        "the witness: the two rows give different slopes here",
    )

    expected = (
        plan.head_value[bucket - 1][plan.head_run_state][1] - live_row
    ) / bucket_kwh
    payload = economic_value_summary(
        solved.outcome, today_interval_count=96, import_price_eur_kwh=0.32
    )

    assert payload["stored_energy_marginal_value_eur_kwh"] == pytest.approx(
        expected, abs=1e-4
    )
    assert payload["stored_value"]["head_run_state"] == RUN_STATE_DISCHARGE


def test_the_bucket_lookup_carries_the_epsilon() -> None:
    """**The off-by-one beta.35 shipped, on a pair that exposes it.**

    ``start_energy_dc_kwh`` is the float product ``n * bucket_kwh``, which need not
    divide cleanly, so a bare ``int(energy / bucket_kwh)`` floors an exact multiple to
    ``n - 1``. Roughly four per cent of bucket sizes in the live 0.15--0.40 band do
    it. When it happens the published marginal value is the slope of the neighbouring
    interval and ``stored_value_eur`` is short by one bucket.

    Asserted on a crafted pair rather than through a solve, because which bucket size
    the lattice selects is a property of the pack and not something a test may choose.

    *Mutation: restore the bare ``int()`` and this fails.*
    """
    bucket_kwh = 0.395
    energy = 13 * bucket_kwh

    assert int(energy / bucket_kwh) == 12, "the witness: the bare division mis-floors"
    assert bucket_at_or_below_kwh(energy, bucket_kwh=bucket_kwh) == 13
    # And the ordinary cases still behave.
    assert bucket_at_or_below_kwh(0.0, bucket_kwh=bucket_kwh) == 0
    assert bucket_at_or_below_kwh(-1.0, bucket_kwh=bucket_kwh) == 0
    assert bucket_at_or_below_kwh(1.0, bucket_kwh=0.0) == 0
    assert bucket_at_or_below_kwh(energy + 0.2, bucket_kwh=bucket_kwh) == 13


def test_both_one_sided_slopes_are_published_and_a_kink_is_named() -> None:
    """A fixed fee inside a minimisation breaks concavity, and the lattice shows it.

    Publishing the average of two disagreeing slopes would invent a derivative the
    value function does not have, so both are published and the disagreement is
    flagged rather than smoothed.
    """
    stored = summary()["stored_value"]

    assert "marginal_value_up_eur_kwh" in stored
    assert "marginal_value_down_eur_kwh" in stored
    assert stored["marginal_value_kinked"] in (True, False, None)


def test_the_bottom_bucket_has_no_lower_side() -> None:
    """**``None`` plus a reason, and a reason that is true.**

    At bucket zero there is nothing below to give the energy up to. Reported as its
    own cause rather than as ``state_unreachable``, which is what the underlying
    method answers for a negative index and would be misleading here: nothing is
    unreachable, the question simply has no lower side.

    **Reached by replacing the head interval's stored energy, because a pack at
    bucket zero cannot produce a comparable plan** -- an empty pack violates the
    reserve, and a violation makes the whole comparison unavailable one branch
    earlier (see the violation test above). So the state is injected and the rule
    under test is production's.
    """
    solved = solve_at(head=HEAD, end=END, stored=STORED).outcome
    head = solved.desired.intervals[0]
    empty = replace(
        solved,
        desired=replace(
            solved.desired,
            intervals=(
                replace(head, start_energy_dc_kwh=0.0),
                *solved.desired.intervals[1:],
            ),
        ),
    )
    payload = economic_value_summary(
        empty, today_interval_count=96, import_price_eur_kwh=0.32
    )

    assert payload["available"] is True, "the comparison itself is still valid"
    assert payload["stored_energy_marginal_value_eur_kwh"] is None
    assert (
        payload["marginal_value_unavailable_reason"]
        == STORED_VALUE_UNDEFINED_BOTTOM_BUCKET
    )
    assert payload["stored_value"]["current_bucket"] == 0


def test_the_top_bucket_guard_holds_and_is_not_reachable_from_a_start_state() -> None:
    """**The guard is correct and defensive, and saying which matters.**

    ``PhysicsTable.energy`` clamps the last bucket to the ceiling, so its interval is
    narrower than ``bucket_kwh`` and dividing by that width would report a slope the
    lattice cannot offer. The existing method refuses it.

    **It cannot be reached from a real starting state, and that is worth recording
    rather than engineering around.** The top bucket needs
    ``buckets * bucket_kwh`` kWh -- 21.6089 on the reference pack -- while the pack
    holds 21.6, so a full battery quantises to bucket 81, not 82. A test that solved
    at capacity and asserted ``None`` would be asserting the wrong branch and passing
    for the wrong reason.

    So the guard is exercised directly, and the unreachability is asserted beside it
    so a future change to the lattice cannot make this test silently vacuous.
    """
    solved = solve_at(head=HEAD, end=END, stored=21.6)
    outcome, plan = solved.outcome, solved.outcome.desired

    # The guard itself.
    value, reason = plan.marginal_value_eur_per_kwh(
        outcome.buckets, bucket_kwh=outcome.bucket_kwh
    )
    assert value is None
    assert reason == STORED_VALUE_UNDEFINED_TOP_BUCKET

    # And it is not where a full pack lands.
    full = bucket_at_or_below_kwh(
        plan.intervals[0].start_energy_dc_kwh, bucket_kwh=outcome.bucket_kwh
    )
    assert full < outcome.buckets
    assert outcome.buckets * outcome.bucket_kwh > 21.6
    payload = economic_value_summary(
        outcome, today_interval_count=96, import_price_eur_kwh=0.32
    )
    assert payload["stored_energy_marginal_value_eur_kwh"] is not None


def test_every_undefined_reason_is_from_the_closed_vocabulary() -> None:
    """Five causes, named, and a sixth cannot arrive unnamed."""
    reason = summary()["marginal_value_unavailable_reason"]
    assert reason is None or reason in STORED_VALUE_UNDEFINED_REASONS, reason
    assert STORED_VALUE_UNDEFINED_VIOLATION in STORED_VALUE_UNDEFINED_REASONS
    assert STORED_VALUE_UNDEFINED_BOTTOM_BUCKET in STORED_VALUE_UNDEFINED_REASONS
    assert STORED_VALUE_UNDEFINED_TOP_BUCKET in STORED_VALUE_UNDEFINED_REASONS
    assert len(set(STORED_VALUE_UNDEFINED_REASONS)) == 5


# ===========================================================================
# 22-25. the reason code
# ===========================================================================


def test_the_reason_code_is_from_the_closed_vocabulary() -> None:
    """Every state the fixture can reach names itself from the published set."""
    for payload in (
        summary(),
        summary(head=76, end=96, stored=19.0),
        summary(price_fn=lambda index: 0.25, gain=5.0),
        summary(stored=0.0),
    ):
        if payload["available"]:
            assert payload["reason_code"] in REASON_CODES, payload["reason_code"]


def test_holding_now_with_a_sale_later_is_not_reported_as_immaterial() -> None:
    """**The ordinary Hold, and the answer that would have been most misleading.**

    The plan is idle at the head and worth several euros over the horizon; its first
    run is simply later. Reporting ``no_material_economic_action`` for that would
    tell a reader the optimiser found nothing worth doing, which is the opposite of
    the truth. Found while implementing, and it is why the vocabulary has a seventh
    entry.
    """
    payload = summary()

    assert payload["current_action"] == "idle"
    assert payload["state"] > 1.0, "the witness: materially worth doing"
    assert payload["reason_code"] == REASON_CODE_AWAITING_PLANNED_ACTION


def test_exporting_at_the_head_says_so() -> None:
    """When the plan is selling now, the code names that and not a price comparison."""
    payload = summary(head=76, end=96, stored=19.0)

    assert payload["current_action"] in ("export", "discharge")
    assert payload["reason_code"] == REASON_CODE_EXPORT_NOW_DOMINATES


def test_a_safety_buy_outranks_every_economic_reading() -> None:
    """**The reserve is physics, and it is reported before any price is consulted.**

    A compelled purchase is not an economic preference, so its code is chosen before
    the advantage, the direction or the marginal value is looked at. The Safety-Buy
    figures stay in their own fields and never merge into the economic ones.
    """
    solved = solve_at(head=20, end=96, stored=0.5)
    outcome = solved.outcome
    payload = economic_value_summary(
        outcome, today_interval_count=96, import_price_eur_kwh=0.32
    )
    if not payload["available"] or not outcome.safety_buy_runs:
        pytest.skip("this shape compelled no purchase; covered by the unit above")

    run = outcome.desired.current_run
    if run is None or run.start_index not in outcome.safety_buy_runs:
        pytest.skip("the compelled run is not at the head on this shape")
    assert payload["reason_code"] == REASON_CODE_PHYSICAL_SAFETY_BUY
    assert payload["plan"]["safety_buy_ac_kwh"] is not None


# ===========================================================================
# the rest of the contract
# ===========================================================================


def test_the_terminal_edge_value_is_published_under_its_own_name() -> None:
    """**Not as a replacement cost, because it is not one.**

    ``edge_value_eur_per_kwh`` is the rate that seeds the terminal credit into the
    value table, so it is already *inside* the marginal stored-energy value.
    Publishing it as an acquisition price would double-count it and would present a
    horizon-boundary parameter as something a person could go and buy at.

    *Mutation: republish it as ``replacement_cost_eur_kwh`` and this fails.*
    """
    payload = summary()

    assert "terminal_edge_value_eur_kwh" in payload
    assert payload["terminal_edge_value_eur_kwh"] is not None
    assert "replacement_cost_eur_kwh" not in payload
    # And it is a different number from the marginal value it feeds.
    assert payload["terminal_edge_value_eur_kwh"] != pytest.approx(
        payload["stored_energy_marginal_value_eur_kwh"]
    )


def test_the_next_planned_charge_price_is_plan_derived_or_absent() -> None:
    """``None`` is the honest answer when the plan contains no future charge."""
    payload = summary()
    price = payload["next_planned_charge_price_eur_kwh"]

    assert price is None or price > 0.0
    runs = [
        run
        for run in solve_at(head=HEAD, end=END, stored=STORED).outcome.desired.runs
        if run.action == "charge" and run.start_index >= HEAD
    ]
    if runs:
        assert price == pytest.approx(runs[0].average_price_eur_kwh, abs=1e-4)
    else:
        assert price is None


def test_the_energy_block_uses_plan_level_sums() -> None:
    """The per-run ``expected_*`` fields are marginal figures and a different thing."""
    plan = solve_at(head=HEAD, end=END, stored=STORED).outcome.desired
    energy = summary()["energy"]

    assert energy["expected_grid_import_kwh"] == pytest.approx(
        plan.planned_grid_import_kwh, abs=0.01
    )
    assert energy["expected_grid_export_kwh"] == pytest.approx(
        plan.planned_grid_export_kwh, abs=0.01
    )
    assert energy["expected_battery_throughput_kwh"] == pytest.approx(
        plan.battery_throughput_kwh, abs=0.01
    )


def test_the_plan_block_uses_cost_naming() -> None:
    """A cost is not a value, and beta.16 renamed a field for exactly this reason."""
    block = summary()["plan"]

    assert "selected_plan_cost_eur" in block
    assert "counterfactual_cost_eur" in block
    assert "selected_plan_value_eur" not in block
    assert "counterfactual_value_eur" not in block


def test_the_day_split_names_carry_their_basis() -> None:
    """``today_value_eur`` would invite a sum the mathematics forbids."""
    payload = summary(end=192, tomorrow_prices_known=True)

    assert "today_interval_value_eur" in payload
    assert "tomorrow_interval_value_eur" in payload
    assert "today_value_eur" not in payload
    assert "tomorrow_value_eur" not in payload
    assert payload["day_split_basis"] == DAY_SPLIT_BASIS_INTERVAL_IDLE
    assert "do NOT sum to decision_advantage_eur" in payload["day_split_rule"]


# ===========================================================================
# the entity itself, through Home Assistant
# ===========================================================================


async def test_the_entity_is_unknown_before_a_plan_exists(
    hass, config_data: dict, source_entities: None, setup_integration
) -> None:
    """**Missing data renders as ``unknown``, never as 0.00 EUR.**

    The contract test beside this one checks the entity's *metadata*. It cannot see
    what the state becomes when no comparison could be formed, and that is the failure
    a dashboard would act on: a confident zero.

    Driven through the real entity so the guard is the one Home Assistant calls.

    *Mutation: return ``0.0`` instead of ``None`` for an unavailable payload and this
    fails.*
    """
    from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

    state = hass.states.get("sensor.alpha_ems_economic_value")

    assert state is not None
    assert state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE), state.state
    assert state.state != "0.0"
    assert state.state != "0"


async def test_the_entity_declares_no_state_class(
    hass, config_data: dict, source_entities: None, setup_integration
) -> None:
    """**A forecast over a shrinking horizon must not become a long-term statistic.**

    Home Assistant pairs ``MONETARY`` with ``TOTAL``, and this is neither a total nor
    a measurement: the horizon shortens through the day, so a statistic over it would
    average a moving definition. The same argument the two expected-load sensors make.

    *Mutation: add ``state_class=MEASUREMENT`` and this fails.*
    """
    state = hass.states.get("sensor.alpha_ems_economic_value")

    assert state is not None
    assert state.attributes.get("state_class") is None
    assert state.attributes.get("device_class") == "monetary"
    assert state.attributes.get("unit_of_measurement") == "\u20ac"
    # And the basis is on the entity, so a reader never has to guess what it means.
    #
    # **The phrase moved in beta.39 and the reason is a contradiction this file
    # already proved.** The old string said "on the exact basis the optimiser
    # minimised" in the same sentence as "both sides are metered cash", and
    # ``test_the_state_is_none_of_the_other_euro_figures`` in this same file has
    # asserted the state is *not* ``objective_eur`` since beta.37 -- so the prose
    # was already refuted by a test above it.
    # What is pinned here is the half that was always true.
    basis = state.attributes["basis"]
    assert "passive ambient-walk comparator" in basis, basis
    assert "both sides are metered cash" in basis, basis
    assert "the basis the optimiser minimised" not in basis, basis
