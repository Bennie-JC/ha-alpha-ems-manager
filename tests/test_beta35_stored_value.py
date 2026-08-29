"""beta.35: what the energy already in the pack is worth, and why it is not FIFO.

**The question this answers.** Buy twenty kilowatt-hours cheap, sell five of them
into a spike: was that a good trade? The tempting answer is an inventory model --
average the purchase price, refuse to sell below it. That prices *sunk cost*, and
``realized`` has argued since beta.18 why that is wrong: what a kilowatt-hour cost
yesterday cannot change what the next one is worth today.

The optimiser already computes the right number and beta.34 threw it away. The
backward induction ends holding the optimal cost-to-go for every storage state at
the head of the horizon -- the dual of the storage constraint, the exact marginal
worth of one more stored kilowatt-hour with all future load, prices, fees and the
reserve already accounted for. beta.35 keeps that curve and publishes it. Zero
solver cost, and **no decision reads it**.

Beside it sits the other half: the terminal value, which is the only thing telling
the optimiser what the pack is worth *after* the horizon ends. beta.34 priced it at
a flat 25th percentile of known import prices and the plan responded exactly as it
should have -- it liquidated, ending at ``end_energy_dc_kwh: 0.00`` after exporting
14.22 kWh. That is not a hoarding bug to be patched with a floor; it is a valuation
error, and it is fixed by valuing the energy correctly.
"""

from __future__ import annotations

import math
from itertools import pairwise

import pytest

from custom_components.alpha_ems_manager.const import (
    STORED_VALUE_UNDEFINED_TOP_BUCKET,
    STORED_VALUE_UNDEFINED_VIOLATION,
    TERMINAL_VALUE_BASIS_DEMAND_BOUNDED,
    TERMINAL_VALUE_BASIS_FLAT_EDGE,
)
from custom_components.alpha_ems_manager.economic import TerminalValue

from .beta34_shape import solve_at

FLOOR = 4.32


def _terminal(**overrides) -> TerminalValue:
    """Return the beta.35 terminal value with the live installation's efficiency."""
    fields = {
        "demand_ac_kwh": 6.0,
        "displaced_price_eur_kwh": 0.30,
        "export_price_eur_kwh": 0.08,
        "discharge_efficiency": 0.95,
        "edge_value_eur_per_kwh": 0.12,
        "edge_creditable_kwh": 21.6,
        "basis": TERMINAL_VALUE_BASIS_DEMAND_BOUNDED,
    }
    fields.update(overrides)
    return TerminalValue(**fields)


# ===========================================================================
# 1. the terminal value has the shape storage requires
# ===========================================================================


def test_the_terminal_credit_is_non_negative_non_decreasing_and_concave() -> None:
    """**The three properties that make it a storage value rather than a bribe.**

    *Non-negative*, because energy in a pack is never a liability here.
    *Non-decreasing*, because more of it is never worth less.
    *Concave*, because the first kilowatt-hour above the floor displaces the most
    expensive thing the household will do and each one after it displaces
    something cheaper -- which is exactly what stops the value becoming a licence
    to hoard. A flat rate high enough to prevent the beta.34 liquidation would
    equally have justified buying a full pack at any price.
    """
    terminal = _terminal()
    energies = [FLOOR + step * 0.25 for step in range(0, 80)]
    credits = [terminal.credit_eur(energy, FLOOR) for energy in energies]

    assert all(math.isfinite(value) for value in credits)
    assert all(value >= 0.0 for value in credits)
    assert all(later >= earlier - 1e-9 for earlier, later in pairwise(credits))

    slopes = [(later - earlier) / 0.25 for earlier, later in pairwise(credits)]
    assert all(later <= earlier + 1e-9 for earlier, later in pairwise(slopes)), (
        "the marginal worth of stored energy may never rise with the level"
    )


def test_nothing_below_the_floor_is_ever_credited() -> None:
    """The reserve is not an economic quantity and may not be paid for.

    Crediting energy below the enforced floor would price something the plan is
    obliged to hold whatever the market does -- and it would put a money term in
    front of a feasibility one, which is the inversion the lexicographic pair
    exists to prevent.
    """
    terminal = _terminal()
    assert terminal.credit_eur(FLOOR, FLOOR) == pytest.approx(0.0)
    assert terminal.credit_eur(FLOOR - 2.0, FLOOR) == pytest.approx(0.0)
    assert terminal.credit_eur(0.0, FLOOR) == pytest.approx(0.0)


def test_the_served_share_is_bounded_by_the_demand_it_can_displace() -> None:
    """**The bound that makes the first segment finite.**

    Only the energy the household will actually consume before the pack refills
    for free displaces an import. Beyond that the credit falls back to what the
    energy could be sold for, which is much less -- and that step down *is* the
    concavity.
    """
    terminal = _terminal(demand_ac_kwh=3.0)
    eta = 0.95

    # Exactly the demand, delivered AC.
    just_enough = FLOOR + 3.0 / eta
    assert terminal.credit_eur(just_enough, FLOOR) == pytest.approx(3.0 * 0.30)

    # One more kilowatt-hour is worth the export basis, not the import one.
    extra = terminal.credit_eur(just_enough + 1.0, FLOOR) - terminal.credit_eur(
        just_enough, FLOOR
    )
    assert extra == pytest.approx(1.0 * eta * 0.08)
    assert extra < 0.30


def test_the_new_rule_reproduces_beta_34_exactly_when_given_its_terms() -> None:
    """The new terminal value is a strict generalisation, not a replacement.

    Take the served segment away (no post-horizon demand), set the spare basis to
    the flat rate the old rule used, and stand the floor where the old rule
    implicitly stood it -- at zero: the two are then the same arithmetic, credit
    for credit. That is what "generalisation" has to mean if it is to mean
    anything.
    """
    eta = 0.95
    reduced = _terminal(
        demand_ac_kwh=0.0,
        export_price_eur_kwh=0.12 / eta,
        discharge_efficiency=eta,
    )
    for energy in (0.0, 2.0, 10.0, 21.6, 30.0):
        assert reduced.credit_eur(energy, 0.0) == pytest.approx(
            reduced.flat.credit_eur(energy, 0.0)
        )


def test_the_counterfactual_is_skipped_only_when_the_arithmetic_is_identical() -> None:
    """**The gate on the seventh solve, and it is deliberately narrow.**

    A terminal value with no demand and no export basis credits *nothing*, which
    is emphatically not the flat rule -- it is the strongest disagreement with it
    there is. So "nothing to serve" is not grounds to skip the comparison; only
    already being the flat rule is. Getting this backwards would silently withdraw
    the counterfactual on exactly the days it has the most to say.
    """
    empty = _terminal(demand_ac_kwh=0.0, export_price_eur_kwh=0.0)
    assert empty.equivalent_to_flat is False
    assert empty.credit_eur(21.6, FLOOR) == pytest.approx(0.0)
    assert empty.flat.credit_eur(21.6, FLOOR) > 0.0

    assert empty.flat.equivalent_to_flat is True


def test_the_flat_rule_is_reconstructed_exactly_for_the_counterfactual() -> None:
    """``legacy`` must be beta.34's arithmetic, or the comparison means nothing."""
    terminal = _terminal()
    flat = terminal.flat
    assert flat.basis == TERMINAL_VALUE_BASIS_FLAT_EDGE
    for energy in (0.0, FLOOR, 10.0, 21.6, 40.0):
        assert flat.credit_eur(energy, FLOOR) == pytest.approx(0.12 * min(energy, 21.6))


def test_a_costlier_night_makes_retained_energy_worth_more() -> None:
    """Expensive future household load raises the value of holding energy.

    The whole point of pricing the edge by what it displaces: the same pack is
    worth more on the eve of a costly night than a cheap one, and beta.34's single
    percentile could not express that at all.
    """
    cheap = _terminal(displaced_price_eur_kwh=0.10)
    dear = _terminal(displaced_price_eur_kwh=0.45)
    level = FLOOR + 5.0
    assert dear.credit_eur(level, FLOOR) > cheap.credit_eur(level, FLOOR)


def test_a_free_refill_tomorrow_lowers_the_value_of_holding_excess() -> None:
    """Energy that will be displaced by production is worth the export basis only.

    ``demand_ac_kwh`` stops at the first forecast surplus, so a pack that will be
    refilled for nothing in the morning has very little to serve -- and what it
    holds beyond that is worth what it can be sold for and no more.
    """
    long_night = _terminal(demand_ac_kwh=10.0)
    early_sun = _terminal(demand_ac_kwh=0.5)
    level = FLOOR + 8.0
    assert early_sun.credit_eur(level, FLOOR) < long_night.credit_eur(level, FLOOR)


def test_export_being_forbidden_removes_the_spare_segment_entirely() -> None:
    """With no export permission the spare share has no basis, so it is worth zero.

    Not a small number: there is genuinely nothing that can be done with it inside
    the model, and inventing a value would be the same error in the other
    direction.
    """
    terminal = _terminal(demand_ac_kwh=2.0, export_price_eur_kwh=0.0)
    served = FLOOR + 2.0 / 0.95
    assert terminal.credit_eur(served + 6.0, FLOOR) == pytest.approx(
        terminal.credit_eur(served, FLOOR)
    )


# ===========================================================================
# 2. the head value curve -- the dual, read rather than modelled
# ===========================================================================


def test_the_head_value_curve_survives_the_solve() -> None:
    """beta.34 computed the dual on every refresh and discarded it.

    ``value`` holds position zero after the loop -- the optimal cost-to-go from
    now, per storage state, per run state. Only ``choice`` was returned.

    *Mutation: return the head value as ``None`` and every stored-value test
    fails.*
    """
    plan = solve_at(head=36, end=96, stored=8.0).outcome.desired
    assert plan.head_value, "the dual must be published, not recomputed"
    assert len(plan.head_value) > 1


def test_the_marginal_value_is_a_price_and_the_stored_value_its_integral() -> None:
    """They are two readings of one curve and may never disagree.

    ``stored_value_eur`` is ``V(floor) - V(current)``, which telescopes into the
    sum of the marginal values across the buckets between them. Asserting the
    identity is what stops the two becoming independent estimates that drift.
    """
    solved = solve_at(head=36, end=96, stored=8.0)
    plan = solved.outcome.desired
    bucket_kwh = solved.outcome.bucket_kwh
    assert bucket_kwh

    floor_bucket = int(plan.terminal_floor_kwh / bucket_kwh)
    current_bucket = min(floor_bucket + 12, len(plan.head_value) - 2)
    if current_bucket <= floor_bucket:
        pytest.skip("this lattice has no room above the floor")

    stored, reason = plan.stored_value_eur(
        floor_bucket=floor_bucket, current_bucket=current_bucket
    )
    if stored is None:
        assert reason in {
            STORED_VALUE_UNDEFINED_VIOLATION,
            STORED_VALUE_UNDEFINED_TOP_BUCKET,
        }
        return

    integral = 0.0
    for bucket in range(floor_bucket, current_bucket):
        marginal, marginal_reason = plan.marginal_value_eur_per_kwh(
            bucket, bucket_kwh=bucket_kwh
        )
        if marginal is None:
            pytest.skip(f"lattice not differentiable here: {marginal_reason}")
        integral += marginal * bucket_kwh

    assert stored == pytest.approx(integral, abs=1e-6)


def test_the_marginal_value_is_none_rather_than_zero_where_it_is_undefined() -> None:
    """**``None`` is an answer; ``0.00`` is a claim, and the wrong one.**

    Three states earn it: a violation term that differs between the two buckets --
    whose money terms were never ranked against each other, so their difference
    prices nothing -- an unreachable state, and the top bucket, whose energy
    interval is clamped short by the ceiling so its slope is not one the lattice
    can offer.
    """
    solved = solve_at(head=36, end=96, stored=8.0)
    plan = solved.outcome.desired
    bucket_kwh = solved.outcome.bucket_kwh

    top = len(plan.head_value) - 1
    value, reason = plan.marginal_value_eur_per_kwh(top, bucket_kwh=bucket_kwh)
    assert value is None
    assert reason == STORED_VALUE_UNDEFINED_TOP_BUCKET

    # Below the enforced floor the violation term differs from any feasible state,
    # so the pair was never ranked on money and no price exists between them.
    undefined = [
        plan.marginal_value_eur_per_kwh(bucket, bucket_kwh=bucket_kwh)
        for bucket in range(top)
    ]
    assert any(value is None for value, _reason in undefined), (
        "a lattice with an enforced floor must have somewhere the price is undefined"
    )
    assert all(
        value is not None or reason is not None for value, reason in undefined
    ), "an undefined value must always say why"


def test_the_marginal_value_declines_overall_and_is_not_pointwise_concave() -> None:
    """**Diminishing worth is real; pointwise concavity is not, and is not claimed.**

    The terminal value *is* concave by construction, and that is asserted where it
    is true. The cost-to-go it seeds is a different object: the recursion takes a
    minimum over run states and charges ``minimum_trade_gain_eur`` on every
    transition out of idle. A fixed cost inside a minimisation breaks concavity --
    a property of the model rather than a defect in it -- and the lattice shows it
    plainly. Measured on three shapes: two to nine local rises each, no negative
    prices anywhere, and every curve worth materially less at the top of the pack
    than just above the floor.

    So what is asserted is what is true and what a reader actually uses. An
    invariant demanding pointwise concavity here would be a false one, and the
    next person to meet it would "fix" the optimiser to satisfy it.
    """
    for head, stored in ((36, 8.0), (20, 5.0), (60, 12.0)):
        solved = solve_at(head=head, end=96, stored=stored)
        plan = solved.outcome.desired
        bucket_kwh = solved.outcome.bucket_kwh

        priced = [
            value
            for value, _reason in (
                plan.marginal_value_eur_per_kwh(bucket, bucket_kwh=bucket_kwh)
                for bucket in range(len(plan.head_value))
            )
            if value is not None
        ]
        assert len(priced) > 2, "the curve must be priced somewhere"
        assert all(math.isfinite(value) for value in priced)
        assert all(value >= 0.0 for value in priced), priced
        assert priced[-1] < priced[0], (
            "the last kilowatt-hour a pack can hold must be worth less at the "
            "margin than the first one above the floor"
        )


def test_the_stored_value_prices_the_position_and_not_its_history() -> None:
    """**The reason there is no inventory model, stated as a test.**

    Two solves over the same horizon from the same storage level return the same
    stored value, whatever was paid to reach that level -- because nothing in the
    curve depends on the purchase history. It cannot, and it must not: a rule that
    refused to sell below average cost would decline a profitable spike because of
    a price paid yesterday.
    """
    solved = solve_at(head=36, end=96, stored=8.0)
    again = solve_at(head=36, end=96, stored=8.0)
    bucket_kwh = solved.outcome.bucket_kwh

    plan = solved.outcome.desired
    floor_bucket = int(plan.terminal_floor_kwh / bucket_kwh)
    current = min(floor_bucket + 8, len(plan.head_value) - 2)

    first, _ = plan.stored_value_eur(floor_bucket=floor_bucket, current_bucket=current)
    second, _ = again.outcome.desired.stored_value_eur(
        floor_bucket=floor_bucket, current_bucket=current
    )
    assert first == second


def test_no_decision_path_reads_the_stored_value() -> None:
    """It is an observation. If it ever became an input, this release changed.

    *Mutation: use ``stored_value_eur`` anywhere in the recursion or in Stage B and
    this fails.*
    """
    import ast
    import pathlib

    package = pathlib.Path("custom_components/alpha_ems_manager")
    readers: list[str] = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for outer in ast.walk(tree):
            if not isinstance(outer, ast.FunctionDef):
                continue
            for node in ast.walk(outer):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else None
                if name in {"stored_value_eur", "marginal_value_eur_per_kwh"}:
                    readers.append(f"{path.name}:{outer.name}")

    # Named, not counted: every caller must be a function whose job is to publish.
    permitted = {
        "economic.py:_stored_value_as_dict",
        "economic.py:_value_curve",
        "coordinator.py:_stored_value_eur",
    }
    assert set(readers) <= permitted, sorted(set(readers) - permitted)
    # And none of them is a decision path. Named individually rather than inferred,
    # so a solve growing a call to either is a failure and not a rename.
    forbidden = {"solve", "_walk_forward", "build_outcome", "_decide", "evaluate"}
    assert not (forbidden & {entry.split(":")[1] for entry in readers})


# ===========================================================================
# 4. the head run state -- Stage B reports a fact, Stage A prices it
# ===========================================================================


def _hurdle_shape(**overrides):
    """Return a horizon whose only trade is a modest export at the very head.

    Deliberately marginal: four intervals at 0.30, everything after at 0.10, a
    steady house load and no production. The sale is worth taking, and it is worth
    taking by less than a large hurdle -- which is what makes the run-start fee
    decisive rather than incidental.
    """
    fields = {
        "head": 0,
        "end": 48,
        "stored": 14.0,
        "price_fn": lambda i: 0.30 if i < 4 else 0.10,
        "load_fn": lambda i: 0.30,
        "pv_fn": lambda i: 0.0,
    }
    fields.update(overrides)
    return solve_at(**fields).outcome


def test_a_running_campaign_is_not_abandoned_for_a_fee_it_already_paid() -> None:
    """**R9, and it is the economic half of the boundary failure.**

    The recursion charges ``minimum_trade_gain_eur`` on every transition out of
    idle, and beta.34 began every solve at idle -- so an export physically in
    flight was priced as a *fresh* run start on every refresh, once a quarter, for
    a fee that had already been paid when the campaign began. On 2026-08-29 the
    plan responded by moving the export from 20:00-20:30 to 21:00-21:30 at the very
    boundary Stage B was executing.

    Measured on the shape above with a 0.80 EUR hurdle: seeded idle, the optimiser
    **declines the sale entirely** -- 0.00 kWh exported, 1.68 EUR -- while seeded
    with what is actually running it takes it, exports 6.80 kWh and pays 0.24. Same
    horizon, same prices, same pack. The difference is a fee charged twice.

    **This is not Stage B inventing economics.** It reports a physical fact --
    which direction the inverter is being driven in -- and Stage A remains the only
    thing that decides anything.

    *Mutation: force ``head_run_state`` back to idle and this fails.*
    """
    from custom_components.alpha_ems_manager.economic import (
        RUN_STATE_DISCHARGE,
        RUN_STATE_IDLE,
    )

    fresh = _hurdle_shape(gain=0.80, head_run_state=RUN_STATE_IDLE)
    running = _hurdle_shape(gain=0.80, head_run_state=RUN_STATE_DISCHARGE)
    assert fresh.available and running.available

    assert fresh.desired.planned_grid_export_kwh == pytest.approx(0.0, abs=1e-6)
    assert running.desired.planned_grid_export_kwh > 1.0
    assert running.desired.cost_eur < fresh.desired.cost_eur


def test_starting_something_genuinely_new_still_pays_the_fee() -> None:
    """Only the head is seeded, so the hurdle still governs everything else.

    That bound is what keeps this from becoming a discount on trading. The same
    shape proves it from the other side: with nothing running, a hurdle the trade
    cannot clear still suppresses it -- which is precisely the ``fresh`` plan
    above, and it is why the fix cannot be read as "export is now cheaper".
    """
    from custom_components.alpha_ems_manager.economic import RUN_STATE_IDLE

    cheap = _hurdle_shape(gain=0.10, head_run_state=RUN_STATE_IDLE)
    dear = _hurdle_shape(gain=0.80, head_run_state=RUN_STATE_IDLE)

    assert cheap.desired.planned_grid_export_kwh > 1.0
    assert dear.desired.planned_grid_export_kwh == pytest.approx(0.0, abs=1e-6)


def test_seeding_the_head_cannot_conjure_a_trade_that_is_not_worth_taking() -> None:
    """A waived fee is not a subsidy: the energy still has to be worth selling.

    With no spread at all there is nothing for the seed to rescue, and it rescues
    nothing -- so the seed can only ever remove a double charge, never create a
    reason to trade.
    """
    from custom_components.alpha_ems_manager.economic import RUN_STATE_DISCHARGE

    flat = _hurdle_shape(
        gain=0.80,
        price_fn=lambda i: 0.10,
        head_run_state=RUN_STATE_DISCHARGE,
    )
    assert flat.available
    assert flat.desired.planned_grid_export_kwh == pytest.approx(0.0, abs=1e-6)


def test_the_head_run_state_is_read_from_physical_authority_only() -> None:
    """Stage B may report what is running. It may not report what it would prefer.

    ``_head_run_state`` returns idle unless there is an admitted open row *and* a
    record naming the run that armed it -- so an unowned, unadmitted or merely
    intended direction contributes nothing, which is what keeps this a measurement
    rather than an opinion.
    """
    import inspect

    from custom_components.alpha_ems_manager.coordinator import AlphaEmsCoordinator

    source = inspect.getsource(AlphaEmsCoordinator._head_run_state)
    body = source.split('"""')[-1]

    # Read from the executing row and the persisted claim, and from nothing else.
    assert "self._quarter" in body
    assert "self._plan" in body
    assert "_owned_run_id" in body
    # No price, no plan preference, no Stage-A run anywhere in it.
    for forbidden in ("price", "cost_eur", "desired", "outcome"):
        assert forbidden not in body, forbidden


def test_the_flat_terminal_liquidates_the_pack_and_the_new_one_does_not() -> None:
    """**The beta.34 defect, reproduced on the solver and then fixed on it.**

    This is the test that matters for the terminal value, because it is the only
    one that watches the *plan* change. The properties above are arithmetic on a
    function; this is the optimiser responding to it.

    Same horizon, same prices, same fees, same starting energy -- only the edge
    priced differently. Measured on the reference shape at head 36 with 8 kWh
    stored:

    * flat 25th-percentile credit: ends at **4.21 kWh**, the enforced floor,
      having exported **7.92 kWh** on the horizon's last evening;
    * demand-bounded credit: ends at **11.09 kWh** and exports **1.11 kWh**.

    The flat rule is not being cautious there, it is being wrong: it has priced the
    energy the household is about to consume at roughly half what the household
    will actually pay for it, so selling it looks like a profit. Nothing about the
    fix forbids selling -- the second plan still exports -- it just stops selling
    energy it is going to have to buy back.

    *Mutation: seed the recursion with ``terminal.flat`` and this fails.*
    """
    bounded = _terminal(
        demand_ac_kwh=8.0,
        displaced_price_eur_kwh=0.32,
        export_price_eur_kwh=0.06,
    )

    kept = solve_at(head=36, end=96, stored=8.0, terminal_value=bounded).outcome
    sold = solve_at(head=36, end=96, stored=8.0, terminal_value=bounded.flat).outcome
    assert kept.available and sold.available

    assert kept.desired.end_energy_dc_kwh > sold.desired.end_energy_dc_kwh + 1.0
    assert kept.desired.planned_grid_export_kwh < (sold.desired.planned_grid_export_kwh)
    # The flat rule liquidates to the floor. That is the number that started this.
    assert sold.desired.end_energy_dc_kwh == pytest.approx(
        sold.desired.terminal_floor_kwh, abs=0.05
    )


def test_the_binding_plan_publishes_what_the_old_terminal_would_have_done() -> None:
    """The counterfactual is solved, and it is diagnostics rather than a decision.

    beta.35 ships the new terminal value **binding**, so what it altered has to be
    visible rather than argued: every applicable refresh also solves the same
    horizon with the beta.34 credit and publishes the difference. The legacy plan
    reaches no execution target and is never chosen.
    """
    bounded = _terminal(
        demand_ac_kwh=8.0,
        displaced_price_eur_kwh=0.32,
        export_price_eur_kwh=0.06,
    )
    outcome = solve_at(head=36, end=96, stored=8.0, terminal_value=bounded).outcome

    legacy = outcome.terminal_legacy
    assert legacy is not None and legacy.available
    assert legacy.end_energy_dc_kwh < outcome.desired.end_energy_dc_kwh
    # Diagnostics only: the binding plan is the one every published target is built
    # from, and it is not this one.
    assert outcome.desired is not legacy
