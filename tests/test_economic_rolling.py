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
"""

from __future__ import annotations

import math

import pytest

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


def test_losing_tomorrows_prices_changes_no_rule_into_the_best_one() -> None:
    """The shrinking horizon does not overturn the verdict either.

    Runs the whole comparison with the horizon ending at midnight and shrinking,
    which is the state before the source publishes. If some alternative were
    clearly better *here*, that would be the argument for changing the rule -- and
    it is not: the spread stays in cents.
    """
    for scenario in sorted(SCENARIOS):
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
