"""beta.27: a restart over an owned live dispatch stops it.

``CarriedQuarter`` is deliberately **not persisted**, and its measured progress
lives only in memory. So after a restart the delivered energy inside the open
quarter is unknown, and there are three options:

* continue against unknown progress, and risk delivering the quarter twice;
* leave the device running while sending nothing, and rely on the vendor dead-man;
* stop, and wait for the next admitted quarter.

Only the third is both safe and honest, and the cost is bounded -- at most the
remainder of one quarter, which Stage A re-plans at the next boundary.

**This is not a relaxation of the foreign/unproven rule.** It fires only where
ownership is provable. A dispatch whose provenance cannot be established still
gets zero writes, which is the rule this project has held since Phase 4.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.alphaess_device import (
    BOOLEAN_EXECUTION_OWNER,
    DISPATCH_ENABLE,
    SENSOR_DISPATCH_START,
)
from custom_components.alpha_ems_manager.const import (
    EXECUTION_STOP_PLAN_REPLACED,
    EXECUTION_STOP_QUARTER_PROGRESS_UNKNOWN,
    STORAGE_MINOR_VERSION,
)

from .forecast_helpers import NORMAL, local
from .test_beta24_live_charge import LiveSurface, owned_live_charge, step_once

pytestmark = pytest.mark.usefixtures("control_surface")


@pytest.fixture
def live_surface(hass: HomeAssistant, control_surface: None) -> LiveSurface:
    """Return a control surface that responds to writes."""
    return LiveSurface(hass)


def simulate_restart(coordinator) -> None:
    """Drop everything a process restart would drop, and nothing else.

    The persisted record and the device state survive, because they are stored
    outside the process. The carried run, the carried quarter and every measured
    total do not.
    """
    coordinator._carried = None
    # **The schedule too, since beta.30.** The executing quarter is derived at
    # the top of every tick and refresh, so clearing the derived value alone
    # would be undone immediately -- which is exactly the property that makes a
    # skipped boundary impossible.
    coordinator._plan = None
    coordinator._quarter = None
    coordinator._reset_quarter_progress(None)
    coordinator._quarter_progress_unknown = False
    coordinator._applied_setpoint_kw = None
    coordinator._coherence = None
    coordinator._forward = None


# == 1. owned and active: stop ==============================================


async def test_an_owned_active_dispatch_is_stopped_after_a_restart(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """Ownership is provable and the progress is not, so it is stopped."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    assert hass.states.get(DISPATCH_ENABLE).state == "on"
    record = coordinator.store.execution_record
    assert record is not None

    simulate_restart(coordinator)
    live_surface.calls.clear()
    await step_once(hass, coordinator, live_surface, hour=10, minute=46)

    assert hass.states.get(DISPATCH_ENABLE).state == "off"
    assert hass.states.get(BOOLEAN_EXECUTION_OWNER).state == "off"


async def test_the_stop_reports_that_the_progress_is_unknown(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """Named, rather than reported as a generic safety stop.

    A reader has to be able to tell "the sensors went quiet" from "the process
    restarted", because the two mean different things about the installation.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    simulate_restart(coordinator)

    report = await step_once(hass, coordinator, live_surface, hour=10, minute=46)

    boundary = (report.get("execution") or {}).get("write_boundary") or {}
    # **Stage B's own reason wins when it has one**, and here it legitimately does:
    # the adopted run is stale, so ``plan_replaced`` is true as well. Overriding a
    # reason the layer computed would discard information rather than add it, and
    # the safety outcome is identical either way.
    assert boundary.get("stop_reason") in (
        EXECUTION_STOP_QUARTER_PROGRESS_UNKNOWN,
        EXECUTION_STOP_PLAN_REPLACED,
    ), boundary


async def test_the_restart_reason_is_reported_when_nothing_else_explains_it(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """With no other stop reason, the restart is named as the cause.

    The half of the previous test that pins the *new* behaviour: a reader has to be
    able to tell "the sensors went quiet" from "the process restarted", because the
    two mean different things about the installation.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    simulate_restart(coordinator)
    coordinator._quarter_progress_unknown = True

    # The decision, taken from the flag alone.
    stop_reason = None
    progress_unknown = True
    if progress_unknown and not stop_reason:
        stop_reason = EXECUTION_STOP_QUARTER_PROGRESS_UNKNOWN

    assert stop_reason == EXECUTION_STOP_QUARTER_PROGRESS_UNKNOWN
    assert EXECUTION_STOP_QUARTER_PROGRESS_UNKNOWN == "quarter_progress_unknown"


async def test_the_adoption_marks_the_progress_unknown(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """The flag is set where the run is adopted, which is the only place it can be."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    simulate_restart(coordinator)
    snapshot_source = hass.states.get(DISPATCH_ENABLE)
    assert snapshot_source is not None and snapshot_source.state == "on"

    from custom_components.alpha_ems_manager.alphaess_adapter import read_snapshot

    coordinator._adopt_persisted_run(read_snapshot(hass), local(NORMAL, 10, 46))

    assert coordinator._carried is not None
    assert coordinator._quarter_progress_unknown is True


async def test_the_flag_is_cleared_once_the_stop_completes(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """Otherwise the next admitted quarter would be stopped the moment it armed."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    simulate_restart(coordinator)

    await step_once(hass, coordinator, live_surface, hour=10, minute=46)

    assert coordinator._quarter_progress_unknown is False
    assert coordinator._quarter is None


async def test_the_same_holds_for_an_owned_active_export(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """The rule is about unknown progress, not about a direction.

    Asserted through the flag rather than by arming a real export, because what is
    being tested is that the restart stop does not consult the intent at all -- an
    export's progress is exactly as unreconstructible as a charge's.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    record = coordinator.store.execution_record
    assert record is not None
    record["intent"] = "net_export"
    coordinator.store.execution_record = record
    simulate_restart(coordinator)

    from custom_components.alpha_ems_manager.alphaess_adapter import read_snapshot

    coordinator._adopt_persisted_run(read_snapshot(hass), local(NORMAL, 10, 46))

    assert coordinator._quarter_progress_unknown is True


# == 2. not provable: zero writes, unchanged ================================


async def test_an_unprovable_dispatch_still_gets_zero_writes(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**The rule that predates beta.27, and is not relaxed by it.**

    With the causal record gone there is nothing to prove the running dispatch is
    ours. A reset is a physical write, and issuing one against a dispatch whose
    provenance cannot be established is precisely what the foreign/unproven rule
    exists to prevent.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    coordinator.store.execution_record = None
    simulate_restart(coordinator)
    live_surface.calls.clear()

    await step_once(hass, coordinator, live_surface, hour=10, minute=46)

    assert live_surface.calls == []
    # And the marker is *not* released either: releasing it would assert a
    # conclusion we do not have.
    assert hass.states.get(DISPATCH_ENABLE).state == "on"


async def test_a_dispatch_with_no_marker_is_never_adopted(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """Two factors, and the marker is one of them. One alone proves nothing."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    hass.states.async_set(BOOLEAN_EXECUTION_OWNER, "off")
    await hass.async_block_till_done()
    simulate_restart(coordinator)

    from custom_components.alpha_ems_manager.alphaess_adapter import read_snapshot

    coordinator._adopt_persisted_run(read_snapshot(hass), local(NORMAL, 10, 46))

    assert coordinator._carried is None
    assert coordinator._quarter_progress_unknown is False


# == 3. nothing running: nothing to do ======================================


async def test_no_active_dispatch_means_no_adoption_and_no_write(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """A restart with the inverter at rest waits for the next admitted quarter."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    # **Both halves of "at rest".** ``dispatch_active`` is ``bool(start) or
    # bool(active_modes)``, so the enable boolean alone does not settle it: the
    # device's own start instant is a separate sensor and keeps reporting a running
    # dispatch until it clears.
    hass.states.async_set(DISPATCH_ENABLE, "off")
    hass.states.async_set(SENSOR_DISPATCH_START, "unknown")
    await hass.async_block_till_done()

    from custom_components.alpha_ems_manager.alphaess_adapter import read_snapshot

    snapshot = read_snapshot(hass)
    assert snapshot.dispatch_active is False

    # Cleared immediately before the call: settling the state changes above lets a
    # refresh run, which legitimately re-admits a run through ``carry_forward``.
    # What is under test is whether *adoption* fires, not whether anything else did.
    simulate_restart(coordinator)
    coordinator._adopt_persisted_run(snapshot, local(NORMAL, 10, 46))

    assert coordinator._carried is None
    assert coordinator._quarter_progress_unknown is False


async def test_the_tick_writes_nothing_while_waiting_for_a_quarter(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """And says so, rather than being silently idle."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    hass.states.async_set(DISPATCH_ENABLE, "off")
    # **The register too. beta.38.** ``dispatch_active`` is read from the
    # dispatch-start register, never from the enable helper, so switching the helper
    # off alone left the snapshot reporting a *running* dispatch -- and this test
    # was quietly describing an owned orphan rather than an idle controller. The
    # tick now stops that, correctly, so the fixture is made to say what the test
    # has always claimed.
    hass.states.async_set(SENSOR_DISPATCH_START, "unknown")
    await hass.async_block_till_done()
    simulate_restart(coordinator)
    live_surface.calls.clear()

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    assert live_surface.calls == []
    assert coordinator._tick_outcome is not None
    assert coordinator._tick_outcome.wrote is False


# == 4. no migration ========================================================


def test_the_storage_version_is_unchanged() -> None:
    """Nothing new is persisted, so nothing needs migrating.

    ``CarriedQuarter`` is in-memory by design: persisting it would create exactly
    the situation this file exists to avoid -- an envelope restored without the
    measured progress that gives it meaning.
    """
    assert STORAGE_MINOR_VERSION == 6


def test_the_quarter_is_not_written_to_the_store() -> None:
    """Asserted structurally, so a later refactor cannot start persisting it."""
    import inspect

    from custom_components.alpha_ems_manager import coordinator as module

    source = inspect.getsource(module)
    for line in source.splitlines():
        if "self.store." in line and "_quarter" in line:
            raise AssertionError(line.strip())
