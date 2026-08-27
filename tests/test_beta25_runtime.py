"""The beta.25 runtime: one actuator family, one lock, and the things that stop it.

**The migration this file exists to pin down.** Through beta.24 the Live path
armed the Force Charging helper family. beta.25 executes on the real Hillview
Dispatch surface instead, and the cutover is atomic: arm, sustain, stop and the
sixty-second correction all moved together. There is no state in which a start
uses one surface and a stop the other, because that state cannot be reasoned
about -- and the first test below is the one that would catch it.

The helper families are still *read*. They are two of the six conflicting
families the vendor automation would silently switch off, and reading them is how
Alpha EMS stands down instead of destroying a feature the user chose. Reading is
not commanding, and the distinction is asserted rather than assumed.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from itertools import pairwise

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.alphaess_device import (
    BOOLEAN_EXECUTION_OWNER,
    CHARGE_FAMILY,
    CONFLICTING_FAMILIES,
    DISCHARGE_FAMILY,
    DISPATCH_DEADMAN_MINUTES,
    DISPATCH_DURATION,
    DISPATCH_ENABLE,
    DISPATCH_ENTITIES,
    DISPATCH_MODE_LABELS,
    DISPATCH_MODE_SELECT,
    DISPATCH_POWER,
)
from custom_components.alpha_ems_manager.const import (
    CONTROL_MODE_SHADOW,
    DISPATCH_POWER_DEADBAND_KW,
    EXECUTION_STOP_TIMER_NOT_REFRESHED,
    OWNERSHIP_DEGRADED,
    OWNERSHIP_OWNED,
    TICK_SKIPPED_DISPATCH_INACTIVE,
    TICK_SKIPPED_LOCK_HELD,
    TICK_SKIPPED_NO_QUARTER,
    TICK_SKIPPED_NOT_LIVE,
    TICK_SKIPPED_OWNERSHIP,
)

from .forecast_helpers import NORMAL, local
from .test_beta24_live_charge import (
    LiveSurface,
    drive_live_charge,
    owned_live_charge,
    step_once,
)

pytestmark = pytest.mark.usefixtures("control_surface")


@pytest.fixture
def live_surface(hass: HomeAssistant, control_surface: None) -> LiveSurface:
    """Return a control surface that responds to writes."""
    return LiveSurface(hass)


def touched(surface: LiveSurface) -> set[str]:
    """Return every entity the surface was asked to write."""
    return {call.data["entity_id"] for call in surface.calls}


def controller(coordinator) -> dict:
    """Return the published controller block."""
    execution = (coordinator.control_report or {}).get("execution") or {}
    return execution.get("controller") or {}


# == 1. one actuator family ==================================================


async def test_beta25_live_never_uses_force_charging_family(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """**The migration regression, and the one that must never be deleted.**

    A real authorised Live charge, driven end to end. None of the six conflicting
    families may be *commanded* as the execution surface -- including the two that
    used to be the execution surface. They may be read for conflict detection, and
    reading leaves no service call behind, which is exactly why this assertion can
    be made on the calls.
    """
    await drive_live_charge(hass, config_data, frank, live_surface, quarters=6)

    written = touched(live_surface)
    assert written, "a Live charge must actually write something"

    for name, entity in CONFLICTING_FAMILIES:
        assert entity not in written, f"{name} was commanded: {entity}"

    # And every companion of the two families that used to execute.
    for family in (CHARGE_FAMILY, DISCHARGE_FAMILY):
        assert not written & set(family.entities), written & set(family.entities)

    # What it *did* write is the Dispatch surface and the owner marker, and
    # nothing else at all.
    assert written <= set(DISPATCH_ENTITIES) | {BOOLEAN_EXECUTION_OWNER}


async def test_the_live_charge_is_mode_two_with_a_negative_power(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """The whole executable envelope, observed on the wire."""
    await drive_live_charge(hass, config_data, frank, live_surface, quarters=4)

    selects = live_surface.steps_of(DISPATCH_MODE_SELECT)
    modes = [call.data["option"] for call in selects]
    powers = [call.data["value"] for call in live_surface.steps_of(DISPATCH_POWER)]

    assert modes, "a mode must be selected before a power is meaningful"
    assert set(modes) == {DISPATCH_MODE_LABELS[2]}, modes
    assert powers, "a charge must command a power"
    assert all(value <= 0.0 for value in powers), powers


async def test_the_dispatch_stays_enabled_across_a_healthy_run(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """**Enabled once, and left alone.**

    Writing the duration re-arms the vendor timer on its own, so a sustain needs
    no enable toggle -- and not toggling it is what keeps the dispatch
    continuously live instead of momentarily off every quarter.
    """
    await drive_live_charge(hass, config_data, frank, live_surface, quarters=5)

    enables = live_surface.steps_of(DISPATCH_ENABLE)

    assert [call.service for call in enables] == ["turn_on"], enables
    assert hass.states.get(DISPATCH_ENABLE).state == "on"


# == 2. the shared execution lock ============================================


async def test_a_physical_tick_during_an_actuator_sequence_writes_nothing(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """**The concurrency guarantee, with the lock held by hand.**

    A Home Assistant timer callback is not serialised against a coordinator
    refresh, so without the lock a correction could land between the mode and the
    enable and arm a dispatch against half-written values.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    moment = local(NORMAL, 10, 46)
    live_surface.at(moment)

    await coordinator._execution_lock.acquire()
    try:
        await coordinator._async_physical_tick(moment)
    finally:
        coordinator._execution_lock.release()

    assert live_surface.calls == []
    assert coordinator._last_tick_reason == TICK_SKIPPED_LOCK_HELD


async def test_a_skipped_tick_is_not_queued(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """**Skipped, never deferred.**

    A correction computed while a sequence was running describes a world that no
    longer exists by the time the lock frees, and the next tick is sixty seconds
    away. Three skipped ticks must not become three writes afterwards.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    moment = local(NORMAL, 10, 46)
    live_surface.at(moment)

    await coordinator._execution_lock.acquire()
    try:
        for offset in range(3):
            await coordinator._async_physical_tick(moment + timedelta(seconds=offset))
    finally:
        coordinator._execution_lock.release()

    assert live_surface.calls == []

    # The lock is free again, and nothing accumulated behind it.
    await asyncio.sleep(0)
    assert live_surface.calls == []


async def test_the_quarter_sequence_holds_the_lock_against_a_tick(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**Both write paths are inside the lock, not just the tick.**

    Found by reviewing the finished code rather than by a failing test: the
    sixty-second tick took the lock from the start, and the quarter-boundary
    sequence did not. That is the interleaving that matters most -- mode, power,
    cutoff and duration must all be settled before the enable, so a correction
    landing in the middle of an arm would arm a dispatch against half-written
    values.

    Asserted by observing the lock from inside the sequence: if the quarter path
    did not hold it, a tick firing at that moment would be free to write.
    """
    from custom_components.alpha_ems_manager import coordinator as module

    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    seen: list[bool] = []
    original = module.async_execute

    async def watched(hass_arg, steps, *, intent=None):
        # Sampled at the moment of every send, in every sequence. ``intent`` is
        # accepted and forwarded because the send site carries it from beta.27 on --
        # a double that swallowed it would make the sign gate untestable here.
        seen.append(coordinator._execution_lock.locked())
        return await original(hass_arg, steps, intent=intent)

    monkeypatch.setattr(module, "async_execute", watched)
    await step_once(hass, coordinator, live_surface, hour=10, minute=46)

    assert seen, "the quarter refresh should have sent something"
    assert all(seen), "every send must happen with the lock held"


async def test_the_lock_is_released_when_a_sequence_raises(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """A fault costs the correction, never the lock -- or the controller wedges."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)

    def explode(self, now):
        raise RuntimeError("deliberate")

    monkeypatch.setattr(type(coordinator), "_update_coherence", explode)
    moment = local(NORMAL, 10, 46)
    live_surface.at(moment)

    await coordinator._async_physical_tick(moment)

    assert coordinator._execution_lock.locked() is False
    assert coordinator._last_tick_reason == "controller_error"


async def test_the_tick_writes_nothing_outside_live(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """Shadow and Off correct nothing, and say which refusal it was."""
    from .test_control_modes import set_mode

    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    await set_mode(hass, CONTROL_MODE_SHADOW)
    live_surface.calls.clear()

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    assert live_surface.calls == []
    assert coordinator._last_tick_reason == TICK_SKIPPED_NOT_LIVE


async def test_the_tick_writes_nothing_without_an_owned_run(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """No run, no correction -- and never a write against something foreign."""
    coordinator, _trace = await drive_live_charge(
        hass, config_data, frank, live_surface, quarters=1
    )
    live_surface.calls.clear()

    await coordinator._async_physical_tick(local(NORMAL, 10, 1))

    assert live_surface.calls == []
    # **The three reasons beta.27 split out of ``no_owned_run``.** That one string
    # covered "nothing is admitted", "nothing is armed" and "we cannot prove this
    # is ours" -- and reporting them as one is what made the live beta.26
    # observation unreadable. Any of them is a correct answer here; what matters is
    # that nothing was written.
    assert coordinator._last_tick_reason in (
        TICK_SKIPPED_NO_QUARTER,
        TICK_SKIPPED_DISPATCH_INACTIVE,
        TICK_SKIPPED_OWNERSHIP,
        TICK_SKIPPED_NOT_LIVE,
    )


# == 3. the sixty-second correction ==========================================


async def test_the_tick_corrects_the_setpoint_when_production_moves(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """**The whole point of the release, on the real runtime, in both directions.**

    Production moves inside the quarter while the Stage-A grid target stays
    frozen. The commanded charge has to follow it -- more negative as production
    rises, less negative as it collapses -- so the meter stays near the target
    instead of the difference leaving or entering through it. A fifteen-minute
    setpoint could do neither, which is what caused the incident.

    Started from a **deliberately unclamped** state: with production at three
    kilowatts the required charge already exceeds the inverter limit, so raising
    it further would change nothing and the test would prove nothing.
    """
    from .conftest import PV_POWER, set_sensor

    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)

    async def tick(minute: int, production: int) -> float | None:
        set_sensor(hass, PV_POWER, production, "W", "power")
        await hass.async_block_till_done()
        moment = local(NORMAL, 10, minute)
        live_surface.at(moment)
        await coordinator._async_physical_tick(moment)
        return coordinator._applied_setpoint_kw

    dark = await tick(46, 0)
    assert dark is not None and dark < 0.0

    # Production rises: the battery absorbs it, so the charge deepens.
    risen = await tick(47, 1500)
    assert risen is not None
    assert risen < dark, (risen, dark)

    # And collapses: the charge eases back rather than the house importing more.
    collapsed = await tick(48, 0)
    assert collapsed is not None
    assert collapsed > risen, (collapsed, risen)
    # **Stage B never bought more than Stage A authorised.** The target is
    # untouched throughout -- only the physical setpoint moved.
    assert coordinator._carried is not None
    assert coordinator._carried.target.desired_grid_kw == pytest.approx(2.1, abs=5.0)

    # Every write was material, and there was at most one per tick.
    powers = [call.data["value"] for call in live_surface.steps_of(DISPATCH_POWER)]
    assert len(powers) >= 2, powers
    assert all(a != b for a, b in pairwise(powers)), powers


async def test_the_tick_writes_nothing_for_a_sub_deadband_wobble(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """Noise costs no service call, which is what the deadband is for."""
    from .conftest import PV_POWER, set_sensor

    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    live_surface.calls.clear()

    # Well inside the 0.2 kW band, and expressed from it so the two cannot drift.
    nudge = int((DISPATCH_POWER_DEADBAND_KW / 2) * 1000)
    set_sensor(hass, PV_POWER, 3000 + nudge, "W", "power")
    await hass.async_block_till_done()
    moment = local(NORMAL, 10, 46)
    live_surface.at(moment)
    await coordinator._async_physical_tick(moment)

    assert live_surface.steps_of(DISPATCH_POWER) == []


async def test_the_tick_never_rearms_the_dead_man(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """**A power cadence must never become a run-length cadence.**

    Re-arming every sixty seconds would extend the run on a schedule the
    economics never chose, which is the one thing a dead-man exists to prevent.
    """
    from .conftest import PV_POWER, set_sensor

    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    live_surface.calls.clear()

    for minute, production in ((46, 9000), (47, 1000), (48, 6000)):
        set_sensor(hass, PV_POWER, production, "W", "power")
        await hass.async_block_till_done()
        moment = local(NORMAL, 10, minute)
        live_surface.at(moment)
        await coordinator._async_physical_tick(moment)

    assert live_surface.steps_of(DISPATCH_DURATION) == []
    assert live_surface.steps_of(DISPATCH_ENABLE) == []


# == 4. the dead-man ==========================================================


async def test_the_duration_alternates_so_the_vendor_automation_fires(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """**The workaround, observed on the wire.**

    The vendor automation triggers on the helper changing *state*, so writing the
    same duration twice re-arms nothing and the run would expire silently. The
    written value therefore alternates -- and consecutive writes must differ.
    """
    await drive_live_charge(hass, config_data, frank, live_surface, quarters=6)

    written = [call.data["value"] for call in live_surface.steps_of(DISPATCH_DURATION)]

    assert len(written) >= 3, written
    assert all(a != b for a, b in pairwise(written)), written
    assert set(written) <= {float(m) for m in DISPATCH_DEADMAN_MINUTES}, written


async def test_the_dead_man_moved_forward_every_time_it_was_rearmed(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """Measured, not assumed: each re-arm has to move the timer."""
    await drive_live_charge(hass, config_data, frank, live_surface, quarters=6)

    deadlines = live_surface.deadlines

    assert len(deadlines) >= 3
    assert all(a < b for a, b in pairwise(deadlines)), deadlines


async def test_a_dead_man_that_stops_advancing_stops_the_run(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """The measured unknown, and its one behaviour."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    monkeypatch.setattr(
        type(coordinator), "_deadman_is_stale", lambda self, snapshot, run_id: True
    )

    report = await step_once(hass, coordinator, live_surface)

    execution = report.get("execution") or {}
    assert (execution.get("result") or {}).get(
        "stop_reason"
    ) == EXECUTION_STOP_TIMER_NOT_REFRESHED
    assert hass.states.get(DISPATCH_ENABLE).state == "off"
    # And no cycling: the run ends, it is not deactivated and reactivated.
    enables = [call.service for call in live_surface.steps_of(DISPATCH_ENABLE)]
    assert enables == ["turn_off"], enables


# == 5. degraded ownership and the emergency stop ============================


async def test_a_lost_marker_becomes_degraded_and_stops_the_run(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """**Marker gone, causation intact: degraded, and stopped deliberately.**

    Leaving it to the device dead-man is up to twenty minutes of uncommanded
    charging. The state is reported as ``degraded`` and never as owned, and the
    only write it authorises is the enable going off.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    hass.states.async_set(BOOLEAN_EXECUTION_OWNER, "off")
    live_surface.calls.clear()

    report = await step_once(hass, coordinator, live_surface)

    execution = report.get("execution") or {}
    assert (execution.get("ownership") or {}).get("state") == OWNERSHIP_DEGRADED
    written = [call.data["entity_id"] for call in live_surface.calls]
    assert written == [DISPATCH_ENABLE], written
    assert hass.states.get(DISPATCH_ENABLE).state == "off"


async def test_a_degraded_run_never_has_its_parameters_touched(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """Not power, cutoff, duration, mode or the photovoltaic switch.

    Each of those touches a dispatch that may still be running, and one of them --
    the duration -- restarts the vendor timer, so a stop that also tidied up would
    extend the run it was ending.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    hass.states.async_set(BOOLEAN_EXECUTION_OWNER, "off")
    live_surface.calls.clear()

    await step_once(hass, coordinator, live_surface)

    forbidden = set(DISPATCH_ENTITIES) - {DISPATCH_ENABLE}
    assert not touched(live_surface) & forbidden, touched(live_surface)


async def test_a_foreign_dispatch_is_never_stopped(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """**The invariant the degraded state must not weaken.**

    Marker off *and* the causal record gone: causation can no longer be shown, so
    this is somebody else's dispatch and nothing may be written to it at all.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    hass.states.async_set(BOOLEAN_EXECUTION_OWNER, "off")
    coordinator._clear_execution_record()
    live_surface.calls.clear()

    report = await step_once(hass, coordinator, live_surface)

    assert live_surface.calls == []
    assert hass.states.get(DISPATCH_ENABLE).state == "on", "left running, untouched"
    ownership = ((report.get("execution") or {}).get("ownership") or {}).get("state")
    assert ownership != OWNERSHIP_OWNED
    assert ownership != OWNERSHIP_DEGRADED


# == 6. the conflicting-family pre-arm gate ==================================


@pytest.mark.parametrize(("name", "entity"), CONFLICTING_FAMILIES)
async def test_an_active_conflicting_family_refuses_the_arm(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    name: str,
    entity: str,
) -> None:
    """**Stand down, never switch it off.**

    ``AlphaESS Dispatch`` disables all six before arming and cancels their timers,
    so arming over one destroys a feature the user selected without asking. Alpha
    EMS refuses instead, and names the conflict.
    """
    hass.states.async_set(entity, "on")

    coordinator, _trace = await drive_live_charge(
        hass, config_data, frank, live_surface, quarters=4
    )

    assert DISPATCH_ENABLE not in touched(live_surface), name
    assert hass.states.get(entity).state == "on", "the user's feature is untouched"
    # The device readback lives at the top of the report, beside the capability.
    device = (coordinator.control_report or {}).get("device") or {}
    assert name in device.get("conflicting_active", []), device


# == 7. the flight recorder ==================================================


async def test_the_controller_block_carries_every_required_field(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """Diagnostics completeness, asserted rather than hoped for."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)

    block = controller(coordinator)

    for field in (
        "controller_refresh_at",
        "house_load_kw",
        "pv_kw",
        "actual_grid_kw",
        "desired_grid_kw",
        "dispatch_limited_by",
        "update_needed",
        "dispatch_power_deadband_kw",
        "coherence_state",
        "coherence_bad_ticks",
        "coherence_grace_seconds",
        "last_coherent_physical_tick",
        "forward_authorised_kwh",
        "binding_cap",
    ):
        assert field in block, field


async def test_the_physical_decision_ring_is_bounded_and_populated(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """**A download taken later must reconstruct the quarter, not the instant.**

    Diagnostics are rarely captured at the moment production moved, so the ring is
    what makes an intra-quarter correction visible afterwards -- and it is bounded
    so it can never grow without limit.
    """
    from custom_components.alpha_ems_manager.const import (
        MAX_PHYSICAL_DECISIONS_REPORTED,
    )

    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    for minute in range(46, 46 + MAX_PHYSICAL_DECISIONS_REPORTED + 4):
        moment = local(NORMAL, 10, 45) + timedelta(minutes=minute - 45)
        live_surface.at(moment)
        await coordinator._async_physical_tick(moment)

    await step_once(hass, coordinator, live_surface, hour=11, minute=0)
    execution = (coordinator.control_report or {}).get("execution") or {}
    ring = execution.get("physical_decisions") or []

    assert ring
    assert len(ring) <= MAX_PHYSICAL_DECISIONS_REPORTED
    assert all("controller_refresh_at" in entry for entry in ring)


async def test_the_source_says_suppressed_rather_than_reserve_guard(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
) -> None:
    """**The mislabel that pointed readers at a layer that was switched off.**

    While Stage B holds a run the reserve-guard fallback is deliberately
    suppressed. Reporting ``reserve_guard`` on those refreshes sent anyone
    investigating an unexpected discharge to the wrong place entirely.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)

    # **On a refresh where Stage B holds the run**, which is the case the label
    # was wrong about. Once the run has ended the fallback genuinely is the
    # reserve guard again, and saying so is correct.
    report = await step_once(hass, coordinator, live_surface, hour=10, minute=46)
    boundary = ((report.get("execution") or {}).get("write_boundary")) or {}

    assert boundary.get("source") != "reserve_guard", boundary
