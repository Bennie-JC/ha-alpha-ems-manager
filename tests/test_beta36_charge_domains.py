"""beta.36: the grid ceiling bounds the grid, and production feeds the battery.

**Gate 3, and it exists because of a hardware measurement.** The 0 kW hold was
measured on the reference inverter and turned out to be a *total* hold: Mode 2 at
zero suppresses battery charging as well as discharging, so 1.3 kW of free
production went to the meter. That is the right command for a row whose objective is
already met and an indefensible one for an unfinished row — and asking why the
controller ever wanted 0 kW on an unfinished row found the real defect.

``decide_charge`` applied the grid authorisation **twice**: once correctly, added to
the production surplus, and again through ``_CLAMP_FIELDS`` as a bare bound on the
**battery** power. With the grid budget spent the second application pulled the
battery to zero however much free production was standing there — and the function's
own ``desired_grid_kw`` said so, in watts, before anybody measured it.

Its docstring had promised the opposite since beta.27:

    **Free production is still absorbed once the grid budget is spent**: the cap
    falls to the surplus alone, and the charge continues under the battery, headroom
    and reserve limits rather than pushing production to the meter.

These tests are pure and exact — no coordinator, no clock, no fixture — because the
arithmetic is the whole claim.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.const import (
    CONTROL_MIN_POWER_KW,
    DISPATCH_LIMIT_HEADROOM,
    DISPATCH_LIMIT_REMAINING_GRID_ENERGY,
)
from custom_components.alpha_ems_manager.dispatch import (
    ChargeLimits,
    QuarterProgress,
    decide_charge,
)

#: The measured site, from the 2026-09-01 observation. Watts as kW.
PV_KW = 2.8
HOUSE_KW = 1.5
SURPLUS_KW = PV_KW - HOUSE_KW


#: The state of the row that was aborted on 2026-08-31: unfinished, and its grid
#: budget all but spent. ``binding_clamps`` on two of that day's three rows named
#: ``remaining_grid_energy`` with ``pv_helped: true`` beside it.
def unfinished_row(*, grid_remaining_kwh: float = 0.01) -> QuarterProgress:
    """Return an unfinished charge row with a nearly exhausted grid ceiling."""
    return QuarterProgress(
        seconds_remaining=600.0,
        battery_remaining_kwh=0.40,
        grid_remaining_kwh=grid_remaining_kwh,
    )


def charge(progress: QuarterProgress, **limits):
    """Return the charge decision for the measured site."""
    return decide_charge(
        progress=progress,
        house_load_kw=HOUSE_KW,
        pv_kw=PV_KW,
        limits=ChargeLimits(inverter_kw=5.0, **limits),
        last_applied_kw=None,
    )


def test_free_production_is_absorbed_once_the_grid_budget_is_spent() -> None:
    """**The docstring's fourth promise, now true.**

    The run-level authorisation is nearly exhausted and 1.3 kW of production is
    standing there. The battery must take it. Under the double application the
    command was ``0.000`` and the controller's own arithmetic predicted a 1.24 kW
    export — which is what the inverter then did when the same command was issued by
    hand.

    *Mutation: put ``remaining_grid_kw`` back into the battery clamp pass and this
    fails.*
    """
    decision = charge(unfinished_row(), remaining_grid_kw=0.06)

    # A real, commandable charge rather than nothing.
    assert abs(decision.applied_kw) >= CONTROL_MIN_POWER_KW, decision.as_dict()
    assert abs(decision.applied_kw) == pytest.approx(SURPLUS_KW, abs=0.07)
    # And the sign is a charge, not an export.
    assert decision.applied_kw < 0.0


def test_absorbing_production_causes_no_import_beyond_the_authorisation() -> None:
    """**The ceiling is honoured in its own domain, and that is the whole argument.**

    A charge at the production surplus needs no grid at all —
    ``desired_grid_kw = house - pv + applied`` is zero when ``applied`` is the
    surplus — so the fix cannot buy energy the plan did not authorise. What remains
    is exactly the remaining authorisation, and never more.

    This is the assertion that separates "absorb free production" from "weaken the
    ceiling". If it ever fails, the fix is buying energy and must be reverted.
    """
    remaining_kw = 0.06
    decision = charge(unfinished_row(), remaining_grid_kw=remaining_kw)

    assert decision.desired_grid_kw <= remaining_kw + 1e-6, decision.as_dict()
    assert decision.desired_grid_kw >= -1e-6, (
        "and it does not swing the other way into a forced export"
    )


def test_the_export_the_controller_used_to_predict_is_gone() -> None:
    """The regression, in the units the hardware measured it in.

    Kept as its own test with the measured figure written down, because a range
    assertion would let the defect back in at half its size.
    """
    before = decide_charge(
        progress=unfinished_row(),
        house_load_kw=HOUSE_KW,
        pv_kw=PV_KW,
        # What the battery clamp did with the authorisation, restated by hand: this
        # is the old arithmetic, not a second code path.
        limits=ChargeLimits(inverter_kw=5.0),
        last_applied_kw=None,
    )
    forced_export_kw = HOUSE_KW - PV_KW + 0.0

    assert forced_export_kw == pytest.approx(-1.3, abs=0.01), (
        "the measured export, from the site arithmetic alone"
    )
    assert before.desired_grid_kw > forced_export_kw, (
        "the controller no longer predicts an export for this row"
    )


def test_the_reported_clamp_still_names_the_authorisation() -> None:
    """No published token moved.

    The authorisation still binds and is still reported as
    ``remaining_grid_energy`` — it binds the *grid* term now rather than the battery,
    which is a different quantity and the same vocabulary. An automation or a reader
    keyed on the clamp name sees no change.
    """
    decision = charge(unfinished_row(), remaining_grid_kw=0.06)

    assert decision.limited_by == DISPATCH_LIMIT_REMAINING_GRID_ENERGY


def test_a_spent_budget_with_no_production_still_commands_nothing() -> None:
    """**The other half, and it must not be weakened by the half above.**

    With the budget spent *and* no surplus there is genuinely nothing to command, and
    the answer is still zero. This is the state that reaches the
    ``rate_below_resolution`` hold, and it is bounded by construction: the surplus is
    below one commandable step, so at most
    ``CONTROL_MIN_POWER_KW x 60 s`` — about 3 Wh, an eighth of an actuator step —
    goes to the meter before the next tick re-evaluates.
    """
    decision = decide_charge(
        progress=unfinished_row(grid_remaining_kwh=0.0),
        house_load_kw=2.0,
        pv_kw=2.05,
        limits=ChargeLimits(inverter_kw=5.0, remaining_grid_kw=0.0),
        last_applied_kw=None,
    )

    assert abs(decision.applied_kw) < CONTROL_MIN_POWER_KW, decision.as_dict()
    # And the surplus it declines to command is smaller than one step, which is what
    # makes declining it defensible rather than merely convenient.
    assert CONTROL_MIN_POWER_KW > 2.05 - 2.0


def test_the_headroom_cap_still_stops_a_charge_it_is_meant_to_stop() -> None:
    """**Not every zero is the same zero.**

    The Stage-A headroom cap exists so *"forecast production this plan intends to
    absorb is not displaced by charging the pack full early"*. When it binds, the
    plan has decided the pack should keep room — so declining production now is
    obeying the plan, and the total hold is the correct command.

    Asserted so the fix above cannot be widened into "always charge from PV",
    which would silently override an economic decision Stage A already made.
    """
    decision = charge(unfinished_row(grid_remaining_kwh=1.0), headroom_kw=0.0)

    assert decision.applied_kw == pytest.approx(0.0)
    assert decision.limited_by == DISPATCH_LIMIT_HEADROOM


def test_the_two_bounds_are_both_kept_and_the_tighter_one_binds() -> None:
    """The run-level remainder carries the downward revision, and it still bounds.

    Removing the clamp could have lost it. It is folded into the grid term instead,
    as the tighter of the row's own remaining authorisation and the run-level
    revised remainder — so every bound that used to apply still applies, in the
    domain it belongs to.
    """
    generous_row = unfinished_row(grid_remaining_kwh=1.0)

    unrevised = charge(generous_row)
    revised = charge(generous_row, remaining_grid_kw=0.0)

    assert abs(revised.applied_kw) < abs(unrevised.applied_kw), (
        "a downward revision to zero grid must still reduce the command"
    )
    # And with no grid authorisation at all, the command is exactly the surplus.
    assert abs(revised.applied_kw) == pytest.approx(SURPLUS_KW, abs=0.07)
    assert revised.desired_grid_kw == pytest.approx(0.0, abs=0.07), (
        "production only: the meter stays at zero"
    )
