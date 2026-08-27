"""beta.27: the quarter envelope on the running coordinator.

The unit-level authority rule lives in ``test_beta27_quarter_authority``; this
file drives the real coordinator and asserts what it *does*: that the tick
executes against the quarter, that reaching the objective stops the dispatch
immediately, that quarter expiry stops it and records the shortfall, and that no
deficit ever crosses a boundary.
"""

from __future__ import annotations

from datetime import timedelta
from itertools import pairwise

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.alphaess_device import (
    BOOLEAN_EXECUTION_OWNER,
    DISCHARGE_FAMILY,
    DISPATCH_ENABLE,
)
from custom_components.alpha_ems_manager.const import (
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_NET_EXPORT,
    QUARTER_END_EXPIRED,
    QUARTER_END_TARGET_REACHED,
    QUARTER_TARGET_TOLERANCE_KWH,
    TICK_SKIPPED_LOCK_HELD,
    TICK_SKIPPED_NO_QUARTER,
    TICK_STOPPED_QUARTER_EXPIRED,
    TICK_STOPPED_TARGET_REACHED,
)
from custom_components.alpha_ems_manager.execution import CarriedQuarter

from .forecast_helpers import NORMAL, local
from .test_beta24_live_charge import LiveSurface, owned_live_charge, step_once

pytestmark = pytest.mark.usefixtures("control_surface")


@pytest.fixture
def live_surface(hass: HomeAssistant, control_surface: None) -> LiveSurface:
    """Return a control surface that responds to writes."""
    return LiveSurface(hass)


def quarter_at(
    hour: int,
    minute: int,
    *,
    intent: str = EXECUTION_INTENT_GRID_CHARGE,
    battery: float = 1.0,
    authorised: float = 0.8,
    export: float = 0.0,
    run_id: str = "run-1",
) -> CarriedQuarter:
    """Return an envelope covering one real quarter, as if admitted before it."""
    opens = local(NORMAL, hour, minute)
    return CarriedQuarter(
        quarter_start=opens,
        quarter_end=opens + timedelta(minutes=15),
        intent=intent,
        battery_target_kwh=battery,
        grid_authorised_kwh=authorised,
        grid_export_target_kwh=export,
        initial_desired_grid_kw=authorised / 0.25,
        run_id=run_id,
        plan_id="plan-1",
        revision=1,
        admitted_at=opens - timedelta(minutes=1),
    )


def install(coordinator, quarter: CarriedQuarter) -> None:
    """Put an admitted quarter in place, with its progress reset to zero."""
    coordinator._quarter = quarter
    coordinator._reset_quarter_progress(quarter)


# == 1. the tick executes against the quarter ==============================


async def test_the_tick_corrects_the_setpoint_inside_an_admitted_quarter(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """The envelope is what the sixty-second correction aims at."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=2.0, authorised=2.0))

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    assert coordinator._last_tick_reason != TICK_SKIPPED_NO_QUARTER
    outcome = coordinator._tick_outcome
    assert outcome is not None
    assert outcome.cadence == "physical_tick"


async def test_a_quarter_with_nothing_admitted_writes_nothing(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """No carrier at all is a refusal, named as such, with no write."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    coordinator._quarter = None
    coordinator._carried = None
    live_surface.calls.clear()

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    assert live_surface.calls == []
    assert coordinator._last_tick_reason == TICK_SKIPPED_NO_QUARTER
    assert coordinator._tick_outcome is not None
    assert coordinator._tick_outcome.wrote is False


# == 2. target reached stops the dispatch, whatever the lease says =========


async def test_reaching_the_battery_objective_stops_the_charge_immediately(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**Invariant 5.** The dead-man is a lease, never an execution entitlement.

    The device duration still has minutes to run. A dispatch left armed because a
    countdown has not expired is exactly how a target gets exceeded, so the stop is
    driven by the objective and not by the timer.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    quarter = quarter_at(10, 45, battery=1.0, authorised=1.0)
    install(coordinator, quarter)
    # Delivered, as measured -- not as commanded.
    coordinator._quarter_battery_kwh = 1.0
    live_surface.calls.clear()

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    assert coordinator._last_tick_reason == TICK_STOPPED_TARGET_REACHED
    assert hass.states.get(DISPATCH_ENABLE).state == "off"
    assert coordinator._quarter is None


async def test_the_completion_is_recorded_with_its_reason(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """A finished quarter enters the history, whatever the outcome was."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=1.0, authorised=1.0))
    coordinator._quarter_battery_kwh = 1.0

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    history = list(coordinator._completed_quarters)
    assert len(history) == 1
    row = history[0]
    assert row["completion_reason"] == QUARTER_END_TARGET_REACHED
    assert row["intent"] == EXECUTION_INTENT_GRID_CHARGE
    assert row["realized_battery_kwh"] == pytest.approx(1.0)
    assert row["shortfall_kwh"] == pytest.approx(0.0)
    assert row["target_reached_at"] is not None


async def test_a_spent_grid_ceiling_does_not_stop_a_charge(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**A ceiling is never a completion test.**

    The grid authorisation is exhausted but the battery objective is not, so free
    production may still fill the pack. This is beta.26's F2, asserted at the level
    that decides whether to stop.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=2.0, authorised=1.0))
    coordinator._quarter_grid_import_kwh = 1.0  # ceiling spent
    coordinator._quarter_battery_kwh = 0.5  # objective not met

    progress = coordinator._quarter_progress(local(NORMAL, 10, 46))

    assert progress is not None
    assert progress.grid_remaining_kwh == pytest.approx(0.0)
    assert progress.battery_remaining_kwh == pytest.approx(1.5)
    assert coordinator._quarter_target_reached(progress) is False


async def test_the_export_objective_is_the_meter_figure(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """And symmetrically: an export finishes on the meter, not on the battery.

    The battery discharge authorisation is exhausted, but what decides completion is
    the meter target -- so the mirror image of the case above.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(
        coordinator,
        quarter_at(10, 45, intent=EXECUTION_INTENT_NET_EXPORT, battery=1.0, export=0.8),
    )
    coordinator._quarter_grid_export_kwh = 0.8
    coordinator._quarter_battery_kwh = 0.2

    progress = coordinator._quarter_progress(local(NORMAL, 10, 46))

    assert progress is not None
    assert progress.grid_remaining_kwh == pytest.approx(0.0)
    assert progress.battery_remaining_kwh == pytest.approx(0.8)
    assert coordinator._quarter_target_reached(progress) is True


async def test_the_tolerance_is_quarter_scale_not_run_scale(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """A run's 0.25 kWh tolerance would call a half-kilowatt-hour quarter done.

    Ten watt-hours is below what one tick can command at the 0.1 kW step, so the
    residue it forgives is smaller than the smallest correction that could chase it.
    """
    from custom_components.alpha_ems_manager.execution import TARGET_TOLERANCE_KWH

    assert QUARTER_TARGET_TOLERANCE_KWH == 0.01
    assert QUARTER_TARGET_TOLERANCE_KWH < TARGET_TOLERANCE_KWH / 10

    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=0.5, authorised=0.5))
    # Half delivered. The run-level tolerance would call this finished.
    coordinator._quarter_battery_kwh = 0.25

    progress = coordinator._quarter_progress(local(NORMAL, 10, 46))
    assert progress is not None
    assert coordinator._quarter_target_reached(progress) is False

    coordinator._quarter_battery_kwh = 0.495
    progress = coordinator._quarter_progress(local(NORMAL, 10, 46))
    assert progress is not None
    assert coordinator._quarter_target_reached(progress) is True


# == 3. expiry stops it, records the shortfall, and carries nothing ========


async def test_quarter_expiry_stops_the_dispatch_and_records_the_shortfall(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**Invariant 4.** The envelope ends at its own end, and the gap is stated."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=2.0, authorised=2.0))
    coordinator._quarter_battery_kwh = 1.2
    live_surface.calls.clear()

    await coordinator._async_physical_tick(local(NORMAL, 11, 0))

    assert coordinator._last_tick_reason == TICK_STOPPED_QUARTER_EXPIRED
    assert hass.states.get(DISPATCH_ENABLE).state == "off"

    row = list(coordinator._completed_quarters)[-1]
    assert row["completion_reason"] == QUARTER_END_EXPIRED
    assert row["planned_battery_kwh"] == pytest.approx(2.0)
    assert row["realized_battery_kwh"] == pytest.approx(1.2)
    # Stated, not left to be derived by subtracting two other published figures.
    assert row["shortfall_kwh"] == pytest.approx(0.8)
    assert row["shortfall_percent"] == pytest.approx(40.0)


async def test_no_deficit_is_carried_into_the_next_quarter(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**Invariant 4, the half that matters most.**

    Stage B accumulating an entitlement no economic layer authorised is how a
    shortfall becomes an overshoot two quarters later. The next envelope carries the
    published figures and nothing else, and its measured progress starts at zero.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=2.0, authorised=2.0))
    coordinator._quarter_battery_kwh = 1.2
    await coordinator._async_physical_tick(local(NORMAL, 11, 0))

    shortfall = list(coordinator._completed_quarters)[-1]["shortfall_kwh"]
    assert shortfall == pytest.approx(0.8)

    # The next quarter, as Stage A published it: 1.0, not 1.8.
    following = quarter_at(11, 0, battery=1.0, authorised=1.0)
    install(coordinator, following)

    assert coordinator._quarter.battery_target_kwh == pytest.approx(1.0)
    assert coordinator._quarter.battery_allowance_kwh() == pytest.approx(1.0)
    assert coordinator._quarter_battery_kwh == 0.0
    assert coordinator._quarter_grid_import_kwh == 0.0

    progress = coordinator._quarter_progress(local(NORMAL, 11, 1))
    assert progress is not None
    assert progress.battery_remaining_kwh == pytest.approx(1.0)


async def test_the_history_states_the_carry_over_rule_with_every_row(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """So a reader looking at a shortfall does not have to guess what follows."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=2.0, authorised=2.0))
    await coordinator._async_physical_tick(local(NORMAL, 11, 0))

    rule = list(coordinator._completed_quarters)[-1]["carry_over_rule"]

    assert "never carried" in rule


# == 4. progress is measured, and reset per quarter ========================


async def test_progress_is_keyed_on_the_quarter_start(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """A new quarter cannot inherit the last one's accumulators."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45))
    coordinator._quarter_battery_kwh = 0.7
    coordinator._quarter_grid_import_kwh = 0.6

    # A different quarter arrives without anyone calling the reset explicitly.
    coordinator._quarter = quarter_at(11, 0)
    coordinator._accrue_quarter_progress(local(NORMAL, 11, 1))

    assert coordinator._quarter_battery_kwh == 0.0
    assert coordinator._quarter_grid_import_kwh == 0.0
    assert coordinator._quarter_key == local(NORMAL, 11, 0)


async def test_a_single_sample_accrues_nothing(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """Integration needs an interval, and one reading is not one."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45))

    coordinator._accrue_quarter_progress(local(NORMAL, 10, 46))

    assert coordinator._quarter_battery_kwh == 0.0
    assert coordinator._quarter_sampled_at == local(NORMAL, 10, 46)


async def test_a_gap_too_long_to_integrate_across_accrues_nothing(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """A silence contributes no energy rather than extrapolating the last reading."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45))

    coordinator._accrue_quarter_progress(local(NORMAL, 10, 45, 30))
    before = coordinator._quarter_battery_kwh
    # Ten minutes later, well past the sampling tolerance.
    coordinator._accrue_quarter_progress(local(NORMAL, 10, 55, 30))

    assert coordinator._quarter_battery_kwh == before


async def test_progress_is_measured_even_on_a_tick_that_writes_nothing(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """What the plant did is a physical fact, and stays true whatever we may write.

    Accruing only on the writing paths would lose energy from the totals on exactly
    the ticks a reader most wants explained.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45))
    coordinator._quarter_sampled_at = None

    # Ownership is spoiled, so the tick refuses -- but it still samples.
    hass.states.async_set(BOOLEAN_EXECUTION_OWNER, "off")
    await hass.async_block_till_done()
    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    assert coordinator._quarter_sampled_at == local(NORMAL, 10, 46)


async def test_the_measured_totals_are_monotonic(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """Every increment is ``max(0, ...) * dt``, so a total can never fall."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45))

    seen: list[tuple[float, float]] = []
    moment = local(NORMAL, 10, 45)
    for _ in range(8):
        coordinator._accrue_quarter_progress(moment)
        seen.append(
            (coordinator._quarter_battery_kwh, coordinator._quarter_grid_import_kwh)
        )
        moment += timedelta(seconds=60)

    for earlier, later in pairwise(seen):
        assert later[0] >= earlier[0] - 1e-12
        assert later[1] >= earlier[1] - 1e-12


# == 5. the history is bounded, and the surface is never widened ===========


async def test_the_completed_quarter_history_is_bounded(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """A ring, so a long day cannot grow the diagnostics payload without limit."""
    from custom_components.alpha_ems_manager.const import (
        MAX_COMPLETED_QUARTERS_REPORTED,
    )

    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    for step in range(MAX_COMPLETED_QUARTERS_REPORTED + 6):
        install(coordinator, quarter_at(10, 45) if step % 2 else quarter_at(11, 0))
        coordinator._record_completed_quarter(coordinator._quarter, QUARTER_END_EXPIRED)

    assert len(coordinator._completed_quarters) == MAX_COMPLETED_QUARTERS_REPORTED


async def test_a_quarter_refresh_never_writes_a_helper_family(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**Invariant 11.** The Dispatch surface is the only actuator, still.

    The trap the ``net_export -> ACTION_DISCHARGE`` mapping created is that an
    export command would fall into the advisory branch and be armed on Force
    Discharging. Asserted on what actually reached the wire.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=2.0, authorised=2.0))
    live_surface.calls.clear()

    await step_once(hass, coordinator, live_surface, hour=10, minute=46)

    written = {call.data["entity_id"] for call in live_surface.calls}
    assert not written & set(DISCHARGE_FAMILY.entities), written


async def test_both_write_paths_are_inside_the_one_lock(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**Invariant 12**, re-asserted with the quarter runtime in place.

    A correction landing in the middle of an arm would arm a dispatch against
    half-written values, so the tick and the quarter sequence share one lock.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=2.0, authorised=2.0))

    # A held lock makes the tick skip rather than queue: a correction computed while
    # an arm is in progress describes a world that has already moved.
    async with coordinator._execution_lock:
        live_surface.calls.clear()
        await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    assert live_surface.calls == []
    assert coordinator._last_tick_reason == TICK_SKIPPED_LOCK_HELD
