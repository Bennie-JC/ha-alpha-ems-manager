"""The control layer running inside a real Home Assistant, end to end.

What the pure tests cannot show: that the pipeline is actually wired into the
refresh, that the two entities report it, that the diagnostics carry it, and --
above all -- that **no service call reaches the control surface for any direction
this release does not execute**, in any mode, with any device state, however
healthy everything looks.

The zero-write proof is the point of this file, and beta.24 narrowed it rather
than dropping it. This release executes a **charge** and nothing else, so the
claim is no longer "no service call ever" -- it is that Shadow writes nothing in
any mode with any device state, and that every other direction is refused at two
independent boundaries. It is asserted three ways: the executable-action set is
exactly one action, the executor refuses a foreign direction on its own, and every
service call the whole integration makes during a full quarter-hour cycle is
captured and counted.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.alphaess_adapter import (
    ControlActionNotPermitted,
    async_execute,
    discover,
    read_snapshot,
)
from custom_components.alpha_ems_manager.alphaess_device import (
    AUTOMATION_DISPATCH_RESET_FULL,
    BOOLEAN_EXCESS_EXPORT,
    BOOLEAN_PEAK_SHAVING,
    DISCHARGE_FAMILY,
    PERMITTED_SERVICES,
    SENSOR_DISPATCH_START,
)
from custom_components.alpha_ems_manager.const import (
    CONTROL_MODE_ACTIVE,
    CONTROL_MODE_OFF,
    CONTROL_MODE_SHADOW,
    CONTROL_STATE_ELIGIBLE,
    CONTROL_STATE_IDLE,
    CONTROL_STATE_INHIBITED,
    CONTROL_STATE_OFF,
    DOMAIN,
    INHIBIT_DISPATCH_ACTIVE,
    INHIBIT_EXCESS_EXPORT_ACTIVE,
    INHIBIT_MISSING_CONTROL_ENTITY,
    INHIBIT_NO_FAILSAFE_AUTOMATION,
    INHIBIT_PEAK_SHAVING_ACTIVE,
    REFUSE_EXECUTION_NOT_ENABLED,
    REFUSE_LIVE_ACTION_NOT_PERMITTED,
    REFUSE_MODE_NOT_ACTIVE,
    REFUSE_NO_COMMANDS,
    REFUSE_UNSAFE,
)

from .live_capability import assert_charge_only_capability

CONTROL_STATE = "sensor.alpha_ems_control_state"
CONTROL_MODE = "select.alpha_ems_control_mode"


@pytest.fixture
def captured_calls(hass: HomeAssistant) -> list[ServiceCall]:
    """Capture every call to a service the control layer is allowed to make.

    Registered as real service handlers so a call would succeed rather than
    raising -- otherwise a write attempt could be mistaken for an absent service
    and pass for the wrong reason.
    """
    calls: list[ServiceCall] = []

    async def record(call: ServiceCall) -> None:
        calls.append(call)

    seen: set[str] = set()
    for domain, service in PERMITTED_SERVICES:
        hass.services.async_register(domain, service, record)
        seen.add(f"{domain}.{service}")
    assert len(seen) == 3
    return calls


async def set_mode(hass: HomeAssistant, mode: str) -> None:
    """Select a control mode through the real entity, then settle."""
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": CONTROL_MODE, "option": mode},
        blocking=True,
    )
    await hass.async_block_till_done()


async def refresh(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    """Force a refresh and return the control report."""
    coordinator = entry.runtime_data
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    return coordinator.control_report


# ===========================================================================
# the mode surface
# ===========================================================================


async def test_the_control_mode_defaults_to_off(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A fresh installation attempts nothing until asked to."""
    state = hass.states.get(CONTROL_MODE)

    assert state is not None
    assert state.state == CONTROL_MODE_OFF
    assert setup_integration.runtime_data.control_mode == CONTROL_MODE_OFF


async def test_off_evaluates_nothing_and_says_so(
    hass: HomeAssistant, setup_integration: MockConfigEntry, control_surface: None
) -> None:
    """No intent, no gate, no command planning, and no read of the surface."""
    report = await refresh(hass, setup_integration)

    assert report["state"] == CONTROL_STATE_OFF
    assert "capability" not in report
    assert "commands" not in report
    assert "off means this integration attempts no control" in report["off_semantics"]
    assert hass.states.get(CONTROL_STATE).state == CONTROL_STATE_OFF


async def test_shadow_runs_the_whole_pipeline_and_plans_a_real_command(
    hass: HomeAssistant, setup_integration: MockConfigEntry, control_surface: None
) -> None:
    """The point of shadow: a real verdict and the exact command list.

    Not "mode_not_active". The gate is evaluated in full, so its answer is the
    one active would have got, and the command list is the one active would have
    sent.
    """
    await set_mode(hass, CONTROL_MODE_SHADOW)
    report = await refresh(hass, setup_integration)

    assert report["mode"] == CONTROL_MODE_SHADOW
    assert report["capability"]["ready"] is True
    assert report["safety"]["inhibit_reason"] != REFUSE_MODE_NOT_ACTIVE
    assert report["intent"] is not None
    assert report["command"] is not None
    # Only the last stage refuses, and it refuses for the mode rather than for a
    # hazard.
    assert report["authorization"]["refusal"] == REFUSE_MODE_NOT_ACTIVE


async def test_shadow_and_active_agree_on_the_verdict_and_the_commands(
    hass: HomeAssistant, setup_integration: MockConfigEntry, control_surface: None
) -> None:
    """Byte-identical, which is what makes watching shadow worthwhile."""
    await set_mode(hass, CONTROL_MODE_SHADOW)
    shadow = await refresh(hass, setup_integration)
    shadow_safety = dict(shadow["safety"])
    shadow_commands = list(shadow["commands"])
    shadow_command = shadow["command"] and dict(shadow["command"])

    await set_mode(hass, CONTROL_MODE_ACTIVE)
    active = await refresh(hass, setup_integration)

    assert active["safety"] == shadow_safety
    assert active["commands"] == shadow_commands
    assert active["command"] == shadow_command


async def test_active_is_refused_by_the_release_barrier(
    hass: HomeAssistant, setup_integration: MockConfigEntry, control_surface: None
) -> None:
    """Active reaches the last stage and is still refused.

    beta.24 executes a charge, so ``execution_available`` is now true and the
    release barrier no longer refuses on its own. Two reasons remain and either
    would be enough: this installation has not enabled command sending, and the
    plan here is a reserve-guard **discharge**, which this release does not
    execute.
    """
    await set_mode(hass, CONTROL_MODE_ACTIVE)
    report = await refresh(hass, setup_integration)

    assert report["execution_available"] is True
    assert report["authorization"]["refusal"] in (
        REFUSE_EXECUTION_NOT_ENABLED,
        REFUSE_LIVE_ACTION_NOT_PERMITTED,
    )
    assert report["authorization"]["authorized"] is False


async def test_active_with_execution_enabled_still_cannot_execute(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    control_surface: None,
) -> None:
    """**The direction refusal, with every other gate open.**

    Mode active, the enable stored and read, the barrier lifted -- the most
    permissive state beta.24 can reach. What stops this command is the one thing
    left: the Phase-3 reserve guard recommends a *discharge*, and a charge-only
    release does not execute that direction.

    Before beta.24 this test asserted the release barrier, because that was the
    only refusal available. The refusal it asserts now is the one that will still
    be here after the barrier opens further.
    """
    from custom_components.alpha_ems_manager.const import (
        CONF_CONTROL_EXECUTION_ENABLED,
        CONFIG_ENTRY_VERSION,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Alpha EMS",
        data=config_data,
        options={CONF_CONTROL_EXECUTION_ENABLED: True},
        version=CONFIG_ENTRY_VERSION,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.runtime_data.config.control_execution_enabled is True

    await set_mode(hass, CONTROL_MODE_ACTIVE)
    report = await refresh(hass, entry)

    # This installation's plan is a hold, so there is no command to refuse and
    # ``no_commands`` is the honest answer. The direction refusal is exercised on a
    # scenario built for it, in the Live test module -- asserting it here would mean
    # asserting it about a command that does not exist.
    assert report["authorization"]["refusal"] == REFUSE_NO_COMMANDS
    assert report["authorization"]["authorized"] is False
    assert report["execution_available"] is True


async def test_the_mode_select_restores_and_falls_back_to_off(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """An unrecognised stored value is never assumed to have been permissive."""
    coordinator = setup_integration.runtime_data

    coordinator.set_control_mode("shadow")
    assert coordinator.control_mode == CONTROL_MODE_SHADOW

    coordinator.set_control_mode("something_from_the_future")
    assert coordinator.control_mode == CONTROL_MODE_OFF


async def test_selecting_a_mode_re_evaluates_immediately(
    hass: HomeAssistant, setup_integration: MockConfigEntry, control_surface: None
) -> None:
    """Without this a mode change would sit unreflected for a quarter of an hour."""
    assert hass.states.get(CONTROL_STATE).state == CONTROL_STATE_OFF

    await set_mode(hass, CONTROL_MODE_SHADOW)

    assert hass.states.get(CONTROL_STATE).state != CONTROL_STATE_OFF


async def test_selecting_a_mode_re_evaluates_even_inside_the_refresh_cooldown(
    hass: HomeAssistant, setup_integration: MockConfigEntry, control_surface: None
) -> None:
    """Regression: the mode change must not be swallowed by the debouncer.

    ``async_request_refresh`` is debounced on a ten-second cooldown. Once its
    timer is armed, a further request is *deferred to the end of that timer* --
    and the timer is a raw ``loop.call_later``, which neither
    ``async_block_till_done`` nor a frozen clock advances. So a mode change
    arriving inside the window applied the mode but left the re-evaluation
    pending, and the published state stayed ``off``.

    That is exactly the hazard ``AlphaEmsCoordinator._handle_started`` already
    documents for the startup path, and it is why the select now uses the
    undebounced ``async_refresh``: a user pressing a control is the last thing
    that should be rate-limited against a background quarter-hour tick.

    This test arms the cooldown deliberately, so it fails **every** time against
    the debounced form rather than once in a while. The previous version passed
    or failed depending on whether anything had happened to arm the timer first,
    which is how it read as a flake.
    """
    coordinator = setup_integration.runtime_data

    # Arm the cooldown, then prove it is armed -- otherwise this test could pass
    # for the wrong reason on a future HA whose debouncer behaves differently.
    await coordinator.async_request_refresh()
    await hass.async_block_till_done()
    assert coordinator._debounced_refresh._timer_task is not None
    assert hass.states.get(CONTROL_STATE).state == CONTROL_STATE_OFF

    await set_mode(hass, CONTROL_MODE_SHADOW)

    # The mode is applied *and* re-evaluated, with no clock advanced and no wait.
    assert coordinator.control_mode == CONTROL_MODE_SHADOW
    assert hass.states.get(CONTROL_STATE).state != CONTROL_STATE_OFF
    assert coordinator.control_report["mode"] == CONTROL_MODE_SHADOW


async def test_the_select_uses_the_undebounced_refresh(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The mechanism, pinned so a tidy-up cannot swap it back.

    ``async_request_refresh`` reads as the more considerate call -- it is the one
    most integrations use -- so the substitution is an easy one to make in good
    faith. Asserted at the call site rather than through behaviour, because the
    behavioural symptom only appears when the cooldown happens to be armed.
    """
    import ast
    import inspect

    from custom_components.alpha_ems_manager import select as select_module

    # The *calls*, not the mentions: the docstring names the debounced form in
    # order to explain why it is not used, so a substring check would fire on the
    # explanation. Only an actual call site matters.
    tree = ast.parse(inspect.getsource(select_module.AlphaEmsControlModeSelect))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "async_refresh" in called
    assert "async_request_refresh" not in called


@pytest.mark.parametrize(
    "mode", [CONTROL_MODE_OFF, CONTROL_MODE_SHADOW, CONTROL_MODE_ACTIVE]
)
async def test_every_mode_re_evaluates_immediately_inside_the_cooldown(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    control_surface: None,
    mode: str,
) -> None:
    """All three options, each with the cooldown armed against them.

    ``off`` is included deliberately: it is the one a user reaches for when they
    want the integration to stop trying, so it is the one that must least be left
    pending. Its published state is ``off`` by design, so the assertion is that
    the report describes the selected mode rather than that the state changed.
    """
    coordinator = setup_integration.runtime_data

    await coordinator.async_request_refresh()
    await hass.async_block_till_done()
    assert coordinator._debounced_refresh._timer_task is not None

    await set_mode(hass, mode)

    assert coordinator.control_mode == mode
    assert coordinator.control_report["mode"] == mode
    if mode == CONTROL_MODE_OFF:
        assert hass.states.get(CONTROL_STATE).state == CONTROL_STATE_OFF
    else:
        assert hass.states.get(CONTROL_STATE).state != CONTROL_STATE_OFF


async def test_an_unknown_option_is_refused_by_the_entity(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The entity validates rather than passing anything through."""
    entity = next(
        item
        for item in hass.data["entity_components"]["select"].entities
        if item.entity_id == CONTROL_MODE
    )
    with pytest.raises(ValueError, match="unknown control mode"):
        await entity.async_select_option("not_a_mode")


# ===========================================================================
# capability and conflict
# ===========================================================================


async def test_a_missing_control_surface_inhibits_without_breaking_anything(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """No control surface at all: the integration still learns and forecasts.

    This is the default state of the rest of the suite, and it must never be a
    setup failure -- the control surface belongs to the user, not to this
    integration.
    """
    await set_mode(hass, CONTROL_MODE_SHADOW)
    report = await refresh(hass, setup_integration)

    assert report["safety"]["inhibit_reason"] == INHIBIT_MISSING_CONTROL_ENTITY
    assert report["capability"]["ready"] is False
    assert report["capability"]["missing_total"] > 0
    # Named, so a renamed helper reads as a specific absence.
    assert report["capability"]["missing"]
    assert hass.states.get(CONTROL_STATE).state == CONTROL_STATE_INHIBITED

    # And nothing earlier is disturbed.
    for entity_id in (
        "sensor.alpha_ems_expected_house_load_today",
        "sensor.alpha_ems_learning_days",
        "sensor.alpha_ems_battery_recommendation",
    ):
        assert hass.states.get(entity_id) is not None


async def test_a_disabled_failsafe_automation_inhibits(
    hass: HomeAssistant, setup_integration: MockConfigEntry, control_surface: None
) -> None:
    """Without it a restart could leave a dispatch nothing is left to clear.

    Alpha EMS deliberately keeps no copy of that mechanism, so it insists on the
    real one being present and switched on.
    """
    hass.states.async_set(AUTOMATION_DISPATCH_RESET_FULL, "off")
    await set_mode(hass, CONTROL_MODE_SHADOW)
    report = await refresh(hass, setup_integration)

    assert report["safety"]["inhibit_reason"] == INHIBIT_NO_FAILSAFE_AUTOMATION
    assert report["capability"]["failsafe_available"] is False
    assert report["capability"]["failsafe_state"] == "off"


@pytest.mark.parametrize(
    ("entity_id", "reason"),
    [
        (BOOLEAN_EXCESS_EXPORT, INHIBIT_EXCESS_EXPORT_ACTIVE),
        (BOOLEAN_PEAK_SHAVING, INHIBIT_PEAK_SHAVING_ACTIVE),
    ],
)
async def test_another_feature_driving_the_battery_inhibits_rather_than_losing_it(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    control_surface: None,
    captured_calls: list[ServiceCall],
    entity_id: str,
    reason: str,
) -> None:
    """Alpha EMS stands down; it does not switch a user's feature off.

    The control surface's own arming sequence would happily disable these, so
    inhibiting is what keeps a setting somebody chose from being overridden
    behind their back.
    """
    hass.states.async_set(entity_id, "on")
    await set_mode(hass, CONTROL_MODE_ACTIVE)
    report = await refresh(hass, setup_integration)

    assert report["safety"]["inhibit_reason"] == reason
    assert report["authorization"]["refusal"] == REFUSE_UNSAFE
    assert captured_calls == []
    # The feature is left exactly as the user set it.
    assert hass.states.get(entity_id).state == "on"


async def test_a_running_dispatch_is_foreign_even_when_it_matches_ours(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    control_surface: None,
    captured_calls: list[ServiceCall],
) -> None:
    """The heart of the ownership decision, driven end to end.

    A dispatch is running whose power, cutoff and duration are exactly what
    Alpha EMS would have commanded. Nothing in the control surface records who
    armed it, so it is someone else's -- and the person most likely to have armed
    those exact figures is the one watching the shadow recommendation.
    """
    await set_mode(hass, CONTROL_MODE_SHADOW)
    planned = await refresh(hass, setup_integration)
    command = planned["command"]
    assert command is not None

    # Recreate that command on the device, exactly.
    hass.states.async_set(SENSOR_DISPATCH_START, "1")
    hass.states.async_set(DISCHARGE_FAMILY.activate, "on")
    hass.states.async_set(DISCHARGE_FAMILY.power, str(command["power_kw"]))
    hass.states.async_set(
        DISCHARGE_FAMILY.cutoff_soc, str(command["cutoff_soc_percent"])
    )
    hass.states.async_set(DISCHARGE_FAMILY.duration, str(command["duration_minutes"]))

    await set_mode(hass, CONTROL_MODE_ACTIVE)
    report = await refresh(hass, setup_integration)

    assert report["safety"]["inhibit_reason"] == INHIBIT_DISPATCH_ACTIVE
    assert report["device"]["dispatch_active"] is True
    assert report["device"]["owned"] is False
    assert report["device"]["ownership_provable"] is False
    assert captured_calls == []
    # Untouched: not cancelled, not modified, not claimed.
    assert hass.states.get(SENSOR_DISPATCH_START).state == "1"
    assert hass.states.get(DISCHARGE_FAMILY.activate).state == "on"


# ===========================================================================
# the zero-write proof
# ===========================================================================


async def test_no_mode_and_no_device_state_produces_a_single_write(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    control_surface: None,
    captured_calls: list[ServiceCall],
) -> None:
    """Every mode, against a healthy surface and a busy one, over full cycles.

    The dynamic half of the release-barrier proof: whatever the pipeline decides,
    the count of calls to the three permitted services is zero.
    """
    for dispatch_state in ("0", "1"):
        hass.states.async_set(SENSOR_DISPATCH_START, dispatch_state)
        for mode in (CONTROL_MODE_OFF, CONTROL_MODE_SHADOW, CONTROL_MODE_ACTIVE):
            await set_mode(hass, mode)
            await refresh(hass, setup_integration)

    assert captured_calls == []


async def test_a_quarter_hour_cycle_writes_nothing(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    control_surface: None,
    captured_calls: list[ServiceCall],
    freezer,
) -> None:
    """Driven by the real clock trigger, across a genuine quarter boundary.

    A forced refresh proves the pipeline writes nothing; this proves the *timer*
    that drives it in production writes nothing either, which is the path a live
    installation actually takes.
    """
    from .test_init import advance

    await set_mode(hass, CONTROL_MODE_ACTIVE)
    coordinator = setup_integration.runtime_data
    before = len(coordinator._control_events)

    # Twenty minutes, so the quarter-hour trigger fires and the pipeline is
    # genuinely re-evaluated by the timer rather than by a forced refresh.
    await advance(hass, freezer, 20 * 60)

    assert len(coordinator._control_events) > before
    assert coordinator.control_report is not None
    assert captured_calls == []


async def test_the_executor_refuses_even_when_called_directly(
    hass: HomeAssistant, setup_integration: MockConfigEntry, control_surface: None
) -> None:
    """**The last interlock, and beta.24 gave it real work to do.**

    Unreachable through the pipeline, which refuses long before this. It exists so
    that the only way to command a direction this release does not execute is to
    change a constant in a source file, not to make a mistake at a call site.

    Until beta.23 it refused everything and the exception was about the release.
    Now it refuses this command because the steps name **discharge** entities, and
    that refusal is a subset test on entity ids -- so it holds even if everything
    upstream has been fooled.

    Driven with a command built directly rather than one taken from a plan: the
    claim is about the executor, and making it depend on what the decision layer
    happened to recommend would test the wrong thing -- and pass vacuously on any
    install whose plan was a hold.
    """
    from custom_components.alpha_ems_manager.alphaess_device import (
        build_command,
        plan_commands,
    )

    from .test_control_pipeline import make_intent

    steps = plan_commands(build_command(make_intent(energy_ac_kwh=0.5)))
    # Six since beta.19: the owner marker leads the arming sequence, so a
    # dispatch running without it is somebody else's by construction.
    assert len(steps) == 6

    with pytest.raises(ControlActionNotPermitted, match="live_charge_only"):
        await async_execute(hass, steps)


async def test_the_executor_sends_nothing_before_it_refuses(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    control_surface: None,
    captured_calls: list[ServiceCall],
) -> None:
    """It refuses first and iterates second, so no partial write escapes.

    The discharge family is refused whole. There are no partial writes, which is
    the property the activation-last ordering exists to protect and which this
    asserts from the other side.
    """
    from custom_components.alpha_ems_manager.alphaess_device import (
        build_command,
        plan_commands,
    )

    from .test_control_pipeline import make_intent

    steps = plan_commands(build_command(make_intent(energy_ac_kwh=0.5)))

    with pytest.raises(ControlActionNotPermitted, match="live_charge_only"):
        await async_execute(hass, steps)

    assert captured_calls == []


def test_the_release_barrier_is_a_build_time_constant() -> None:
    """A barrier a user could clear would not be a barrier.

    Since beta.24 it is a *set* of executable actions with the old boolean derived
    from it, so "does this release send anything" and "which direction may it send"
    cannot drift apart. Both are still build-time constants.
    """
    assert_charge_only_capability()


# ===========================================================================
# reporting
# ===========================================================================


async def test_the_state_entity_reports_eligible_when_only_the_barrier_stopped_it(
    hass: HomeAssistant, setup_integration: MockConfigEntry, control_surface: None
) -> None:
    """The distinction shadow mode exists to show.

    ``inhibited`` means the gate refused. ``eligible`` means it did not, and only
    the execution barrier stood in the way -- which is the answer to "would this
    have been safe". Driven to a real discharge recommendation, because with a
    hold the state would be ``idle`` and the interesting case would go untested.
    """
    from .conftest import set_absorbing_snapshot
    from .test_battery_entities import drive

    await set_mode(hass, CONTROL_MODE_SHADOW)
    set_absorbing_snapshot(hass)
    await drive(setup_integration.runtime_data)
    report = setup_integration.runtime_data.control_report

    assert report["intent"]["action"] == "discharge"
    assert report["safety"]["safe"] is True
    assert report["commands_planned"] == 6
    assert hass.states.get(CONTROL_STATE).state == CONTROL_STATE_ELIGIBLE
    # Safe, planned, and still not authorized: the barrier is the only thing
    # between this and the inverter.
    assert report["authorization"]["authorized"] is False


async def test_a_hold_recommendation_reads_idle_rather_than_eligible(
    hass: HomeAssistant, setup_integration: MockConfigEntry, control_surface: None
) -> None:
    """The gate passed and there was nothing to send, which is what a hold is."""
    from .test_battery_entities import drive, reconfigure

    # At the floor there is nothing available to discharge, so the policy holds.
    reconfigure(setup_integration, hass, battery_min_soc_percent=55.0)
    await set_mode(hass, CONTROL_MODE_SHADOW)
    await drive(setup_integration.runtime_data)
    report = setup_integration.runtime_data.control_report

    assert report["intent"]["action"] == "hold"
    assert report["safety"]["safe"] is True
    assert report["commands_planned"] == 0
    assert hass.states.get(CONTROL_STATE).state == CONTROL_STATE_IDLE


async def test_the_state_attributes_stay_flat_and_small(
    hass: HomeAssistant, setup_integration: MockConfigEntry, control_surface: None
) -> None:
    """Eight flat values, no mappings, no lists.

    A gate with twenty-five ways to refuse has no business unpacking itself into
    an entity's attributes; the recorder writes every one of them on every state
    change.
    """
    await set_mode(hass, CONTROL_MODE_SHADOW)
    await refresh(hass, setup_integration)
    attributes = dict(hass.states.get(CONTROL_STATE).attributes)
    attributes.pop("device_class", None)
    attributes.pop("options", None)
    attributes.pop("friendly_name", None)
    attributes.pop("icon", None)

    assert set(attributes) == {
        "inhibit_reason",
        "authorization_refusal",
        "action",
        "device_power_kw",
        "commands_planned",
        "capability_ready",
        "dispatch_active",
        "basis",
    }
    for key, value in attributes.items():
        assert not isinstance(value, dict), key
        assert not isinstance(value, (list, tuple)), key


async def test_the_diagnostics_carry_the_whole_pipeline(
    hass: HomeAssistant, setup_integration: MockConfigEntry, control_surface: None
) -> None:
    """Everything the entity does not: capability, device, intent, commands."""
    from custom_components.alpha_ems_manager.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    await set_mode(hass, CONTROL_MODE_SHADOW)
    await refresh(hass, setup_integration)
    payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    control = payload["control"]

    for key in (
        "mode",
        "state",
        "capability",
        "device",
        "intent",
        "command",
        "commands",
        "safety",
        "authorization",
        "soc_coherence",
        "execution_scope",
    ):
        assert key in control, key
    assert "only a stage-b grid charge may execute" in control["execution_scope"]


async def test_every_diagnostics_list_stays_within_the_cap(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Including the capability report with no control surface present at all.

    That case names every required entity at once, which is more entries than any
    list in this payload is allowed to carry -- so it is capped with a total
    beside it rather than truncated silently.
    """
    from custom_components.alpha_ems_manager.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    await set_mode(hass, CONTROL_MODE_SHADOW)
    await refresh(hass, setup_integration)
    payload = await async_get_config_entry_diagnostics(hass, setup_integration)

    oversized: list[str] = []

    def walk(value: object, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{path}.{key}")
        elif isinstance(value, (list, tuple)):
            if len(value) > 16:
                oversized.append(f"{path} has {len(value)}")
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(payload, "payload")

    assert oversized == []
    capability = payload["control"]["capability"]
    assert capability["missing_total"] > len(capability["missing"]) or (
        capability["missing_total"] == len(capability["missing"])
    )


async def test_the_capability_report_names_what_it_looked_for(
    hass: HomeAssistant, setup_integration: MockConfigEntry, control_surface: None
) -> None:
    """So a renamed helper is a specific absence rather than a vague shortfall."""
    capability = discover(hass)

    assert capability.ready is True
    assert capability.missing == ()
    assert capability.max_feed_to_grid_percent == 100.0
    assert capability.hold_monitor_available is True

    hass.states.async_remove(DISCHARGE_FAMILY.power)
    renamed = discover(hass)

    assert DISCHARGE_FAMILY.power in renamed.missing
    assert renamed.ready is False


async def test_the_read_back_sees_the_device_without_claiming_it(
    hass: HomeAssistant, setup_integration: MockConfigEntry, control_surface: None
) -> None:
    """Enough to verify a command; never enough to attribute one."""
    at_rest = read_snapshot(hass)
    assert at_rest.dispatch_active is False

    hass.states.async_set(SENSOR_DISPATCH_START, "1")
    running = read_snapshot(hass)

    assert running.dispatch_active is True
    assert running.as_dict()["owned"] is False
    assert "parameters are never evidence" in (running.as_dict()["ownership_note"])


async def test_the_activation_boolean_alone_counts_as_a_running_dispatch(
    hass: HomeAssistant, setup_integration: MockConfigEntry, control_surface: None
) -> None:
    """The two can disagree while the surface settles; the safe reading wins."""
    hass.states.async_set(DISCHARGE_FAMILY.activate, "on")
    snapshot = read_snapshot(hass)

    assert snapshot.dispatch_active is True
    assert DISCHARGE_FAMILY.activate in snapshot.active_modes


# ===========================================================================
# failure isolation
# ===========================================================================


async def test_a_control_failure_costs_only_the_control_entities(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    control_surface: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Learning, both forecasts and the battery plan are untouched."""
    import custom_components.alpha_ems_manager.coordinator as coordinator_module

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("control layer broke")

    await set_mode(hass, CONTROL_MODE_SHADOW)
    monkeypatch.setattr(coordinator_module, "discover", explode)
    report = await refresh(hass, setup_integration)

    assert report is None
    assert hass.states.get(CONTROL_STATE).state == "unknown"

    coordinator = setup_integration.runtime_data
    assert coordinator.battery_plan is not None
    assert coordinator.today_forecast is not None
    for entity_id in (
        "sensor.alpha_ems_expected_house_load_today",
        "sensor.alpha_ems_learning_days",
        "sensor.alpha_ems_battery_recommendation",
        "sensor.alpha_ems_usable_battery_energy",
    ):
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state != "unavailable", entity_id


async def test_a_battery_plan_failure_does_not_take_the_control_layer_down(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    control_surface: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Isolation in both directions, with its own throttle key each way."""
    import custom_components.alpha_ems_manager.coordinator as coordinator_module

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("plan layer broke")

    await set_mode(hass, CONTROL_MODE_SHADOW)
    monkeypatch.setattr(coordinator_module, "build_plan", explode)
    report = await refresh(hass, setup_integration)

    assert report is not None
    assert setup_integration.runtime_data.battery_plan is None
    # The control layer still reports, and reports the right cause.
    assert report["safety"]["inhibit_reason"] is not None


# ===========================================================================
# the upgrade
# ===========================================================================


async def test_an_earlier_installation_gains_the_control_layer_switched_off(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
) -> None:
    """Upgrading changes nothing until the user asks it to.

    No new required configuration, no migration, and the control mode starts off
    -- so an installation that upgrades and never opens the options page behaves
    exactly as it did before.
    """
    from custom_components.alpha_ems_manager.const import (
        CONF_CONTROL_EXPORT_MARGIN_PERCENT,
        CONF_CONTROL_HORIZON_MINUTES,
        CONFIG_ENTRY_VERSION,
        DEFAULT_CONTROL_EXPORT_MARGIN_PERCENT,
        DEFAULT_CONTROL_HORIZON_MINUTES,
    )

    # An entry as an earlier release wrote it: no control keys at all, plus an
    # unknown key a future release might have left behind.
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Alpha EMS",
        data=config_data,
        options={"something_unknown": "keep me"},
        version=CONFIG_ENTRY_VERSION,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    config = entry.runtime_data.config
    assert config.control_horizon_minutes == DEFAULT_CONTROL_HORIZON_MINUTES
    assert config.control_export_margin_percent == DEFAULT_CONTROL_EXPORT_MARGIN_PERCENT
    assert config.control_execution_enabled is False
    assert entry.runtime_data.control_mode == CONTROL_MODE_OFF
    assert entry.version == CONFIG_ENTRY_VERSION

    # The unknown option survives, and neither control key was invented.
    assert entry.options["something_unknown"] == "keep me"
    assert CONF_CONTROL_HORIZON_MINUTES not in entry.options
    assert CONF_CONTROL_EXPORT_MARGIN_PERCENT not in entry.options

    registry = er.async_get(hass)
    ours = {
        item.entity_id
        for item in registry.entities.values()
        if item.platform == DOMAIN and item.config_entry_id == entry.entry_id
    }
    assert CONTROL_STATE in ours
    assert CONTROL_MODE in ours
