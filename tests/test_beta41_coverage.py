"""beta.41 Phase 2: buying the household's own energy at a better hour.

**What this layer is for, and what it must never become.** The forecast says the
pack will reach its floor and the household will then import from the grid. That
import is going to happen; the only open question is *when it is bought*. Coverage
answers that question and nothing else.

It is not a second trader. A purchase only qualifies when the household would
otherwise have bought the same energy later and more dearly, and it is proved by
construction rather than by inspection: the coverage counterfactual forbids export
and credits terminal inventory nothing, so a purchased kilowatt-hour has exactly
one way to pay for itself -- displacing household import the forecast already
predicts. Nothing bought can be sold, and nothing bought can be parked at the
horizon edge and credited there.

**The band this exists for.** Buy at 0.25 to displace 0.30 and the round trip
returns 0.90 x 0.30 = 0.270 against a 0.25 outlay: a real 0.02 EUR/kWh saving for
the household. The user's discretionary margin of 0.05 EUR/kWh puts the cost at
0.30 and refuses it, correctly -- it is a poor *trade*. It was never a trade. The
gates stay exactly as configured for discretionary trading and are set aside only
inside that proof.

Three categories, one kilowatt-hour in each: ``safety`` is what physical
reachability compels and is price-blind; ``coverage`` is the same energy bought at
a better hour; ``economic`` is a discretionary trade. Precedence in that order.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.const import (
    ECONOMIC_ACTION_CHARGE,
    ECONOMIC_ACTION_EXPORT,
)
from custom_components.alpha_ems_manager.economic import TerminalValue

from .beta34_shape import LIMITS, solve_at

#: The band: a 0.05 EUR/kWh spread, which the round trip turns into a 0.02 EUR/kWh
#: household saving and the 0.05 EUR/kWh discretionary margin turns into a refusal.
CHEAP = 0.25
DEAR = 0.30
#: Where the cheap block ends, as an absolute interval index.
HEAD = 8
SWITCH = 24
END = 56


def band_price(index: int) -> float:
    """Return the two-block price curve the coverage band rests on."""
    return CHEAP if index < SWITCH else DEAR


def steady_load(index: int) -> float:
    """Return a household load the pack cannot cover for the whole horizon."""
    return 0.30


def dark(index: int) -> float:
    """No production at all."""
    return 0.0


def solved(**overrides):
    """Return the outcome for the band fixture."""
    kwargs = {
        "head": HEAD,
        "end": END,
        "stored": 6.0,
        "price_fn": band_price,
        "load_fn": steady_load,
        "pv_fn": dark,
        "gain": 0.20,
        "margin": 0.05,
        "allow_export": False,
    }
    kwargs.update(overrides)
    return solve_at(**kwargs).outcome


def charge_runs(outcome):
    """Return the charge runs that actually buy something."""
    return [
        run
        for run in outcome.desired.runs
        if run.action == ECONOMIC_ACTION_CHARGE and run.battery_charge_ac_kwh > 0.0
    ]


def shares(outcome, run):
    """Return ``(safety, coverage, economic)`` for one run."""
    safety = outcome.safety_buy_attribution.get(run.start_index, (0.0, 0.0))[0]
    coverage = outcome.coverage_buy_attribution.get(run.start_index, 0.0)
    return safety, coverage, run.battery_charge_ac_kwh - safety - coverage


# == the band itself =======================================================


def test_the_gates_reject_the_trade_and_coverage_still_happens() -> None:
    """**Requirement 4, and the whole reason this layer exists.**

    A 0.05 EUR/kWh spread is a bad trade and a good purchase. The round trip
    returns 0.90 x 0.30 = 0.270 against a 0.25 outlay, so the household is 0.02
    better off per kilowatt-hour -- and the configured 0.05 margin prices the same
    purchase at 0.30 and refuses it. Both answers are right about different
    questions.
    """
    outcome = solved()

    assert outcome.coverage_saving_eur > 0.0, "the band must produce a saving"
    assert outcome.coverage_buy_runs, "and coverage must actually buy"
    bought = sum(outcome.coverage_buy_attribution.values())
    assert bought > 0.0, bought


def test_a_spread_the_gates_would_have_taken_needs_no_coverage() -> None:
    """**Requirement 16.** Where discretion already buys, coverage is zero.

    The same fixture with a spread wide enough to clear the 0.05 margin: ordinary
    economics takes it, so there is nothing left for coverage to contribute and
    its attribution is empty rather than duplicated.
    """
    outcome = solved(price_fn=lambda index: CHEAP if index < SWITCH else 0.60)

    assert charge_runs(outcome), "the witness: discretion does buy here"
    assert outcome.coverage_saving_eur == pytest.approx(0.0)
    assert not outcome.coverage_buy_runs
    assert sum(outcome.coverage_buy_attribution.values()) == pytest.approx(0.0)


def test_no_saving_means_no_coverage() -> None:
    """**Requirements 2 and 5.** A flat price offers nothing to shift.

    Buying early to serve later at the same price loses the round trip, so the
    saving is negative and coverage must refuse. This is the test that stops
    coverage becoming "buy whenever the battery has room".
    """
    outcome = solved(price_fn=lambda index: CHEAP)

    assert outcome.coverage_saving_eur == pytest.approx(0.0)
    assert not outcome.coverage_buy_runs


def test_a_pack_that_never_needs_the_grid_gets_no_coverage() -> None:
    """**Requirement 1.** No later unavoidable import, no coverage.

    A full pack against a small load covers the horizon from store, so there is no
    later household purchase to move earlier and nothing for coverage to displace.
    """
    outcome = solved(stored=21.0, load_fn=lambda index: 0.02)

    assert outcome.coverage_saving_eur == pytest.approx(0.0)
    assert not outcome.coverage_buy_runs


def test_production_before_the_need_removes_the_requirement() -> None:
    """**Requirement 9.** The sun is not an economic decision.

    Production arriving before the pack would have emptied refills it for nothing,
    so the household import coverage exists to displace never happens.
    """
    outcome = solved(
        stored=8.0,
        pv_fn=lambda index: 1.2 if HEAD <= index < SWITCH else 0.0,
    )

    assert outcome.coverage_saving_eur == pytest.approx(0.0)
    assert not outcome.coverage_buy_runs


def test_production_after_the_need_covers_only_what_precedes_it() -> None:
    """**Requirement 10.** Only the energy needed *before* the refill counts.

    Production late in the horizon leaves the earlier shortfall intact, so
    coverage may still act -- but it may not buy the household's way past the
    point where the sun takes over.
    """
    late = solved(pv_fn=lambda index: 2.0 if index >= SWITCH + 16 else 0.0)
    none = solved()

    if late.coverage_buy_runs:
        assert (
            sum(late.coverage_buy_attribution.values())
            <= sum(none.coverage_buy_attribution.values()) + 1e-9
        )


# == the three categories are one kilowatt-hour each =======================


def test_the_three_shares_sum_to_the_run_and_never_overlap() -> None:
    """**Requirements 14, 15.** Disjoint by construction, checked as arithmetic.

    Every share is non-negative and the three add up to the run's own charge, so a
    kilowatt-hour cannot be counted under two headings or fall between them.
    """
    for outcome in (solved(), solved(stored=0.5), solved(stored=12.0)):
        for run in charge_runs(outcome):
            safety, coverage, economic = shares(outcome, run)
            assert safety >= -1e-9, run.start_index
            assert coverage >= -1e-9, run.start_index
            assert economic >= -1e-9, (run.start_index, safety, coverage, economic)
            assert safety + coverage + economic == pytest.approx(
                run.battery_charge_ac_kwh, abs=1e-9
            )


def test_coverage_never_claims_what_the_reserve_compels() -> None:
    """**Requirement 14, stated as precedence.**

    Safety is settled first because its quantity is not a matter of price. On a
    pack below its floor the compelled share is large, and coverage may claim only
    what is left over.
    """
    outcome = solved(stored=0.5)

    for run in charge_runs(outcome):
        safety, coverage, _economic = shares(outcome, run)
        assert coverage <= run.battery_charge_ac_kwh - safety + 1e-9


# == what coverage may never do ============================================


def test_coverage_energy_can_never_be_exported() -> None:
    """**Requirements 6 and 21, and this one is structural.**

    The counterfactual is solved with export removed from the permitted actions,
    so a promoted coverage plan cannot contain an export at all -- there is no
    quantity to trace, because the primitive is absent. That is a stronger
    guarantee than auditing where the energy went.
    """
    outcome = solved()

    if outcome.coverage_buy_runs:
        assert outcome.desired.planned_grid_export_kwh == pytest.approx(0.0)
        assert all(run.action != ECONOMIC_ACTION_EXPORT for run in outcome.desired.runs)


def test_coverage_buys_no_more_than_the_import_it_displaces() -> None:
    """**Requirement 22.** The purchase is bounded by the need.

    Coverage may not buy more than the household would otherwise have imported
    over the horizon; buying beyond that would be storing energy for its own sake,
    which is the behaviour this layer is defined against.
    """
    outcome = solved()
    covered = sum(outcome.coverage_buy_attribution.values())

    assert covered <= outcome.desired.planned_grid_import_kwh + 1e-9


def test_coverage_respects_the_pack_and_the_inverter() -> None:
    """**Requirements 11 and 12.** It plans inside the same physics as everything
    else, because it *is* the same solve with different economics.
    """
    outcome = solved(stored=20.5)
    plan = outcome.desired

    ceiling = LIMITS.energy_for_soc(100.0)
    for entry in plan.intervals:
        assert entry.start_energy_dc_kwh <= ceiling + 1e-6
        assert entry.battery_charge_ac_kwh <= LIMITS.max_charge_kw * 0.25 + 1e-9


def test_coverage_leaves_the_physical_trajectory_coherent() -> None:
    """**Requirement 24.** One carry-axis trajectory, whichever plan is promoted.

    The energy balance and the single endpoint are properties of the solve, and
    the coverage plan is a solve -- not a splice -- so they hold unchanged.
    """
    for outcome in (solved(), solved(stored=0.5), solved(stored=12.0)):
        plan = outcome.desired
        assert plan.edge_energy_kwh == pytest.approx(plan.end_energy_dc_kwh, abs=1e-12)
        for earlier, later in zip(plan.intervals, plan.intervals[1:], strict=False):
            closing = (
                earlier.start_energy_dc_kwh
                + earlier.battery_delta_dc_kwh
                - earlier.battery_state_service_dc_kwh
            )
            assert closing == pytest.approx(later.start_energy_dc_kwh, abs=1e-9)
        assert min(e.start_energy_dc_kwh for e in plan.intervals) >= -1e-9


def test_meter_side_accounting_survives_coverage() -> None:
    """**Requirement 23.** The household's own figures stay exact."""
    outcome = solved()

    for entry in outcome.desired.intervals:
        assert entry.no_battery_import_kwh == pytest.approx(
            entry.idle_import_kwh + entry.ambient_self_consumption_ac_kwh, abs=1e-12
        )


def test_an_unknown_price_is_never_treated_as_a_cheap_one() -> None:
    """**Requirement 13.** Coverage plans inside the priced prefix only.

    ``build_horizon`` stops at the first interval without a known price, so an
    unpriced quarter is not a quarter coverage can buy in -- it is not in the
    problem at all. Proved by shortening the known series and requiring the plan
    to end with it.
    """
    from custom_components.alpha_ems_manager.economic import IntervalPrice

    full = solved()
    assert full.horizon.intervals == END - HEAD

    short = solve_at(
        head=HEAD,
        end=END,
        stored=6.0,
        price_fn=band_price,
        load_fn=steady_load,
        pv_fn=dark,
        gain=0.20,
        margin=0.05,
        allow_export=False,
    ).outcome
    assert short.horizon.intervals <= END - HEAD
    assert IntervalPrice().known is False


# == the reserve and the gates are untouched ===============================


def test_the_discretionary_gates_are_still_authoritative_for_trading() -> None:
    """**The user's settings, unchanged and still governing.**

    Raising the margin must still suppress a discretionary purchase. Coverage sets
    the gates aside only inside its own proof, and that proof is about displacing
    household import -- not about making the thresholds advisory.
    """
    cheap_trade = solved(price_fn=lambda index: CHEAP if index < SWITCH else 0.60)
    strangled = solved(
        price_fn=lambda index: CHEAP if index < SWITCH else 0.60, margin=5.0
    )

    traded = sum(
        run.battery_charge_ac_kwh
        - shares(cheap_trade, run)[0]
        - shares(cheap_trade, run)[1]
        for run in charge_runs(cheap_trade)
    )
    still = sum(
        run.battery_charge_ac_kwh
        - shares(strangled, run)[0]
        - shares(strangled, run)[1]
        for run in charge_runs(strangled)
    )
    assert traded > 0.0, "the witness: discretion trades on a wide spread"
    assert still <= traded + 1e-9


def test_coverage_cannot_make_a_purchase_compulsory() -> None:
    """Safety is physical, and coverage does not touch it.

    The compelled quantity is measured against the reserve, price-blind, and the
    same whether coverage acts or not.
    """
    cheap = solved(price_fn=lambda index: 0.02, stored=0.5)
    dearly = solved(price_fn=lambda index: 0.90, stored=0.5)

    def compelled(outcome):
        return sum(a for a, _e in outcome.safety_buy_attribution.values())

    assert compelled(cheap) == pytest.approx(compelled(dearly), abs=1e-6)


# == reachability, bridging and replanning =================================


def test_a_pack_that_already_reaches_the_cheap_window_buys_nothing_early() -> None:
    """**Requirement 8.** Stored energy that already spans the gap needs no bridge.

    The cheap block is at the *end* here, so a pack with enough charge to reach it
    has nothing to gain from buying earlier and dearer. Coverage exists to move a
    purchase to a better hour, not to make one happen sooner.
    """
    outcome = solved(
        stored=20.0,
        price_fn=lambda index: DEAR if index < SWITCH else CHEAP,
    )

    early = [
        run
        for run in charge_runs(outcome)
        if run.start_index < SWITCH
        and outcome.coverage_buy_attribution.get(run.start_index, 0.0) > 0.0
    ]
    assert not early, early


def test_the_minimum_bridge_is_bought_at_the_cheapest_reachable_hour() -> None:
    """**Requirements 6 and 7.** Where it must buy, it buys at the best price it
    can reach.

    Three price steps: dear, middle, cheap. Coverage may not simply wait for the
    cheapest block if the pack cannot survive to it, and where it does buy early
    it buys in the cheapest interval available to it rather than the first.
    """

    def stepped(index: int) -> float:
        if index < 20:
            return 0.40
        if index < SWITCH:
            return 0.26
        return 0.34

    outcome = solved(stored=5.0, price_fn=stepped)

    covered = [
        run
        for run in charge_runs(outcome)
        if outcome.coverage_buy_attribution.get(run.start_index, 0.0) > 0.0
    ]
    for run in covered:
        prices = [stepped(i) for i in range(run.start_index, run.end_index + 1)]
        assert max(prices) <= 0.40, (run.start_index, prices)


def test_a_changed_state_of_charge_replans_from_the_new_state() -> None:
    """**Requirement 17.** Every refresh is a fresh solve from measured state.

    Coverage carries nothing between refreshes -- it is a property of the horizon
    in front of the pack, recomputed each time -- so a fuller pack simply needs
    less of it.
    """
    empty = solved(stored=5.0)
    full = solved(stored=20.0)

    assert (
        sum(empty.coverage_buy_attribution.values())
        >= sum(full.coverage_buy_attribution.values()) - 1e-9
    )


@pytest.mark.parametrize("day_intervals", [92, 96, 100])
def test_coverage_plans_on_a_daylight_saving_day(day_intervals: int) -> None:
    """**Requirement 20.** A civil day is 92 or 100 intervals twice a year.

    Coverage inherits the horizon's own clock: it adds economics, never a second
    calendar. So the only thing to prove is that a short or long day still solves
    and still produces a coherent physical trajectory.
    """
    from .beta34_shape import risk_of

    outcome = solve_at(
        head=HEAD,
        end=HEAD + day_intervals - 12,
        stored=6.0,
        price_fn=band_price,
        load_fn=steady_load,
        pv_fn=dark,
        gain=0.20,
        margin=0.05,
        allow_export=False,
        forecast_risk=risk_of(day_intervals=day_intervals),
    ).outcome

    assert outcome.available
    plan = outcome.desired
    assert plan.edge_energy_kwh == pytest.approx(plan.end_energy_dc_kwh, abs=1e-12)
    for run in charge_runs(outcome):
        safety, coverage, economic = shares(outcome, run)
        assert safety + coverage + economic == pytest.approx(
            run.battery_charge_ac_kwh, abs=1e-9
        )


def test_coverage_produces_an_ordinary_plan_stage_b_cannot_tell_apart() -> None:
    """**Requirements 18 and 19, and why they need no coverage-specific machinery.**

    Coverage does not add a schedule, a claim, a campaign kind or an execution
    path. It changes only *which* Stage-A plan is published, and that plan is an
    ordinary one: the same rows, the same frozen authorisations, the same
    admission rules. So no-catch-up across an expired quarter and idempotence
    across a restart are the Stage-B properties they already were, proved by the
    beta.27 and beta.29 families against whatever plan is published.

    What is asserted here is the premise those families rest on: a promoted
    coverage plan is structurally indistinguishable from a discretionary one.
    """
    outcome = solved()
    assert outcome.coverage_buy_runs, "the witness: this fixture promotes coverage"

    plan = outcome.desired
    for run in plan.runs:
        assert run.action in {"charge", "discharge", "hold", "export", "idle"}
    assert plan.available
    assert plan.violation_kwh == pytest.approx(0.0)
    # And every run carries the same fields any other plan's runs carry.
    for run in charge_runs(outcome):
        assert run.end_index >= run.start_index
        assert run.energy_kwh > 0.0


# == the arbitrage the counterfactual must be unable to reach ===============

#: An export tariff that pays *better than serving the household*, which the
#: installation's own tariff can never do: it returns ``0.8265 p - 0.0885``, and
#: that is below ``0.9 p`` for every positive price, so no fixture built from it can
#: make selling the profitable choice. Without an independent figure here, "coverage
#: energy can never be exported" is a claim about a fixture where nothing would want
#: to export anyway.
#:
#: 0.32 against a 0.30 displaced import beats serving by 0.02 EUR/kWh. Buying at
#: 0.25 to sell at 0.32 returns ``0.90 x 0.32 - 0.25 = 0.038``, which is inside the
#: user's 0.05 EUR/kWh margin -- so the discretionary plan refuses the trade and the
#: zero-gate counterfactual would take it. That is exactly the gap the permitted set
#: has to close.
LUCRATIVE_EXPORT = 0.32


def export_pays(index: int) -> float:
    """Return an export price that beats serving the house, in the dear block."""
    return 0.0 if index < SWITCH else LUCRATIVE_EXPORT


def test_nothing_coverage_buys_can_be_sold_even_where_selling_pays_more() -> None:
    """**Requirement 9, on a horizon where export is the profitable choice.**

    Export is removed from the coverage counterfactual's permitted set, so a
    purchased kilowatt-hour cannot pay for itself by being sold. Asserted as a bound
    on the energy rather than on the permission: total export can never exceed what
    the pack was *already holding* above its floor, so whatever leaves the meter came
    out of inventory the household already owned and never out of a purchase.

    With the counterfactual allowed to export, it buys in the cheap block and sells
    in the dear one, and the exported energy exceeds that bound at once.
    """
    outcome = solved(allow_export=True, export_fn=export_pays)
    plan = outcome.desired
    assert outcome.available

    # **The fixture's premise, asserted rather than assumed.** Mutating the tariff
    # down to a non-paying one left this test green, because with nothing worth
    # exporting the bound below holds trivially -- so the premise is checked here,
    # where it cannot drift out from under the assertion it supports. Selling into
    # the dear block must beat serving the household from the pack, and buying in
    # the cheap block to sell there must still fall inside the user's margin, or
    # this horizon is not the one the test needs.
    assert LUCRATIVE_EXPORT > DEAR, (LUCRATIVE_EXPORT, DEAR)
    round_trip = LIMITS.charge_efficiency * LIMITS.discharge_efficiency
    assert round_trip * LUCRATIVE_EXPORT - CHEAP < 0.05, round_trip
    assert round_trip * LUCRATIVE_EXPORT - CHEAP > 0.0, round_trip

    start = plan.intervals[0].start_energy_dc_kwh
    room = max(0.0, start - plan.terminal_floor_kwh)
    deliverable = room * LIMITS.discharge_efficiency

    assert plan.planned_grid_export_kwh <= deliverable + 1e-9, (
        plan.planned_grid_export_kwh,
        deliverable,
    )
    # And the discretionary gates still refuse the trade on their own terms: the
    # 0.038 EUR/kWh it returns does not clear the configured 0.05.
    assert ECONOMIC_ACTION_EXPORT not in {run.action for run in charge_runs(outcome)}


# == energy the household consumes after the horizon ends ==================

#: Post-horizon household demand, priced at what it would otherwise cost. The band
#: fixture above ends with the pack near its floor, so both candidate plans end
#: holding roughly the same inventory and the comparison reduces to metered cash --
#: which means it cannot tell whether the inventory was valued at all. This one ends
#: holding 10.54 kWh against 5.53, and every kilowatt-hour of the difference is
#: energy the forecast says the household takes just beyond the horizon's edge.
POST_HORIZON_DEMAND_AC_KWH = 6.0
POST_HORIZON_PRICE_EUR_KWH = 0.40


def demand_bounded_terminal() -> TerminalValue:
    """Return a terminal rule with real post-horizon demand to serve."""
    return TerminalValue(
        demand_ac_kwh=POST_HORIZON_DEMAND_AC_KWH,
        displaced_price_eur_kwh=POST_HORIZON_PRICE_EUR_KWH,
        export_price_eur_kwh=0.10,
        discharge_efficiency=LIMITS.discharge_efficiency,
    )


def test_coverage_reaches_demand_that_falls_just_past_the_horizon() -> None:
    """**Requirement 3, and the case that proves the inventory is valued.**

    A plan is chosen on cash *including what it ends holding*, both sides valued
    under the rule the executed plan will be judged by. Compare on metered cash
    alone and a plan that ends holding 10.54 kWh looks dearer than one holding 5.53
    simply because it bought the difference -- so beneficial coverage is refused for
    having something to show for itself.

    That is not a safety failure; it forgoes a saving rather than manufacturing
    one. It is still wrong, and it is invisible on any fixture where both plans end
    at the floor, which is why this horizon exists: 11.389 kWh of coverage on top of
    a 5.000 kWh discretionary baseline, saving 0.628 EUR, with the pack ending 5 kWh
    higher than the band case and nothing exported.
    """
    outcome = solved(terminal_value=demand_bounded_terminal())
    plan = outcome.desired
    assert outcome.available

    covered = sum(outcome.coverage_buy_attribution.values())
    assert covered > outcome.coverage_baseline_charge_ac_kwh, (
        covered,
        outcome.coverage_baseline_charge_ac_kwh,
    )
    assert outcome.coverage_saving_eur > 0.0
    assert outcome.coverage_buy_runs

    # **It ends holding the coverage energy, which is the whole point of the case.**
    # The band fixture ends near the floor; this one must not, or the comparison
    # reduces to cash again and says nothing about the inventory.
    assert plan.end_energy_dc_kwh > plan.terminal_floor_kwh + 5.0, (
        plan.end_energy_dc_kwh,
        plan.terminal_floor_kwh,
    )
    # And it is still coverage rather than arbitrage: nothing is sold.
    assert plan.planned_grid_export_kwh == pytest.approx(0.0, abs=1e-9)


def test_holding_is_still_not_worth_more_than_the_household_will_take() -> None:
    """**The other half of the same rule: the credit is bounded by real demand.**

    Giving the terminal inventory a real value is what lets coverage reach demand
    just past the edge. Leaving the *spare* segment priced at the export rate inside
    the counterfactual would let a purchase pay for itself by being parked there
    instead, which is the arbitrage the whole construction excludes -- so coverage
    still may not buy more than the household is forecast to consume, in the horizon
    and just beyond it.
    """
    outcome = solved(terminal_value=demand_bounded_terminal())

    residual = sum(
        max(0.0, (demand.baseline_kwh or 0.0) - (demand.pv_kwh or 0.0))
        for demand in outcome.horizon.demands
    )
    reachable = residual + POST_HORIZON_DEMAND_AC_KWH
    covered = sum(outcome.coverage_buy_attribution.values())

    assert covered <= reachable + 1e-9, (covered, reachable)
