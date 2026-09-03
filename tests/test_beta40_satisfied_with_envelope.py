"""beta.40 Gate 4: a satisfied row goes on storing free production.

**The path this release exists for, and the one beta.36 measured the hardware on.**

A row whose objective is met takes one of two outcomes in beta.39: hold at zero, or
end the quarter. Both stop the battery -- and Mode 2 at 0 kW was *measured* on the
reference inverter to be a **total** hold that suppresses charging as well as
discharging. So the satisfied row is precisely where free production is guaranteed
to leak, and on 2026-08-31 that was 1.3 kW of it going to the meter with the pack
half empty.

beta.40 adds a third outcome. A satisfied objective, with Stage A's retention
verdict on the row and measured surplus standing there, falls through to the
ordinary setpoint path so the absorption branch can command the surplus. Nothing
else changes: the target-reached latch stays set, so the moment production goes the
next tick takes the beta.39 path and the row is satisfied and held exactly as
before. No new lifecycle state and no recovery path to invent.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.const import (
    CONTROL_MIN_POWER_KW,
    HOLD_REASON_QUARTER_SATISFIED,
    SHORTFALL_ABSORBING_FREE_PV,
    TICK_STOPPED_TARGET_REACHED,
)

from .beta40_trace import HOUSE_KW, PV_KW, SURPLUS_KW
from .conftest import BATTERY_POWER, GRID_POWER, HOUSE_LOAD, PV_POWER, set_sensor
from .forecast_helpers import NORMAL, local
from .test_beta24_live_charge import LiveSurface, owned_live_charge
from .test_beta27_quarter_execution import install, quarter_at

pytestmark = pytest.mark.usefixtures("control_surface")

DISPATCH_POWER = "input_number.alphaess_helper_dispatch_power"


@pytest.fixture
def live_surface(hass: HomeAssistant, control_surface: None) -> LiveSurface:
    """Return a control surface that responds to writes."""
    return LiveSurface(hass)


def install_row(
    coordinator,
    *,
    battery: float = 0.28,
    authorised: bool = True,
    until: float | None = 99.0,
):
    """Admit one row carrying Stage A's retention verdict.

    **The verdict lives on the row and not on the quarter**, which is why this does
    not simply patch the ``CarriedQuarter``: ``_refresh_executing_quarter`` derives
    the executing quarter from the frozen schedule at the top of every tick, so a
    field set on the quarter alone is overwritten immediately. That is the beta.30
    property that makes a skipped boundary unrepresentable, and beta.40 inherits it
    -- the row is the single source of the authority.
    """
    from dataclasses import replace

    quarter = quarter_at(10, 45, battery=battery, authorised=0.04)
    install(coordinator, quarter)
    plan = coordinator._plan
    assert plan is not None
    coordinator._plan = replace(
        plan,
        rows=tuple(
            replace(row, retention_authorised=authorised, retention_until_dc_kwh=until)
            for row in plan.rows
        ),
    )
    coordinator._quarter = replace(
        quarter, retention_authorised=authorised, retention_until_dc_kwh=until
    )


def site(hass, *, pv_kw: float, house_kw: float, battery_charge_kw: float) -> None:
    """Point the live flows at a coherent site with production to spare.

    Coherent in the balance layer's own terms -- ``pv + import == load + charge`` --
    because an incoherent snapshot makes the tick skip accrual entirely.
    """
    grid_kw = house_kw + battery_charge_kw - pv_kw
    set_sensor(hass, PV_POWER, pv_kw * 1000.0, "W", "power")
    set_sensor(hass, HOUSE_LOAD, house_kw * 1000.0, "W", "power")
    set_sensor(hass, BATTERY_POWER, -battery_charge_kw * 1000.0, "W", "power")
    set_sensor(hass, GRID_POWER, grid_kw * 1000.0, "W", "power")


def satisfy(coordinator, *, total: float) -> None:
    """Put the open row past its objective, as the accrual would have."""
    coordinator._quarter_battery_kwh = total


def recorded_clamps(coordinator) -> set[str]:
    """Return the clamps of the row just completed, or the open one's.

    **Ending a quarter resets the live clamp set**, so a test that read
    ``_quarter_clamps`` after a tick that closed the row would find it empty and
    pass on anything. ``_record_completed_quarter`` captures them first, which is
    the only place the answer survives.
    """
    if coordinator._completed_quarters:
        return set(coordinator._completed_quarters[-1]["binding_clamps"])
    return set(coordinator._quarter_clamps)


def power_writes(live_surface) -> list[float]:
    """Return every dispatch power written since the last clear."""
    return [
        call.data["value"]
        for call in live_surface.calls
        if call.data.get("entity_id") == DISPATCH_POWER
    ]


# == 1. the third outcome ==================================================


async def test_a_satisfied_row_with_surplus_keeps_charging(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**The gate.** No zero is written and the row does not end.

    *Mutation: route the absorbing branch through ``_async_hold_at_zero`` and this
    fails on the written value.*
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install_row(coordinator)
    site(hass, pv_kw=PV_KW, house_kw=HOUSE_KW, battery_charge_kw=1.49)
    satisfy(coordinator, total=0.28)
    live_surface.calls.clear()

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    # It commanded a charge, and a substantial one.
    written = power_writes(live_surface)
    assert written, "the tick wrote no power at all"
    assert all(value < 0.0 for value in written), written
    assert abs(written[-1]) >= CONTROL_MIN_POWER_KW
    assert abs(written[-1]) == pytest.approx(2.5, abs=0.1)
    # The row is still open and the campaign is still running.
    assert coordinator._quarter is not None
    assert coordinator._last_tick_reason != TICK_STOPPED_TARGET_REACHED


async def test_the_absorbing_row_is_recorded_as_absorbing(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """A reader has to be able to see that the row did more than its objective."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install_row(coordinator)
    site(hass, pv_kw=PV_KW, house_kw=HOUSE_KW, battery_charge_kw=1.49)
    satisfy(coordinator, total=0.28)

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    assert SHORTFALL_ABSORBING_FREE_PV in coordinator._quarter_clamps


async def test_the_latch_is_still_set_while_absorbing(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**Why there is no new lifecycle state.**

    The row *is* satisfied and says so; it is simply not finished. The latch is what
    makes the return to the beta.39 path automatic.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install_row(coordinator)
    site(hass, pv_kw=PV_KW, house_kw=HOUSE_KW, battery_charge_kw=1.49)
    satisfy(coordinator, total=0.28)

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    assert coordinator._quarter_target_reached_at is not None


# == 2. it stops when there is nothing left to store =====================


async def test_the_beta39_path_resumes_when_the_surplus_goes(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**The recovery, and it needed no code.**

    Absorb on one tick, then the sun goes behind a cloud. The next tick finds the
    latch set and no surplus, takes the beta.39 satisfied path, and holds at zero --
    which is the right command for a row with nothing left to do.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install_row(coordinator)
    site(hass, pv_kw=PV_KW, house_kw=HOUSE_KW, battery_charge_kw=1.49)
    satisfy(coordinator, total=0.28)
    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    # The cloud.
    site(hass, pv_kw=0.2, house_kw=HOUSE_KW, battery_charge_kw=0.0)
    live_surface.calls.clear()
    await coordinator._async_physical_tick(local(NORMAL, 10, 47))

    written = power_writes(live_surface)
    assert written, "a satisfied row with nothing to store must command a rest"
    assert written[-1] == pytest.approx(0.0, abs=1e-9)


async def test_an_unauthorised_satisfied_row_holds_exactly_as_beta39(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**The regression gate.** No verdict, no third outcome.

    Same surplus, same met objective, Stage A refused: the row holds at zero, which
    is what beta.39 did with the identical inputs.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install_row(coordinator, authorised=False)
    site(hass, pv_kw=PV_KW, house_kw=HOUSE_KW, battery_charge_kw=1.49)
    satisfy(coordinator, total=0.28)
    live_surface.calls.clear()

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    written = power_writes(live_surface)
    assert written, "beta.39 commands a rest here"
    assert written[-1] == pytest.approx(0.0, abs=1e-9)


async def test_a_full_pack_holds_rather_than_absorbing(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """No overcharge, and no forced zero-export either.

    With no headroom the production genuinely has nowhere to go, so the row rests
    and the surplus goes to the meter -- which is correct, not a defect.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install_row(coordinator)
    site(hass, pv_kw=PV_KW, house_kw=HOUSE_KW, battery_charge_kw=0.0)
    satisfy(coordinator, total=0.28)
    # A pack with nothing left to give.
    set_sensor(hass, "sensor.alphaess_soc_battery", 100.0, "%", "battery")
    live_surface.calls.clear()

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    written = power_writes(live_surface)
    if written:
        assert abs(written[-1]) < CONTROL_MIN_POWER_KW, written


async def test_a_trickle_of_surplus_is_a_rest_and_not_a_write(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**The clause that stops the latch turning into a sub-resolution command.**

    The below-resolution hold is guarded on the latch, so once a row is satisfied it
    can no longer route a tiny figure to a rest. A surplus under the actuator's own
    0.2 kW minimum must therefore be refused *before* the fall-through, or the tick
    writes something the device cannot express.

    *Mutation: drop the ``>= CONTROL_MIN_POWER_KW`` clause from ``_absorption_live``
    and this fails.*
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install_row(coordinator)
    # 0.05 kW of surplus: real, and below what the helper step can command.
    site(hass, pv_kw=HOUSE_KW + 0.05, house_kw=HOUSE_KW, battery_charge_kw=0.0)
    satisfy(coordinator, total=0.28)
    live_surface.calls.clear()

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    written = power_writes(live_surface)
    assert written, "it must still command a rest"
    assert written[-1] == pytest.approx(0.0, abs=1e-9)


async def test_an_unreadable_production_sensor_earns_nothing(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """Fail-safe: absent is not a measurement, whatever the plan authorised."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install_row(coordinator)
    site(hass, pv_kw=PV_KW, house_kw=HOUSE_KW, battery_charge_kw=0.0)
    satisfy(coordinator, total=0.28)
    hass.states.async_remove(PV_POWER)
    live_surface.calls.clear()

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    assert coordinator._measured_pv_surplus_kw() is None
    written = power_writes(live_surface)
    if written:
        assert abs(written[-1]) < CONTROL_MIN_POWER_KW, written


# == 3. the refresh cadence must not undo the tick =======================


async def test_the_refresh_does_not_command_zero_over_a_live_absorption(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**``_quarter_is_satisfied`` is "satisfied AND not absorbing" for this reason.**

    The refresh path reads it to force a zero setpoint. Left as "satisfied", the
    quarter boundary would write a zero straight over the absorption the tick had
    just commanded -- undoing on the economic cadence exactly what the physical one
    achieved.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install_row(coordinator)
    site(hass, pv_kw=PV_KW, house_kw=HOUSE_KW, battery_charge_kw=1.49)
    satisfy(coordinator, total=0.28)
    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    # Satisfied, and absorbing, so not finished.
    assert coordinator._quarter_target_reached_at is not None
    assert coordinator._quarter_is_satisfied(local(NORMAL, 10, 46)) is False

    # And once the surplus goes it is finished, on the same reading.
    site(hass, pv_kw=0.1, house_kw=HOUSE_KW, battery_charge_kw=0.0)
    assert coordinator._quarter_is_satisfied(local(NORMAL, 10, 47)) is True


async def test_the_hold_reason_is_never_quarter_satisfied_while_absorbing(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """The hardware gate, stated as the token it turns on.

    ``quarter_satisfied`` is the reason that does not recover, and it commands the
    total hold. A row still storing free production must never carry it.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install_row(coordinator)
    site(hass, pv_kw=PV_KW, house_kw=HOUSE_KW, battery_charge_kw=1.49)
    satisfy(coordinator, total=0.28)

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    report = coordinator.control_report or {}
    boundary = ((report.get("execution") or {}).get("write_boundary")) or {}
    assert boundary.get("hold_reason") != HOLD_REASON_QUARTER_SATISFIED


# == 4. what absorbing does to the books ================================


async def test_absorbing_advances_the_absorbed_figure_and_not_the_objective(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """The split, end to end through the real accrual.

    Two ticks of measured charge on a row whose objective was already met: every
    kilowatt-hour of it lands in ``absorbed`` and none of it in ``objective``.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install_row(coordinator, battery=0.28)
    site(hass, pv_kw=PV_KW, house_kw=HOUSE_KW, battery_charge_kw=2.5)
    satisfy(coordinator, total=0.28)

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))
    await coordinator._async_physical_tick(local(NORMAL, 10, 47))

    total = coordinator._quarter_battery_kwh
    assert total > 0.28, "the accrual must have moved"
    assert coordinator._quarter_objective_kwh == pytest.approx(0.28, abs=1e-9)
    assert coordinator._quarter_absorbed_kwh == pytest.approx(total - 0.28, abs=1e-9)
    # And it bought nothing: the surplus covered the whole charge.
    assert SURPLUS_KW >= 2.5


async def test_a_trickle_is_not_recorded_as_absorption(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**The observable half of the resolution floor.**

    Both paths end in a rest, because the sub-resolution safety net catches what
    the floor lets through -- so asserting the written value cannot tell them
    apart. What differs is the *claim*: a row that took the absorbing branch says
    so, and a trickle is not absorption.

    *Mutation: drop the ``>= CONTROL_MIN_POWER_KW`` clause from
    ``_absorption_live`` and this fails.*
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install_row(coordinator)
    site(hass, pv_kw=HOUSE_KW + 0.05, house_kw=HOUSE_KW, battery_charge_kw=0.0)
    satisfy(coordinator, total=0.28)

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    assert SHORTFALL_ABSORBING_FREE_PV not in recorded_clamps(coordinator)


async def test_an_unauthorised_row_is_never_recorded_as_absorbing(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**Two independent gates, and this pins the outer one.**

    ``decide_charge`` refuses an unauthorised row on its own, so the written value
    is a rest either way and cannot distinguish them. The claim can: a row Stage A
    did not authorise must not report having absorbed anything.

    *Mutation: remove the ``retention_authorised`` check from ``_absorption_live``
    and this fails.*
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install_row(coordinator, authorised=False)
    site(hass, pv_kw=PV_KW, house_kw=HOUSE_KW, battery_charge_kw=1.49)
    satisfy(coordinator, total=0.28)

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    assert SHORTFALL_ABSORBING_FREE_PV not in recorded_clamps(coordinator)


async def test_a_rest_is_commanded_once_and_not_twice(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**The early return, and what its absence actually costs.**

    A satisfied row with nothing to absorb and something still after it *holds*:
    ownership, claim and schedule all stay and the power goes to zero, once. Falling
    through to the setpoint path afterwards reaches the sub-resolution net and
    commands the same rest a second time -- which is a duplicated write to flash-
    adjacent helpers on every tick of a satisfied row.

    *Mutation: drop the ``return`` after ``_async_finish_satisfied_row`` and this
    fails on the count.*
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install_row(coordinator)
    # Two rows, so this one is not the last and the row holds rather than ending.
    from dataclasses import replace

    plan = coordinator._plan
    assert plan is not None
    first = plan.rows[0]
    coordinator._plan = replace(
        plan,
        rows=(
            first,
            replace(
                first,
                start=first.end,
                end=first.end + (first.end - first.start),
            ),
        ),
    )
    site(hass, pv_kw=0.0, house_kw=HOUSE_KW, battery_charge_kw=0.0)
    satisfy(coordinator, total=0.28)
    live_surface.calls.clear()

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    written = power_writes(live_surface)
    assert written == pytest.approx([0.0]), written


async def test_an_absorbing_row_clamped_to_nothing_is_finished_not_left_open(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**The sub-resolution net's own case, which the early return cannot reach.**

    ``_absorption_live`` says yes on evidence it can see: the verdict, measured
    surplus, and a pack with room. It cannot see the clamps. So an authorised row
    with real surplus can still arrive at the write boundary asking for less than
    the actuator's 0.1 kW step -- a nearly-full pack under ``max_end_energy_kwh``,
    or an inverter already committed elsewhere.

    The latch has by then disabled the below-resolution hold, so without the net the
    row would sit open writing a figure the device cannot express. It must rest
    instead, on the beta.39 path.

    *Mutation: invert the latch condition on the sub-resolution net and this fails.*
    """
    from custom_components.alpha_ems_manager.dispatch import ChargeLimits

    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install_row(coordinator)
    site(hass, pv_kw=PV_KW, house_kw=HOUSE_KW, battery_charge_kw=0.0)
    satisfy(coordinator, total=0.28)
    # A clamp the live gate cannot see: an inverter with nothing left to give.
    coordinator._charge_limits = lambda *_a, **_k: ChargeLimits(inverter_kw=0.05)
    live_surface.calls.clear()

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    written = power_writes(live_surface)
    assert written, "it must command something rather than nothing"
    assert written[-1] == pytest.approx(0.0, abs=1e-9), written
    assert abs(written[-1]) < CONTROL_MIN_POWER_KW
    # **And the row is finished, which is the part the written value cannot show.**
    # Without the net the tick still writes a zero -- through the ordinary deadband
    # path -- and leaves the row open, asking for an inexpressible figure again on
    # every tick until the quarter runs out.
    assert coordinator._quarter is None, "the row must be closed, not left open"


# == 5. the pack's own ceiling, per tick =================================

SOC_ENTITY = "sensor.alphaess_soc_battery"


@pytest.mark.parametrize("soc", [95.0, 99.0, 99.5, 99.9, 100.0])
async def test_absorption_is_bounded_by_the_packs_own_room_at_every_soc(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface,
    soc: float,
) -> None:
    """**The per-tick energy clamp the audit asked for, measured live.**

    Before the beta.40 corrective there was none for a charge: ``headroom_kw`` is
    ``None`` unless Stage A published ``max_end_energy_kwh``, so at 99.9 % the
    branch commanded the full inverter limit and only the device cutoff and the
    pack's own management stopped it. Those are real protections and they are not
    software bounds.

    ``retention_remaining_kwh`` now carries the pack's room as well as the economic
    ceiling, differenced against a **live** state of charge, so the command is
    bounded on every tick by what the pack can actually take.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install_row(coordinator)
    # 10 kW of surplus against a small house: the strongest case there is.
    site(hass, pv_kw=10.8, house_kw=0.8, battery_charge_kw=0.0)
    set_sensor(hass, SOC_ENTITY, soc, "%", "battery")
    satisfy(coordinator, total=0.28)
    now = local(NORMAL, 10, 46)

    limits = coordinator.battery_plan.state.limits
    room_dc = max(
        0.0,
        limits.energy_for_soc(100.0) - limits.energy_for_soc(soc),
    )
    decision = coordinator._dispatch_setpoint(now)

    # Never more than the pack's remaining room, converted once.
    horizon_h = 90.0 / 3600.0
    assert (
        abs(decision.applied_kw) * horizon_h
        <= room_dc / limits.charge_efficiency + 1e-6
    )
    if soc >= 100.0:
        assert coordinator._absorption_live(coordinator._quarter_progress(now)) is False


async def test_a_stale_plan_state_cannot_widen_the_pack_bound(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**Why the bound is differenced against a live reading.**

    ``battery_plan.state`` is rebuilt on the economic cadence, so on the cadence
    that commands it can be a quarter of an hour old -- long enough at these powers
    to fill the room it still reports. Here the plan believes there is room and the
    pack is full; the live reading governs.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install_row(coordinator)
    site(hass, pv_kw=10.8, house_kw=0.8, battery_charge_kw=0.0)
    satisfy(coordinator, total=0.28)
    stale = coordinator.battery_plan.state.headroom_energy_kwh
    assert stale > 1.0, "the fixture must actually have stale room to report"

    set_sensor(hass, SOC_ENTITY, 100.0, "%", "battery")
    now = local(NORMAL, 10, 46)

    # The plan still says there is room; the live reading says there is none.
    assert coordinator.battery_plan.state.headroom_energy_kwh == pytest.approx(stale)
    assert coordinator._retainable_kwh(coordinator._quarter) == pytest.approx(0.0)
    assert coordinator._absorption_live(coordinator._quarter_progress(now)) is False


async def test_an_exhausted_economic_ceiling_stops_absorption_with_room_to_spare(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**The ceiling is not the pack, and this is the case that shows it.**

    Sun shining, pack half empty, verdict granted -- and absorption still stops,
    because past the published level the optimiser's own dual no longer clears the
    export price. Selling pays better from here, and the release exists to make
    that comparison rather than to fill the battery.

    *Mutation: let ``_absorption_live`` ignore the remaining retainable energy and
    this fails.*
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    # A ceiling already below where the pack stands: nothing left worth keeping.
    install_row(coordinator, until=0.5)
    site(hass, pv_kw=PV_KW, house_kw=HOUSE_KW, battery_charge_kw=0.0)
    set_sensor(hass, SOC_ENTITY, 50.0, "%", "battery")
    satisfy(coordinator, total=0.28)
    now = local(NORMAL, 10, 46)

    # The pack has plenty of room, and the economics say stop anyway.
    assert coordinator.battery_plan.state.headroom_energy_kwh > 1.0
    assert coordinator._retainable_kwh(coordinator._quarter) == pytest.approx(0.0)
    assert coordinator._absorption_live(coordinator._quarter_progress(now)) is False
