"""The physical setpoint, checked against the failure that produced it.

**The incident these tests exist for.** Writes happen only on a full coordinator
refresh, and refreshes happen on quarter boundaries -- so a battery setpoint chosen
at :00 stood for fifteen minutes while production and house load moved underneath
it. A fixed 1.3 kW charge caused unintended import *and* unintended export on the
same afternoon. Following a grid target with live measurements is the fix, and the
two named regressions below are that afternoon in both directions.

Everything here is pure. No Home Assistant, no coordinator, no clock -- which is
the point of putting the arithmetic in its own module: the sign conventions are the
easiest thing in this release to get quietly wrong, and they are checkable without
a device.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from custom_components.alpha_ems_manager.alphaess_device import (
    DISPATCH_DEADMAN_MINUTES,
    DISPATCH_MODE_LABELS,
    DISPATCH_MODE_SOC_CONTROL,
)
from custom_components.alpha_ems_manager.const import (
    ACTION_CHARGE,
    ACTION_DISCHARGE,
    DISPATCH_CLAMP_ORDER,
    DISPATCH_LIMIT_DEADBAND,
    DISPATCH_LIMIT_DIRECTION_GATE,
    DISPATCH_LIMIT_DYNAMIC_RESERVE,
    DISPATCH_LIMIT_HEADROOM,
    DISPATCH_LIMIT_INVERTER_POWER,
    DISPATCH_LIMIT_NONE,
    DISPATCH_LIMIT_QUANTISATION,
    DISPATCH_LIMIT_REMAINING_GRID_ENERGY,
    DISPATCH_POWER_DEADBAND_KW,
    DISPATCH_POWER_STEP_KW,
    TICK_APPLIED,
    TICK_SKIPPED_DEADBAND,
)
from custom_components.alpha_ems_manager.dispatch import (
    _CLAMP_FIELDS,
    ChargeLimits,
    achievable_grid_kw,
    clamp_charge_kw,
    crosses_zero,
    deadman_minutes,
    decide,
    mode_for,
    quantise_kw,
    required_dispatch_kw,
)


def charge(**limits) -> ChargeLimits:
    """Return limits with only the named bounds set; the rest unconstrained."""
    return ChargeLimits(**limits)


# -- 1. the canonical identity ------------------------------------------------


@pytest.mark.parametrize(
    ("name", "house", "pv", "grid", "dispatch"),
    [
        ("controlled import", 2.0, 5.0, 0.8, -3.8),
        ("controlled export", 1.5, 0.0, -2.0, 3.5),
        ("zero grid, absorbing", 2.6, 2.9, 0.0, -0.3),
        ("cloud passes", 2.6, 1.5, 0.5, 0.6),
        ("sun returns", 2.6, 2.9, 0.5, -0.8),
        ("no production", 3.0, 0.0, 3.0, 0.0),
        ("production exceeds load, exporting on purpose", 1.0, 6.0, -2.0, -3.0),
    ],
)
def test_the_identity_holds_in_every_quadrant(
    name: str, house: float, pv: float, grid: float, dispatch: float
) -> None:
    """``grid = house - pv - dispatch``, rearranged, across all four quadrants.

    The last two rows of the table are the same economic decision at two physical
    setpoints -- a **sign crossing inside one quarter**, with Stage A having said
    nothing. That is an ordinary consequence of the identity and not a new
    economic direction.
    """
    assert required_dispatch_kw(
        house_load_kw=house, pv_kw=pv, desired_grid_kw=grid
    ) == pytest.approx(dispatch)


@pytest.mark.parametrize(
    ("house", "pv", "grid"),
    [(2.0, 5.0, 0.8), (1.5, 0.0, -2.0), (2.6, 2.9, 0.0), (0.0, 0.0, 0.0)],
)
def test_the_identity_round_trips(house: float, pv: float, grid: float) -> None:
    """Solving for dispatch and back for grid returns the target unchanged."""
    dispatch = required_dispatch_kw(house_load_kw=house, pv_kw=pv, desired_grid_kw=grid)
    assert achievable_grid_kw(
        house_load_kw=house, pv_kw=pv, applied_kw=dispatch
    ) == pytest.approx(grid)


def test_achievable_grid_reports_a_target_that_cannot_be_reached() -> None:
    """**The whole reason this figure is published.**

    A plan wanting +0.5 kW that computes -7.0 and can only apply -3.0 achieves
    +4.5. Saying so is the difference between a diagnosable clamp and a mystery.
    """
    decision = decide(
        desired_grid_kw=0.5,
        house_load_kw=2.5,
        pv_kw=10.0,
        limits=charge(headroom_kw=3.0),
        last_applied_kw=None,
    )

    assert decision.required_kw == pytest.approx(-8.0)
    assert decision.applied_kw == pytest.approx(-3.0)
    # Exporting 4.5 kW, not importing 0.5: the surplus the battery could not take
    # leaves through the meter, and the sign is the whole point of publishing it.
    assert decision.achievable_grid_kw == pytest.approx(-4.5)
    assert decision.limited_by == DISPATCH_LIMIT_HEADROOM
    # The identity ties the four figures together, so the report cannot be
    # internally inconsistent: achievable = desired + (calculated - applied).
    assert decision.achievable_grid_kw == pytest.approx(
        decision.desired_grid_kw + (decision.calculated_kw - decision.applied_kw)
    )


# -- 2. quantisation ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "quantised"),
    [
        (-2.34, -2.3),
        (-2.36, -2.3),
        (2.34, 2.3),
        (-0.09, 0.0),
        (0.0, 0.0),
        (-0.3, -0.3),
        (-3.0, -3.0),
    ],
)
def test_quantisation_never_increases_the_magnitude(
    raw: float, quantised: float
) -> None:
    """Toward zero for either sign, which is conservative in both directions."""
    result = quantise_kw(raw)

    assert result == pytest.approx(quantised)
    assert abs(result) <= abs(raw) + 1e-9


def test_quantisation_lands_on_the_device_step() -> None:
    """Swept, because the float division does not land on integers by itself."""
    for milliwatts in range(0, 5001, 7):
        value = -milliwatts / 1000.0
        result = quantise_kw(value)
        steps = result / DISPATCH_POWER_STEP_KW
        assert abs(steps - round(steps)) < 1e-6, (value, result)
        assert abs(result) <= abs(value) + 1e-9


# -- 3. the clamp hierarchy ---------------------------------------------------


def test_the_clamp_slots_match_the_published_order() -> None:
    """**The order a reader is promised is the order actually applied.**

    ``DISPATCH_CLAMP_ORDER`` is the documented hierarchy and ``_CLAMP_FIELDS`` is
    what the code walks. Tying them here is what stops the documentation and the
    behaviour drifting apart, which for a safety hierarchy is not cosmetic.

    Quantisation is the last entry in the published order and is not a
    ``ChargeLimits`` field -- it is applied after the bounds, on the result.
    """
    assert tuple(reason for _, reason in _CLAMP_FIELDS) == DISPATCH_CLAMP_ORDER[:-1]
    assert DISPATCH_CLAMP_ORDER[-1] == DISPATCH_LIMIT_QUANTISATION


def test_an_unconstrained_limit_is_none_and_never_zero() -> None:
    """A missing reading must not become a prohibition."""
    applied, reason = clamp_charge_kw(4.0, ChargeLimits())

    assert applied == pytest.approx(4.0)
    assert reason == DISPATCH_LIMIT_NONE


def test_the_binding_clamp_is_the_one_reported() -> None:
    """**The bound the final figure came from, not the first one encountered.**

    Three bounds all bite here. Reporting the inverter limit because it was
    checked first would send a reader to a constraint that is not what held the
    charge down.
    """
    applied, reason = clamp_charge_kw(
        9.0, charge(inverter_kw=6.0, reserve_kw=4.0, headroom_kw=2.0)
    )

    assert applied == pytest.approx(2.0)
    assert reason == DISPATCH_LIMIT_HEADROOM


def test_the_only_clamp_that_bites_is_reported_whatever_its_slot() -> None:
    """A single binding constraint is named directly, early slot or late."""
    applied, reason = clamp_charge_kw(9.0, charge(inverter_kw=6.0))

    assert applied == pytest.approx(6.0)
    assert reason == DISPATCH_LIMIT_INVERTER_POWER


def test_clamps_only_ever_reduce() -> None:
    """A generous bound cannot raise a modest request."""
    applied, reason = clamp_charge_kw(1.0, charge(inverter_kw=10.0, headroom_kw=9.0))

    assert applied == pytest.approx(1.0)
    assert reason == DISPATCH_LIMIT_NONE


def test_a_zero_bound_stops_the_charge() -> None:
    """Exhausted authorisation is a real zero, distinct from unconstrained."""
    applied, reason = clamp_charge_kw(4.0, charge(remaining_grid_kw=0.0))

    assert applied == pytest.approx(0.0)
    assert reason == DISPATCH_LIMIT_REMAINING_GRID_ENERGY


# -- 4. the direction gate ----------------------------------------------------


def test_a_required_discharge_is_held_at_zero_not_inverted() -> None:
    """**The charge-only envelope, and why zero is a legitimate setpoint.**

    Holding the battery still is exactly what "do not discharge" means
    physically. Refusing to write anything would leave the previous charge
    running into the reversal it was told to stop, which is worse than writing
    zero.
    """
    decision = decide(
        desired_grid_kw=0.5,
        house_load_kw=2.6,
        pv_kw=1.5,
        limits=charge(),
        last_applied_kw=-2.0,
    )

    assert decision.required_kw == pytest.approx(0.6)
    assert decision.calculated_kw == pytest.approx(0.0)
    assert decision.applied_kw == pytest.approx(0.0)
    assert decision.limited_by == DISPATCH_LIMIT_DIRECTION_GATE
    assert decision.update_needed is True


def test_the_required_discharge_is_still_reported() -> None:
    """The gate suppresses the command, never the observation."""
    decision = decide(
        desired_grid_kw=-1.0,
        house_load_kw=4.0,
        pv_kw=0.0,
        limits=charge(),
        last_applied_kw=None,
    )

    assert decision.required_kw == pytest.approx(5.0)
    assert decision.applied_kw == pytest.approx(0.0)


def test_a_discharge_is_permitted_when_the_envelope_is_widened() -> None:
    """The gate is a parameter, not a hard-coded truth -- so it can be tested."""
    decision = decide(
        desired_grid_kw=-1.0,
        house_load_kw=4.0,
        pv_kw=0.0,
        limits=charge(),
        last_applied_kw=None,
        charge_only=False,
    )

    assert decision.applied_kw == pytest.approx(5.0)
    assert decision.limited_by == DISPATCH_LIMIT_NONE


# -- 5. deadband and hysteresis ----------------------------------------------


def test_a_sub_deadband_wobble_writes_nothing() -> None:
    """-2.00 to -2.04 quantises to the same step, so the call buys nothing."""
    decision = decide(
        desired_grid_kw=0.0,
        house_load_kw=0.0,
        pv_kw=2.04,
        limits=charge(),
        last_applied_kw=-2.0,
    )

    assert decision.update_needed is False
    assert decision.update_reason == TICK_SKIPPED_DEADBAND
    assert decision.applied_kw == pytest.approx(-2.0)
    assert decision.limited_by == DISPATCH_LIMIT_DEADBAND


def test_a_real_correction_is_written() -> None:
    """-2.0 to -2.3 clears the band and is a correction worth making."""
    decision = decide(
        desired_grid_kw=0.0,
        house_load_kw=0.0,
        pv_kw=2.3,
        limits=charge(),
        last_applied_kw=-2.0,
    )

    assert decision.update_needed is True
    assert decision.update_reason == TICK_APPLIED
    assert decision.applied_kw == pytest.approx(-2.3)


def test_the_first_tick_of_a_run_always_writes() -> None:
    """With no previous setpoint there is nothing to be within a band of."""
    decision = decide(
        desired_grid_kw=0.0,
        house_load_kw=0.0,
        pv_kw=0.05,
        limits=charge(),
        last_applied_kw=None,
    )

    assert decision.update_needed is True


@pytest.mark.parametrize(
    ("last", "candidate", "permitted"),
    [
        (-0.1, 0.1, False),
        (0.1, -0.1, False),
        (-0.1, 0.3, True),
        (-0.1, 0.2, True),
        (-2.0, -2.5, True),
        (0.0, 0.1, True),
        (-0.1, 0.0, True),
    ],
)
def test_a_sign_change_must_clear_the_band_on_the_far_side(
    last: float, candidate: float, permitted: bool
) -> None:
    """**The deadband alone would still allow chatter around zero.**

    A setpoint at -0.1 with a 0.2 band is free to hop to +0.1 and back, because
    each hop is inside the band measured from the other side. Reversing a sign
    therefore has to clear the band past zero: noise cannot flip it, a real
    reversal can.
    """
    assert crosses_zero(last, candidate, DISPATCH_POWER_DEADBAND_KW) is permitted


def test_jitter_around_zero_never_writes() -> None:
    """The oscillation the hysteresis exists to stop, driven as a sequence."""
    last = -0.1
    writes = 0
    for pv in (0.1, -0.1, 0.1, -0.1, 0.1):
        decision = decide(
            desired_grid_kw=0.0,
            house_load_kw=0.0,
            pv_kw=pv,
            limits=charge(),
            last_applied_kw=last,
            charge_only=False,
        )
        if decision.update_needed:
            writes += 1
            last = decision.applied_kw

    assert writes == 0


def test_at_most_one_write_per_decision() -> None:
    """The decision is a single setpoint, so a tick can never write twice."""
    decision = decide(
        desired_grid_kw=0.5,
        house_load_kw=2.0,
        pv_kw=6.0,
        limits=charge(),
        last_applied_kw=-1.0,
    )

    assert isinstance(decision.applied_kw, float)
    assert decision.update_needed is True


# -- 6. the named hardware regressions ---------------------------------------


def test_pv_rise_does_not_leak_export() -> None:
    """**The observed failure, in the direction it was observed.**

    House 2.6 kW, production 2.9 kW, Stage A holding a charge-authorised grid
    target. Production then rises while the house holds. The charge must become
    *more negative* -- absorbing the additional production -- so the meter stays
    near target instead of the surplus leaving through it.

    A stale fixed battery power is what produced the export: the setpoint chosen
    at the top of the quarter could not know the sun had come out.
    """
    target = 0.5
    before = decide(
        desired_grid_kw=target,
        house_load_kw=2.6,
        pv_kw=2.9,
        limits=charge(),
        last_applied_kw=None,
    )
    after = decide(
        desired_grid_kw=target,
        house_load_kw=2.6,
        pv_kw=5.9,
        limits=charge(),
        last_applied_kw=before.applied_kw,
    )

    assert before.applied_kw == pytest.approx(-0.8)
    assert after.applied_kw == pytest.approx(-3.8)
    assert after.applied_kw < before.applied_kw
    assert after.achievable_grid_kw == pytest.approx(target)
    assert after.achievable_grid_kw > 0.0, "the meter must not go into export"

    # And the counterfactual: holding the old setpoint is what exported.
    stale = achievable_grid_kw(house_load_kw=2.6, pv_kw=5.9, applied_kw=-0.8)
    assert stale < 0.0, "the stale setpoint is the export this test forbids"


def test_pv_collapse_does_not_invent_import() -> None:
    """The inverse, and the rule Stage B may never break.

    Production collapses, so the charge becomes less negative and may reach zero.
    What must **not** happen is Stage B buying more from the grid to keep the
    battery on its old trajectory: the grid target is unchanged, so the achieved
    meter figure must not exceed it.
    """
    target = 0.5
    before = decide(
        desired_grid_kw=target,
        house_load_kw=2.6,
        pv_kw=5.9,
        limits=charge(),
        last_applied_kw=None,
    )
    after = decide(
        desired_grid_kw=target,
        house_load_kw=2.6,
        pv_kw=0.4,
        limits=charge(),
        last_applied_kw=before.applied_kw,
    )

    assert before.applied_kw == pytest.approx(-3.8)
    assert after.applied_kw >= before.applied_kw
    assert after.applied_kw == pytest.approx(0.0)
    assert after.limited_by == DISPATCH_LIMIT_DIRECTION_GATE
    assert after.achievable_grid_kw <= 2.6 - 0.4 + 1e-9
    assert after.desired_grid_kw == pytest.approx(target), "the target is untouched"


def test_pv_ahead_still_absorbs_free_production() -> None:
    """**Review finding F2, as a regression.**

    Production runs ahead of forecast, so the composite ``battery_target_kwh``
    would already be satisfied -- but the *grid* authorisation is untouched, and
    absorbing production the house cannot use costs nothing. Clamping this
    controller with the battery composite is what would push free energy out to
    the meter at an export price the optimizer had already judged worse than
    storing it.

    Here the grid cap is not exhausted and headroom is what bounds the charge, so
    absorption continues and nothing is exported.
    """
    decision = decide(
        desired_grid_kw=0.2,
        house_load_kw=1.0,
        pv_kw=5.0,
        limits=charge(remaining_grid_kw=5.0, headroom_kw=4.0),
        last_applied_kw=-1.0,
    )

    assert decision.applied_kw == pytest.approx(-4.0)
    assert decision.limited_by == DISPATCH_LIMIT_HEADROOM
    assert decision.achievable_grid_kw == pytest.approx(0.0)
    assert decision.achievable_grid_kw >= 0.0, "no free production may be exported"

    # **The counterfactual, which is the finding.** Clamp four denominated in
    # battery energy would already be satisfied by the production that arrived
    # early, driving the setpoint to zero -- and four kilowatts of free energy
    # would leave through the meter at an export price the optimizer had already
    # judged worse than storing it.
    leaked = achievable_grid_kw(house_load_kw=1.0, pv_kw=5.0, applied_kw=0.0)
    assert leaked == pytest.approx(-4.0)


def test_an_exhausted_grid_budget_stops_buying_but_the_reason_is_visible() -> None:
    """The other half of F2: a real grid stop must be nameable in diagnostics."""
    decision = decide(
        desired_grid_kw=2.0,
        house_load_kw=1.0,
        pv_kw=0.0,
        limits=charge(remaining_grid_kw=0.0),
        last_applied_kw=-2.0,
    )

    assert decision.applied_kw == pytest.approx(0.0)
    assert decision.limited_by == DISPATCH_LIMIT_REMAINING_GRID_ENERGY


@pytest.mark.parametrize(
    ("label", "house_before", "house_after"),
    [("house rises", 2.0, 4.0), ("house falls", 4.0, 2.0)],
)
def test_house_movement_moves_the_setpoint_not_the_target(
    label: str, house_before: float, house_after: float
) -> None:
    """House load moves the physical setpoint; the economic target is fixed."""
    target = 0.5
    before = decide(
        desired_grid_kw=target,
        house_load_kw=house_before,
        pv_kw=6.0,
        limits=charge(),
        last_applied_kw=None,
    )
    after = decide(
        desired_grid_kw=target,
        house_load_kw=house_after,
        pv_kw=6.0,
        limits=charge(),
        last_applied_kw=before.applied_kw,
    )

    assert after.desired_grid_kw == pytest.approx(target)
    assert after.applied_kw != pytest.approx(before.applied_kw)
    assert after.achievable_grid_kw == pytest.approx(target)


def test_simultaneous_movement_still_lands_on_the_target() -> None:
    """Production and load moving together is one subtraction, not two rules."""
    decision = decide(
        desired_grid_kw=1.0,
        house_load_kw=3.5,
        pv_kw=5.5,
        limits=charge(),
        last_applied_kw=-0.5,
    )

    assert decision.applied_kw == pytest.approx(-3.0)
    assert decision.achievable_grid_kw == pytest.approx(1.0)


def test_a_reserve_clamp_is_named_rather_than_silent() -> None:
    """Every clamp that bites has to be attributable, including the reserve."""
    decision = decide(
        desired_grid_kw=0.0,
        house_load_kw=0.0,
        pv_kw=8.0,
        limits=charge(reserve_kw=2.5),
        last_applied_kw=None,
    )

    assert decision.applied_kw == pytest.approx(-2.5)
    assert decision.limited_by == DISPATCH_LIMIT_DYNAMIC_RESERVE


# -- 7. mode selection -------------------------------------------------------


def test_mode_two_is_selected_for_a_negative_charge() -> None:
    """The one controllable kW primitive, and the only executable combination."""
    choice = mode_for(ACTION_CHARGE, signed_power_kw=-2.3)

    assert choice.mode == DISPATCH_MODE_SOC_CONTROL
    assert choice.executable is True


def test_a_charge_with_a_non_negative_power_is_not_executable() -> None:
    """**The sign is part of the barrier, not a downstream check.**

    A charge must be negative on this surface. A non-negative figure is a sign
    error or a rounding artefact, and neither may be sent.
    """
    choice = mode_for(ACTION_CHARGE, signed_power_kw=2.3)

    assert choice.executable is False
    assert choice.reason == "charge_requires_negative_power"


@pytest.mark.parametrize("action", [ACTION_DISCHARGE, "export", "curtail", None])
def test_nothing_but_a_charge_selects_a_mode(action: str | None) -> None:
    """Planned and explained, and deliberately given no mode to be sent with."""
    choice = mode_for(action, signed_power_kw=-2.0)

    assert choice.mode is None
    assert choice.executable is False


def test_the_mode_label_is_the_exact_string_the_package_parses() -> None:
    """The package takes the number out of the label, so near enough is wrong."""
    assert DISPATCH_MODE_LABELS[2] == "State of Charge Control (2)"
    assert DISPATCH_MODE_LABELS[6] == "Optimise Consumption (6)"
    assert DISPATCH_MODE_LABELS[7] == "Maximise Consumption (7)"


def test_mode_selection_reads_no_price() -> None:
    """Structural: a price sign must never choose a mode.

    Mode selection happens *after* Stage A has decided what to do. A rule shaped
    "if the price is negative then pick mode N" would be an economic decision
    taken in the execution layer, which is the one thing Stage B may not do.
    """
    import inspect

    source = inspect.getsource(mode_for)
    for forbidden in ("price", "eur", "tariff", "cost"):
        assert forbidden not in source.lower().split('"""')[-1], forbidden


# -- 8. the dead-man alternation ---------------------------------------------


def test_the_duration_alternates_so_the_vendor_automation_fires() -> None:
    """**A state-change trigger is why this alternates at all.**

    ``AlphaESS_Update_Dispatch_Duration`` fires on the ``input_number`` changing
    state, so writing the same value re-arms nothing and the run would expire
    silently mid-charge. There is no cleaner path: the package exposes no
    ``script:`` section and no service or event that re-triggers it.
    """
    low, high = DISPATCH_DEADMAN_MINUTES

    assert deadman_minutes(None) == low
    assert deadman_minutes(float(low)) == high
    assert deadman_minutes(float(high)) == low


def test_the_alternation_never_repeats_a_value() -> None:
    """Driven as a sequence, because a repeat is a silently expired dead-man."""
    written: list[int] = []
    previous: float | None = None
    for _ in range(8):
        minutes = deadman_minutes(previous)
        written.append(minutes)
        previous = float(minutes)

    assert all(a != b for a, b in pairwise(written)), written


def test_both_values_sit_on_the_helper_step_and_bound_the_run() -> None:
    """The helper steps in fives, and the semantic dead-man stays about twenty.

    The twenty-five is not a longer run, not more energy and not a different
    planning horizon -- it exists only to be a different number from twenty.
    """
    low, high = DISPATCH_DEADMAN_MINUTES

    assert low % 5 == 0 and high % 5 == 0
    assert low == 20
    assert high - low == 5
    assert high <= 30, "the alternation must not become a longer horizon"
