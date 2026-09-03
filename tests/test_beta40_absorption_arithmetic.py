"""beta.40 Gate 1: free production is stored, and not one watt of it is bought.

**The 2026-09-03 capture, and the one line of arithmetic that explains it.** Mid
campaign, owned, executing, 25 of 25 safety checks passing, 12.61 kWh of pack
headroom and 8.527 kWh of grid authorisation still unspent: PV 3.309 kW against a
0.792 kW house, 1.490 kW going into the pack and **0.942 kW going out to the
meter**. `decide_charge` had predicted it, in watts, before anybody measured it --
its own `desired_grid_kw` came out **-1.027**.

Nothing was broken. `applied_kw` is seeded from the frozen row's objective and
every line after the seed only reduces, so a row sized to a *forecast* surplus caps
the live response to forecast error. beta.36 let production substitute for planned
grid energy inside the objective; it still could not let production exceed it.

beta.40 adds the one term in that function that may raise a command, and bounds it
by the measured surplus alone. These tests are pure and exact -- no coordinator, no
clock, no fixture -- because the arithmetic is the whole claim.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.const import (
    CONTROL_MIN_POWER_KW,
    DISPATCH_LIMIT_FREE_PV_ABSORPTION,
    DISPATCH_LIMIT_HEADROOM,
    DISPATCH_LIMIT_INVERTER_POWER,
)
from custom_components.alpha_ems_manager.dispatch import (
    ChargeLimits,
    QuarterProgress,
    decide_charge,
)

from .beta40_trace import (
    BETA39_PREDICTED_EXPORT_KW,
    HEADROOM_DC_KWH,
    HOUSE_KW,
    MAX_CHARGE_KW,
    PV_KW,
    REFRESH_ACHIEVABLE_GRID_KW,
    REFRESH_APPLIED_KW,
    REFRESH_DESIRED_GRID_KW,
    REFRESH_HOUSE_KW,
    REFRESH_LIMITED_BY,
    REFRESH_PV_KW,
    ROW_BATTERY_KWH,
    ROW_REMAINING_AT_CAPTURE_KWH,
    SECONDS_REMAINING_AT_CAPTURE,
    SURPLUS_KW,
)


def capture_row(*, authorised: bool, grid_remaining_kwh: float = 0.0):
    """Return the capture's frozen row as it stood at 12:14:17."""
    return QuarterProgress(
        seconds_remaining=SECONDS_REMAINING_AT_CAPTURE,
        battery_remaining_kwh=ROW_REMAINING_AT_CAPTURE_KWH,
        grid_remaining_kwh=grid_remaining_kwh,
        retention_authorised=authorised,
    )


def site_limits(**overrides):
    """Return the capture's physical bounds. Neither binds at these powers."""
    fields = {
        "inverter_kw": MAX_CHARGE_KW,
        # The pack's own room as a rate over the remaining quarter.
        "headroom_kw": HEADROOM_DC_KWH / 0.25,
    }
    fields.update(overrides)
    return ChargeLimits(**fields)


def charge(progress, *, pv_kw=PV_KW, house_kw=HOUSE_KW, **limits):
    """Return the charge decision for the measured site."""
    return decide_charge(
        progress=progress,
        house_load_kw=house_kw,
        pv_kw=pv_kw,
        limits=site_limits(**limits),
        last_applied_kw=None,
    )


# == 1. the capture, before and after =====================================


def test_beta39_predicted_the_export_it_then_measured() -> None:
    """**The defect, reconstructed from the frozen row alone.**

    The objective rate is ``remaining / max(90/3600, seconds/3600)``, and 0.0373 kWh
    over the floored 0.025 h is 1.49 kW -- the figure the pack was measured at. The
    identity then predicts the meter, and it predicted an export.

    *Mutation: authorise the row and this fails, which is the point.*
    """
    decision = charge(capture_row(authorised=False))

    assert decision.applied_kw < 0.0, "a charge is negative on this surface"
    assert abs(decision.applied_kw) == pytest.approx(1.4, abs=0.01)
    # What the controller said it would do to the meter, to the watt.
    assert decision.desired_grid_kw == pytest.approx(
        BETA39_PREDICTED_EXPORT_KW, abs=0.01
    )
    assert decision.desired_grid_kw < 0.0, "it exported free production"


def test_the_authorised_row_stores_the_surplus_instead() -> None:
    """**The mandatory acceptance case: 1.49 kW becomes 2.50 kW.**

    Not 1.49 to 1.6 with most of the same production still leaking -- the command
    goes to the measured surplus, quantised down one step by the actuator's own
    floor-toward-zero rule, and the predicted export goes to nearly nothing.

    *Mutation: make the branch a ``min`` instead of a ``max``, or bound it by the
    grid rate, and this fails.*
    """
    decision = charge(capture_row(authorised=True))

    assert abs(decision.applied_kw) == pytest.approx(2.5, abs=0.001)
    assert decision.limited_by == DISPATCH_LIMIT_FREE_PV_ABSORPTION
    # The surplus, less one quantisation step. 2.517 -> 2.500.
    assert abs(decision.applied_kw) <= SURPLUS_KW + 1e-9
    assert abs(decision.applied_kw) > SURPLUS_KW - 0.1
    # And the export the beta.39 row predicted is gone.
    assert decision.achievable_grid_kw == pytest.approx(-0.017, abs=0.002)
    assert abs(decision.achievable_grid_kw) < 0.1


def test_the_gain_is_the_whole_surplus_and_not_a_fraction_of_it() -> None:
    """A fix that recovered a tenth of the production would be no fix.

    Stated as a ratio so it cannot pass on an implementation that merely nudges the
    setpoint: the command must close at least 95 % of the gap between the row's own
    objective rate and the measured surplus.
    """
    before = abs(charge(capture_row(authorised=False)).applied_kw)
    after = abs(charge(capture_row(authorised=True)).applied_kw)

    gap = SURPLUS_KW - before
    assert gap > 1.0, "the capture must actually have a gap to close"
    assert (after - before) / gap >= 0.95, (before, after, gap)


# == 2. the invariant: it can never buy ===================================


def test_the_absorption_branch_can_never_cause_grid_import() -> None:
    """**The algebra, swept rather than argued.**

        delta = max(objective, surplus) - objective = max(0, surplus - objective)
              <= surplus = pv - house

    so whenever this branch is what bound the command,

        desired_grid = house - pv + applied <= house - pv + (pv - house) = 0

    -- export or zero, never import, for every input and not merely for the one
    capture. Asserted on both the pre-clamp figure and the achievable one.

    **Swept in one test with a vacuity gate rather than parametrised with a skip.**
    Most combinations are bound by some other term, and a per-case ``skip`` would
    let an implementation where the branch *never* fires report a green sweep of
    skips. So the cases are counted, and the count is asserted.

    *Mutation: drop ``pv_surplus_kw`` from the branch, or add ``grid_cap_kw`` to it,
    and this fails on the rows where production is short of the objective.*
    """
    bound = 0
    for pv_kw in (0.0, 0.5, 0.792, 1.5, 3.309, 6.0, 12.0):
        for house_kw in (0.0, 0.792, 2.5, 6.0):
            for objective_kwh in (0.0, 0.0373, 0.28, 2.5):
                for grid_kwh in (0.0, 0.04, 2.27):
                    progress = QuarterProgress(
                        seconds_remaining=SECONDS_REMAINING_AT_CAPTURE,
                        battery_remaining_kwh=objective_kwh,
                        grid_remaining_kwh=grid_kwh,
                        retention_authorised=True,
                    )
                    decision = charge(progress, pv_kw=pv_kw, house_kw=house_kw)
                    unauthorised = charge(
                        QuarterProgress(
                            seconds_remaining=SECONDS_REMAINING_AT_CAPTURE,
                            battery_remaining_kwh=objective_kwh,
                            grid_remaining_kwh=grid_kwh,
                        ),
                        pv_kw=pv_kw,
                        house_kw=house_kw,
                    )
                    case = (pv_kw, house_kw, objective_kwh, grid_kwh, decision)
                    # **Classified on the decision, not on the token.** A physical
                    # clamp legitimately overwrites ``limited_by`` after the branch
                    # has raised the command, so reading the token here would
                    # mis-sort exactly the cases the guard exists for.
                    if decision.as_dict() == unauthorised.as_dict():
                        continue
                    bound += 1
                    # Authorisation is the only term that can have moved it, and it
                    # can only ever have moved it up.
                    assert abs(decision.applied_kw) > abs(unauthorised.applied_kw), case
                    # It only ever *reduced* export. ``desired_grid`` is signed with
                    # positive as import, so storing production raises it toward zero.
                    assert decision.desired_grid_kw >= unauthorised.desired_grid_kw, (
                        case
                    )
                    # And it bought nothing at all.
                    assert decision.desired_grid_kw <= 1e-9, case
                    assert decision.achievable_grid_kw <= 1e-9, case
                    assert (
                        abs(decision.applied_kw) <= max(0.0, pv_kw - house_kw) + 1e-9
                    ), case

    # **The vacuity gate.** A sweep in which the branch never bound would assert
    # nothing about it.
    assert bound >= 20, bound


def test_a_row_with_no_surplus_absorbs_nothing_however_authorised() -> None:
    """Authorisation is a permission over production, not over the grid.

    House above production with the grid budget spent is the beta.36 rest, and it
    stays a rest: nothing to substitute, nothing free to keep.
    """
    decision = charge(capture_row(authorised=True), pv_kw=1.0, house_kw=2.0)

    assert abs(decision.applied_kw) < CONTROL_MIN_POWER_KW
    assert decision.limited_by != DISPATCH_LIMIT_FREE_PV_ABSORPTION


# == 3. nothing that bounded a charge stops bounding it ==================


def test_an_unauthorised_row_is_bit_for_bit_beta39() -> None:
    """**The neutrality proof, at the only layer that commands anything.**

    Field by field, because a decision that agreed on ``applied_kw`` and disagreed
    on ``limited_by`` would still have changed what a reader is told.
    """
    before = charge(capture_row(authorised=False), pv_kw=2.0, house_kw=0.5)
    # Constructed without the beta.40 field at all, which is what every pre-beta.40
    # plan yields once parsed.
    legacy = decide_charge(
        progress=QuarterProgress(
            seconds_remaining=SECONDS_REMAINING_AT_CAPTURE,
            battery_remaining_kwh=ROW_REMAINING_AT_CAPTURE_KWH,
            grid_remaining_kwh=0.0,
        ),
        house_load_kw=0.5,
        pv_kw=2.0,
        limits=site_limits(),
        last_applied_kw=None,
    )

    assert before.as_dict() == legacy.as_dict()


def test_the_grid_ceiling_still_binds_and_is_still_named() -> None:
    """**beta.36, unmoved.** The refresh direction of the same capture.

    At 12:00:05 the ceiling *did* bind: 0.569 kW of surplus plus 0.161 kW of
    remaining authorisation against a 1.127 kW objective. Reproduced to the watt,
    with the published token unchanged.
    """
    progress = QuarterProgress(
        seconds_remaining=894.7,
        battery_remaining_kwh=ROW_BATTERY_KWH,
        grid_remaining_kwh=0.04,
    )
    decision = charge(progress, pv_kw=REFRESH_PV_KW, house_kw=REFRESH_HOUSE_KW)

    assert decision.applied_kw == pytest.approx(REFRESH_APPLIED_KW, abs=0.001)
    assert decision.desired_grid_kw == pytest.approx(REFRESH_DESIRED_GRID_KW, abs=0.002)
    assert decision.achievable_grid_kw == pytest.approx(
        REFRESH_ACHIEVABLE_GRID_KW, abs=0.002
    )
    assert decision.limited_by == REFRESH_LIMITED_BY


def test_authorising_the_row_does_not_unlock_extra_buying() -> None:
    """The ceiling is honoured in its own domain, with the branch live.

    Same refresh instant, now authorised: production is 0.569 kW and the objective
    wants 1.127, so the objective still binds through the grid term and the
    absorption branch -- which can only offer the surplus -- changes nothing about
    how much is bought.
    """
    grid_only = charge(
        QuarterProgress(
            seconds_remaining=894.7,
            battery_remaining_kwh=ROW_BATTERY_KWH,
            grid_remaining_kwh=0.04,
        ),
        pv_kw=REFRESH_PV_KW,
        house_kw=REFRESH_HOUSE_KW,
    )
    authorised = charge(
        QuarterProgress(
            seconds_remaining=894.7,
            battery_remaining_kwh=ROW_BATTERY_KWH,
            grid_remaining_kwh=0.04,
            retention_authorised=True,
        ),
        pv_kw=REFRESH_PV_KW,
        house_kw=REFRESH_HOUSE_KW,
    )

    assert authorised.desired_grid_kw <= grid_only.desired_grid_kw + 1e-9


@pytest.mark.parametrize(
    ("limits", "expected"),
    [
        ({"inverter_kw": 1.0}, DISPATCH_LIMIT_INVERTER_POWER),
        ({"headroom_kw": 0.0}, DISPATCH_LIMIT_HEADROOM),
    ],
)
def test_the_physical_clamps_still_bound_an_absorbing_tick(
    limits: dict, expected: str
) -> None:
    """**The magnitude is theirs, which is why Stage A publishes no kilowatt-hour.**

    Inverter power and pack headroom are applied by the one clamp that owns them.
    An authorised row is not exempt from either, and the binding clamp is still
    named -- so a full pack absorbs nothing and says ``headroom``.

    *Mutation: exempt the absorption branch from ``clamp_charge_kw`` and this
    fails.*
    """
    decision = charge(capture_row(authorised=True), **limits)

    assert decision.limited_by == expected
    assert abs(decision.applied_kw) <= 1.0 + 1e-9


def test_the_tick_horizon_still_guards_an_absorbing_tick() -> None:
    """It may spend the envelope's energy, never more than one interval of it.

    The guard exists because target-reached is detected a tick late. Widening it to
    the absorbing branch is necessary -- against the objective remainder alone an
    absorbing tick clamps to nothing the moment the objective is met -- and it must
    still be a bound.
    """
    decision = charge(capture_row(authorised=True))

    # One 90-second horizon at the commanded power, and no more.
    assert abs(decision.applied_kw) * (90.0 / 3600.0) <= 2.5 * 0.25 + 1e-9


def test_a_charge_is_still_never_positive() -> None:
    """The direction gate, unchanged: this surface charges negative."""
    for authorised in (False, True):
        assert charge(capture_row(authorised=authorised)).applied_kw <= 0.0


def test_the_verdict_never_caps_a_grid_fed_objective() -> None:
    """**The mirror of beta.36, and the reason the branch is a ``max``.**

    An authorised row whose objective wants more than production can supply must
    still get its objective: the rest is bought, under the authorisation that
    already permitted it. Capping total battery power by a free-production figure
    would be beta.36's defect with the domains swapped -- and it would show up as a
    charge that slowed down whenever the sun came out.

    *Mutation: add ``applied = min(applied, absorb)`` on the authorised path and
    this fails.*
    """
    # 2.5 kWh still to deliver over a full quarter: a 10 kW objective against
    # 2.517 kW of production and plenty of authorised grid.
    progress = QuarterProgress(
        seconds_remaining=900.0,
        battery_remaining_kwh=2.5,
        grid_remaining_kwh=2.5,
        retention_authorised=True,
    )
    refused = QuarterProgress(
        seconds_remaining=900.0,
        battery_remaining_kwh=2.5,
        grid_remaining_kwh=2.5,
    )

    authorised = charge(progress)
    baseline = charge(refused)

    # The objective is far above the surplus, so authorising must change nothing.
    assert abs(authorised.applied_kw) > SURPLUS_KW
    assert authorised.as_dict() == baseline.as_dict()


def test_an_unauthorised_row_never_absorbs_at_all() -> None:
    """Stated against the objective rate, not against another decision.

    Comparing a refused row with a legacy one is circular if the branch fires
    unconditionally: both would absorb and both would agree. So this pins the
    refused row to the arithmetic the objective alone permits.

    *Mutation: make ``absorb_kw`` unconditional and this fails.*
    """
    for pv_kw in (2.0, 3.309, 9.0):
        progress = QuarterProgress(
            seconds_remaining=SECONDS_REMAINING_AT_CAPTURE,
            battery_remaining_kwh=ROW_REMAINING_AT_CAPTURE_KWH,
            grid_remaining_kwh=0.0,
        )
        decision = charge(progress, pv_kw=pv_kw)

        # Everything an unauthorised row may ask for: its own objective rate,
        # bounded by production plus the grid it is allowed to buy.
        ceiling_kw = min(
            progress.battery_rate_kw, max(0.0, pv_kw - HOUSE_KW) + progress.grid_rate_kw
        )
        assert abs(decision.applied_kw) <= ceiling_kw + 1e-9, pv_kw
        assert decision.limited_by != DISPATCH_LIMIT_FREE_PV_ABSORPTION
