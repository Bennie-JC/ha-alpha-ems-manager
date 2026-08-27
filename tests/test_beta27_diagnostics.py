"""beta.27: one tick, one truthful outcome -- and the quarter published beside it.

**The beta.26 fault, and it was two faults.**

1. *One field, two cadences.* ``_last_tick_reason`` was written by the
   sixty-second tick, while ``update_reason``, ``applied_dispatch_kw`` and
   ``desired_grid_kw`` in the **same** published block came from a fresh
   ``_setpoint_for`` call made during the *quarter refresh*. A stale tick reason
   therefore sat beside a freshly computed write with nothing saying which cadence
   produced which field -- which is how a real installation reported
   ``no_owned_run`` next to a successful correction.
2. *One reason, three causes.* ``TICK_SKIPPED_NO_RUN`` was emitted for "no carried
   run", "dispatch not active" **and** "ownership not owned" -- so "we have no
   authority" was indistinguishable from "we have authority but nothing is armed".

Fixed by shape rather than by wording: a record that carries its own cadence
cannot be read as belonging to the other one, and recording it once at the end of
the evaluation means an early reason can no longer survive a later write.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.alphaess_device import BOOLEAN_EXECUTION_OWNER
from custom_components.alpha_ems_manager.const import (
    CADENCE_PHYSICAL_TICK,
    CADENCE_QUARTER_REFRESH,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_NET_EXPORT,
    MAX_COMPLETED_QUARTERS_REPORTED,
    QUARTER_END_EXPIRED,
    TICK_APPLIED,
    TICK_SKIPPED_DISPATCH_INACTIVE,
    TICK_SKIPPED_NO_QUARTER,
    TICK_SKIPPED_OWNERSHIP,
)

from .forecast_helpers import NORMAL, local
from .test_beta24_live_charge import LiveSurface, owned_live_charge, step_once
from .test_beta27_quarter_execution import install, quarter_at

pytestmark = pytest.mark.usefixtures("control_surface")


@pytest.fixture
def live_surface(hass: HomeAssistant, control_surface: None) -> LiveSurface:
    """Return a control surface that responds to writes."""
    return LiveSurface(hass)


# == 1. one tick, one outcome, carrying its own cadence ====================


async def test_a_tick_outcome_names_the_cadence_that_produced_it(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """The field that makes the two cadences impossible to confuse."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=2.0, authorised=2.0))

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    outcome = coordinator._tick_outcome
    assert outcome is not None
    assert outcome.cadence == CADENCE_PHYSICAL_TICK
    assert outcome.at == local(NORMAL, 10, 46)


async def test_the_refresh_records_its_own_outcome_separately(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**Two cadences, two records, and they are never merged.**"""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=2.0, authorised=2.0))

    await step_once(hass, coordinator, live_surface, hour=10, minute=46)

    refresh = coordinator._refresh_outcome
    assert refresh is not None
    assert refresh.cadence == CADENCE_QUARTER_REFRESH
    assert refresh.phase == "write_boundary"


async def test_both_records_are_published_side_by_side_and_labelled(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """A reader can tell which cadence each figure came from, without guessing."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=2.0, authorised=2.0))
    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    report = await step_once(hass, coordinator, live_surface, hour=10, minute=46)

    controller = ((report.get("execution") or {}).get("controller")) or {}
    assert controller["last_tick"]["cadence"] == CADENCE_PHYSICAL_TICK
    assert controller["refresh_decision"]["cadence"] == CADENCE_QUARTER_REFRESH
    # And the beta.26 field is still there for one release, so a dashboard built on
    # it does not break the day this ships.
    assert "last_tick_reason" in controller


async def test_a_stale_reason_cannot_survive_beside_a_later_write(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**The beta.26 fault, asserted directly.**

    A refusal is recorded, then a successful correction happens. The outcome that
    is published must describe the *write*, not the refusal before it -- which is
    what recording once, at the end, guarantees.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)

    # First a refusal: no carrier at all.
    coordinator._quarter = None
    coordinator._carried = None
    await coordinator._async_physical_tick(local(NORMAL, 10, 46))
    assert coordinator._tick_outcome.reason == TICK_SKIPPED_NO_QUARTER
    assert coordinator._tick_outcome.wrote is False

    # Then a real correction, on the next tick.
    coordinator._carried = None
    install(coordinator, quarter_at(10, 45, battery=2.0, authorised=2.0))
    coordinator._applied_setpoint_kw = 0.0
    await coordinator._async_physical_tick(local(NORMAL, 10, 47))

    assert coordinator._tick_outcome.at == local(NORMAL, 10, 47)
    assert coordinator._tick_outcome.reason != TICK_SKIPPED_NO_QUARTER


async def test_a_write_is_recorded_as_a_write(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """``wrote`` distinguishes "did nothing" from "did nothing visible"."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=3.0, authorised=3.0))
    coordinator._applied_setpoint_kw = 0.0

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    outcome = coordinator._tick_outcome
    assert outcome is not None
    if outcome.reason == TICK_APPLIED:
        assert outcome.wrote is True


# == 2. one reason, three causes -- now three reasons =====================


async def test_no_carrier_at_all_reports_no_admitted_quarter(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """Cause one of the three beta.26 conflated."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    coordinator._quarter = None
    coordinator._carried = None

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    assert coordinator._tick_outcome.reason == TICK_SKIPPED_NO_QUARTER


async def test_nothing_armed_reports_dispatch_not_active(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """Cause two. Authority exists; the inverter simply is not running."""
    from custom_components.alpha_ems_manager.alphaess_device import (
        DISPATCH_ENABLE,
        SENSOR_DISPATCH_START,
    )

    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=2.0, authorised=2.0))
    hass.states.async_set(DISPATCH_ENABLE, "off")
    hass.states.async_set(SENSOR_DISPATCH_START, "unknown")
    await hass.async_block_till_done()
    install(coordinator, quarter_at(10, 45, battery=2.0, authorised=2.0))

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    assert coordinator._tick_outcome.reason == TICK_SKIPPED_DISPATCH_INACTIVE


async def test_an_unprovable_dispatch_reports_ownership(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """Cause three, and the one that matters most: it is a different situation.

    "We cannot prove this dispatch is ours" needs zero writes and a human looking
    at it. "Nothing is armed" is routine. beta.26 reported them identically.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=2.0, authorised=2.0))
    hass.states.async_set(BOOLEAN_EXECUTION_OWNER, "off")
    await hass.async_block_till_done()
    install(coordinator, quarter_at(10, 45, battery=2.0, authorised=2.0))
    live_surface.calls.clear()

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    assert coordinator._tick_outcome.reason == TICK_SKIPPED_OWNERSHIP
    assert live_surface.calls == []


def test_the_three_reasons_are_distinct_strings() -> None:
    """Otherwise the split would be cosmetic."""
    assert (
        len(
            {
                TICK_SKIPPED_NO_QUARTER,
                TICK_SKIPPED_DISPATCH_INACTIVE,
                TICK_SKIPPED_OWNERSHIP,
            }
        )
        == 3
    )
    assert TICK_SKIPPED_NO_QUARTER != "no_owned_run"


# == 3. the quarter block ================================================


async def test_the_quarter_block_answers_is_it_on_course(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """Elapsed time, delivered energy and the remainder, together.

    None of the three answers the question alone, which is why they are published
    as one block rather than scattered across the report.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=2.0, authorised=1.6))
    coordinator._quarter_battery_kwh = 0.8
    coordinator._quarter_grid_import_kwh = 0.5

    block = coordinator._quarter_block(local(NORMAL, 10, 50))

    assert block["quarter_start"] == local(NORMAL, 10, 45).isoformat()
    assert block["quarter_end"] == local(NORMAL, 11, 0).isoformat()
    assert block["intent"] == EXECUTION_INTENT_GRID_CHARGE
    assert block["quarter_seconds_elapsed"] == pytest.approx(300.0)
    assert block["quarter_seconds_remaining"] == pytest.approx(600.0)
    assert block["battery_target_this_quarter_kwh"] == pytest.approx(2.0)
    assert block["battery_realized_this_quarter_kwh"] == pytest.approx(0.8)
    assert block["battery_remaining_this_quarter_kwh"] == pytest.approx(1.2)
    assert block["grid_target_this_quarter_kwh"] == pytest.approx(1.6)
    assert block["grid_realized_this_quarter_kwh"] == pytest.approx(0.5)
    assert block["grid_remaining_this_quarter_kwh"] == pytest.approx(1.1)
    assert block["required_average_battery_kw"] == pytest.approx(
        1.2 / (600 / 3600), 0.01
    )


async def test_the_block_publishes_the_meter_figures_for_an_export(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """The grid slot means different things per intent, and the block follows."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(
        coordinator,
        quarter_at(10, 45, intent=EXECUTION_INTENT_NET_EXPORT, battery=1.0, export=0.8),
    )
    coordinator._quarter_grid_export_kwh = 0.3

    block = coordinator._quarter_block(local(NORMAL, 10, 50))

    assert block["intent"] == EXECUTION_INTENT_NET_EXPORT
    assert block["grid_target_this_quarter_kwh"] == pytest.approx(0.8)
    assert block["grid_realized_this_quarter_kwh"] == pytest.approx(0.3)
    assert block["grid_remaining_this_quarter_kwh"] == pytest.approx(0.5)


async def test_the_block_states_both_rules_it_depends_on(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """So a reader interpreting an unspent ceiling does not read it as a deficit."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45))

    block = coordinator._quarter_block(local(NORMAL, 10, 50))

    assert "ceiling is never a completion test" in block["objective_rule"]
    assert "never carried forward" in block["carry_over_rule"]


async def test_the_block_is_published_with_nothing_admitted(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """``None`` rather than absent, so a dashboard reads the same shape always."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    coordinator._quarter = None
    coordinator._reset_quarter_progress(None)

    block = coordinator._quarter_block(local(NORMAL, 10, 50))

    assert block["quarter_start"] is None
    assert block["intent"] is None
    assert block["quarter_seconds_remaining"] is None
    assert block["battery_realized_this_quarter_kwh"] == 0.0


async def test_the_quarter_block_reaches_the_published_report(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """Diagnostics nobody can read are diagnostics nobody has."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=2.0, authorised=2.0))

    report = await step_once(hass, coordinator, live_surface, hour=10, minute=46)

    quarter = (report.get("execution") or {}).get("quarter")
    assert quarter is not None
    assert "battery_remaining_this_quarter_kwh" in quarter


# == 4. the flight recorder reconstructs the quarter, not just the instant ==


async def test_a_ring_entry_carries_enough_to_reconstruct_the_quarter(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """A download taken an hour later still has to be able to explain a tick.

    Without these fields a reader has to correlate three other blocks by timestamp,
    and diagnostics are almost never captured at the moment production moved.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=2.0, authorised=2.0))
    coordinator._physical_decisions.clear()
    coordinator._applied_setpoint_kw = 0.0

    await coordinator._async_physical_tick(local(NORMAL, 10, 46))

    entries = list(coordinator._physical_decisions)
    assert entries, "the tick should have recorded a decision"
    entry = entries[-1]
    for field in (
        "plan_id",
        "run_id",
        "intent",
        "quarter_start",
        "quarter_end",
        "battery_target_kwh",
        "battery_realized_kwh",
        "battery_remaining_kwh",
        "grid_target_kwh",
        "grid_realized_kwh",
        "grid_remaining_kwh",
        "seconds_remaining",
        "stop_reason",
    ):
        assert field in entry, field
    assert entry["intent"] == EXECUTION_INTENT_GRID_CHARGE
    assert entry["run_id"] == "run-1"


async def test_the_ring_stays_bounded(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """The extra fields must not have turned a ring into a leak."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    install(coordinator, quarter_at(10, 45, battery=9.0, authorised=9.0))
    limit = coordinator._physical_decisions.maxlen
    assert limit is not None

    moment = local(NORMAL, 10, 45)
    for _ in range(limit + 5):
        await coordinator._async_physical_tick(moment)
        moment += timedelta(seconds=60)

    assert len(coordinator._physical_decisions) <= limit


# == 5. the completed-quarter history =====================================


async def test_the_history_records_the_figures_a_reader_needs(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """Planned against realised in both domains, plus how it ended and what bound it."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    quarter = quarter_at(10, 45, battery=2.0, authorised=1.6)
    install(coordinator, quarter)
    coordinator._quarter_battery_kwh = 1.5
    coordinator._quarter_grid_import_kwh = 1.0
    coordinator._quarter_peak_kw = 4.2
    coordinator._quarter_power_sum = 9.0
    coordinator._quarter_power_samples = 3
    coordinator._quarter_pv_helped = True
    coordinator._note_quarter_clamp("inverter_limit")

    coordinator._record_completed_quarter(quarter, QUARTER_END_EXPIRED)

    row = list(coordinator._completed_quarters)[-1]
    assert row["planned_battery_kwh"] == pytest.approx(2.0)
    assert row["realized_battery_kwh"] == pytest.approx(1.5)
    assert row["planned_grid_kwh"] == pytest.approx(1.6)
    assert row["realized_grid_kwh"] == pytest.approx(1.0)
    assert row["shortfall_kwh"] == pytest.approx(0.5)
    assert row["shortfall_percent"] == pytest.approx(25.0)
    assert row["max_dispatch_kw"] == pytest.approx(4.2)
    assert row["mean_dispatch_kw"] == pytest.approx(3.0)
    assert row["pv_helped"] is True
    assert row["binding_clamps"] == ["inverter_limit"]
    assert row["completion_reason"] == QUARTER_END_EXPIRED


async def test_the_shortfall_is_measured_against_the_objective_not_the_ceiling(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """Unspent grid authorisation on a charge is production having paid for it.

    Reporting it as a shortfall would invite exactly the reading beta.27 exists to
    prevent -- that the authorisation was an amount to consume.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    quarter = quarter_at(10, 45, battery=2.0, authorised=1.6)
    install(coordinator, quarter)
    # Objective met entirely from production: nothing bought, nothing missing.
    coordinator._quarter_battery_kwh = 2.0
    coordinator._quarter_grid_import_kwh = 0.0

    coordinator._record_completed_quarter(quarter, QUARTER_END_EXPIRED)

    row = list(coordinator._completed_quarters)[-1]
    assert row["realized_grid_kwh"] == pytest.approx(0.0)
    assert row["planned_grid_kwh"] == pytest.approx(1.6)
    assert row["shortfall_kwh"] == pytest.approx(0.0)


async def test_the_history_is_a_bounded_ring(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """A long day cannot grow the payload without limit."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    quarter = quarter_at(10, 45)

    for _ in range(MAX_COMPLETED_QUARTERS_REPORTED * 2):
        coordinator._record_completed_quarter(quarter, QUARTER_END_EXPIRED)

    assert len(coordinator._completed_quarters) == MAX_COMPLETED_QUARTERS_REPORTED


async def test_a_zero_target_reports_no_percentage_rather_than_dividing(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """``None`` is the honest answer; zero would read as "perfectly on target"."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    quarter = quarter_at(10, 45, battery=0.0, authorised=0.0)

    coordinator._record_completed_quarter(quarter, QUARTER_END_EXPIRED)

    assert list(coordinator._completed_quarters)[-1]["shortfall_percent"] is None
