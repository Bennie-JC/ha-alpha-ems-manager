"""How much the battery must hold, and the proof that the figure means it.

Every case here is **synthetic and says so**. The live installation was read four
times on one August afternoon; it supplied the battery configuration, the horizon
length and the shape of a real sunny day, and it could not supply a winter
horizon, a daylight-saving transition, a missing interval or a requirement above
the pack. Those are covered by construction rather than by observation, and none
of them is claimed as live-verified.

Two things dominate this file.

**The definition is proved against an independent oracle**, not restated. A
requirement is only a requirement if starting there is enough and starting a
whisker below it is not, so the load-bearing tests hand ``F + M[i]`` to the
Phase-3 simulator -- which applies every limit through the clamp and knows
nothing about this module -- and check both halves. Those tests pass only with
``absorb_surplus=True``, and that is not an accident of setup: the flag *is* the
replenishment assumption, so needing it is the proof of what the figure assumes.

**The requirement is not the peak.** An earlier draft published the largest
requirement anywhere in the horizon instead. It is kept as a diagnostic and it is
wrong as a reserve, most visibly at midday: it would hold six kilowatt-hours
against tonight while the sun is about to supply them. Scenarios A and C exist to
pin that difference, and ``test_reserve_mutations`` pins the substitution.

Arithmetic is asserted at exact values wherever the arithmetic is exact.
"""

from __future__ import annotations

import math
from datetime import date
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest

from custom_components.alpha_ems_manager.battery import (
    BatteryLimits,
    build_state,
    static_reserve,
)
from custom_components.alpha_ems_manager.const import (
    CONSTRAINT_MAX_CHARGE_POWER,
    CONSTRAINT_MAX_DISCHARGE_POWER,
    RESERVE_BOUND_HEADROOM,
    RESERVE_BOUND_TRUNCATED,
    RESERVE_BOUND_TRUNCATED_HEADROOM,
    RESERVE_HORIZON_CLOSED,
    RESERVE_HORIZON_TRUNCATED,
    RESERVE_UNAVAILABLE_FORECAST,
    RESERVE_UNAVAILABLE_HORIZON_INCOMPLETE,
)
from custom_components.alpha_ems_manager.policy import ReserveGuardPolicy
from custom_components.alpha_ems_manager.reserve import (
    build_reserve,
    build_reserve_pv_blind,
    build_reserve_same_interval_only,
    compare_to_trajectory,
    shortfall,
)
from custom_components.alpha_ems_manager.simulation import IntervalDemand, simulate
from custom_components.alpha_ems_manager.storage import expected_quarters_for

from .test_battery_model import limits_for

#: The reference installation, as read from live diagnostics on 2026-08-21:
#: 22 kWh DC usable, a 20 % floor, 10 kW each way, 90 % round trip.
REFERENCE = {
    "capacity_kwh": 22.0,
    "max_charge_kw": 10.0,
    "max_discharge_kw": 10.0,
    "round_trip_efficiency_percent": 90.0,
}
FLOOR_PERCENT = 20.0
#: One boundary crossing at the reference efficiency.
ETA = math.sqrt(0.9)

NORMAL = date(2026, 8, 19)
SPRING_FORWARD = date(2026, 3, 29)
FALL_BACK = date(2026, 10, 25)
#: The project's test timezone, and the one whose transitions the counts below
#: describe. Named rather than repeated, like every other zone in the suite.
TZ = ZoneInfo("Europe/Amsterdam")


def reference_limits() -> BatteryLimits:
    """Return the live installation's limits."""
    return limits_for(**REFERENCE)


def floor_energy(limits: BatteryLimits, percent: float = FLOOR_PERCENT) -> float:
    """Return the configured floor as DC energy, through the one conversion."""
    return limits.energy_for_soc(percent)


def blocks(*spec: tuple[int, float | None, float | None]) -> tuple[IntervalDemand, ...]:
    """Build a horizon from ``(count, load, pv)`` runs.

    Runs rather than per-interval literals, because every scenario here is a
    shape -- a night, a sunny afternoon, an evening -- and spelling out
    a hundred and thirty-five quarters would hide that shape rather than show it.
    """
    demands: list[IntervalDemand] = []
    index = 0
    for count, load, pv in spec:
        for _ in range(count):
            demands.append(IntervalDemand(index=index, baseline_kwh=load, pv_kwh=pv))
            index += 1
    return tuple(demands)


def required(demands: tuple[IntervalDemand, ...], *, percent: float = FLOOR_PERCENT):
    """Return the authoritative projection for a horizon."""
    limits = reference_limits()
    return build_reserve(
        limits=limits,
        floor_energy_kwh=floor_energy(limits, percent),
        demands=demands,
    )


# -- the floor is the terminal condition, and the lowest possible answer -----


def test_no_future_demand_requires_exactly_the_configured_floor() -> None:
    """Nothing to cover, so nothing to hold beyond the user's own setting.

    20 % of 22 kWh is 4.4 kWh DC, exactly. The floor is the recursion's base
    case, so this is the one figure in the phase that is not an accumulation of
    anything.
    """
    projection = required(blocks((96, 0.0, 0.0)))

    assert projection.required_now_dc_kwh == pytest.approx(4.4)
    assert projection.required_now_soc_percent == pytest.approx(FLOOR_PERCENT)
    assert projection.horizon_basis == RESERVE_HORIZON_CLOSED
    assert projection.lower_bound_reason is None


def test_a_zero_floor_is_honoured_rather_than_replaced_by_a_default() -> None:
    """Zero is a legal setting, and the requirement must not invent one.

    The configured minimum may be zero -- the inverter's own floor still protects
    the cells -- and a reserve that quietly substituted twenty per cent would be
    overriding the user rather than serving them.
    """
    projection = required(blocks((96, 0.0, 0.0)), percent=0.0)

    assert projection.required_now_dc_kwh == pytest.approx(0.0)


@pytest.mark.parametrize("percent", [0.0, 5.0, 20.0, 50.0, 95.0])
def test_the_requirement_never_falls_below_the_configured_floor(
    percent: float,
) -> None:
    """Swept across floors and across a shape with surplus in it.

    The recursion cannot produce less than its base case, but a reserve that
    could dip below the user's own setting would break the one promise this
    project makes about the battery, so it is asserted rather than reasoned.
    """
    limits = reference_limits()
    expected = floor_energy(limits, percent)
    projection = required(
        blocks((20, 0.1, 0.6), (24, 0.25, 0.0), (36, 0.1, 0.6), (16, 0.25, 0.0)),
        percent=percent,
    )

    for interval in projection.intervals:
        assert interval.required_dc_kwh is not None
        assert interval.required_dc_kwh >= expected - 1e-9


# -- exact arithmetic, including the discharge power limit -------------------


def test_the_discharge_power_limit_caps_what_one_interval_can_require() -> None:
    """Four kilowatt-hours in a quarter is sixteen kilowatts; ten is the limit.

    So the battery can serve 2.5 kWh AC of it and the remaining 1.5 kWh is grid
    demand whatever the state of charge -- reserving against it would hold energy
    that could never reach the load it was held for.

    With the next interval's 0.5 kWh the requirement is ``4.4 + 3.0 / sqrt(0.9)``
    = 7.5622776601683795 kWh DC, and the third interval's surplus cancels the
    tail. Both the figure and the excluded demand are asserted, so the cap cannot
    quietly become a reduction of the requirement instead.
    """
    projection = required(blocks((1, 4.0, 0.0), (1, 0.5, 0.0), (1, 0.5, 1.5)))

    assert projection.required_now_dc_kwh == pytest.approx(4.4 + 3.0 / ETA)
    assert projection.demand_beyond_discharge_power_kwh == pytest.approx(1.5)
    assert projection.servable_ac_kwh == pytest.approx(3.0)
    assert CONSTRAINT_MAX_DISCHARGE_POWER in projection.intervals[0].constraints
    assert projection.constraint_counts[CONSTRAINT_MAX_DISCHARGE_POWER] == 1


def test_capping_one_interval_takes_nothing_from_the_next() -> None:
    """Household load is not a deferrable backlog.

    The 1.5 kWh the battery could not serve in the first interval is drawn from
    the grid at that moment; it does not reappear later. So the second interval
    contributes its own 0.5 kWh in full, and the total is the sum of two
    independently capped terms rather than a cap applied to their sum.
    """
    capped = required(blocks((1, 4.0, 0.0), (1, 0.5, 0.0)))
    alone = required(blocks((1, 0.5, 0.0)))

    contribution = capped.required_now_dc_kwh - capped.intervals[1].required_dc_kwh
    assert contribution == pytest.approx(2.5 / ETA)
    assert alone.required_now_dc_kwh == pytest.approx(4.4 + 0.5 / ETA)


def test_the_charge_power_limit_caps_what_one_interval_can_credit() -> None:
    """Twelve kilowatts of surplus offsets ten kilowatts of it, and no more.

    A quarter-hour absorbs at most ``10 * 0.25`` = 2.5 kWh AC however bright the
    forecast, so the credit is 2.5 * sqrt(0.9) DC and the remaining 0.5 kWh AC is
    reported as surplus the inverter could not have taken.
    """
    projection = required(blocks((4, 0.5, 0.0), (1, 0.0, 3.0)))
    demand_dc = 4 * 0.5 / ETA

    assert projection.intervals[4].credited_ac_kwh == pytest.approx(2.5)
    assert projection.surplus_beyond_charge_power_kwh == pytest.approx(0.5)
    assert CONSTRAINT_MAX_CHARGE_POWER in projection.intervals[4].constraints
    assert projection.required_now_dc_kwh == pytest.approx(4.4 + demand_dc)


def test_surplus_is_credited_at_the_charge_boundary_not_the_discharge_one() -> None:
    """``S * eta``, which is the smaller of the two and therefore the safe one.

    One kilowatt-hour of surplus arriving before two of demand offsets
    ``sqrt(0.9)`` = 0.9487 kWh DC of it. Expressing the same surplus through the
    discharge boundary would credit ``1 / sqrt(0.9)`` = 1.0541 instead -- eleven
    per cent more -- and every unit of over-credit lowers a safety figure.
    """
    projection = required(blocks((1, 0.0, 1.0), (8, 0.25, 0.0)))
    demand_dc = 8 * 0.25 / ETA

    assert projection.intervals[0].credited_ac_kwh == pytest.approx(1.0)
    assert projection.required_now_dc_kwh == pytest.approx(4.4 + demand_dc - 1.0 * ETA)


def test_surplus_arriving_after_the_demand_it_would_pay_for_credits_nothing() -> None:
    """Tomorrow's sun cannot power last night, and the recursion knows it.

    The same kilowatt-hour of surplus, moved to the far side of the demand, buys
    nothing at all: walking backwards it meets a deficit of zero, and the floor
    stops the credit accumulating past it. That floor is what makes the direction
    of time part of the arithmetic rather than something a reader has to check.
    """
    before = required(blocks((1, 0.0, 1.0), (8, 0.25, 0.0)))
    after = required(blocks((8, 0.25, 0.0), (1, 0.0, 1.0)))
    unaided = required(blocks((8, 0.25, 0.0)))

    assert before.required_now_dc_kwh < unaided.required_now_dc_kwh
    assert after.required_now_dc_kwh == pytest.approx(unaided.required_now_dc_kwh)


# -- the five scenarios ------------------------------------------------------


def scenario_a() -> tuple[IntervalDemand, ...]:
    """Cheap night, sunny day, expensive evening. Prices are labels only."""
    return blocks((24, 0.0625, 0.0), (36, 0.1, 0.6), (16, 0.25, 0.0))


def scenario_b() -> tuple[IntervalDemand, ...]:
    """Cheap night, dark winter day, expensive evening. No surplus anywhere."""
    return blocks((24, 0.0625, 0.0), (36, 0.1556, 0.1), (16, 0.25, 0.0))


def scenario_c() -> tuple[IntervalDemand, ...]:
    """Midday with strong production now, and a dark evening ahead."""
    return blocks((20, 0.1, 0.6), (24, 0.25, 0.0), (36, 0.1, 0.6), (16, 0.25, 0.0))


def scenario_d() -> tuple[IntervalDemand, ...]:
    """A broken morning with two isolated, marginal surplus quarters."""
    return blocks(
        (24, 0.1958, 0.0),
        (1, 0.0, 0.02),
        (11, 0.05, 0.0),
        (1, 0.0, 0.03),
        (11, 0.05, 0.0),
        (16, 0.0, 0.5),
        (8, 0.25, 0.0),
    )


def scenario_e() -> tuple[IntervalDemand, ...]:
    """No future production at all."""
    return blocks((96, 0.1427, 0.0))


def test_expected_production_lowers_the_requirement() -> None:
    """Scenario A: the sun covers the evening, so the night need not.

    5.98 kWh against the 10.20 the superseded same-interval rule asks for. This
    is the summer behaviour the phase exists to produce, and it emerges from load
    and production alone -- there is no season, no month check and no mode.
    """
    demands = scenario_a()
    projection = required(demands)
    limits = reference_limits()
    same = build_reserve_same_interval_only(
        limits=limits, floor_energy_kwh=floor_energy(limits), demands=demands
    )

    assert projection.required_now_dc_kwh == pytest.approx(5.981, abs=0.001)
    assert same.required_now_dc_kwh == pytest.approx(10.198, abs=0.001)
    # And the peak is higher still, which is exactly why it is not the reserve:
    # holding it at the cheap hour would buy energy the sun is about to supply.
    assert projection.peak_required_reserve_kwh == pytest.approx(8.616, abs=0.001)


def test_without_expected_production_the_requirement_rises() -> None:
    """Scenario B: nothing to credit, so the two definitions agree exactly.

    The winter behaviour, and it needs no winter mode: with no surplus in the
    horizon the credit term is zero at every interval, so the recursion reduces
    to the sum of net demand on its own.
    """
    demands = scenario_b()
    limits = reference_limits()
    projection = required(demands)
    same = build_reserve_same_interval_only(
        limits=limits, floor_energy_kwh=floor_energy(limits), demands=demands
    )

    assert projection.required_now_dc_kwh == pytest.approx(12.307, abs=0.001)
    assert projection.required_now_dc_kwh == pytest.approx(same.required_now_dc_kwh)
    assert projection.credited_ac_kwh == pytest.approx(0.0)
    assert projection.required_now_dc_kwh > required(scenario_a()).required_now_dc_kwh


def test_at_midday_the_requirement_is_the_floor_and_the_peak_is_not() -> None:
    """Scenario C, and the case that decides the whole design.

    Ten kilowatt-hours of surplus arrive before tonight needs six, so nothing has
    to be held *now*. The peak still reports the six, because that is what the
    pack will be asked for later -- and publishing the peak as the requirement
    would reserve energy the sun is about to supply, at the one hour of the day
    when buying it would be most obviously wasteful.
    """
    projection = required(scenario_c())

    assert projection.required_now_dc_kwh == pytest.approx(4.4)
    assert projection.peak_required_reserve_kwh == pytest.approx(10.725, abs=0.001)
    assert projection.peak_required_at > 0


def test_an_isolated_marginal_surplus_quarter_does_not_end_the_drawdown() -> None:
    """Scenario D: two quarters of +0.02 and +0.03 kWh cost 0.05, not 1.05.

    A definition that stopped accumulating at the first positive surplus would
    answer 4.7 kWh here and leave the 1.1 kWh the house still draws afterwards
    uncovered. The floored recursion dips by the surplus and keeps climbing, which
    is the whole reason it is a running deficit rather than a search for a seam.
    """
    projection = required(scenario_d())

    assert projection.required_now_dc_kwh == pytest.approx(10.465, abs=0.001)
    # The two marginal quarters were credited, and for what they were worth.
    assert projection.credited_ac_kwh == pytest.approx(8.05)


def test_with_no_production_every_definition_collapses_to_one() -> None:
    """Scenario E: the requirement, the counterfactual and the bracket agree.

    Which is the honest degeneration: with nothing to net and nothing to credit
    there is only one answer, and the phase owes no reduction to sunlight it
    never forecast.
    """
    demands = scenario_e()
    limits = reference_limits()
    projection = required(demands)
    same = build_reserve_same_interval_only(
        limits=limits, floor_energy_kwh=floor_energy(limits), demands=demands
    )
    blind = build_reserve_pv_blind(
        limits=limits, floor_energy_kwh=floor_energy(limits), demands=demands
    )

    assert projection.required_now_dc_kwh == pytest.approx(18.840, abs=0.001)
    assert same.required_now_dc_kwh == pytest.approx(projection.required_now_dc_kwh)
    assert blind.required_now_dc_kwh == pytest.approx(projection.required_now_dc_kwh)
    assert projection.pv_blind_intervals == 0


# -- the definition, proved against the simulator ---------------------------


SCENARIOS = {
    "a-night-then-sun": scenario_a,
    "b-dark-winter-day": scenario_b,
    "c-midday-strong-sun": scenario_c,
    "d-marginal-surplus": scenario_d,
    "e-no-production": scenario_e,
    "f-power-capped": lambda: blocks((1, 4.0, 0.0), (8, 0.5, 0.0), (4, 0.0, 1.0)),
}


def walk(demands: tuple[IntervalDemand, ...], energy: float, *, absorb: bool):
    """Run the Phase-3 simulator from a given stored energy."""
    limits = reference_limits()
    state = build_state(
        soc_percent=limits.soc_for_energy(energy),
        limits=limits,
        reserve=static_reserve(FLOOR_PERCENT),
    )
    assert state is not None
    return simulate(
        state, demands, ReserveGuardPolicy().provider(), absorb_surplus=absorb
    )


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_starting_at_the_requirement_is_exactly_enough(name: str) -> None:
    """The definition, checked against an oracle that never heard of it.

    ``simulation.simulate`` applies every limit through the clamp and imports
    nothing from this module, so it is an independent implementation of the
    physics. Starting at the requirement, the only grid import is the demand the
    battery could never have served in time -- and the pack touches its floor
    without crossing it.
    """
    demands = SCENARIOS[name]()
    projection = required(demands)
    floor = floor_energy(reference_limits())

    trajectory = walk(demands, projection.required_now_dc_kwh, absorb=True)

    assert trajectory.minimum_energy_kwh >= floor - 1e-9
    assert trajectory.grid_import_kwh == pytest.approx(
        projection.demand_beyond_discharge_power_kwh, abs=1e-6
    )


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_starting_below_the_requirement_is_not_enough(name: str) -> None:
    """The other half, without which the test above would pass for any large number.

    A whisker below the requirement the battery runs short, so the grid covers
    more than the power limit alone forced it to. This is what makes the figure a
    *minimum* rather than merely a sufficient one.
    """
    demands = SCENARIOS[name]()
    projection = required(demands)
    floor = floor_energy(reference_limits())
    critical = projection.required_now_dc_kwh
    if critical <= floor + 1e-9:
        pytest.skip("the floor is already the answer, so there is nothing below it")

    enough = walk(demands, critical, absorb=True)
    short = walk(demands, critical - 0.05, absorb=True)

    assert short.grid_import_kwh > enough.grid_import_kwh + 1e-9


def test_the_requirement_needs_the_replenishment_assumption_to_hold() -> None:
    """And this is where that assumption is visible rather than argued.

    The same start energy that suffices when the inverter stores surplus does not
    suffice when it does not. That is the whole content of the relaxation the
    phase declares -- and it is why ``required_same_interval_only_kwh`` is
    published beside the authoritative figure rather than instead of it.
    """
    demands = scenario_a()
    limits = reference_limits()
    projection = required(demands)
    same = build_reserve_same_interval_only(
        limits=limits, floor_energy_kwh=floor_energy(limits), demands=demands
    )

    absorbing = walk(demands, projection.required_now_dc_kwh, absorb=True)
    not_absorbing = walk(demands, projection.required_now_dc_kwh, absorb=False)

    assert absorbing.grid_import_kwh == pytest.approx(0.0)
    assert not_absorbing.grid_import_kwh > 0.0
    # The counterfactual is what such an installation should be reading, and it
    # is enough there.
    assert same.required_now_dc_kwh > projection.required_now_dc_kwh
    assert walk(
        demands, same.required_now_dc_kwh, absorb=False
    ).grid_import_kwh == pytest.approx(0.0)


# -- the four figures, and their ordering ------------------------------------


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_the_four_figures_are_ordered(name: str) -> None:
    """Authoritative, then the two counterfactuals, then the peak.

    Provable rather than incidental: dropping the credit can only raise every
    term of the recursion, dropping production as well can only raise it again,
    and the peak is a maximum over the same trajectory the first figure starts.
    """
    demands = SCENARIOS[name]()
    limits = reference_limits()
    floor = floor_energy(limits)
    authoritative = required(demands).required_now_dc_kwh
    same = build_reserve_same_interval_only(
        limits=limits, floor_energy_kwh=floor, demands=demands
    ).required_now_dc_kwh
    blind = build_reserve_pv_blind(
        limits=limits, floor_energy_kwh=floor, demands=demands
    ).required_now_dc_kwh
    peak = required(demands).peak_required_reserve_kwh

    assert authoritative <= same + 1e-9
    assert same <= blind + 1e-9
    assert authoritative <= peak + 1e-9


def test_the_pv_blind_bracket_ignores_production_entirely() -> None:
    """Not a forecast of darkness -- the question "what do I owe to the sun".

    Scenario A carries 9.1 kWh of gross load, so with production discarded the
    requirement is the floor plus all of it, one boundary crossing up. Every
    interval is counted blind, which is what distinguishes this bracket from a
    horizon that genuinely had no forecast.
    """
    limits = reference_limits()
    blind = build_reserve_pv_blind(
        limits=limits, floor_energy_kwh=floor_energy(limits), demands=scenario_a()
    )
    gross = 24 * 0.0625 + 36 * 0.1 + 16 * 0.25

    assert gross == pytest.approx(9.1)
    assert blind.credited_ac_kwh == pytest.approx(0.0)
    assert blind.pv_blind_intervals == len(blind.intervals)
    assert blind.required_now_dc_kwh == pytest.approx(4.4 + gross / ETA)


def test_the_trajectory_rises_and_falls_rather_than_only_decaying() -> None:
    """It is not a monotone staircase, and assuming so was a real mistake.

    An earlier draft asserted monotonicity, which is true of the peak and false
    of the requirement. In scenario C the requirement at the first interval is the
    floor while a later one is far above it -- because replenishment is imminent
    now and spent by then.
    """
    projection = required(scenario_c())
    values = [interval.required_dc_kwh for interval in projection.intervals]

    assert values[0] == pytest.approx(4.4)
    assert max(values) > values[0] + 1.0
    assert any(later > earlier + 1e-9 for earlier, later in pairwise(values))


# -- the requirement does not depend on the present -------------------------


@pytest.mark.parametrize("soc", [0.0, 20.0, 55.5, 84.8, 100.0])
def test_the_requirement_is_the_same_whatever_the_battery_holds(soc: float) -> None:
    """A requirement is a requirement; what the pack has is the shortfall.

    Keeping the two apart is what makes the dependency direction one-way, and it
    is why the reserve can be compared against a projected trajectory without the
    comparison being able to move the thing it is comparing.
    """
    limits = reference_limits()
    demands = scenario_a()
    projection = required(demands)
    state = build_state(
        soc_percent=soc, limits=limits, reserve=static_reserve(FLOOR_PERCENT)
    )

    again = required(demands)
    report = shortfall(projection, state)

    assert again.required_now_dc_kwh == pytest.approx(projection.required_now_dc_kwh)
    assert report["required_reserve_kwh"] == pytest.approx(
        round(projection.required_now_dc_kwh, 2)
    )


def test_a_battery_below_the_requirement_reports_the_gap_and_nothing_else() -> None:
    """A shortfall is published; no charge is proposed anywhere.

    Phase 7 identifies need. Nothing in this module can express an intention to
    satisfy it, which is why the shortfall is a number rather than a decision.
    """
    limits = reference_limits()
    projection = required(scenario_b())
    state = build_state(
        soc_percent=30.0, limits=limits, reserve=static_reserve(FLOOR_PERCENT)
    )

    report = shortfall(projection, state)

    assert report["reserve_shortfall_kwh"] > 0.0
    assert report["margin_to_reserve_kwh"] == pytest.approx(0.0)
    assert report["reserve_met"] is False


def test_a_battery_exactly_at_the_requirement_has_no_shortfall_and_no_margin() -> None:
    """The boundary, asserted at both zeros so neither can drift into the other."""
    limits = reference_limits()
    projection = required(scenario_a())
    state = build_state(
        soc_percent=limits.soc_for_energy(projection.required_now_dc_kwh),
        limits=limits,
        reserve=static_reserve(FLOOR_PERCENT),
    )

    report = shortfall(projection, state)

    assert report["reserve_shortfall_kwh"] == pytest.approx(0.0)
    assert report["margin_to_reserve_kwh"] == pytest.approx(0.0)
    assert report["reserve_met"] is True


def test_a_battery_above_the_requirement_reports_a_margin() -> None:
    """The live installation's own case: 18.66 kWh stored against a lower need."""
    limits = reference_limits()
    projection = required(scenario_a())
    state = build_state(
        soc_percent=84.8, limits=limits, reserve=static_reserve(FLOOR_PERCENT)
    )

    report = shortfall(projection, state)

    assert report["reserve_shortfall_kwh"] == pytest.approx(0.0)
    assert report["margin_to_reserve_kwh"] > 0.0
    assert report["reserve_met"] is True


def test_without_a_state_of_charge_the_requirement_survives() -> None:
    """The point of separating them: no reading, no shortfall, still a requirement.

    A young or partly configured installation still gets the figure the
    minimum-SoC setting controls, exactly as ``Usable Battery Energy`` survives a
    withheld forecast.
    """
    projection = required(scenario_a())

    report = shortfall(projection, None)

    assert projection.required_now_dc_kwh is not None
    assert report["reserve_shortfall_kwh"] is None
    assert report["reserve_met"] is None


# -- reachability, and the headroom bound -----------------------------------


def test_a_requirement_the_pack_could_hold_is_reachable() -> None:
    """Reachable is about capacity, not about what is stored now."""
    projection = required(scenario_a())

    assert projection.reachable is True
    assert projection.headroom_bound is False
    assert projection.surplus_beyond_headroom_kwh == pytest.approx(0.0)


def test_a_requirement_above_the_pack_is_reported_unreachable_and_uncapped() -> None:
    """Twenty-two kilowatt-hours cannot hold thirty, and the figure says thirty.

    Clamping it to the ceiling would report a satisfiable requirement, which is
    the opposite of the truth. The uncapped value is what makes the gap visible.
    """
    projection = required(blocks((96, 0.25, 0.0)))

    assert projection.required_now_dc_kwh > 22.0
    assert projection.reachable is False


def test_credit_the_pack_could_not_have_held_marks_a_lower_bound() -> None:
    """Thirty kilowatt-hours of surplus, then twenty-five of demand.

    The recursion credits the surplus and answers the floor -- but an interval in
    between requires more than the whole pack, so no starting energy would have
    served that stretch and the published figure understates. Detected and said
    out loud; never silently corrected, and never swapped for another model.
    """
    projection = required(blocks((20, 0.0, 1.5), (40, 0.625, 0.0)))

    assert projection.required_now_dc_kwh == pytest.approx(4.4)
    assert projection.peak_required_reserve_kwh > 22.0
    assert projection.headroom_bound is True
    assert projection.surplus_beyond_headroom_kwh > 0.0
    assert projection.lower_bound_reason == RESERVE_BOUND_TRUNCATED_HEADROOM


def test_credit_the_pack_could_have_held_marks_nothing() -> None:
    """The near miss, and the reason the detector is not the excursion.

    Thirty kilowatt-hours of surplus followed by *fifteen* of demand also loses
    credit to a full pack -- but every requirement in the horizon fits, the
    simulator confirms the floor is enough, and calling that a lower bound would
    be a false alarm. An earlier draft did exactly that.
    """
    demands = blocks((20, 0.0, 1.5), (24, 0.625, 0.0))
    projection = required(demands)

    assert projection.required_now_dc_kwh == pytest.approx(4.4)
    assert projection.headroom_bound is False
    assert projection.lower_bound_reason == RESERVE_BOUND_TRUNCATED
    assert walk(demands, 4.4, absorb=True).grid_import_kwh == pytest.approx(0.0)


def test_a_horizon_that_ends_in_surplus_is_closed_rather_than_truncated() -> None:
    """Closed means the last drawdown ended inside the horizon.

    Truncated means the deficit was still climbing at the last interval anyone
    forecast, so demand continues past it and the figure is a lower bound. The
    distinction is the honest statement of where the answer stops being complete.
    """
    closed = required(blocks((24, 0.25, 0.0), (24, 0.0, 0.5)))
    truncated = required(blocks((24, 0.0, 0.5), (24, 0.25, 0.0)))

    assert closed.horizon_basis == RESERVE_HORIZON_CLOSED
    assert closed.lower_bound_reason is None
    assert truncated.horizon_basis == RESERVE_HORIZON_TRUNCATED
    assert truncated.lower_bound_reason == RESERVE_BOUND_TRUNCATED


def test_only_a_headroom_bound_reports_the_headroom_reason_alone() -> None:
    """All four values of the reason are reachable, including the pair.

    A compound value nothing can produce would be decoration, so the closed
    variant of the headroom case is built explicitly.
    """
    projection = required(blocks((20, 0.0, 1.5), (40, 0.625, 0.0), (40, 0.0, 1.5)))

    assert projection.horizon_basis == RESERVE_HORIZON_CLOSED
    assert projection.headroom_bound is True
    assert projection.lower_bound_reason == RESERVE_BOUND_HEADROOM


# -- missing data ------------------------------------------------------------


def test_an_empty_horizon_yields_no_requirement() -> None:
    """No forecast, no demands, no requirement, and a named reason."""
    projection = required(())

    assert projection.available is False
    assert projection.unavailable_reason == RESERVE_UNAVAILABLE_FORECAST
    assert projection.required_now_dc_kwh is None


def test_an_unforecast_interval_stops_the_recursion_rather_than_bridging_it() -> None:
    """One hole, and everything before it is unknown rather than smaller.

    An unforecast interval is not an interval of no demand. Reading it as zero
    would answer with a requirement that covers only the part of the night the
    model happened to predict, which is worse than answering nothing.
    """
    projection = required(blocks((4, 0.25, 0.0), (1, None, 0.0), (4, 0.25, 0.0)))

    assert projection.available is False
    assert projection.unavailable_reason == RESERVE_UNAVAILABLE_HORIZON_INCOMPLETE
    assert projection.required_now_dc_kwh is None
    assert projection.intervals_unknown == 5
    assert projection.intervals_evaluated == 4
    # The intervals after the hole were still answered, which is what makes the
    # horizon partial rather than absent. The last one covers only itself.
    assert projection.intervals[-1].required_dc_kwh == pytest.approx(4.4 + 0.25 / ETA)


def test_a_hole_read_as_zero_would_understate_the_requirement() -> None:
    """The mutation this rule exists to prevent, made numeric.

    Filling the hole with a zero produces a smaller, entirely plausible-looking
    figure. Nothing about it would look wrong in a diagnostics download.
    """
    with_hole = required(blocks((4, 0.25, 0.0), (1, None, 0.0), (4, 0.25, 0.0)))
    as_zero = required(blocks((4, 0.25, 0.0), (1, 0.0, 0.0), (4, 0.25, 0.0)))
    honest = required(blocks((9, 0.25, 0.0)))

    assert with_hole.required_now_dc_kwh is None
    assert as_zero.required_now_dc_kwh < honest.required_now_dc_kwh


def test_a_missing_production_forecast_is_blind_rather_than_dark() -> None:
    """No netting and no credit, which raises the requirement.

    The conservative direction, and declared: a PV-blind interval is counted, so
    a reader can tell a requirement built on a covered horizon from one built on
    a partly uncovered one.
    """
    blind_tail = required(
        blocks(
            (24, 0.25, None),
        )
    )
    covered = required(
        blocks(
            (24, 0.25, 0.1),
        )
    )

    assert blind_tail.pv_blind_intervals == 24
    assert covered.pv_blind_intervals == 0
    # 0.25 kWh netted against nothing, against 0.15 netted against production.
    assert blind_tail.required_now_dc_kwh == pytest.approx(4.4 + 24 * 0.25 / ETA)
    assert covered.required_now_dc_kwh == pytest.approx(4.4 + 24 * 0.15 / ETA)
    assert blind_tail.required_now_dc_kwh > covered.required_now_dc_kwh


def test_production_without_load_is_not_evaluated() -> None:
    """An interval with a forecast for the sun and none for the house.

    No load is no demand *and* no surplus -- ``surplus_kwh`` is zero when either
    term is absent -- so the interval neither raises nor lowers anything, and the
    recursion stops there rather than inventing a load to net against.
    """
    projection = required(blocks((4, 0.25, 0.0), (1, None, 2.0)))

    assert projection.demands[4].surplus_kwh == pytest.approx(0.0)
    assert projection.required_now_dc_kwh is None


# -- daylight saving ---------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "intervals"),
    [(SPRING_FORWARD, 92), (NORMAL, 96), (FALL_BACK, 100)],
    ids=["spring-forward", "normal", "fall-back"],
)
def test_a_civil_day_is_walked_at_its_real_length(day: date, intervals: int) -> None:
    """92, 96 and 100 intervals, each of them fifteen minutes long.

    The count of quarters in a civil day changes; their duration never does. The
    recursion carries no duration at all -- every conversion goes through the
    clamp, which derives it once -- so the only thing a transition can change here
    is how many terms are summed.
    """
    assert expected_quarters_for(day, TZ) == intervals

    projection = required(blocks((intervals, 0.125, 0.0)))

    assert projection.intervals_evaluated == intervals
    assert projection.required_now_dc_kwh == pytest.approx(
        4.4 + intervals * 0.125 / ETA
    )


def test_the_requirement_scales_with_the_day_length_and_nothing_else() -> None:
    """A short day needs less and a long day needs more, by exactly four quarters.

    Which is the whole of the daylight-saving story for this phase: no clock
    arithmetic, no local-time stepping, and therefore nothing that a transition
    can silently skip or repeat.
    """
    short = required(blocks((92, 0.125, 0.0))).required_now_dc_kwh
    normal = required(blocks((96, 0.125, 0.0))).required_now_dc_kwh
    long_day = required(blocks((100, 0.125, 0.0))).required_now_dc_kwh

    assert normal - short == pytest.approx(4 * 0.125 / ETA)
    assert long_day - normal == pytest.approx(4 * 0.125 / ETA)


# -- comparison against the projected trajectory ----------------------------


def test_the_comparison_reports_a_violation_without_changing_anything() -> None:
    """The shipped policy discharges to the configured floor, so it violates.

    That is the evidence a later phase needs rather than a fault: the requirement
    is above the floor, nothing enforces it, and the projected trajectory
    therefore crosses it. The comparison is read-only in both directions, which
    the identical requirement either side of it asserts.
    """
    demands = scenario_b()
    projection = required(demands)
    before = [interval.required_dc_kwh for interval in projection.intervals]
    # Started below the requirement on purpose. A full pack under this horizon
    # stays above the curve, and asserting a violation from there would be
    # asserting nothing.
    assert projection.required_now_dc_kwh > 10.0
    trajectory = walk(demands, 10.0, absorb=True)

    report = compare_to_trajectory(projection, trajectory)

    assert report is not None
    assert report["violation_expected"] is True
    assert report["first_violation_interval"] is not None
    assert report["minimum_margin_to_reserve_kwh"] < 0.0
    assert [interval.required_dc_kwh for interval in projection.intervals] == before


def test_a_trajectory_that_stays_above_the_requirement_reports_no_violation() -> None:
    """The counterpart, so the test above is not vacuous.

    Scenario C requires only the floor at the outset, and a full pack under a
    horizon the sun covers never falls below the curve.
    """
    demands = scenario_c()
    projection = required(demands)
    trajectory = walk(demands, 22.0, absorb=True)

    report = compare_to_trajectory(projection, trajectory)

    assert report is not None
    assert report["violation_expected"] is False
    assert report["minimum_margin_to_reserve_kwh"] >= 0.0


def test_the_comparison_is_absent_without_a_trajectory() -> None:
    """No projection to compare against is reported as absence, not as agreement."""
    assert compare_to_trajectory(required(scenario_a()), None) is None


# -- one direction per interval ---------------------------------------------


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_no_interval_carries_both_a_demand_and_a_surplus(name: str) -> None:
    """The structural invariant the signed term relies on.

    ``IntervalDemand`` floors both at zero after netting, so at most one is ever
    non-zero. If that ever stopped holding, the recursion would be adding a
    demand and subtracting a credit for the same kilowatt-hour.
    """
    for demand in SCENARIOS[name]():
        if demand.net_demand_kwh is None:
            continue
        assert demand.net_demand_kwh == 0.0 or demand.surplus_kwh == 0.0
