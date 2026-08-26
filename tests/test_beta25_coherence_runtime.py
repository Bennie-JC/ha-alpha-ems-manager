"""Control-grade coherence, on the running controller rather than in the abstract.

The state machine itself is proved in ``test_beta25_safety_layers``. What is
proved here is that the runtime *obeys* it: a hold writes nothing and keeps the
last setpoint, a recovery resumes, expiry stops a provably owned run, and the
dead-man is never re-armed while the measurements are untrusted.

**The bound is counted in physical ticks, and that is the whole design.** Two
economic refreshes is about thirty minutes -- longer than the twenty-minute device
dead-man it is supposed to sit inside, so the device would end the run before the
controller decided to. Three sixty-second ticks is 180 seconds.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.alphaess_device import (
    DISPATCH_DURATION,
    DISPATCH_ENABLE,
    DISPATCH_POWER,
)
from custom_components.alpha_ems_manager.const import (
    COHERENCE_ACTION_HOLD,
    COHERENCE_HOLDING,
    COHERENCE_OK,
    CONTROL_COHERENCE_GRACE_TICKS,
    SAFETY_SAMPLE_SECONDS,
    TICK_SKIPPED_INCOHERENT,
)

from .conftest import GRID_POWER, HOUSE_LOAD, PV_POWER, set_sensor
from .forecast_helpers import NORMAL, local
from .test_beta24_live_charge import LiveSurface, owned_live_charge

pytestmark = pytest.mark.usefixtures("control_surface")


@pytest.fixture
def live_surface(hass: HomeAssistant, control_surface: None) -> LiveSurface:
    """Return a control surface that responds to writes."""
    return LiveSurface(hass)


def blind(hass: HomeAssistant) -> None:
    """Make the house load unreadable, which is what an unusable tick means.

    **No fallback is invented.** House load and the meter are what the controller
    corrects *against*; without them it does not know what it is correcting, so it
    holds rather than guessing a figure that would look like a measurement.
    """
    hass.states.async_set(HOUSE_LOAD, "unavailable")


def sighted(hass: HomeAssistant) -> None:
    """Restore a readable, coherent site."""
    set_sensor(hass, HOUSE_LOAD, 2000, "W", "power")
    set_sensor(hass, PV_POWER, 0, "W", "power")
    set_sensor(hass, GRID_POWER, 2000, "W", "power")


async def tick(coordinator, surface: LiveSurface, minute: int) -> None:
    """Run one physical tick at a given minute inside the run window."""
    moment = local(NORMAL, 10, minute)
    surface.at(moment)
    await coordinator._async_physical_tick(moment)


async def running(hass, config_data, frank, surface):
    """Return a coordinator owning a live charge, with a settled setpoint."""
    coordinator = await owned_live_charge(hass, config_data, frank, surface)
    assert coordinator._applied_setpoint_kw is not None
    surface.calls.clear()
    return coordinator


# == the hold ================================================================


async def test_one_bad_tick_holds_the_last_setpoint(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """**Calculate nothing, write nothing, keep the economic target.**

    The setpoint on the wire is left exactly where it was. That is safe because
    the device dead-man bounds how long a held setpoint can run -- which is why
    the grace period has to be much shorter than it.
    """
    coordinator = await running(hass, config_data, frank, live_surface)
    held = coordinator._applied_setpoint_kw
    target_before = coordinator._carried.target.desired_grid_kw

    blind(hass)
    await hass.async_block_till_done()
    await tick(coordinator, live_surface, 46)

    assert live_surface.calls == []
    assert coordinator._applied_setpoint_kw == held
    assert coordinator._last_tick_reason == TICK_SKIPPED_INCOHERENT
    assert coordinator._coherence is not None
    assert coordinator._coherence.state == COHERENCE_HOLDING
    assert coordinator._coherence.action == COHERENCE_ACTION_HOLD
    # **The economic target is untouched.** A sensor fault is not a reason to
    # revise what Stage A decided.
    assert coordinator._carried.target.desired_grid_kw == target_before


async def test_recovery_inside_the_grace_period_resumes_control(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """The hold clears, the counter resets, and the run continues."""
    coordinator = await running(hass, config_data, frank, live_surface)

    blind(hass)
    await hass.async_block_till_done()
    await tick(coordinator, live_surface, 46)
    assert coordinator._coherence.bad_ticks == 1

    sighted(hass)
    await hass.async_block_till_done()
    await tick(coordinator, live_surface, 47)

    assert coordinator._coherence.state == COHERENCE_OK
    assert coordinator._coherence.bad_ticks == 0
    assert hass.states.get(DISPATCH_ENABLE).state == "on", "the run continues"


async def test_the_hold_never_rearms_the_dead_man(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """**Deterministic, not a judgement call.**

    Re-arming on measurements the controller does not trust is exactly "keep the
    run alive indefinitely while blind", which is what the grace period exists to
    prevent.
    """
    coordinator = await running(hass, config_data, frank, live_surface)

    blind(hass)
    await hass.async_block_till_done()
    for minute in (46, 47):
        await tick(coordinator, live_surface, minute)

    assert live_surface.steps_of(DISPATCH_DURATION) == []
    assert coordinator._coherence.may_rearm_deadman is False


# == the bound ===============================================================


async def test_the_grace_period_is_shorter_than_the_dead_man() -> None:
    """The invariant the whole bound exists to satisfy, stated in seconds."""
    grace = CONTROL_COHERENCE_GRACE_TICKS * SAFETY_SAMPLE_SECONDS

    assert grace == 180
    # Twenty minutes is the semantic dead-man, so the controller decides first by
    # a wide margin rather than racing the device.
    assert grace * 6 <= 20 * 60


async def test_the_bound_expires_and_stops_the_owned_run(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """**Three unusable ticks and the run is stopped deliberately.**

    Not left to the device dead-man: that would be up to twenty minutes of
    charging against measurements nobody trusts. The stop is the narrow one --
    enable off, then verified inactive before anything else is written.
    """
    coordinator = await running(hass, config_data, frank, live_surface)

    blind(hass)
    await hass.async_block_till_done()
    for offset in range(CONTROL_COHERENCE_GRACE_TICKS):
        await tick(coordinator, live_surface, 46 + offset)

    # **The transient state is gone, because the stop cleared it**, which is the
    # right order: a coherence counter surviving the run it belonged to would be
    # inherited by the next one. The expired state itself is asserted on the pure
    # state machine; what matters here is that the run really stopped.
    assert coordinator._coherence is None
    written = [call.data["entity_id"] for call in live_surface.calls]
    assert DISPATCH_ENABLE in written, written
    assert hass.states.get(DISPATCH_ENABLE).state == "off"


async def test_the_expiry_stop_writes_no_power_or_duration_first(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """The enable goes off before anything else, and nothing is calculated.

    Writing the duration here would restart the vendor timer and extend the very
    run being stopped, and writing a power would mean acting on the readings that
    are the reason for stopping.
    """
    coordinator = await running(hass, config_data, frank, live_surface)

    blind(hass)
    await hass.async_block_till_done()
    for offset in range(CONTROL_COHERENCE_GRACE_TICKS):
        await tick(coordinator, live_surface, 46 + offset)

    written = [call.data["entity_id"] for call in live_surface.calls]
    assert written[0] == DISPATCH_ENABLE, written
    powers = [index for index, entity in enumerate(written) if entity == DISPATCH_POWER]
    durations = [
        index for index, entity in enumerate(written) if entity == DISPATCH_DURATION
    ]
    # Any resting value that *is* written comes after the verified stop, never
    # before it.
    assert all(index > 0 for index in powers + durations), written


async def test_the_run_is_released_once_the_stop_is_verified(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """Only after verified inactivity: the record goes, and the marker last."""
    coordinator = await running(hass, config_data, frank, live_surface)

    blind(hass)
    await hass.async_block_till_done()
    for offset in range(CONTROL_COHERENCE_GRACE_TICKS):
        await tick(coordinator, live_surface, 46 + offset)

    assert hass.states.get(DISPATCH_ENABLE).state == "off"
    assert coordinator.store.execution_record is None
    assert coordinator._carried is None
    # The transient controller state goes with it, so a later run cannot inherit
    # a deadband comparison or a coherence counter from this one.
    assert coordinator._applied_setpoint_kw is None
    assert coordinator._coherence is None


async def test_a_foreign_dispatch_is_untouched_during_a_sensor_failure(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """**A sensor fault is never a licence to write to something not ours.**

    The coherence path only ever stops a *provably owned* run. With the marker off
    and the record gone the dispatch is foreign, and foreign stays untouched
    however bad the measurements are.
    """
    from custom_components.alpha_ems_manager.alphaess_device import (
        BOOLEAN_EXECUTION_OWNER,
    )

    coordinator = await running(hass, config_data, frank, live_surface)
    hass.states.async_set(BOOLEAN_EXECUTION_OWNER, "off")
    coordinator._clear_execution_record()

    blind(hass)
    await hass.async_block_till_done()
    for offset in range(CONTROL_COHERENCE_GRACE_TICKS + 1):
        await tick(coordinator, live_surface, 46 + offset)

    assert live_surface.calls == []
    assert hass.states.get(DISPATCH_ENABLE).state == "on", "left running, untouched"


# == diagnostics =============================================================


async def test_the_coherence_state_is_published(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """A hold that nothing reports is indistinguishable from a working controller."""
    from .test_beta24_live_charge import step_once

    coordinator = await running(hass, config_data, frank, live_surface)
    blind(hass)
    await hass.async_block_till_done()
    await tick(coordinator, live_surface, 46)

    report = await step_once(hass, coordinator, live_surface, hour=10, minute=47)
    block = ((report.get("execution") or {}).get("controller")) or {}

    assert block.get("coherence_state") in (COHERENCE_HOLDING, COHERENCE_OK)
    assert block.get("coherence_grace_seconds") == 180
    assert "coherence_bad_since" in block
    assert "last_coherent_physical_tick" in block
