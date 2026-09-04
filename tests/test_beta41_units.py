"""beta.41's decision functions, tested directly rather than through a solve.

**Why a second file, and why unit tests.** The sweeps in
``test_beta41_physical_energy.py`` and ``test_beta41_coverage.py`` check properties
of solved plans, and mutation testing showed the limit of that: a great many ways of
breaking these functions leave a plan *internally consistent*. Turn
:func:`_physical_energy_kwh` into the identity and the recursion, the forward walk,
the terminal credit and both published endpoints all move together -- back to
exactly the beta.40 model, with every self-consistency assertion still passing.

So the rounding direction, the thresholds and the precedence arithmetic are pinned
here, on the functions themselves, where a changed sign or a moved boundary has
nowhere to hide. The properties that need a whole plan are in
``test_beta41_invariants.py``, anchored on quantities computed outside the model.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.const import (
    AMBIENT_CARRY_STEPS,
    ECONOMIC_ACTION_CHARGE,
    TERMINAL_WINDOW_CLOCK_MATCHED,
)
from custom_components.alpha_ems_manager.economic import (
    IntervalPrice,
    _ambient_carry_steps,
    _carry_compelled_forward,
    _coverage_runs,
    _net_of_coverage,
    _physical_energy_kwh,
    post_horizon_window,
)
from custom_components.alpha_ems_manager.simulation import IntervalDemand

BUCKET = 0.2635230352303523
STEP = BUCKET / AMBIENT_CARRY_STEPS


# == the physical coordinate ================================================


def test_physical_energy_subtracts_the_carry_and_is_not_the_lattice_level() -> None:
    """**The whole of Phase 1 in one function, so it is pinned as arithmetic.**

    The lattice level is what the recursion can address; the carry is sub-bucket
    household service already taken out of it. An implementation that returned the
    level would be beta.40 exactly -- and would keep every self-consistency
    property of a solved plan, which is why the identity is refused here rather
    than inferred from a trajectory.
    """
    assert _physical_energy_kwh(10.0, 0, STEP) == pytest.approx(10.0)
    assert _physical_energy_kwh(10.0, 1, STEP) == pytest.approx(10.0 - STEP)
    assert _physical_energy_kwh(10.0, 8, STEP) == pytest.approx(10.0 - BUCKET)

    # Strictly decreasing in the carry, by exactly one step each time. A scale
    # factor anywhere in here would drain the pack at the wrong rate.
    for carry in range(AMBIENT_CARRY_STEPS):
        near = _physical_energy_kwh(10.0, carry, STEP)
        far = _physical_energy_kwh(10.0, carry + 1, STEP)
        assert near - far == pytest.approx(STEP, abs=1e-12), carry


def test_a_full_carry_is_exactly_one_bucket_and_no_more() -> None:
    """The axis has to close onto the lattice, or the two disagree again.

    Releasing a whole bucket is what advances ``bucket`` by one, so the eight steps
    must sum to precisely one bucket: a carry of ``AMBIENT_CARRY_STEPS`` at level
    *n* has to be the same energy as a carry of zero at level *n-1*.
    """
    assert _physical_energy_kwh(10.0, AMBIENT_CARRY_STEPS, STEP) == pytest.approx(
        _physical_energy_kwh(10.0 - BUCKET, 0, STEP), abs=1e-12
    )


# == the rounding direction, which is a physical constraint =================


def test_household_service_rounds_down_and_never_up() -> None:
    """**Down, because the inverter covers the residual load and no more.**

    Rounding to nearest lets the modelled service exceed the actual residual --
    0.125 kWh against a 0.099 kWh residual on a measured interval -- and the
    surplus then has to leave as export or be spilled, neither of which the
    hardware does. Flooring understates how long the pack lasts, which is the safe
    way to be wrong about a battery.
    """
    # Just under two steps must register one, not two.
    assert _ambient_carry_steps(STEP * 1.99, STEP) == 1
    assert _ambient_carry_steps(STEP * 2.0, STEP) == 2
    # Just under one step registers nothing at all, and the grid covers it.
    assert _ambient_carry_steps(STEP * 0.99, STEP) == 0
    # Never more than the energy asked for, at any magnitude.
    for multiple in (0.1, 0.5, 0.9, 1.4, 3.7, 12.2, 99.6):
        steps = _ambient_carry_steps(STEP * multiple, STEP)
        assert steps * STEP <= STEP * multiple + 1e-12, multiple


def test_nothing_and_no_resolution_are_both_no_service() -> None:
    """The two degenerate inputs, so neither raises and neither invents service."""
    assert _ambient_carry_steps(0.0, STEP) == 0
    assert _ambient_carry_steps(-1.0, STEP) == 0
    assert _ambient_carry_steps(1.0, 0.0) == 0
    assert _ambient_carry_steps(1.0, -1.0) == 0


# == the materiality thresholds =============================================


def test_a_coverage_run_must_clear_a_whole_bucket_to_be_labelled() -> None:
    """**One state-space bucket, the same threshold Safety Buy uses.**

    A rounding may not label a run. This is the boundary, asserted on both sides of
    it: below and at the bucket the run is not coverage, above it the run is.
    """
    assert _coverage_runs({4: BUCKET * 0.5}, BUCKET) == ()
    assert _coverage_runs({4: BUCKET}, BUCKET) == ()
    assert _coverage_runs({4: BUCKET * 1.001}, BUCKET) == (4,)
    # Zero is never material, which is what stops every run of an unpromoted plan
    # being listed.
    assert _coverage_runs({4: 0.0, 8: 0.0}, BUCKET) == ()
    # Sorted by index, so the published order is the horizon's order.
    assert _coverage_runs({8: BUCKET * 2, 4: BUCKET * 2}, BUCKET) == (4, 8)


# == precedence, as arithmetic ==============================================


def test_the_economic_share_is_published_net_of_coverage() -> None:
    """**Three headings that cannot overlap, and this is the subtraction.**

    ``_safety_buy_attribution`` answers one question -- how much did the reserve
    compel -- and calls the rest economic, which was exact while there were two
    categories. A coverage kilowatt-hour must not be published as a discretionary
    trade any more than it may be published as compulsory.
    """
    safety = {4: (1.0, 3.0), 8: (0.0, 2.0)}
    coverage = {4: 2.0, 8: 2.0}

    assert _net_of_coverage(safety, coverage) == {4: (1.0, 1.0), 8: (0.0, 0.0)}


def test_the_economic_share_never_goes_negative() -> None:
    """A clamp rather than a signed remainder, because a negative share is not a
    quantity of energy. If coverage ever exceeded the non-compelled part the fault
    is upstream, and publishing a negative kWh would hide it behind an arithmetic
    curiosity."""
    assert _net_of_coverage({4: (1.0, 1.0)}, {4: 5.0}) == {4: (1.0, 0.0)}


def test_no_coverage_leaves_the_pair_exactly_as_it_was() -> None:
    """The ordinary case -- discretion already buys the useful energy -- must be a
    no-op, not a rebuild that could perturb a figure."""
    safety = {4: (1.0, 3.0)}

    assert _net_of_coverage(safety, {}) == safety


def test_the_compelled_quantity_is_carried_forward_earliest_first() -> None:
    """**The reserve is a deadline, so the allocation is not a matter of taste.**

    Energy that has to exist by a given quarter has to be bought before it, and a
    later run cannot satisfy an earlier requirement. So a compelled quantity is
    spread over the executed plan's runs in index order, capped at each run's own
    charge, and the remainder of every run is economic.
    """

    class _Run:
        action = ECONOMIC_ACTION_CHARGE

        def __init__(self, index: int, charge: float) -> None:
            self.start_index = index
            self.battery_charge_ac_kwh = charge

    class _Plan:
        def __init__(self, runs) -> None:
            self.runs = runs

    plan = _Plan([_Run(4, 2.0), _Run(8, 3.0)])

    # Fits inside the first run: the second carries none of it.
    assert _carry_compelled_forward(plan, 1.5) == {4: (1.5, 0.5), 8: (0.0, 3.0)}
    # Spills into the second, and only by what the first could not hold.
    assert _carry_compelled_forward(plan, 2.5) == {4: (2.0, 0.0), 8: (0.5, 2.5)}
    # Never exceeds a run's charge, however large the quantity.
    assert _carry_compelled_forward(plan, 99.0) == {4: (2.0, 0.0), 8: (3.0, 0.0)}
    # Nothing compelled leaves every run entirely economic.
    assert _carry_compelled_forward(plan, 0.0) == {4: (0.0, 2.0), 8: (0.0, 3.0)}

    # And the shares sum to the run in every one of those cases.
    for quantity in (0.0, 1.5, 2.5, 99.0):
        for run in plan.runs:
            compelled, economic = _carry_compelled_forward(plan, quantity)[
                run.start_index
            ]
            assert compelled + economic == pytest.approx(run.battery_charge_ac_kwh)


def test_a_non_charge_run_is_never_given_a_compelled_share() -> None:
    """Discharging is not a purchase, so it cannot be a compulsory one."""

    class _Run:
        def __init__(self, index: int, action: str) -> None:
            self.start_index = index
            self.action = action
            self.battery_charge_ac_kwh = 0.0

    class _Plan:
        def __init__(self, runs) -> None:
            self.runs = runs

    plan = _Plan([_Run(4, "discharge"), _Run(8, "export")])

    assert _carry_compelled_forward(plan, 5.0) == {}


# == the clock, which is arithmetic and not a modulus =======================


def _series(count: int, *, start: int, price: float = 0.30):
    demands = tuple(
        IntervalDemand(index=start + offset, baseline_kwh=0.25, pv_kwh=0.0)
        for offset in range(count)
    )
    prices = tuple(
        IntervalPrice(import_eur_kwh=price, export_eur_kwh=price - 0.02)
        for _ in demands
    )
    return demands, prices


@pytest.mark.parametrize("day", [92, 96, 100])
def test_the_clock_slot_is_the_civil_slot_on_a_day_of_any_length(day: int) -> None:
    """**A modulus against 96 is wrong twice a year, and this is the arithmetic.**

    The window is built by matching civil-time slots, so an interval index has to
    resolve to the slot it really is. Checked by construction rather than by
    inspection: a horizon that starts mid-afternoon and runs into the following day
    must produce a replay whose width the day length explains.
    """
    demands, prices = _series(day, start=day // 2)
    window = post_horizon_window(
        demands, prices, horizon_intervals=len(demands), today_interval_count=day
    )

    assert window.basis == TERMINAL_WINDOW_CLOCK_MATCHED
    assert window.demand_ac_kwh > 0.0
    assert window.intervals > 0
    # Every proxy came from a priced slot, so the price is the one that was known.
    assert window.displaced_price_eur_kwh == pytest.approx(0.30)


def test_a_zero_interval_count_degrades_to_an_empty_window() -> None:
    """The sentinel, so a missing count is a defined answer and not an exception."""
    demands, prices = _series(48, start=0)
    window = post_horizon_window(
        demands, prices, horizon_intervals=len(demands), today_interval_count=0
    )

    assert window.demand_ac_kwh == 0.0
    assert window.intervals == 0


def test_the_window_reads_no_price_the_horizon_did_not_price() -> None:
    """**Sourced from the priced prefix only, which is a structural guarantee.**

    Half the series is priced and half is not. The window's estimate must be the
    priced half's price, whatever the unpriced half says -- and here the unpriced
    half is given an absurd price so that reading it would be unmistakable.
    """
    demands, _ = _series(48, start=0)
    priced = tuple(
        IntervalPrice(import_eur_kwh=0.30, export_eur_kwh=0.28) for _ in range(24)
    )
    absurd = tuple(
        IntervalPrice(import_eur_kwh=99.0, export_eur_kwh=98.0) for _ in range(24)
    )
    window = post_horizon_window(
        demands, priced + absurd, horizon_intervals=24, today_interval_count=96
    )

    assert window.displaced_price_eur_kwh == pytest.approx(0.30)
    assert window.displaced_price_eur_kwh < 1.0
