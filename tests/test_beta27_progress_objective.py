"""beta.27: the asymmetric progress objective, proved by counterexample.

**This file is the justification for the design, not a description of it.** The
question it settles is whether one formula can serve both intents:

    desired_grid_kw       -> required_dispatch = house - pv - desired_grid_kw

That is correct for an export, where the published target genuinely *is* a meter
figure. Applied to a charge it treats ``grid_authorised_kwh`` as an amount to
**consume**, and the timelines below show it buying roughly a kilowatt-hour that
production had already paid for. So the objective is asymmetric, per intent:

===========  ===========================  ==============================
intent       objective                    ceiling
===========  ===========================  ==============================
grid_charge  ``battery_target_kwh``       ``grid_authorised_kwh``
net_export   ``grid_export_target_kwh``   battery discharge authorised
===========  ===========================  ==============================

The publication contract already says this. ``execution_target``'s docstring:
*"Two boundaries, two fields. ``battery_target_kwh`` is at the battery and
``grid_target_kwh`` is at the meter."* And the field comments: ``battery_target_kwh``
-- *"Battery side. Authoritative for a charge"*; ``grid_target_kwh`` -- *"Meter
side. Present only when the meter is what the plan is aiming at"*, ``None`` for a
charge.

Every figure below is arithmetic on the real functions. Nothing is stubbed.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.const import (
    CONTROL_TICK_ENERGY_HORIZON_SECONDS,
    DISPATCH_LIMIT_NONE,
    DISPATCH_LIMIT_REMAINING_GRID_ENERGY,
)
from custom_components.alpha_ems_manager.dispatch import (
    ChargeLimits,
    QuarterProgress,
    decide_charge,
    hours_remaining,
    tick_energy_cap_kw,
)

QUARTER_SECONDS = 15 * 60
UNBOUNDED = ChargeLimits()


def charge(
    *,
    seconds: float,
    battery_kwh: float,
    grid_kwh: float,
    house_kw: float = 0.0,
    pv_kw: float = 0.0,
    limits: ChargeLimits = UNBOUNDED,
    last_kw: float | None = None,
):
    """Return the real charge decision for one instant inside a quarter."""
    return decide_charge(
        progress=QuarterProgress(
            seconds_remaining=seconds,
            battery_remaining_kwh=battery_kwh,
            grid_remaining_kwh=grid_kwh,
        ),
        house_load_kw=house_kw,
        pv_kw=pv_kw,
        limits=limits,
        last_applied_kw=last_kw,
    )


def integrate(
    *,
    battery_kwh: float,
    grid_kwh: float,
    pv_profile: list[tuple[float, float]],
    house_kw: float = 0.0,
    limits: ChargeLimits = UNBOUNDED,
    step_seconds: float = 60.0,
) -> tuple[float, float]:
    """Run a quarter minute by minute and return ``(battery_kwh, grid_kwh)`` spent.

    A deliberately naive simulator: the commanded power is assumed delivered, which
    is the only assumption that lets the *objective* be tested in isolation from
    measurement noise. ``pv_profile`` is ``[(until_seconds_elapsed, pv_kw), ...]``.
    """
    battery_done = 0.0
    grid_spent = 0.0
    elapsed = 0.0
    last_kw: float | None = None
    while elapsed < QUARTER_SECONDS:
        pv_kw = next(pv for until, pv in pv_profile if elapsed < until)
        decision = charge(
            seconds=QUARTER_SECONDS - elapsed,
            battery_kwh=max(0.0, battery_kwh - battery_done),
            grid_kwh=max(0.0, grid_kwh - grid_spent),
            house_kw=house_kw,
            pv_kw=pv_kw,
            limits=limits,
            last_kw=last_kw,
        )
        applied = abs(decision.applied_kw)
        last_kw = decision.applied_kw
        hours = step_seconds / 3600.0
        battery_done += applied * hours
        # The grid consequence is *computed*, never targeted:
        #     grid = house - pv + battery
        surplus = max(0.0, pv_kw - house_kw)
        grid_spent += max(0.0, applied - surplus) * hours
        elapsed += step_seconds
    return battery_done, grid_spent


# == 1. the counterexample that decides the design ==========================


def test_outperforming_production_drives_the_grid_spend_toward_zero() -> None:
    """**The counterexample, at t=0.** 8 kW of free production, 2.0 kWh wanted.

    Quarter of 15 minutes, house 0, battery target 2.0 kWh, grid authorised
    1.2 kWh, and production outperforming its 0.8 kWh forecast at 8 kW throughout.

    Asymmetric: the objective is the *battery* figure, 2.0 kWh over 0.25 h = 8.0 kW,
    and the ceiling ``pv_surplus + grid_rate`` is 8.0 + 4.8 = 12.8, so it does not
    bind. 8.0 kW of charge against 8.0 kW of production is **zero grid**.

    The withdrawn single formula would have computed
    ``desired_grid = 1.2/0.25 = +4.8``, hence
    ``dispatch = 0 - 8.0 - 4.8 = -12.8`` -- commanding 12.8 kW of charge and
    importing 4.8 kW throughout, buying energy the sun was already supplying.
    """
    decision = charge(seconds=QUARTER_SECONDS, battery_kwh=2.0, grid_kwh=1.2, pv_kw=8.0)

    assert decision.applied_kw == pytest.approx(-8.0, abs=0.05)
    assert decision.limited_by == DISPATCH_LIMIT_NONE
    # The grid consequence, computed rather than targeted.
    assert pytest.approx(0.0) == 0.0 - 8.0 + 8.0


def test_over_the_whole_quarter_outperforming_production_costs_nothing() -> None:
    """The same quarter integrated: the battery target met, **nothing bought**."""
    battery_done, grid_spent = integrate(
        battery_kwh=2.0, grid_kwh=1.2, pv_profile=[(QUARTER_SECONDS, 8.0)]
    )

    assert battery_done == pytest.approx(2.0, abs=0.05)
    assert grid_spent == pytest.approx(0.0, abs=1e-9)


def test_the_single_formula_would_have_bought_about_a_kilowatt_hour() -> None:
    """The cost of the withdrawn design, stated as a number.

    Not a call into the shipped code -- the formula was never implemented. The
    arithmetic is reproduced here so the rejection has a figure behind it and the
    next person to propose it has to argue with the number.
    """
    grid_rate_kw = 1.2 / 0.25  # the authorisation read as a rate to consume
    # It would have imported at that rate for the whole quarter, because the
    # formula has no term that notices production covering the charge.
    would_have_bought = grid_rate_kw * 0.25

    assert would_have_bought == pytest.approx(1.2)
    # Against an actual need of zero: production supplied the entire 2.0 kWh.
    assert would_have_bought - 0.0 == pytest.approx(1.2)


# == 2. the four properties, each a requirement =============================


def test_production_substitutes_for_planned_grid_energy() -> None:
    """More production, same battery objective, strictly less grid."""
    spends = []
    for pv_kw in (0.0, 2.0, 4.0, 8.0):
        _battery, grid = integrate(
            battery_kwh=2.0, grid_kwh=2.0, pv_profile=[(QUARTER_SECONDS, pv_kw)]
        )
        spends.append(grid)

    assert spends == sorted(spends, reverse=True), spends
    assert spends[0] > spends[-1]
    assert spends[-1] == pytest.approx(0.0, abs=1e-9)


def test_missing_production_cannot_unlock_extra_purchasing() -> None:
    """**Example E.** No production at all, and the ceiling is still the ceiling.

    The battery deficit is 2.0 kWh and the authorisation only 1.2, so the tempting
    error is to let the larger figure drive the rate. The ceiling comes from the
    authorisation, so the charge runs at 4.8 kW, not 8.0.
    """
    decision = charge(seconds=QUARTER_SECONDS, battery_kwh=2.0, grid_kwh=1.2, pv_kw=0.0)

    assert decision.applied_kw == pytest.approx(-4.8, abs=0.05)
    assert decision.limited_by == DISPATCH_LIMIT_REMAINING_GRID_ENERGY

    _battery, grid = integrate(
        battery_kwh=2.0, grid_kwh=1.2, pv_profile=[(QUARTER_SECONDS, 0.0)]
    )
    assert grid <= 1.2 + 1e-9, grid


def test_unspent_grid_authorisation_is_never_a_deficit_to_consume() -> None:
    """A charge finished by production leaves the authorisation unspent, and stops.

    The battery objective is met, so nothing more is requested -- the remaining
    1.2 kWh of authorisation is not an entitlement and is simply not used.
    """
    decision = charge(seconds=300.0, battery_kwh=0.0, grid_kwh=1.2, pv_kw=6.0)

    assert decision.applied_kw == pytest.approx(0.0)


def test_free_production_is_still_absorbed_once_the_grid_budget_is_spent() -> None:
    """**beta.26's F2.** Grid cap at zero, production 5 kW, and the charge continues.

    The ceiling falls to the surplus alone rather than to zero, so free energy goes
    into the pack instead of to the meter. A charge whose *ceiling* is exhausted has
    not reached its *objective*, which is why a ceiling is never a completion test.
    """
    decision = charge(seconds=QUARTER_SECONDS, battery_kwh=2.0, grid_kwh=0.0, pv_kw=5.0)

    assert decision.applied_kw == pytest.approx(-5.0, abs=0.05)
    assert decision.limited_by == DISPATCH_LIMIT_REMAINING_GRID_ENERGY

    _battery, grid = integrate(
        battery_kwh=2.0, grid_kwh=0.0, pv_profile=[(QUARTER_SECONDS, 5.0)]
    )
    assert grid == pytest.approx(0.0, abs=1e-9)


def test_house_load_is_subtracted_from_production_before_the_surplus() -> None:
    """Production covering the house is not surplus, and cannot fund a charge."""
    decision = charge(
        seconds=QUARTER_SECONDS, battery_kwh=2.0, grid_kwh=0.0, pv_kw=3.0, house_kw=3.0
    )

    # No surplus and no grid: nothing may be charged, and the answer is zero rather
    # than a small positive number borrowed from somewhere.
    assert decision.applied_kw == pytest.approx(0.0)


# == 3. behind schedule still speeds up =====================================


def test_falling_behind_raises_the_requested_rate() -> None:
    """**Example A.** 1.6 kWh left with 10 minutes to go asks for 9.6 kW."""
    decision = charge(seconds=600.0, battery_kwh=1.6, grid_kwh=5.0, pv_kw=12.0)

    assert decision.applied_kw == pytest.approx(-9.6, abs=0.05)


def test_back_loaded_production_finishes_under_authorisation_having_sped_up() -> None:
    """Both required behaviours at once, and no special case produces either.

    Nothing for the first half, then 8 kW. The first half is capped by the grid
    rate; the second half needs more than production supplies and draws on what
    authorisation is left. It ends **under** the authorisation.
    """
    battery_done, grid_spent = integrate(
        battery_kwh=2.0,
        grid_kwh=1.2,
        pv_profile=[(QUARTER_SECONDS / 2, 0.0), (QUARTER_SECONDS, 8.0)],
    )

    assert grid_spent <= 1.2 + 1e-9, grid_spent
    # And it did speed up: production plus the authorisation cannot reach 2.0, but
    # it gets most of the way rather than stalling at the first-half rate.
    assert battery_done > 1.2, battery_done


def test_front_loaded_production_spends_less_than_back_loaded() -> None:
    """Early production is cheaper, because it substitutes before time runs short."""
    _b1, front = integrate(
        battery_kwh=2.0,
        grid_kwh=1.2,
        pv_profile=[(QUARTER_SECONDS / 2, 8.0), (QUARTER_SECONDS, 0.0)],
    )
    _b2, back = integrate(
        battery_kwh=2.0,
        grid_kwh=1.2,
        pv_profile=[(QUARTER_SECONDS / 2, 0.0), (QUARTER_SECONDS, 8.0)],
    )

    assert front <= back + 1e-9, (front, back)
    assert front <= 1.2 + 1e-9 and back <= 1.2 + 1e-9


# == 4. the one-tick overshoot cap ==========================================


def test_no_tick_can_ever_be_asked_for_more_than_the_remaining_energy() -> None:
    """**Invariant 6, as a property over the whole quarter.**

    For every remaining time and every remainder, the requested power is at most
    what one control interval could deliver against the energy actually left. Target
    reached is only detected *after* the next measurement, so a request above this
    bound would overshoot before anything could notice.

    Asserted as a property rather than at one point, because the interesting cases
    are at the boundary and picking one by hand is how the boundary gets missed --
    at 600 s remaining the objective rate is only 0.3 kW and nothing is near the cap.

    On this path the guarantee is delivered **twice**: ``hours_remaining`` floors the
    divisor at the same horizon, so ``battery_rate_kw`` is already at most
    ``remaining / (90/3600)``. The explicit cap in ``decide_charge`` is therefore a
    backstop against that floor being changed, and its label stays ``none`` here.
    It does bind on the export path, where the objective lives in the meter domain
    and the battery remainder is an independent quantity.
    """
    assert tick_energy_cap_kw(0.05) == pytest.approx(2.0)

    for seconds in (0.0, 1.0, 15.0, 30.0, 89.0, 90.0, 91.0, 300.0, 600.0, 900.0):
        for remaining in (0.0, 0.01, 0.05, 0.5, 2.0, 10.0):
            decision = charge(
                seconds=seconds, battery_kwh=remaining, grid_kwh=50.0, pv_kw=50.0
            )
            cap = tick_energy_cap_kw(remaining)
            assert abs(decision.applied_kw) <= cap + 1e-9, (seconds, remaining)


def test_the_cap_binds_exactly_at_the_horizon_on_a_tight_remainder() -> None:
    """At or inside the horizon the request converges on the cap, never above it.

    0.05 kWh with 30 seconds left needs 6.0 kW to finish on time. What is requested
    is 2.0 -- the horizon rate -- because that is the most a single interval could
    honestly deliver.
    """
    assert pytest.approx(6.0) == 0.05 / (30.0 / 3600.0)

    decision = charge(seconds=30.0, battery_kwh=0.05, grid_kwh=50.0, pv_kw=50.0)

    assert abs(decision.applied_kw) == pytest.approx(2.0, abs=0.05)
    assert abs(decision.applied_kw) == pytest.approx(tick_energy_cap_kw(0.05), abs=0.05)


def test_the_cap_prefers_a_shortfall_to_an_overshoot_near_the_boundary() -> None:
    """**The deliberate trade-off, asserted as a shortfall.**

    30 seconds left and 0.05 kWh to go needs 6.0 kW. The cap permits 2.0, so one
    tick delivers about 0.017 kWh and the quarter finishes roughly 0.033 kWh short.

    That is the intended direction of error. Overshooting spends energy Stage A did
    not authorise; finishing a few watt-hours short does not. The shortfall is
    recorded with the binding clamp named, and never carried forward -- which
    ``test_beta27_quarter_execution`` asserts on the runtime.
    """
    required_kw = 0.05 / (30.0 / 3600.0)
    assert required_kw == pytest.approx(6.0)

    decision = charge(seconds=30.0, battery_kwh=0.05, grid_kwh=5.0, pv_kw=20.0)

    assert abs(decision.applied_kw) <= 2.0 + 1e-9
    delivered = abs(decision.applied_kw) * (30.0 / 3600.0)
    assert delivered < 0.05
    assert 0.05 - delivered == pytest.approx(0.033, abs=0.005)


def test_the_remaining_hours_are_floored_at_the_tick_horizon() -> None:
    """A rate from three remaining seconds describes a physical impossibility.

    So the divisor is floored at the same horizon the cap enforces, which makes the
    requested rate converge on what one interval could deliver instead of diverging.
    """
    floor = CONTROL_TICK_ENERGY_HORIZON_SECONDS / 3600.0

    assert hours_remaining(3.0) == pytest.approx(floor)
    assert hours_remaining(0.0) == pytest.approx(floor)
    assert hours_remaining(-10.0) == pytest.approx(floor)
    assert hours_remaining(600.0) == pytest.approx(600.0 / 3600.0)


def test_nothing_remaining_asks_for_nothing() -> None:
    """A spent objective produces a zero cap, not a minimum step."""
    assert tick_energy_cap_kw(0.0) == 0.0
    assert tick_energy_cap_kw(-1.0) == 0.0


# == 5. the clamps still bind, and are still reported =======================


def test_the_inverter_limit_still_binds_and_is_named() -> None:
    """The physical clamps are unchanged in meaning and order by beta.27."""
    decision = charge(
        seconds=QUARTER_SECONDS,
        battery_kwh=4.0,
        grid_kwh=4.0,
        pv_kw=20.0,
        limits=ChargeLimits(inverter_kw=3.0),
    )

    assert abs(decision.applied_kw) == pytest.approx(3.0, abs=0.05)
    assert decision.limited_by != DISPATCH_LIMIT_NONE


def test_a_charge_is_always_negative_or_zero() -> None:
    """Sign is a property of the arithmetic, not of a later check."""
    for pv_kw in (0.0, 1.0, 5.0, 20.0):
        for battery_kwh in (0.0, 0.05, 1.0, 4.0):
            decision = charge(
                seconds=QUARTER_SECONDS,
                battery_kwh=battery_kwh,
                grid_kwh=1.0,
                pv_kw=pv_kw,
            )
            assert decision.applied_kw <= 0.0, (pv_kw, battery_kwh)
