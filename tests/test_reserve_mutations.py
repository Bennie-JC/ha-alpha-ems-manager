"""Deliberately break each reserve invariant, and prove a test notices.

A green suite is not evidence on its own. A test that would also pass against the
broken implementation it exists to protect against is decoration, and the only
way to find out which kind you have is to break the thing and watch.

Every mutation here is a *plausible* refactor rather than an absurdity -- the kind
of change someone might make in good faith while tidying up. Four are worth
singling out, and two of them were real:

* **the peak published as the requirement.** This is what an earlier draft of the
  phase actually did. It reads as the safer choice and is wrong in the direction
  that costs money: at midday it holds six kilowatt-hours against a night the sun
  is about to cover, and at a cheap hour it would tell a later phase to buy four
  kilowatt-hours it does not need.
* **the headroom flag measured as a cumulative excursion.** Also real, and caught
  during implementation. It labels a correct answer a lower bound -- thirty
  kilowatt-hours of surplus followed by fifteen of demand needs only the floor,
  delivers no grid import from it, and was still flagged.
* **the fingerprint taken over the answer instead of the inputs.** Also real. The
  requirement legitimately differs every quarter-hour, so digesting it stores
  ninety-six documents a day and breaks the rule that an unchanged refresh costs
  no I/O.
* **surplus credited at the discharge boundary.** ``1 / sqrt(0.9)`` where
  ``sqrt(0.9)`` belongs is eleven per cent of over-credit in a safety figure, and
  every round-trip test that can be written still passes.

The mutations are reimplemented locally rather than monkeypatched, so the real
module is never modified and the last test in this file proves it.
"""

from __future__ import annotations

import math

import pytest

from custom_components.alpha_ems_manager.battery import (
    BatteryLimits,
    BatteryRequest,
    apply_request,
    build_state,
    static_reserve,
)
from custom_components.alpha_ems_manager.const import (
    RESERVE_BOUND_HEADROOM,
    RESERVE_BOUND_TRUNCATED,
    RESERVE_BOUND_TRUNCATED_HEADROOM,
    RESERVE_HORIZON_TRUNCATED,
)
from custom_components.alpha_ems_manager.reserve import (
    build_reserve,
    build_reserve_same_interval_only,
    fingerprint_reserve,
    shortfall,
)
from custom_components.alpha_ems_manager.simulation import IntervalDemand

from .test_reserve_model import (
    ETA,
    FLOOR_PERCENT,
    blocks,
    floor_energy,
    reference_limits,
    required,
    scenario_a,
    scenario_c,
    scenario_d,
)


def mutated(
    demands: tuple[IntervalDemand, ...],
    *,
    no_floor_clamp: bool = False,
    forward: bool = False,
    credit_uncapped: bool = False,
    credit_at_discharge_boundary: bool = False,
    credit_signed_wrong: bool = False,
    ignore_discharge_power: bool = False,
    count_unserved: bool = False,
    terminal_zero: bool = False,
    gross_load: bool = False,
    pv_twice: bool = False,
    hole_as_zero: bool = False,
) -> tuple[float, list[float]]:
    """Return ``(required_now, trajectory)`` under one deliberate mutation.

    A single local reimplementation with switches, rather than eleven near-copies:
    the shape of the recursion is what every mutation below distorts, and keeping
    it in one place is what makes each distortion legible as a one-line change.
    """
    limits = reference_limits()
    floor = floor_energy(limits)
    zero = static_reserve(0.0)
    full = build_state(soc_percent=limits.max_soc_percent, limits=limits, reserve=zero)
    empty = build_state(soc_percent=0.0, limits=limits, reserve=zero)
    assert full is not None and empty is not None

    order = range(len(demands)) if forward else range(len(demands) - 1, -1, -1)
    deficit = 0.0
    base = 0.0 if terminal_zero else floor
    trajectory: list[float] = [0.0] * len(demands)

    for position in order:
        demand = demands[position]
        net = demand.baseline_kwh if gross_load else demand.net_demand_kwh
        if net is None:
            if not hole_as_zero:
                break
            net = 0.0
        if pv_twice:
            net = max(0.0, net - (demand.pv_kwh or 0.0))

        power = net / 0.25
        if ignore_discharge_power:
            withdrawal = net / limits.discharge_efficiency
        else:
            outcome = apply_request(full, BatteryRequest.discharge(power))
            served = net if count_unserved else outcome.discharge_ac_kwh
            withdrawal = (
                served / limits.discharge_efficiency
                if count_unserved
                else full.energy_kwh - outcome.end_energy_kwh
            )

        credit = 0.0
        if demand.surplus_kwh > 0.0:
            if credit_uncapped:
                credit = demand.surplus_kwh * limits.charge_efficiency
            elif credit_at_discharge_boundary:
                credit = demand.surplus_kwh / limits.discharge_efficiency
            else:
                charged = apply_request(
                    empty, BatteryRequest.charge(demand.surplus_kwh / 0.25)
                )
                credit = charged.end_energy_kwh - empty.energy_kwh
            if credit_signed_wrong:
                credit = -credit

        signed = withdrawal - credit
        deficit = signed + deficit if no_floor_clamp else max(0.0, signed + deficit)
        trajectory[position] = base + deficit

    return base + deficit, trajectory


def limits_and_floor() -> tuple[BatteryLimits, float]:
    """Return the reference limits and their floor energy."""
    limits = reference_limits()
    return limits, floor_energy(limits)


# --- the recursion itself ----------------------------------------------------


def test_publishing_the_peak_as_the_requirement_is_caught() -> None:
    """Mutation: report the largest requirement in the horizon instead.

    What an earlier draft did, and it reads as the cautious choice. At midday it
    demands 10.7 kWh where the answer is the 4.4 kWh floor, because the surplus
    arriving this afternoon refills the pack long before the evening needs it.
    ``test_reserve_model`` pins both numbers, so this cannot be reintroduced.
    """
    projection = required(scenario_c())

    assert projection.required_now_dc_kwh == pytest.approx(4.4)
    assert projection.peak_required_reserve_kwh == pytest.approx(10.725, abs=0.001)
    assert projection.peak_required_reserve_kwh > projection.required_now_dc_kwh + 6.0


def test_publishing_the_peak_would_overstate_a_cheap_hour_too() -> None:
    """The same mutation, in the case that costs money rather than comfort.

    Scenario A is the overnight shape a later phase would price. The requirement
    is 5.98 kWh and the peak is 8.62, so a phase acting on the peak would buy
    2.6 kWh of grid it never needed -- while the sun was forecast to supply it.
    """
    projection = required(scenario_a())

    assert projection.required_now_dc_kwh == pytest.approx(5.981, abs=0.001)
    assert projection.peak_required_reserve_kwh == pytest.approx(8.616, abs=0.001)


def test_removing_the_floor_from_the_recursion_is_caught() -> None:
    """Mutation: accumulate the signed term without flooring it at zero.

    Plausible -- the floor looks like a redundant guard on a quantity that is
    already non-negative most of the time. It is not: without it, surplus banks
    credit across a zero, so tomorrow's sun pays for last night and the
    requirement collapses below the user's own floor.
    """
    demands = blocks((8, 0.25, 0.0), (10, 0.0, 2.5), (8, 0.25, 0.0))
    honest = required(demands)
    broken, _ = mutated(demands, no_floor_clamp=True)

    assert broken < honest.required_now_dc_kwh
    assert broken < 4.4, "the mutation drops below the configured floor"
    assert honest.required_now_dc_kwh >= 4.4


def test_running_the_recursion_forwards_is_caught() -> None:
    """Mutation: walk the horizon in time order instead of backwards.

    A natural tidy-up -- every other loop in the project runs forwards -- and it
    answers a different question: the deficit accumulated *behind* an interval
    rather than the energy needed to get through what lies ahead of it.
    """
    demands = scenario_a()
    honest = required(demands)
    broken, _ = mutated(demands, forward=True)

    assert broken != pytest.approx(honest.required_now_dc_kwh)


def test_a_terminal_condition_of_zero_is_caught() -> None:
    """Mutation: base the recursion at zero rather than at the configured floor.

    Plausible if the floor is thought of as something the clamp adds later. It
    understates every requirement by exactly the floor, so the figure looks
    reasonable and a battery held at it would sit on the floor with nothing above.
    """
    demands = scenario_a()
    honest = required(demands)
    broken, _ = mutated(demands, terminal_zero=True)

    assert broken == pytest.approx(honest.required_now_dc_kwh - 4.4)


# --- the two conversions ----------------------------------------------------


def test_crediting_surplus_at_the_discharge_boundary_is_caught() -> None:
    """Mutation: ``S / eta`` where ``S * eta`` belongs.

    Both are one crossing at the configured efficiency, both are the same order
    of magnitude, and the sign of the mistake is the dangerous one: eleven per
    cent more credit means eleven per cent less reserve.
    """
    demands = blocks((1, 0.0, 1.0), (8, 0.25, 0.0))
    honest = required(demands)
    broken, _ = mutated(demands, credit_at_discharge_boundary=True)

    assert broken < honest.required_now_dc_kwh
    assert honest.required_now_dc_kwh - broken == pytest.approx(1 / ETA - ETA)


def test_crediting_surplus_without_the_charge_power_cap_is_caught() -> None:
    """Mutation: multiply the whole surplus by the efficiency and skip the clamp.

    Arithmetically tidier and physically wrong: a quarter-hour cannot absorb more
    than ten kilowatts however bright the forecast, so a three-kilowatt-hour
    surplus quarter would offset all of itself instead of the 2.5 kWh the
    inverter could actually have taken.
    """
    demands = blocks((8, 0.25, 0.0), (1, 0.0, 3.0), (16, 0.25, 0.0))
    honest = required(demands)
    broken, _ = mutated(demands, credit_uncapped=True)

    # The whole 3.0 kWh credited rather than the 2.5 the inverter could take.
    assert honest.required_now_dc_kwh - broken == pytest.approx(0.5 * ETA)
    assert broken < honest.required_now_dc_kwh


def test_signing_the_charge_delta_the_wrong_way_is_caught() -> None:
    """Mutation: read the clamp's charge delta as ``start - end``.

    The discharge helper beside it *is* ``start - end``, so making the two
    symmetrical looks like a cleanup. It turns every credit into a second demand
    and inflates the requirement, which is the safe direction -- and still wrong.
    """
    demands = scenario_a()
    honest = required(demands)
    broken, _ = mutated(demands, credit_signed_wrong=True)

    assert broken > honest.required_now_dc_kwh


# --- the power limit --------------------------------------------------------


def test_ignoring_the_discharge_power_limit_is_caught() -> None:
    """Mutation: convert the whole net demand rather than asking the clamp.

    The exact figure ``test_reserve_model`` pins is 7.5622776601683795 kWh; this
    answers 9.14 by reserving against 1.5 kWh the inverter could never have
    delivered in that quarter-hour.
    """
    demands = blocks((1, 4.0, 0.0), (1, 0.5, 0.0), (1, 0.5, 1.5))
    honest = required(demands)
    broken, _ = mutated(demands, ignore_discharge_power=True)

    assert honest.required_now_dc_kwh == pytest.approx(4.4 + 3.0 / ETA)
    assert broken == pytest.approx(4.4 + 4.5 / ETA)
    assert broken > honest.required_now_dc_kwh


def test_counting_the_unserved_remainder_is_caught() -> None:
    """Mutation: cap the power but keep charging the full demand to the battery.

    A halfway change, and the most plausible of the three: the clamp is called,
    its constraint is even reported, and the energy taken from the requirement is
    the unclamped one. It produces exactly the figure the mutation above does.
    """
    demands = blocks((1, 4.0, 0.0), (1, 0.5, 0.0), (1, 0.5, 1.5))
    honest = required(demands)
    broken, _ = mutated(demands, count_unserved=True)

    assert broken == pytest.approx(4.4 + 4.5 / ETA)
    assert honest.demand_beyond_discharge_power_kwh == pytest.approx(1.5)


# --- what a demand is -------------------------------------------------------


def test_using_gross_load_instead_of_net_demand_is_caught() -> None:
    """Mutation: forget to net production off the load.

    The first mistake the original brief warned about. Built on a horizon where
    production offsets *part* of the load in every interval, which is the case
    that separates the two: with 0.25 kWh of load against 0.1 of production the
    battery is asked for 0.15, and the mutation reserves against all of it.
    """
    demands = blocks((48, 0.25, 0.1))
    honest = required(demands)
    broken, _ = mutated(demands, gross_load=True)

    assert honest.required_now_dc_kwh == pytest.approx(4.4 + 48 * 0.15 / ETA)
    assert broken == pytest.approx(4.4 + 48 * 0.25 / ETA)
    assert broken > honest.required_now_dc_kwh


def test_subtracting_production_twice_is_caught() -> None:
    """Mutation: net production off a demand that was already netted.

    The second mistake the brief warned about, and the mirror of the first: the
    demand disappears entirely wherever production was forecast, so the
    requirement falls to the floor on a day that genuinely needs more.
    """
    demands = blocks((24, 0.25, 0.1), (24, 0.25, 0.0))
    honest = required(demands)
    broken, _ = mutated(demands, pv_twice=True)

    assert broken < honest.required_now_dc_kwh


def test_reading_an_unforecast_interval_as_zero_is_caught() -> None:
    """Mutation: treat a hole as a quarter-hour of no demand.

    This project's canonical mistake, restated for this layer. The answer looks
    entirely normal -- a slightly smaller requirement -- where the honest answer
    is that there is no requirement at all.
    """
    demands = blocks((4, 0.25, 0.0), (1, None, 0.0), (4, 0.25, 0.0))
    honest = required(demands)
    broken, _ = mutated(demands, hole_as_zero=True)

    assert honest.required_now_dc_kwh is None
    assert broken == pytest.approx(4.4 + 8 * 0.25 / ETA)


def test_reading_a_missing_production_forecast_as_production_is_caught() -> None:
    """Mutation: fill an absent PV interval from its neighbour.

    Interpolation looks harmless on a smooth curve. It nets demand against
    production nobody forecast, which lowers a safety figure, and it hides the
    ``pv_blind_intervals`` count that would otherwise say the horizon was only
    partly covered.
    """
    blind = required(blocks((24, 0.25, None)))
    filled = required(blocks((24, 0.25, 0.1)))

    assert blind.pv_blind_intervals == 24
    assert filled.pv_blind_intervals == 0
    assert filled.required_now_dc_kwh < blind.required_now_dc_kwh


# --- the headroom flag, and the fingerprint ---------------------------------


def test_measuring_the_headroom_bound_as_an_excursion_is_caught() -> None:
    """Mutation: flag when the cumulative range exceeds usable capacity.

    A real defect, caught while implementing this phase. It sounds right -- the
    pack cannot absorb an excursion larger than itself -- but the excursion may
    happen entirely after the point being reported, in which case the answer is
    correct and the label is a false alarm. Thirty kilowatt-hours of surplus then
    fifteen of demand needs only the floor and delivers no grid import from it.

    The honest test is whether some requirement *in* the horizon exceeds the
    pack, because that is the only way capacity can make an earlier figure
    understate.
    """
    limits, floor = limits_and_floor()
    span = limits.energy_for_soc(limits.max_soc_percent) - floor
    harmless = blocks((20, 0.0, 1.5), (24, 0.625, 0.0))
    genuine = blocks((20, 0.0, 1.5), (40, 0.625, 0.0))

    def excursion_flag(demands: tuple[IntervalDemand, ...]) -> bool:
        """Reproduce the old detector: the *cumulative* range against capacity.

        Two accumulators, exactly as the withdrawn version had them -- the
        floored running deficit, and the unfloored minimum of the same signed
        cumulative -- and the flag fires when the distance between them exceeds
        the usable window.
        """
        zero = static_reserve(0.0)
        full = build_state(
            soc_percent=limits.max_soc_percent, limits=limits, reserve=zero
        )
        empty = build_state(soc_percent=0.0, limits=limits, reserve=zero)
        assert full is not None and empty is not None
        deficit = 0.0
        trough = 0.0
        for demand in reversed(demands):
            out = apply_request(full, BatteryRequest.discharge(demand.power_kw or 0.0))
            withdrawal = full.energy_kwh - out.end_energy_kwh
            credit = 0.0
            if demand.surplus_kwh > 0.0:
                charged = apply_request(
                    empty, BatteryRequest.charge(demand.surplus_kwh / 0.25)
                )
                credit = charged.end_energy_kwh - empty.energy_kwh
            signed = withdrawal - credit
            deficit = max(0.0, signed + deficit)
            trough = min(0.0, signed + trough)
        return (deficit - trough) > span

    # The old detector fires on both; only one of them is genuinely understated.
    assert excursion_flag(harmless) is True
    assert excursion_flag(genuine) is True
    assert required(harmless).headroom_bound is False
    assert required(genuine).headroom_bound is True


def test_fingerprinting_the_answer_instead_of_the_inputs_is_caught() -> None:
    """Mutation: digest the requirement itself, as the other snapshots digest theirs.

    A real defect, caught by ``test_forecast_issuance``. The requirement is a
    function of the interval it is asked from, so it differs every quarter-hour
    even when nothing about the forecast has changed -- and a digest over it
    stores ninety-six documents a day.

    Two horizons that differ only by having advanced one interval must therefore
    fingerprint the same.
    """
    demands = scenario_a()
    later = demands[1:]
    first = required(demands)
    second = required(later)

    assert first.required_now_dc_kwh != pytest.approx(second.required_now_dc_kwh)
    assert fingerprint_reserve(
        first, config_fingerprint="cfg", load_fingerprint="ld", pv_fingerprint="pv"
    ) == fingerprint_reserve(
        second, config_fingerprint="cfg", load_fingerprint="ld", pv_fingerprint="pv"
    )


def test_the_fingerprint_still_moves_when_the_battery_changes() -> None:
    """The converse, so the fix above is not simply a constant.

    A different configuration is a different belief even with both forecasts
    unchanged, which is the entire reason the configuration is fingerprinted.
    """
    projection = required(scenario_a())

    assert fingerprint_reserve(
        projection, config_fingerprint="one", load_fingerprint=None, pv_fingerprint=None
    ) != fingerprint_reserve(
        projection, config_fingerprint="two", load_fingerprint=None, pv_fingerprint=None
    )


def test_a_changed_floor_changes_the_fingerprint() -> None:
    """The floor is an input, not an output, and the digest has to agree.

    Raising a minimum state of charge changes the requirement while both
    forecasts stand still, so a digest that ignored the floor would keep the old
    belief and never record the new one.
    """
    twenty = required(scenario_a(), percent=20.0)
    thirty = required(scenario_a(), percent=30.0)

    assert fingerprint_reserve(
        twenty, config_fingerprint="cfg", load_fingerprint=None, pv_fingerprint=None
    ) != fingerprint_reserve(
        thirty, config_fingerprint="cfg", load_fingerprint=None, pv_fingerprint=None
    )


# --- the shortfall ----------------------------------------------------------


def test_measuring_the_shortfall_above_the_floor_is_caught() -> None:
    """Mutation: subtract the requirement from energy *above* the floor.

    Plausible because ``Usable Battery Energy`` is measured that way. The
    requirement already includes the floor, so subtracting it twice reports a
    shortfall the size of the floor on a battery that is comfortably ahead.
    """
    limits, _ = limits_and_floor()
    # Chosen so the requirement sits between the energy above the floor and the
    # energy stored: 16.0 kWh against 14.26 usable and 18.66 held. That band is
    # exactly where the two readings disagree, and it is where a real
    # installation spends most of a summer evening.
    projection = required(blocks((44, 0.25, 0.0)))
    state = build_state(
        soc_percent=84.8, limits=limits, reserve=static_reserve(FLOOR_PERCENT)
    )
    assert state is not None
    assert state.usable_energy_kwh < projection.required_now_dc_kwh < state.energy_kwh

    honest = shortfall(projection, state)
    broken = max(0.0, projection.required_now_dc_kwh - state.usable_energy_kwh)

    assert honest["reserve_shortfall_kwh"] == pytest.approx(0.0)
    assert broken > 0.0


def test_comparing_reachability_against_the_usable_span_is_caught() -> None:
    """Mutation: ask whether the requirement fits *above* the floor.

    The requirement is an absolute stored energy, so it has to be compared with
    the ceiling rather than with the window above the floor. The mutation calls a
    perfectly holdable 20 kWh requirement unreachable on a 22 kWh pack.
    """
    limits, floor = limits_and_floor()
    ceiling = limits.energy_for_soc(limits.max_soc_percent)
    # 20.21 kWh: above the 17.6 kWh window over the floor, below the 22 kWh pack.
    projection = required(blocks((60, 0.25, 0.0)))
    required_now = projection.required_now_dc_kwh

    assert ceiling - floor < required_now < ceiling
    assert projection.reachable is True
    assert (required_now <= ceiling - floor) is False


# --- the counterfactuals stay counterfactual --------------------------------


def test_substituting_the_counterfactual_for_the_requirement_is_caught() -> None:
    """Mutation: publish the same-interval figure whenever absorption is off.

    Tempting, and it would make the phase state-dependent: the live installation
    flipped absorption inside fifteen minutes, so the requirement would have
    jumped from roughly 6 to 10 kWh on scenario A with no physical change at all.
    The two figures are published side by side precisely so nothing has to
    choose.
    """
    limits, floor = limits_and_floor()
    demands = scenario_a()
    authoritative = build_reserve(
        limits=limits, floor_energy_kwh=floor, demands=demands
    )
    counterfactual = build_reserve_same_interval_only(
        limits=limits, floor_energy_kwh=floor, demands=demands
    )

    assert authoritative.credited_surplus is True
    assert counterfactual.credited_surplus is False
    assert counterfactual.required_now_dc_kwh > authoritative.required_now_dc_kwh


def test_the_marginal_surplus_shortcut_is_caught() -> None:
    """Mutation: stop accumulating at the first interval with any surplus.

    The definition an earlier revision proposed. On scenario D two quarters of
    +0.02 and +0.03 kWh end the horizon and the answer falls to 4.7 kWh, leaving
    the 1.1 kWh the house still draws afterwards uncovered.
    """
    demands = scenario_d()
    honest = required(demands)

    # The mutation: accumulate only up to the first positive-surplus interval.
    limits, floor = limits_and_floor()
    first_surplus = next(
        index for index, demand in enumerate(demands) if demand.surplus_kwh > 0.0
    )
    truncated = build_reserve(
        limits=limits, floor_energy_kwh=floor, demands=demands[:first_surplus]
    )

    assert truncated.required_now_dc_kwh < honest.required_now_dc_kwh - 1.0


# --- hygiene ----------------------------------------------------------------


def test_every_mutation_in_this_file_is_reverted() -> None:
    """The real module is untouched, and the mutations live only in this file.

    Every break above is a local reimplementation or a rearrangement of the real
    inputs, so there is nothing to undo -- but saying so is cheap and the
    alternative is a monkeypatch that outlives its test.
    """
    limits, floor = limits_and_floor()
    projection = build_reserve(
        limits=limits, floor_energy_kwh=floor, demands=scenario_a()
    )

    assert projection.required_now_dc_kwh == pytest.approx(5.981, abs=0.001)
    assert projection.credited_surplus is True
    assert projection.horizon_basis == RESERVE_HORIZON_TRUNCATED
    assert projection.lower_bound_reason == RESERVE_BOUND_TRUNCATED
    assert math.isclose(floor, 4.4)
    # And the three bound reasons are all distinct constants, so a compound one
    # cannot be produced by string concatenation somewhere.
    assert (
        len(
            {
                RESERVE_BOUND_TRUNCATED,
                RESERVE_BOUND_HEADROOM,
                RESERVE_BOUND_TRUNCATED_HEADROOM,
            }
        )
        == 3
    )
