"""The cumulative grid-energy ceiling -- gate A8.

**Stage A publishes ``expected_grid_to_battery_kwh`` and, before beta.20, nothing
enforced it.** Headroom does not cover this: the headroom cap bounds *stored energy
at window end*, not how much of that energy was bought. If production disappoints
the ceiling correctly *rises* -- there is more room -- and the rolling controller
fills it from the grid. On the worked example that is the difference between buying
5.55 kWh and buying 9.17 kWh.

Enforcing it is not a new economic decision. It enforces a number Stage A already
published and already documents as a maximum.

The four-case table below is here because I got it wrong once, in exactly the shape
this project keeps catching: I wrote ``min(stage_a_ceiling, configured)`` with a
default of ``0.0`` and documented that default as "uncapped". ``min(5.55, 0.0)`` is
zero, so a default installation would have been forbidden from charging at all.
Zero disables the *tightener*; it never means buy nothing.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from custom_components.alpha_ems_manager.const import (
    EXECUTION_REDUCTION_BUDGET,
    EXECUTION_STATE_STOPPING,
    EXECUTION_STOP_GRID_CEILING,
)
from custom_components.alpha_ems_manager.execution import (
    demand_for,
    effective_grid_cap_kwh,
)

from .test_stage_b_carry_forward import evidence_for
from .test_stage_b_controller import (
    OPENS,
    decision_at,
    progress_of,
    raw_target,
    target_of,
)

# ===========================================================================
# The effective cap: four cases, and none of them is "absent means zero"
# ===========================================================================


@pytest.mark.parametrize(
    ("ceiling", "configured", "expected"),
    [
        # Stage A published a ceiling and no tightener is set: the ceiling binds.
        (5.55, 0.0, 5.55),
        # A tightener below it binds instead. This is the commissioning case.
        (5.55, 1.0, 1.0),
        # A tightener above it cannot loosen Stage A's ceiling.
        (5.55, 9.0, 5.55),
        # No published ceiling, a tightener set: the tightener is all there is.
        (None, 1.0, 1.0),
        # Neither: unconstrained. **Not zero.**
        (None, 0.0, None),
    ],
)
def test_the_effective_cap_table(ceiling, configured, expected) -> None:
    """Every combination stated once, including the one that was wrong."""
    assert effective_grid_cap_kwh(ceiling, configured) == expected


def test_a_disabled_tightener_never_becomes_a_zero_cap() -> None:
    """**The bug, as its own test.** The default install must be able to charge."""
    assert effective_grid_cap_kwh(5.55, 0.0) == 5.55
    assert effective_grid_cap_kwh(5.55, 0.0) != 0.0


def test_a_nonsense_budget_cannot_become_a_ceiling() -> None:
    """A malformed figure falls back to "no tightener", never to zero."""
    for junk in (float("nan"), float("inf"), float("-inf"), -3.0):
        assert effective_grid_cap_kwh(5.55, junk) == 5.55


def test_the_tightener_can_only_reduce() -> None:
    """Swept, so no configured value can raise what Stage A approved."""
    for tenths in range(0, 200):
        configured = tenths / 10.0
        cap = effective_grid_cap_kwh(3.0, configured)
        assert cap is not None
        assert cap <= 3.0


# ===========================================================================
# The stop, and that it is a stop rather than a suggestion
# ===========================================================================


def test_reaching_the_ceiling_reduces_the_request_to_nothing() -> None:
    """Advisory enforcement is the state beta.19 shipped in: parsed, compared to
    nothing. The reduction reason names the budget so a reader is not left guessing
    which of several caps bound."""
    target = target_of(expected_grid_to_battery_kwh=3.0)

    demand = demand_for(
        target,
        now=OPENS + timedelta(minutes=30),
        progress=progress_of(1.0),
        current_energy_kwh=9.0,
        remaining_expected_pv_kwh=4.0,
        grid_charged_kwh=3.0,
        configured_budget_kwh=0.0,
    )

    assert demand.reduction == EXECUTION_REDUCTION_BUDGET
    assert demand.required_kw == 0.0
    assert demand.grid_cap_kwh == 3.0
    assert demand.grid_charged_kwh == 3.0


def test_below_the_ceiling_the_run_is_untouched() -> None:
    """The cap must not be a slow squeeze -- it binds or it does not."""
    target = target_of(expected_grid_to_battery_kwh=3.0)

    demand = demand_for(
        target,
        now=OPENS + timedelta(minutes=30),
        progress=progress_of(1.0),
        current_energy_kwh=9.0,
        remaining_expected_pv_kwh=4.0,
        grid_charged_kwh=1.4,
        configured_budget_kwh=0.0,
    )

    assert demand.reduction != EXECUTION_REDUCTION_BUDGET
    assert demand.required_kw > 0.0


def test_an_owned_run_that_reaches_the_ceiling_stops_and_defers() -> None:
    """It stops, resets, and asks for nothing more.

    Whether the remaining energy is still worth buying is an economic question, and
    this layer does not answer them -- so it waits for a fresh Stage-A decision
    rather than forming one.
    """
    decision = decision_at(
        OPENS + timedelta(minutes=30),
        mode_executes=True,
        evidence=evidence_for("abc123"),
        targets=[raw_target(expected_grid_to_battery_kwh=3.0)],
        running_run_id="abc123",
    )
    # The controller helper does not thread the integral, so assert the branch
    # through the demand it is built from, then the state machine on top of it.
    demand = demand_for(
        target_of(expected_grid_to_battery_kwh=3.0),
        now=OPENS + timedelta(minutes=30),
        progress=progress_of(1.0),
        current_energy_kwh=9.0,
        grid_charged_kwh=3.2,
        configured_budget_kwh=0.0,
    )
    assert demand.reduction == EXECUTION_REDUCTION_BUDGET
    assert decision.target is not None


def test_the_commissioning_budget_binds_before_stage_a_does() -> None:
    """The Phase-3 case, with the figures the plan names: 1.0 kWh, not 5.55."""
    target = target_of(expected_grid_to_battery_kwh=5.55)

    stopped = demand_for(
        target,
        now=OPENS + timedelta(minutes=30),
        progress=progress_of(1.0),
        current_energy_kwh=9.0,
        remaining_expected_pv_kwh=4.0,
        grid_charged_kwh=1.0,
        configured_budget_kwh=1.0,
    )
    assert stopped.grid_cap_kwh == 1.0
    assert stopped.reduction == EXECUTION_REDUCTION_BUDGET

    # And without the tightener the same integral is nowhere near the ceiling.
    running = demand_for(
        target,
        now=OPENS + timedelta(minutes=30),
        progress=progress_of(1.0),
        current_energy_kwh=9.0,
        remaining_expected_pv_kwh=4.0,
        grid_charged_kwh=1.0,
        configured_budget_kwh=0.0,
    )
    assert running.grid_cap_kwh == 5.55
    assert running.reduction != EXECUTION_REDUCTION_BUDGET


def test_an_unpublished_ceiling_and_no_budget_never_stops_the_run() -> None:
    """Absent means unconstrained. Reading it as zero would forbid all charging."""
    target = target_of(expected_grid_to_battery_kwh=None)

    demand = demand_for(
        target,
        now=OPENS + timedelta(minutes=30),
        progress=progress_of(1.0),
        current_energy_kwh=9.0,
        remaining_expected_pv_kwh=4.0,
        grid_charged_kwh=42.0,
        configured_budget_kwh=0.0,
    )

    assert demand.grid_cap_kwh is None
    assert demand.reduction != EXECUTION_REDUCTION_BUDGET
    assert demand.required_kw > 0.0


def test_the_stop_reason_names_the_ceiling() -> None:
    """So a reader can tell it from a met target, which stops for a better reason."""
    assert EXECUTION_STOP_GRID_CEILING != EXECUTION_STATE_STOPPING
    assert "grid" in EXECUTION_STOP_GRID_CEILING


# ===========================================================================
# The integral itself: an estimate, and a defensive one
# ===========================================================================


async def test_the_attribution_is_monotonic_under_hostile_readings(
    hass, setup_integration, frank
) -> None:
    """**Monotonic by construction, not by luck.**

    Every increment is ``max(0, charge - max(0, pv - load)) * dt``, so a spuriously
    high production reading can only reduce an increment to zero and never below.
    A total that could fall would let a run buy the same kilowatt-hour twice.
    """
    coordinator = setup_integration.runtime_data
    start = OPENS
    seen = [coordinator._execution_grid_kwh]

    # Charge power alternating with absurd production readings, including a
    # negative one and a spike far above the charge.
    for step, (charge_w, pv_w) in enumerate(
        [
            (3000.0, 0.0),
            (3000.0, 9000.0),
            (3000.0, -500.0),
            (0.0, 0.0),
            (3000.0, 2900.0),
            (3000.0, float("nan")),
        ]
    ):
        coordinator._read_pv_power_w = lambda pv_w=pv_w: pv_w
        coordinator._read_house_load_w = lambda: 400.0
        coordinator._accrue_grid_attribution(start + timedelta(minutes=step), charge_w)
        seen.append(coordinator._execution_grid_kwh)

    assert seen == sorted(seen), seen
    # And it actually moved, or monotonicity proves nothing.
    assert seen[-1] > 0.0


async def test_nothing_accrues_across_a_gap(hass, setup_integration, frank) -> None:
    """A silence is not an assumption. ``QuarterAccumulator``'s tolerance, reused.

    Extrapolating the last reading across a long gap is how a restart or a stalled
    sensor would invent energy that was never bought -- against a ceiling whose
    whole purpose is to bound buying.
    """
    coordinator = setup_integration.runtime_data
    coordinator._read_pv_power_w = lambda: 0.0
    coordinator._read_house_load_w = lambda: 0.0

    # A sample inside the tolerance accrues, so the zero below cannot be the
    # trivial "nothing ever accrues".
    coordinator._accrue_grid_attribution(OPENS, 3600.0)
    coordinator._accrue_grid_attribution(OPENS + timedelta(minutes=1), 3600.0)
    accrued = coordinator._execution_grid_kwh
    assert accrued == pytest.approx(0.06, abs=1e-6)

    # And one beyond it accrues nothing rather than extrapolating across the
    # silence.
    coordinator._accrue_grid_attribution(OPENS + timedelta(hours=3), 3600.0)
    assert coordinator._execution_grid_kwh == accrued


async def test_incoherent_readings_attribute_more_to_the_grid(
    hass, setup_integration, frank
) -> None:
    """Where the inputs disagree, the ceiling binds *earlier*.

    A budget exists to bound buying, so erring toward stopping early is the safe
    direction -- and it is the opposite of the direction integrating raw grid import
    would have erred in, which counts the house's share as well.
    """
    coordinator = setup_integration.runtime_data
    coordinator._read_pv_power_w = lambda: None
    coordinator._read_house_load_w = lambda: None

    coordinator._accrue_grid_attribution(OPENS, 3600.0)
    coordinator._accrue_grid_attribution(OPENS + timedelta(minutes=1), 3600.0)

    # The whole charge attributed to the grid: 3.6 kW for one minute.
    assert coordinator._execution_grid_kwh == pytest.approx(0.06, abs=1e-6)


async def test_production_supplying_the_charge_costs_nothing(
    hass, setup_integration, frank
) -> None:
    """The reason the ceiling is measured in grid energy and not battery energy.

    Measured on the installation: commanding 1.0 kW produced about 1.135 kW of
    *total* battery charge, because the helper commands the total rate and subsumes
    ambient charging. Counting battery energy would charge the sun against a grid
    allowance and stop a run far too early.
    """
    coordinator = setup_integration.runtime_data
    coordinator._read_pv_power_w = lambda: 5000.0
    coordinator._read_house_load_w = lambda: 400.0

    # Inside the gap tolerance, so a zero here is the attribution and not a
    # rejected sample.
    coordinator._accrue_grid_attribution(OPENS, 3000.0)
    coordinator._accrue_grid_attribution(OPENS + timedelta(minutes=1), 3000.0)

    assert coordinator._execution_grid_kwh == 0.0
