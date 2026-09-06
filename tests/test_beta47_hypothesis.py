"""beta.47: the root-cause hypothesis, as executable claims.

The 2026-09-06 evening capture said a dispatch takes ~40 s from ownership claim to
the vendor register showing active, and ~80 s to controller-observed delivery. The
investigation attributed 84-86 % of the first figure to our own Stage A solve, on the
strength of ``solve_ms`` tracking ``activation_latency_s`` across three export arms.

That is an architectural claim about *ordering*, and an architectural claim belongs in
a test rather than in a comment. These pin the ordering the beta.47 design rests on,
and the two constraints a later boundary-aligned arm would have to satisfy.

They are deliberately independent of beta.47's own instrumentation: they would have
passed on beta.46, and they must keep passing afterwards.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.alphaess_device import (
    DISPATCH_ENABLE,
    DISPATCH_MODE_SOC_CONTROL,
    SENSOR_DISPATCH_START,
    build_command,
    plan_dispatch_arm,
    plan_marker_claim,
)
from custom_components.alpha_ems_manager.const import (
    ACTION_DISCHARGE,
    CONTROL_HORIZON_MINUTES,
    CONTROL_MODE_ACTIVE,
    EXECUTION_INTENT_NET_EXPORT,
    INHIBIT_STALE_PLAN_INTERVAL,
    TICK_SKIPPED_DISPATCH_INACTIVE,
)
from custom_components.alpha_ems_manager.dispatch import deadman_minutes
from custom_components.alpha_ems_manager.execution import (
    CarriedQuarter,
    quarter_intent_for,
)
from custom_components.alpha_ems_manager.safety import ControlContext, evaluate

from .forecast_helpers import NORMAL, local
from .test_beta24_live_charge import LiveSurface, owned_live_charge, step_once

pytestmark = pytest.mark.usefixtures("control_surface")

#: The export row the 19:45 arm executed, as published.
EXPORT_KWH = 2.26
EXPORT_KW = 9.04


@pytest.fixture
def live_surface(hass: HomeAssistant, control_surface: None) -> LiveSurface:
    """Return a control surface that responds to writes."""
    return LiveSurface(hass)


def _written(live_surface: LiveSurface) -> list[str]:
    """Return the entity ids written so far, in order."""
    return [call.data["entity_id"] for call in live_surface.calls]


def _export_quarter() -> CarriedQuarter:
    """Return the 19:45 export row, frozen as admission froze it."""
    opens = local(NORMAL, 19, 45)
    return CarriedQuarter(
        quarter_start=opens,
        quarter_end=opens + timedelta(minutes=15),
        intent=EXECUTION_INTENT_NET_EXPORT,
        battery_target_kwh=EXPORT_KWH,
        grid_authorised_kwh=0.0,
        grid_export_target_kwh=EXPORT_KWH,
        initial_desired_grid_kw=EXPORT_KW,
        run_id="run-1",
        plan_id="plan-1",
        revision=1,
        admitted_at=opens - timedelta(minutes=15),
    )


def _intent_at(start_index: int):
    """Return the export intent an admitted row builds, at ``start_index``."""
    opens = local(NORMAL, 19, 45)
    return quarter_intent_for(
        _export_quarter(),
        battery_power_kw=EXPORT_KW,
        floor_soc_percent=10.0,
        ceiling_soc_percent=100.0,
        horizon_minutes=CONTROL_HORIZON_MINUTES,
        target_day=opens.date(),
        start_index=start_index,
        built_at=opens,
    )


# =====================================================================
# A -- the ordering the whole diagnosis rests on
# =====================================================================


async def test_no_activation_write_can_occur_while_the_solve_is_running(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**The root cause, as an ordering claim.**

    The write boundary is the sixteenth of seventeen statements in
    ``_async_update_data``; the solve is the ninth. So a run that should open at the
    boundary cannot reach the inverter until the solve has returned -- which on the
    reference hardware is 32-35 s later.

    Asserted by observing the world *from inside the solve*: at that instant this
    refresh has written no activation. That is the whole of beta.47's premise, and it
    is what a later boundary-aligned arm would exist to change.

    *Mutation: hoist the write boundary above the solve and this fails.*
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    seen: dict[str, list[str]] = {}
    kind = type(coordinator)
    original = kind._async_economic_outcome_safely

    async def watching(self, *args, **kwargs):
        seen.setdefault("at_solve", _written(live_surface))
        return await original(self, *args, **kwargs)

    kind._async_economic_outcome_safely = watching
    try:
        live_surface.calls.clear()
        await step_once(hass, coordinator, live_surface, hour=10, minute=46)
    finally:
        kind._async_economic_outcome_safely = original

    assert "at_solve" in seen, "the solve never ran, so nothing was proved"
    assert DISPATCH_ENABLE not in seen["at_solve"], seen["at_solve"]


async def test_the_physical_tick_cannot_arm_an_inactive_dispatch(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**The sixty-second tick is not a second way in.**

    It returns on ``not snapshot.dispatch_active``, which is the pre-arm condition by
    definition, so the cadence that runs every minute can never shorten the wait for
    the cadence that runs every fifteen. Asserted on the wire, not on a state name.

    *Mutation: drop the ``dispatch_active`` guard and a tick writes a claim.*
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    # The register *and* the enable, because ``read_snapshot`` is pessimistic: a
    # dispatch counts as running if either says so.
    hass.states.async_set(DISPATCH_ENABLE, "off")
    hass.states.async_set(SENSOR_DISPATCH_START, "0")
    await hass.async_block_till_done()
    live_surface.calls.clear()

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    assert live_surface.calls == [], _written(live_surface)
    assert coordinator._last_tick_reason == TICK_SKIPPED_DISPATCH_INACTIVE


async def test_a_second_refresh_over_a_running_dispatch_never_rearms(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**Interleaving cannot mint a second arm.**

    A user changing control mode mid-solve is the one path that can enter
    ``_async_update_data`` a second time, so the property that matters is that a
    refresh reaching an already-owned dispatch sustains: same ``claim_id``, no new
    marker claim. A second arm would mint a new claim and reset the row's progress
    accounting, which is why this is asserted on the claim and not on the steps.

    *Mutation: drop ``owned`` from the sustain condition and the arm branch re-arms.*
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    before = (coordinator.store.execution_record or {}).get("claim_id")
    assert before is not None

    for minute in (46, 47):
        live_surface.calls.clear()
        await step_once(hass, coordinator, live_surface, hour=10, minute=minute)
        after = (coordinator.store.execution_record or {}).get("claim_id")
        assert after == before, (minute, after, before)


# =====================================================================
# B -- groundwork for a later boundary-aligned arm: both halves
# =====================================================================


def test_an_admitted_row_can_build_an_arm_without_a_solve() -> None:
    """**The feasible half.**

    A boundary-aligned arm would build its command from the frozen row rather than
    from the refresh in flight. The cutoff depends only on the reserve floor and the
    pack ceiling -- configuration and a device limit, neither of them a solve output
    -- so a complete seven-step sequence is reachable from an admitted row alone.

    The infeasible half is the next test. Recorded together so a future release
    cannot quietly assume this one without satisfying that one.
    """
    intent = _intent_at(79)
    assert intent is not None
    assert intent.action == ACTION_DISCHARGE

    command = build_command(intent)
    assert command is not None
    assert command.cutoff_soc_percent is not None

    steps = plan_marker_claim() + plan_dispatch_arm(
        mode=DISPATCH_MODE_SOC_CONTROL,
        power_kw=-EXPORT_KW,
        cutoff_soc_percent=command.cutoff_soc_percent,
        duration_minutes=deadman_minutes(None),
        pv_enabled=True,
    )

    assert len(steps) == 7, [step.entity_id for step in steps]
    assert steps[-1].entity_id == DISPATCH_ENABLE


def test_an_inherited_start_index_is_refused_by_the_safety_gate() -> None:
    """**The infeasible half, pinned before anyone builds against it.**

    An intent must be *for the interval it acts in*. A boundary arm that reused the
    previous refresh's ``start_index`` -- the obvious implementation, since that is
    what ``_stage_b_intent`` reads off the ``BatteryPlan`` -- is refused, because at
    the boundary ``current_start_index`` has already advanced by one.

    So the frozen row is enough to *build* an arm but not to *authorise* one without
    recomputing the index. A later release starts from this constraint.
    """
    opens = local(NORMAL, 19, 45)
    context = ControlContext(
        mode=CONTROL_MODE_ACTIVE,
        execution_enabled=True,
        failsafe_available=True,
        battery_configured=True,
        now=opens,
        today=opens.date(),
        current_start_index=79,
    )

    fresh = evaluate(_intent_at(79), context)
    stale = evaluate(_intent_at(78), context)

    # The gate is reached at all only because the fresh index gets past it.
    assert fresh.inhibit_reason != INHIBIT_STALE_PLAN_INTERVAL
    assert stale.safe is False
    assert stale.inhibit_reason == INHIBIT_STALE_PLAN_INTERVAL


async def test_the_dispatch_register_is_actually_subscribed_and_stays_read_only(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**beta.47 end to end: the observer is wired up, and it only observes.**

    Two claims in one, because they fail in opposite directions and both matter.

    *Wired up*: without the subscription the release silently does nothing -- the
    arm would still be measured, just up to sixty seconds late, which is the defect
    beta.47 exists to remove and would be invisible in every unit test.

    *Read-only*: a callback that reaches the wire is a second control path. It takes
    no lock, calls no service, and writes nothing, so a register change must move the
    measurement and nothing else.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    # One ordinary tick to open the arm, which is the pre-beta.47 path.
    await coordinator._async_physical_tick(local(NORMAL, 10, 46))
    assert coordinator._arm_open is not None, "no arm to observe"

    seen: list[bool] = []
    original = coordinator._observe_arm

    def watching(snapshot, now):
        seen.append(True)
        return original(snapshot, now)

    coordinator._observe_arm = watching
    live_surface.calls.clear()

    # A register transition, with no tick anywhere near it.
    hass.states.async_set(SENSOR_DISPATCH_START, "1")
    await hass.async_block_till_done()

    assert seen, (
        "the register moved and nothing observed it: the subscription is missing"
    )
    assert live_surface.calls == [], _written(live_surface)
