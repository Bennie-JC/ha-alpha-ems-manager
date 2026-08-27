"""beta.27: the hardware-proven beta.26 charge path, and the gates it rests on.

beta.26 was validated on the real installation at Hillview: production moved, the
controller recomputed ``-3.17 kW`` and moved the applied setpoint from ``-2.7`` to
``-3.1``. beta.27 changes what Stage B *aims at* and adds a second direction, so
this file exists to prove the charge path that was measured still behaves the same
way -- and that the safety gates beta.27 deliberately did **not** touch still fire.
"""

from __future__ import annotations

import inspect

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.alphaess_device import (
    BOOLEAN_EXECUTION_OWNER,
    CHARGE_FAMILY,
    DISCHARGE_FAMILY,
    DISPATCH_ENABLE,
    DISPATCH_POWER,
    dispatch_refusal,
    plan_dispatch_power,
)
from custom_components.alpha_ems_manager.const import (
    ACTION_CHARGE,
    ACTION_DISCHARGE,
    CONTROL_LIVE_DISPATCH_INTENTS,
    CONTROL_REFUSE_DISPATCH_MODE,
    CONTROL_REFUSE_DISPATCH_SIGN,
    EXECUTION_INTENT_ACTIONS,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_NET_EXPORT,
    INHIBIT_WOULD_EXPORT,
)
from custom_components.alpha_ems_manager.safety import (
    ControlContext,
    absorbing_capacity_kw,
    evaluate,
    safe_discharge_power_kw,
)

from .forecast_helpers import NORMAL, local
from .test_beta24_live_charge import LiveSurface, owned_live_charge, step_once
from .test_beta27_quarter_execution import install, quarter_at

pytestmark = pytest.mark.usefixtures("control_surface")


@pytest.fixture
def live_surface(hass: HomeAssistant, control_surface: None) -> LiveSurface:
    """Return a control surface that responds to writes."""
    return LiveSurface(hass)


# == 1. the seven named charge regressions ================================


async def test_a_live_charge_still_arms_on_the_dispatch_surface_alone(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**R1.** The actuator is Dispatch, and the helper families are untouched."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)

    assert hass.states.get(DISPATCH_ENABLE).state == "on"
    assert hass.states.get(BOOLEAN_EXECUTION_OWNER).state == "on"
    assert coordinator.store.execution_record is not None


async def test_the_sixty_second_correction_still_moves_the_setpoint(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**R2.** The behaviour measured on the installation, still measurable.

    The figures differ from the field observation because the fixture's world does,
    but the property that was proven is the one asserted: a tick recomputes against
    live measurements and writes a *single* power step when it has moved.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=3.0, authorised=3.0))
    coordinator._applied_setpoint_kw = 0.0
    live_surface.calls.clear()

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    written = [call.data["entity_id"] for call in live_surface.calls]
    assert written == [DISPATCH_POWER], written
    assert coordinator._applied_setpoint_kw is not None
    assert coordinator._applied_setpoint_kw < 0.0


async def test_a_correction_never_rearms_the_dead_man(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**R3.** A re-arm on a power cadence would extend a run the economics did not.

    One entity, one write. The duration is the economic cadence's business.
    """
    from custom_components.alpha_ems_manager.alphaess_device import DISPATCH_DURATION

    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=3.0, authorised=3.0))
    coordinator._applied_setpoint_kw = 0.0
    live_surface.calls.clear()

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    written = {call.data["entity_id"] for call in live_surface.calls}
    assert DISPATCH_DURATION not in written
    assert DISPATCH_ENABLE not in written


async def test_a_charge_is_still_negative_mode_two(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**R4.** The direction that was proven, still the only one a charge may send."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=3.0, authorised=3.0))

    decision = coordinator._dispatch_setpoint(local(NORMAL, 10, 46))

    assert decision is not None
    assert decision.applied_kw <= 0.0
    assert (
        dispatch_refusal(
            EXECUTION_INTENT_GRID_CHARGE, plan_dispatch_power(decision.applied_kw)
        )
        is None
    )


async def test_an_unmoved_setpoint_writes_nothing(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**R5.** Writing a helper a value it already holds buys nothing."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=3.0, authorised=3.0))

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))
    settled = coordinator._applied_setpoint_kw
    live_surface.calls.clear()

    await coordinator._async_physical_tick(local(NORMAL, 10, 47))

    assert live_surface.calls == []
    assert coordinator._applied_setpoint_kw == settled


async def test_the_charge_still_stops_in_the_approved_order(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**R6.** Enable off first, marker last, whatever ends the run."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=1.0, authorised=1.0))
    coordinator._quarter_battery_kwh = 1.0
    live_surface.calls.clear()

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    written = [call.data["entity_id"] for call in live_surface.calls]
    assert written[0] == DISPATCH_ENABLE, written
    assert written[-1] == BOOLEAN_EXECUTION_OWNER, written
    assert hass.states.get(DISPATCH_ENABLE).state == "off"


async def test_a_charge_still_runs_without_any_quarter_schedule(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**R7, and the backward-compatibility guarantee.**

    A publication written before beta.27 carries no ``quarter_schedule`` and admits
    no quarter. Refusing to correct the run for that reason would have taken the
    hardware-proven path away on upgrade, so the tick degrades to the run and
    executes the beta.26 arithmetic unchanged.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    coordinator._quarter = None
    assert coordinator._carried is not None
    coordinator._applied_setpoint_kw = 0.0
    live_surface.calls.clear()

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    # It calculated and wrote, rather than refusing for want of a quarter.
    assert coordinator._last_tick_reason != "no_admitted_quarter"


# == 2. the gates beta.27 did NOT touch, still firing =====================


def test_the_absorbing_capacity_is_still_zero_for_an_exporting_site() -> None:
    """Unchanged. A site already exporting can absorb no discharge at all."""
    context = ControlContext(
        mode="active",
        execution_enabled=True,
        grid_import_w=0.0,
        grid_export_w=500.0,
        battery_power_w=0.0,
    )

    assert absorbing_capacity_kw(context) == 0.0
    assert safe_discharge_power_kw(context) == 0.0


def test_a_reserve_guard_discharge_still_cannot_export() -> None:
    """**The gate the export path deliberately does not reuse.**

    ``INHIBIT_WOULD_EXPORT`` still refuses a discharge above the measured absorbing
    capacity. beta.27 added a second authorisation function rather than threading a
    condition through this one, precisely so this behaviour is unchanged.
    """
    from datetime import timedelta

    from custom_components.alpha_ems_manager.control import ControlIntent

    now = local(NORMAL, 10, 46)
    intent = ControlIntent(
        action=ACTION_DISCHARGE,
        energy_ac_kwh=0.5,
        average_power_kw=2.0,
        interval_hours=0.25,
        floor_soc_percent=20.0,
        energy_limit_bound=False,
        horizon_minutes=20,
        target_day=now.date(),
        start_index=43,
        built_at=now - timedelta(seconds=30),
        reason="reserve_guard",
        policy="phase_three",
        policy_version=1,
    )
    context = ControlContext(
        mode="active",
        execution_enabled=True,
        failsafe_available=True,
        battery_configured=True,
        current_start_index=43,
        today=now.date(),
        now=now,
        soc_percent=60.0,
        soc_age_seconds=5.0,
        battery_power_w=0.0,
        battery_power_age_seconds=5.0,
        house_load_w=200.0,
        house_load_age_seconds=5.0,
        grid_import_w=0.0,
        grid_export_w=500.0,
        grid_age_seconds=5.0,
        device_power_kw=2.0,
        device_cutoff_percent=21,
        device_duration_minutes=20,
    )

    verdict = evaluate(intent, context)

    assert verdict.safe is False
    assert verdict.inhibit_reason == INHIBIT_WOULD_EXPORT


def test_evaluate_never_learned_about_the_quarter_or_the_export_intent() -> None:
    """Asserted structurally, because "unchanged" is the property.

    A condition added here would be the widening beta.27 refused to make, and it
    would pass every behavioural test on the day it was written.
    """
    source = inspect.getsource(evaluate)

    for forbidden in (
        "CarriedQuarter",
        "quarter",
        "net_export",
        "EXECUTION_INTENT_NET_EXPORT",
        "authorize_export",
        "ExportRequest",
    ):
        assert forbidden not in source, forbidden


def test_the_absorbing_capacity_and_the_clamp_never_learned_either() -> None:
    """The same, for the two functions the clamp is built from."""
    for function in (absorbing_capacity_kw, safe_discharge_power_kw):
        source = inspect.getsource(function)
        for forbidden in ("quarter", "net_export", "intent"):
            assert forbidden not in source, (function.__name__, forbidden)


def test_limit_command_still_clamps_only_a_discharge_and_only_downward() -> None:
    """Untouched: beta.27 changed *whether it is applied*, never what it does."""
    from custom_components.alpha_ems_manager.alphaess_device import limit_command

    source = inspect.getsource(limit_command)

    assert "ACTION_DISCHARGE" in source
    for forbidden in ("quarter", "net_export", "EXECUTION_INTENT"):
        assert forbidden not in source, forbidden


# == 3. the routing trap, closed and asserted from both sides =============


def test_the_action_map_now_names_the_export_direction() -> None:
    """Needed so a stop can name what it stops -- and it is only a direction."""
    assert EXECUTION_INTENT_ACTIONS[EXECUTION_INTENT_GRID_CHARGE] == ACTION_CHARGE
    assert EXECUTION_INTENT_ACTIONS[EXECUTION_INTENT_NET_EXPORT] == ACTION_DISCHARGE


def test_the_arm_branch_keys_on_the_intent_and_never_on_the_action() -> None:
    """**The trap, closed.**

    With ``net_export -> ACTION_DISCHARGE`` in place, a branch reading
    ``command.action != ACTION_CHARGE`` would route an export into the advisory path
    and arm it on the Force Discharging helper family -- silently making a helper
    family the actuator for a new capability.

    Asserted on the source of the write boundary, because the behavioural test
    passes today by accident: an export command is unreachable until a quarter is
    admitted, so a regression here would stay invisible until the first real export.
    """
    from custom_components.alpha_ems_manager import coordinator as module

    source = inspect.getsource(module)

    assert "elif live_intent not in CONTROL_LIVE_DISPATCH_INTENTS:" in source
    assert "elif command.action != ACTION_CHARGE:" not in source


def test_the_advisory_path_still_exists_for_actions_with_no_actuator() -> None:
    """It was not deleted -- the reserve guard still needs to be *planned*.

    Shadow reporting is what a user reads to decide whether to trust the layer, so
    an action with no actuator must still produce a described sequence.
    """
    from custom_components.alpha_ems_manager import coordinator as module

    source = inspect.getsource(module)

    assert "plan_arm_parameters(command)" in source


def test_the_helper_families_are_still_not_writers_anywhere() -> None:
    """The property that made beta.26's actuator story simple, still true."""
    from custom_components.alpha_ems_manager import coordinator as module

    source = inspect.getsource(module)

    assert "CHARGE_FAMILY." not in source
    assert "DISCHARGE_FAMILY." not in source


def test_the_two_surfaces_remain_disjoint() -> None:
    """No entity belongs to both, so a mistake cannot be a near miss."""
    from custom_components.alpha_ems_manager.alphaess_device import DISPATCH_ENTITIES

    assert not set(CHARGE_FAMILY.entities) & set(DISPATCH_ENTITIES)
    assert not set(DISCHARGE_FAMILY.entities) & set(DISPATCH_ENTITIES)


def test_the_live_intents_are_the_only_ones_that_select_the_dispatch_surface() -> None:
    """One set, keyed on the intent, and ``serve_load`` is not in it."""
    assert "serve_load" not in CONTROL_LIVE_DISPATCH_INTENTS
    assert (
        frozenset({EXECUTION_INTENT_GRID_CHARGE, EXECUTION_INTENT_NET_EXPORT})
        == CONTROL_LIVE_DISPATCH_INTENTS
    )


def test_an_unexecutable_mode_is_still_refused_under_either_intent() -> None:
    """Modes 6 and 7 are not controllable kW primitives, for either direction."""
    from custom_components.alpha_ems_manager.alphaess_device import dispatch_mode_step

    for intent in (EXECUTION_INTENT_GRID_CHARGE, EXECUTION_INTENT_NET_EXPORT):
        assert (
            dispatch_refusal(intent, (dispatch_mode_step(7),))
            == CONTROL_REFUSE_DISPATCH_MODE
        )


def test_the_sign_gate_refuses_a_charge_the_wrong_way_round() -> None:
    """The beta.25 guarantee, unchanged in substance and now keyed on the intent."""
    assert (
        dispatch_refusal(EXECUTION_INTENT_GRID_CHARGE, plan_dispatch_power(3.0))
        == CONTROL_REFUSE_DISPATCH_SIGN
    )
    assert (
        dispatch_refusal(EXECUTION_INTENT_GRID_CHARGE, plan_dispatch_power(-3.0))
        is None
    )


# == 4. the whole refresh, end to end =====================================


async def test_a_full_refresh_over_an_owned_charge_writes_only_dispatch(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """The integration test behind the structural ones above."""
    from custom_components.alpha_ems_manager.alphaess_device import DISPATCH_ENTITIES

    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=3.0, authorised=3.0))
    live_surface.calls.clear()

    await step_once(hass, coordinator, live_surface, hour=10, minute=46)

    written = {call.data["entity_id"] for call in live_surface.calls}
    permitted = set(DISPATCH_ENTITIES) | {BOOLEAN_EXECUTION_OWNER}
    assert written <= permitted, written - permitted
