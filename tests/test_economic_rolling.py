"""Rolling re-optimisation: what the plan actually *does*, not what it says.

**This file exists because beta.16 drew a wrong conclusion from a single solve.**

A plan is rebuilt every quarter-hour and only its *first* interval is ever
executed. So a difference concentrated in a plan's tail is discarded before it can
happen, and a whole-horizon figure can be enormous while the realised consequence
is nothing. beta.16 published ``terminal_protection_cost_eur``, the live
installation reported about EUR 3.9 of it, and it read as EUR 3.9 of lost money.

Rolling the horizon forward says otherwise: re-plan each quarter, execute one
interval, roll the state through the same physics, and total what was actually
paid. Measured that way the terminal rule and every alternative to it land within
about ten cents of each other per day. The EUR 3.9 was a tail nobody ever paid.

The harness is deliberately crude about everything except the loop. Prices and
production are simple synthetic shapes; what matters is that the *same* shapes are
handed to every candidate rule, so the comparison is between rules rather than
between scenarios. Physics comes from the solver's own transition table, which is
built from ``apply_request`` -- nothing here reimplements a clamp or an
efficiency.

The candidate rules, and why these four:

* ``ambient`` -- the released rule: end no lower than doing nothing would have.
* ``start`` -- end no lower than you began. Arbitrary, and included to show it.
* ``reserve`` -- end at or above the Phase-7 requirement.
* ``none`` -- no terminal bound at all, only the pointwise reserve.

``reserve`` and ``none`` are expected to be *identical*, and one of the tests
below pins that: the reserve is already enforced at every interval, so requiring
it again at the last one adds nothing. That equivalence is the reason the
candidate list is shorter than it looks.

What beta.18 added, and what it found
-------------------------------------

The beta.17 conclusion -- every rule within about ten cents a day -- was measured
on shapes whose valuable quarters sat in the *middle* of the horizon. beta.18
added four endings the earlier evidence never covered, and two of them break that
conclusion badly.

When the dearest quarters are the horizon's **last** ones, the released rule
refuses to sell into them, because selling would end lower than it started and the
bound forbids that. Measured: **+0.87 EUR against -5.52 EUR** on a single
19-quarter horizon, with a seventh as much energy sold into a 1.20 EUR/kWh peak.

That was a real defect, and beta.18 **fixed it by deleting the rule**: the
coordinator now passes the configured physical floor, and the pointwise dynamic
reserve is the only physical floor the optimizer is given.

The measurement stayed exactly as it was. These tests solve the candidate rules
directly, so they still compare ``ambient`` against ``none`` on the same shapes,
and they still have to hold -- they are the evidence the removal rests on rather
than a record of an outstanding fault. See
``test_the_ratchet_refuses_the_dearest_quarters_when_they_end_the_horizon``, and
``tests/test_terminal_reserve_only.py`` for what production does now.
"""

from __future__ import annotations

import math

import pytest

from custom_components.alpha_ems_manager.const import (
    ECONOMIC_ACTION_DISCHARGE,
    ECONOMIC_ACTION_EXPORT,
)
from custom_components.alpha_ems_manager.economic import IntervalPrice, solve
from custom_components.alpha_ems_manager.simulation import IntervalDemand

from .test_economic_model import (
    EVERYTHING,
    FLOOR_PERCENT,
    horizon_for,
    reference_table,
)

TABLE = reference_table()
FLOOR = TABLE.limits.energy_for_soc(FLOOR_PERCENT)
CEILING = TABLE.limits.energy_for_soc(100.0)

#: Rule name -> how to express it as a terminal floor. ``ambient`` asks for more
#: than the pack holds on purpose: ``solve`` clamps the request down to the
#: ambient walk's endpoint, which is the released behaviour.
RULES = ("ambient", "start", "reserve", "none")


def terminal_for(rule: str, *, soc: float, reserve: float) -> float:
    """Return the terminal floor this rule asks for."""
    if rule == "ambient":
        return CEILING * 2.0
    if rule == "start":
        return soc
    if rule == "reserve":
        return reserve
    return FLOOR


def day_shape(*, pv_total: float, days: int, load_kwh: float = 0.30):
    """Return production and import price per quarter, repeated over ``days``.

    A cheap night, an ordinary daytime, and a dear evening block -- the shape a
    battery exists for. Production is a half-sine across the middle of the day so
    a summer case really does fill the pack and a winter case really does not.
    """
    production, price = [], []
    for index in range(96 * days):
        quarter = index % 96
        if 32 <= quarter < 80 and pv_total > 0.0:
            arc = math.sin(math.pi * (quarter - 32) / 48)
            production.append(max(0.0, pv_total * arc / 30.55))
        else:
            production.append(0.0)
        if quarter < 24:
            price.append(0.10)
        elif 78 <= quarter < 84:
            price.append(0.40)
        else:
            price.append(0.22)
    return production, price, [load_kwh] * (96 * days)


#: Rolled results, memoised. ``roll`` and ``roll_edge`` are pure functions of
#: hashable arguments that each run a dozen solves, and the tests below ask for the
#: same ``(rule, scenario)`` pairs repeatedly -- ten parametrised cases over four
#: rules and a handful of scenarios, re-solving the identical rollouts each time.
#: Measured at 21.4 minutes, the second-largest cost in the suite.
#:
#: A **copy** is handed out on every call: the rollouts return plain dicts, and a
#: shared mutable result is how one test comes to depend on another having run
#: first. The values are floats, so a shallow copy is a complete one.
_ROLL_CACHE: dict[tuple, dict] = {}


def _roll_uncached(
    rule: str,
    *,
    pv_total: float,
    start_kwh: float,
    reserve_kwh: float,
    steps: int = 12,
    tomorrow_known: bool = True,
    days: int = 2,
    offset: int = 40,
):
    """Re-plan every quarter, execute one interval, and total what was paid.

    ``tomorrow_known`` is what makes this able to answer the pre-publication
    question: when it is false the horizon ends at midnight tonight and shrinks
    as the day goes on, which is the state the installation is in before the price
    source publishes. No clock is involved anywhere -- the flag decides what data
    the horizon is given, which is exactly how the real thing works.
    """
    production, price, load = day_shape(pv_total=pv_total, days=days)
    soc = start_kwh
    paid = imported = exported = violation = 0.0
    lowest = start_kwh
    for offset_step in range(steps):
        step = offset + offset_step
        day = step // 96
        if tomorrow_known:
            end = min(96 * (day + 2), 96 * days)
        else:
            end = 96 * (day + 1)
        window = range(step + 1, end)
        if len(window) < 2:
            break
        horizon = horizon_for(
            TABLE,
            demands=[
                IntervalDemand(
                    index=index - (step + 1),
                    baseline_kwh=load[index],
                    pv_kwh=production[index],
                )
                for index in window
            ],
            prices=[
                IntervalPrice(
                    import_eur_kwh=price[index], export_eur_kwh=price[index] * 0.55
                )
                for index in window
            ],
            reserve_kwh=[reserve_kwh] * len(window),
        )
        plan = solve(
            table=TABLE,
            horizon=horizon,
            start_energy_kwh=soc,
            terminal_floor_kwh=terminal_for(rule, soc=soc, reserve=reserve_kwh),
            minimum_trade_gain_eur=0.10,
            permitted=EVERYTHING,
        )
        if not plan.available or not plan.intervals:
            break
        executed = plan.intervals[0]
        # The plan's own arithmetic, from the table the clamp built. Clamped to
        # the physical window because a rolled state must stay a real state.
        soc = min(
            CEILING,
            max(FLOOR, executed.start_energy_dc_kwh + executed.battery_delta_dc_kwh),
        )
        paid += executed.cost_eur
        imported += executed.grid_import_kwh
        exported += executed.grid_export_kwh
        lowest = min(lowest, soc)
        violation += max(0.0, reserve_kwh - soc)
    return {
        "paid_eur": paid,
        "import_kwh": imported,
        "export_kwh": exported,
        "end_kwh": soc,
        "lowest_kwh": lowest,
        "violation_kwh": violation,
    }


SCENARIOS = {
    "summer-high-soc": {"pv_total": 30.0, "start_kwh": 21.0, "reserve_kwh": 15.5},
    "summer-low-soc": {"pv_total": 30.0, "start_kwh": 6.0, "reserve_kwh": 15.5},
    "winter-high-soc": {"pv_total": 2.0, "start_kwh": 19.5, "reserve_kwh": FLOOR},
    "winter-low-soc": {"pv_total": 2.0, "start_kwh": 6.0, "reserve_kwh": FLOOR},
}


# ===========================================================================
# A. the finding: the terminal rule barely matters to what is paid
# ===========================================================================


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
@pytest.mark.parametrize("tomorrow_known", [True, False])
def test_every_terminal_rule_realises_within_ten_cents(
    scenario: str, tomorrow_known: bool
) -> None:
    """**The measurement beta.17 rests on.**

    Four rules whose *plans* differ by euros realise within about ten cents of
    each other, in summer and winter, at high and low state of charge, with and
    without tomorrow's prices. The bound is deliberately generous -- the claim is
    an order of magnitude, not a decimal -- and it is what makes replacing the
    terminal condition unjustifiable on the evidence available.
    """
    results = {
        rule: roll(rule, tomorrow_known=tomorrow_known, **SCENARIOS[scenario])
        for rule in RULES
    }
    paid = [result["paid_eur"] for result in results.values()]

    spread = max(paid) - min(paid)
    assert spread < 0.35, {rule: round(r["paid_eur"], 4) for rule, r in results.items()}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_no_terminal_rule_ever_breaks_the_hard_floor(scenario: str) -> None:
    """Whatever the tail says, the configured floor is never crossed.

    The safety half of the same measurement, and it is careful about *which*
    guarantee is absolute. The configured floor is: the clamp enforces it and no
    lattice or terminal rule can reach past it. The dynamic reserve is a
    *requirement*, and a pack that starts below it cannot be brought above it
    instantly -- ``summer-low-soc`` begins at 6.0 kWh against a 15.5 kWh
    requirement, so a shortfall is unavoidable and the lexicographic objective
    minimises rather than eliminates it.

    So: the floor is inviolable, and a plan that starts at or above its
    requirement must never be the reason the pack falls below it.
    """
    settings = SCENARIOS[scenario]
    for rule in RULES:
        result = roll(rule, **settings)
        assert result["lowest_kwh"] >= FLOOR - 1e-9, (rule, result)
        if settings["start_kwh"] >= settings["reserve_kwh"]:
            assert result["violation_kwh"] == pytest.approx(0.0, abs=1e-9), (
                rule,
                result,
            )
            assert result["lowest_kwh"] >= settings["reserve_kwh"] - 1e-9
        else:
            # Starting short: it may not dig the hole deeper.
            assert result["lowest_kwh"] >= settings["start_kwh"] - 1e-9
            assert result["end_kwh"] >= settings["start_kwh"] - 1e-9


def test_requiring_the_reserve_at_the_end_is_the_same_as_requiring_nothing() -> None:
    """``reserve`` and ``none`` are one rule, not two.

    Proved rather than asserted, because it collapses the design space: the
    reserve is enforced at *every* interval, so demanding it again at the last one
    cannot change a single decision. Any future proposal to "end at the reserve"
    is therefore a proposal to remove the terminal bound, and should be argued as
    one.
    """
    for scenario in sorted(SCENARIOS):
        with_bound = roll("reserve", **SCENARIOS[scenario])
        without = roll("none", **SCENARIOS[scenario])
        assert with_bound == without, scenario


def test_the_plan_figure_dwarfs_what_the_rule_actually_costs() -> None:
    """The specific error beta.16 made, reproduced and measured.

    A single solve says the bound costs euros; rolling the same shape says it
    costs cents. Both numbers are correct about different questions, and beta.16
    published only the one that reads as money.
    """
    scenario = SCENARIOS["summer-high-soc"]
    production, price, load = day_shape(pv_total=scenario["pv_total"], days=1)
    horizon = horizon_for(
        TABLE,
        demands=[
            IntervalDemand(index=i, baseline_kwh=load[i], pv_kwh=production[i])
            for i in range(96)
        ],
        prices=[
            IntervalPrice(import_eur_kwh=price[i], export_eur_kwh=price[i] * 0.55)
            for i in range(96)
        ],
        reserve_kwh=[scenario["reserve_kwh"]] * 96,
    )
    common = {
        "table": TABLE,
        "horizon": horizon,
        "start_energy_kwh": scenario["start_kwh"],
        "minimum_trade_gain_eur": 0.10,
        "permitted": EVERYTHING,
    }
    bounded = solve(terminal_floor_kwh=CEILING * 2.0, **common)
    free = solve(terminal_floor_kwh=FLOOR, **common)
    single_solve_gap = bounded.cost_eur - free.cost_eur

    realised_gap = abs(
        roll("ambient", **scenario)["paid_eur"] - roll("none", **scenario)["paid_eur"]
    )

    assert single_solve_gap > 1.0
    assert realised_gap < single_solve_gap / 5.0


# ===========================================================================
# B. the pre-publication regime, without inventing a price or reading a clock
# ===========================================================================


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_filling_the_pack_before_publication_is_priced_not_forced(
    scenario: str,
) -> None:
    """Tomorrow unknown must not become "charge to full because the data ran out".

    The concern the brief raised -- and the test has to be careful, because
    ending a *winter* day full is often correct: buying a cheap night at 0.10 to
    serve a 0.40 evening is exactly what the battery is for, and a rule that
    forbade it would be the defect.

    So the question is not "did it fill?" but "would it have filled anyway?".
    The counterfactual answers it: with the bound removed entirely the pack must
    reach the same place. Measured on the winter case all three rules end at
    22.0 kWh having imported an identical 30.74 kWh -- the fill is the price
    curve's doing, not the horizon's end.
    """
    settings = SCENARIOS[scenario]
    bounded = roll("ambient", tomorrow_known=False, **settings)
    unbounded = roll("none", tomorrow_known=False, **settings)

    assert bounded["end_kwh"] <= CEILING + 1e-9
    # Within one state-space bucket: the bound may not be what put the energy
    # there. If these diverge, the fill is a horizon artefact and worth a defect.
    assert bounded["end_kwh"] == pytest.approx(
        unbounded["end_kwh"], abs=TABLE.bucket_kwh * 2
    )
    assert bounded["import_kwh"] == pytest.approx(
        unbounded["import_kwh"], abs=TABLE.bucket_kwh * 2
    )


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_losing_tomorrows_prices_changes_no_rule_into_the_best_one(
    scenario: str,
) -> None:
    """The shrinking horizon does not overturn the verdict either.

    Runs the whole comparison with the horizon ending at midnight and shrinking,
    which is the state before the source publishes. If some alternative were
    clearly better *here*, that would be the argument for changing the rule -- and
    it is not: the spread stays in cents.

    **Split by scenario in beta.42, and every comparison is unchanged.** This was
    the last single test costing five minutes, which made it the floor for its whole
    shard however many workers it had. The rollouts are memoised, so the scenarios
    that other tests also ask for are now free here; the rest simply run beside each
    other instead of in series.
    """
    known = {
        rule: roll(rule, tomorrow_known=True, **SCENARIOS[scenario])["paid_eur"]
        for rule in RULES
    }
    unknown = {
        rule: roll(rule, tomorrow_known=False, **SCENARIOS[scenario])["paid_eur"]
        for rule in RULES
    }
    assert max(unknown.values()) - min(unknown.values()) < 0.35, (scenario, unknown)
    # And seeing further never costs money, which is a sanity check on the
    # harness as much as on the optimizer.
    for rule in RULES:
        assert known[rule] <= unknown[rule] + 0.35, (scenario, rule)


def test_the_harness_actually_executes_something() -> None:
    """A comparison of four rules that all did nothing would prove nothing.

    The guard against a silently broken loop: energy has to move, money has to
    change hands, and the state of charge has to end somewhere other than where
    it started.
    """
    result = roll("ambient", **SCENARIOS["summer-high-soc"])

    # Energy crossed the meter and money changed hands. Note that the state of
    # charge legitimately *can* end where it began -- on a sunny midday window the
    # plan holds the pack and exports the surplus, which is exactly right and is
    # why this does not assert that the state of charge moved.
    assert result["import_kwh"] + result["export_kwh"] > 1.0
    assert result["paid_eur"] != pytest.approx(0.0)
    assert result["lowest_kwh"] >= FLOOR


# ===========================================================================
# C. the four shapes the terminal rule was never measured on
# ===========================================================================
#
# beta.17 kept the terminal condition on the evidence that every alternative
# realised within about ten cents a day of it. These are the shapes that
# evidence did not cover, and they are the ones where a horizon-end artefact
# would be most likely to show: a peak in the final quarters, a trough there, and
# a horizon that stops immediately after something worth doing.
#
# **Nothing here changes the optimizer.** If one of these had shown a serious
# defect the correct response was to stop and report, not to redesign quietly.


def edge_shape(kind: str, *, days: int = 2, load_kwh: float = 0.30):
    """Return production, price and load for one awkward horizon ending.

    The tail is what differs. Each shape is otherwise the ordinary day used
    throughout this file, so a difference in outcome is attributable to the
    ending rather than to the whole curve being different.
    """
    production, price = [], []
    total = 96 * days
    for index in range(total):
        quarter = index % 96
        production.append(0.0)
        if quarter < 24:
            base = 0.10
        elif 78 <= quarter < 84:
            base = 0.40
        else:
            base = 0.22
        price.append(base)

    tail = slice(total - 4, total)
    if kind == "late_peak":
        # The dearest quarters of the whole horizon are its last four.
        price[tail] = [1.20] * 4
    elif kind == "cheap_tail":
        price[tail] = [0.01] * 4
    elif kind == "expensive_tail":
        price[tail] = [0.60] * 4
    elif kind == "peak_then_stop":
        # Something well worth doing, and then the data simply ends.
        price[total - 8 : total - 4] = [1.20] * 4
        price[tail] = [0.20] * 4
    else:  # pragma: no cover - guarded by the parametrisation
        raise AssertionError(kind)
    return production, price, [load_kwh] * total


def roll(
    rule: str,
    *,
    pv_total: float,
    start_kwh: float,
    reserve_kwh: float,
    steps: int = 12,
    tomorrow_known: bool = True,
    days: int = 2,
    offset: int = 40,
):
    """Return :func:`_roll_uncached`, solving each distinct rollout exactly once."""
    key = (
        "roll",
        rule,
        pv_total,
        start_kwh,
        reserve_kwh,
        steps,
        tomorrow_known,
        days,
        offset,
    )
    hit = _ROLL_CACHE.get(key)
    if hit is None:
        hit = _ROLL_CACHE[key] = _roll_uncached(
            rule,
            pv_total=pv_total,
            start_kwh=start_kwh,
            reserve_kwh=reserve_kwh,
            steps=steps,
            tomorrow_known=tomorrow_known,
            days=days,
            offset=offset,
        )
    return dict(hit)


def _roll_edge_uncached(
    rule: str, kind: str, *, start_kwh: float, reserve_kwh: float, steps: int = 10
):
    """Roll the last stretch of an awkward horizon, executing one interval each time.

    Starts deliberately close to the end, because the whole question is what the
    rule does when the horizon's edge is within reach.
    """
    production, price, load = edge_shape(kind)
    total = len(price)
    soc = start_kwh
    paid = violation = 0.0
    lowest = start_kwh
    first_actions = []
    for offset in range(steps):
        step = total - steps - 4 + offset
        window = range(step + 1, total)
        if len(window) < 2:
            break
        horizon = horizon_for(
            TABLE,
            demands=[
                IntervalDemand(
                    index=index - (step + 1),
                    baseline_kwh=load[index],
                    pv_kwh=production[index],
                )
                for index in window
            ],
            prices=[
                IntervalPrice(
                    import_eur_kwh=price[index], export_eur_kwh=price[index] * 0.55
                )
                for index in window
            ],
            reserve_kwh=[reserve_kwh] * len(window),
        )
        plan = solve(
            table=TABLE,
            horizon=horizon,
            start_energy_kwh=soc,
            terminal_floor_kwh=terminal_for(rule, soc=soc, reserve=reserve_kwh),
            minimum_trade_gain_eur=0.10,
            permitted=EVERYTHING,
        )
        if not plan.available or not plan.intervals:
            break
        executed = plan.intervals[0]
        soc = min(
            CEILING,
            max(FLOOR, executed.start_energy_dc_kwh + executed.battery_delta_dc_kwh),
        )
        paid += executed.cost_eur
        lowest = min(lowest, soc)
        violation += max(0.0, reserve_kwh - soc)
        first_actions.append(executed.action)
    return {
        "paid_eur": paid,
        "end_kwh": soc,
        "lowest_kwh": lowest,
        "violation_kwh": violation,
        "actions": first_actions,
    }


EDGE_SHAPES = ("late_peak", "cheap_tail", "expensive_tail", "peak_then_stop")


#: Shapes whose most valuable quarters are **not** at the horizon edge. On these
#: the beta.17 conclusion holds and every rule realises within cents.
BENIGN_SHAPES = ("cheap_tail", "peak_then_stop")

#: Shapes whose dearest quarters *are* at the edge. These expose a defect in the
#: released rule -- see the module note below and the beta.18 review.
EDGE_DEFECT_SHAPES = ("late_peak", "expensive_tail")


def roll_edge(
    rule: str, kind: str, *, start_kwh: float, reserve_kwh: float, steps: int = 10
):
    """Return :func:`_roll_edge_uncached`, solving each distinct rollout once."""
    key = ("roll_edge", rule, kind, start_kwh, reserve_kwh, steps)
    hit = _ROLL_CACHE.get(key)
    if hit is None:
        hit = _ROLL_CACHE[key] = _roll_edge_uncached(
            rule, kind, start_kwh=start_kwh, reserve_kwh=reserve_kwh, steps=steps
        )
    return dict(hit)


@pytest.mark.parametrize("kind", BENIGN_SHAPES)
@pytest.mark.parametrize("start_kwh", [8.0, 19.5])
def test_the_rules_agree_when_the_value_is_not_at_the_horizon_edge(
    kind: str, start_kwh: float
) -> None:
    """A cheap tail, and a peak that has already passed: cents apart, as before.

    This is the beta.17 result reproduced on two new shapes. It matters because it
    localises the defect below: the released rule is not generally worse, it is
    worse specifically when the best quarters are the last ones.
    """
    results = {
        rule: roll_edge(rule, kind, start_kwh=start_kwh, reserve_kwh=FLOOR)
        for rule in RULES
    }
    paid = [outcome["paid_eur"] for outcome in results.values()]

    assert max(paid) - min(paid) < 1.2, {
        rule: round(outcome["paid_eur"], 4) for rule, outcome in results.items()
    }


@pytest.mark.parametrize("kind", EDGE_DEFECT_SHAPES)
@pytest.mark.parametrize("start_kwh", [8.0, 19.5])
def test_the_ratchet_refuses_the_dearest_quarters_when_they_end_the_horizon(
    kind: str, start_kwh: float
) -> None:
    """**The measurement that removed the terminal rule. Do not silence this test.**

    beta.18 pinned this as an outstanding defect. It is now the evidence for a
    deletion, and every assertion below is unchanged: if the old rule ever stops
    looking worse on these shapes, the justification for removing it has gone and
    that needs to be known.

    The rule this measures is the endpoint of the idle-with-absorption walk.
    On a horizon with no production ahead that walk is flat, so the bound equals
    the *current* state of charge -- and the plan may then never end lower than it
    started. When the dearest quarters of the horizon are its **last** ones,
    selling into them would end lower, so it does not sell.

    Measured on a 19-quarter horizon ending in four quarters at 1.20 EUR/kWh,
    starting at 19.5 kWh:

    * released rule: cost **+0.87 EUR**, sold **1.17 kWh** into the peak;
    * no terminal bound: cost **-5.52 EUR**, sold **8.29 kWh** into the peak.

    Under rolling execution the released rule pays about **3 EUR more** over the
    last ten quarters and ends at the ceiling having *bought* into the peak.

    beta.17 measured every rule as within ten cents a day of the others and kept
    the bound on that evidence. **That evidence was incomplete**: none of its
    shapes had value at the horizon edge, which is exactly where a real Frank day
    sits before tomorrow's prices publish -- the tail of the visible horizon is the
    evening peak.

    **What beta.18 did about it.** It removed the rule. The coordinator passes the
    configured physical floor and nothing else, and the reserve -- already enforced
    at every interval, and forecast further ahead than the prices reach -- is the
    only physical floor. Nothing replaced it: no continuation value, no salvage
    term, no boundary bridge.

    This test still solves the candidate rules directly, so it keeps measuring what
    the old rule would do rather than what production does. That is deliberate. It
    is the reason the deletion was justified, and it is cheap to keep true.
    """
    results = {
        rule: roll_edge(rule, kind, start_kwh=start_kwh, reserve_kwh=FLOOR)
        for rule in RULES
    }
    bounded = results["ambient"]["paid_eur"]
    unbounded = results["none"]["paid_eur"]

    # The finding: the removed rule costs materially more on these shapes.
    assert bounded > unbounded + 0.5, {
        rule: round(outcome["paid_eur"], 4) for rule, outcome in results.items()
    }
    # And "end no lower than you started" is the mechanism, so those two agree.
    assert results["ambient"]["paid_eur"] == pytest.approx(
        results["start"]["paid_eur"], abs=1e-9
    )
    # While the two rules that permit net discharge agree with each other.
    assert results["reserve"]["paid_eur"] == pytest.approx(unbounded, abs=1e-9)


def test_the_ratchet_is_the_bound_equalling_the_current_state_of_charge() -> None:
    """The mechanism, isolated from the rolling loop.

    Not a property of the harness: a single solve on a no-production horizon
    reports an enforced terminal floor equal to the state of charge it started
    from, which is what forbids net discharge.
    """
    production, price, load = edge_shape("late_peak")
    total = len(price)
    step = total - 20
    window = range(step + 1, total)
    horizon = horizon_for(
        TABLE,
        demands=[
            IntervalDemand(
                index=index - (step + 1),
                baseline_kwh=load[index],
                pv_kwh=production[index],
            )
            for index in window
        ],
        prices=[
            IntervalPrice(
                import_eur_kwh=price[index], export_eur_kwh=price[index] * 0.55
            )
            for index in window
        ],
        reserve_kwh=[FLOOR] * len(window),
    )
    common = {
        "table": TABLE,
        "horizon": horizon,
        "start_energy_kwh": 19.5,
        "minimum_trade_gain_eur": 0.10,
        "permitted": EVERYTHING,
    }
    bounded = solve(terminal_floor_kwh=CEILING * 2.0, **common)
    free = solve(terminal_floor_kwh=FLOOR, **common)

    assert bounded.terminal_floor_kwh == pytest.approx(19.5, abs=0.3)
    assert bounded.end_energy_dc_kwh == pytest.approx(19.5, abs=0.3)
    # Six euros of difference on one horizon, and a seventh of the peak sold.
    assert bounded.cost_eur - free.cost_eur > 5.0
    peak = [index for index, value in enumerate(price[step + 1 : total]) if value > 1.0]
    sold_bounded = sum(bounded.intervals[index].grid_export_kwh for index in peak)
    sold_free = sum(free.intervals[index].grid_export_kwh for index in peak)
    assert sold_free > sold_bounded * 5.0


@pytest.mark.parametrize("kind", EDGE_SHAPES)
def test_an_awkward_ending_never_breaks_the_floor(kind: str) -> None:
    """The configured floor holds at every horizon edge, under every rule.

    The safety half, and it is unaffected by the defect above: the ratchet makes
    the plan hold *too much* energy rather than too little, so nothing here can
    strand the house.
    """
    for rule in RULES:
        outcome = roll_edge(rule, kind, start_kwh=19.5, reserve_kwh=FLOOR)
        assert outcome["lowest_kwh"] >= FLOOR - 1e-9, (rule, kind)


@pytest.mark.parametrize("kind", EDGE_SHAPES)
def test_a_substantial_requirement_is_met_through_an_awkward_ending(kind: str) -> None:
    """With a real reserve in force the requirement governs the tail, under every rule.

    The live installation runs a requirement around 15.5 kWh in summer, which is a
    far stronger constraint than the configured floor -- and it is met on all four
    shapes without a single violation.
    """
    for rule in RULES:
        outcome = roll_edge(rule, kind, start_kwh=19.5, reserve_kwh=15.5)
        assert outcome["lowest_kwh"] >= 15.5 - 1e-9, (rule, kind)
        assert outcome["violation_kwh"] == pytest.approx(0.0, abs=1e-9), (rule, kind)


def test_a_late_peak_is_reachable_by_some_rule() -> None:
    """A guard on the shape: if nothing could sell into it, it would prove nothing.

    The released rule mostly refuses -- that is the defect above -- so this asserts
    the *shape* is sound by checking the unbounded rule does trade into it.
    """
    outcome = roll_edge("none", "late_peak", start_kwh=19.5, reserve_kwh=FLOOR)

    assert any(
        action in (ECONOMIC_ACTION_EXPORT, ECONOMIC_ACTION_DISCHARGE)
        for action in outcome["actions"]
    )
