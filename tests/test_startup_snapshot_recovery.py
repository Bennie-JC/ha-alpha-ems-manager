"""What is read during setup is provisional, and must not stand for a quarter hour.

The same live diagnostics download that exposed the Solcast defect showed two more
things that looked like bugs and had one cause between them:

    sources.battery_soc      exists, reads 96.0 %
    battery_plan.available   false
    battery_plan.reason      missing_soc
    inputs.soc_percent       null

    sources.pv_power                exists, reads 141 W
    pv.actual_today.intervals_recorded  0

The first is a real defect and is fixed here. The second is expected, and this
file proves which is which rather than asserting it.

The mechanism: Alpha EMS takes its first refresh during its own setup, and
refreshes are then driven by the quarter-hour tick rather than an interval. At
Home Assistant startup the AlphaESS Modbus sensors have not necessarily published
yet, so that refresh legitimately sees nothing -- and the resulting snapshot stood
for up to fifteen minutes, printed in diagnostics beside source blocks that are
read live. Hence a plan reporting a missing state of charge next to a reading of
96 %.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import BATTERY_POWER, BATTERY_SOC, PV_POWER, set_sensor
from .forecast_helpers import history_before, local, refresh_at, seed
from .test_init import START, advance, setup_at

#: The reason the live installation reported.
MISSING_SOC = "missing_soc"


# -- the battery plan --------------------------------------------------------


async def test_a_state_of_charge_that_has_not_published_yet_gives_no_plan(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """The provisional reading, and why it is correct in itself.

    With no state of charge there is nothing to apply the model to, so refusing to
    plan is right. The defect was never this refusal -- it was that the refusal
    could not be revisited for fifteen minutes.
    """
    await setup_at(hass, freezer, mock_config_entry, START)
    hass.states.async_remove(BATTERY_SOC)
    coordinator = mock_config_entry.runtime_data

    seed(coordinator, history_before(START.date()))
    await refresh_at(coordinator, local(START.date(), 12, 5))

    plan = coordinator.battery_plan
    assert plan is not None
    assert plan.unavailable_reason == MISSING_SOC
    assert plan.inputs.soc_percent is None


async def test_the_plan_recovers_on_the_next_refresh_without_a_restart(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """The fix: a source that publishes late is picked up, not waited out.

    Reproduces the live ordering -- Alpha EMS up, the Modbus sensors not yet
    publishing -- and then lets them arrive.
    """
    await setup_at(hass, freezer, mock_config_entry, START)
    hass.states.async_remove(BATTERY_SOC)
    hass.states.async_remove(BATTERY_POWER)
    coordinator = mock_config_entry.runtime_data

    seed(coordinator, history_before(START.date()))
    await refresh_at(coordinator, local(START.date(), 12, 5))
    assert coordinator.battery_plan.unavailable_reason == MISSING_SOC

    # The inverter's sensors finish publishing.
    set_sensor(hass, BATTERY_SOC, 96, "%", "battery")
    set_sensor(hass, BATTERY_POWER, -664, "W", "power")

    await refresh_at(coordinator, local(START.date(), 12, 20))

    plan = coordinator.battery_plan
    assert plan.unavailable_reason is None
    assert plan.inputs.soc_percent == 96.0
    assert plan.inputs.battery_power_w == 664.0
    assert plan.decision.decided is True


async def test_home_assistant_starting_is_what_makes_the_recovery_prompt(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """Without this the recovery waits for the next quarter-hour boundary.

    A refresh is requested once Home Assistant reports itself started, which is
    after every integration has had its chance to load and publish. That is the
    difference between a plan that recovers in seconds and one that recovers in up
    to fifteen minutes -- and, for the user, between an install that works and one
    that looks broken.
    """
    await setup_at(hass, freezer, mock_config_entry, START)
    hass.states.async_remove(BATTERY_SOC)
    coordinator = mock_config_entry.runtime_data
    seed(coordinator, history_before(START.date()))
    await refresh_at(coordinator, local(START.date(), 12, 5))
    assert coordinator.battery_plan.unavailable_reason == MISSING_SOC

    set_sensor(hass, BATTERY_SOC, 96, "%", "battery")
    coordinator._handle_hass_started(hass)
    await hass.async_block_till_done()

    assert coordinator.battery_plan.inputs.soc_percent == 96.0


async def test_a_configured_source_with_a_valid_reading_is_never_dropped(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """The plain case the live report seemed to contradict.

    Both entities present with valid numeric states, refreshed after setup: the
    plan must receive them. If this ever failed, the defect really would be in the
    source resolution rather than in startup ordering.
    """
    await setup_at(hass, freezer, mock_config_entry, START)
    set_sensor(hass, BATTERY_SOC, 96, "%", "battery")
    set_sensor(hass, BATTERY_POWER, -664, "W", "power")
    coordinator = mock_config_entry.runtime_data

    seed(coordinator, history_before(START.date()))
    await refresh_at(coordinator, local(START.date(), 12, 5))

    plan = coordinator.battery_plan
    assert plan.inputs.soc_percent == 96.0
    assert plan.inputs.battery_power_w == 664.0
    assert plan.unavailable_reason is None


async def test_the_source_block_and_the_plan_now_describe_the_same_instant(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """What made the live download so hard to read.

    ``sources.battery_soc`` is probed when diagnostics are generated, while the
    plan is a snapshot from the last refresh. The two can still differ -- a
    snapshot is a snapshot -- but the payload now says when the snapshot was taken,
    so the reader is not left to conclude the integration is contradicting itself.
    """
    from custom_components.alpha_ems_manager.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    await setup_at(hass, freezer, mock_config_entry, START)
    set_sensor(hass, BATTERY_SOC, 96, "%", "battery")
    coordinator = mock_config_entry.runtime_data
    seed(coordinator, history_before(START.date()))
    await refresh_at(coordinator, local(START.date(), 12, 5))

    payload = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert payload["sources"]["battery_soc"]["state"] == "96"
    assert payload["battery_plan"]["inputs"]["soc_percent"] == 96.0
    assert payload["pv"]["last_refresh_at"] is not None


# -- actual PV evidence ------------------------------------------------------


async def test_no_pv_evidence_before_a_full_quarter_has_elapsed(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """Expected, not a defect. A part-observed quarter cannot be recorded.

    This is the state the live download caught: a PV sensor reading 141 W and no
    intervals recorded, moments after a restart. Guessing at the unobserved
    remainder of the open quarter would fabricate generation across the downtime.
    """
    set_sensor(hass, PV_POWER, 141, "W", "power")
    # Seven and a half minutes into the quarter, so it can never be fully covered.
    await setup_at(hass, freezer, mock_config_entry, START.replace(minute=7, second=30))
    set_sensor(hass, PV_POWER, 141, "W", "power")
    coordinator = mock_config_entry.runtime_data

    await advance(hass, freezer, 300)

    assert START.date() not in coordinator.store.days
    assert coordinator.open_pv_coverage is not None
    assert coordinator.open_pv_coverage < 1.0


async def test_pv_evidence_is_recorded_once_a_full_quarter_completes(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """The case that must work, and the one that proves the recorder attached.

    4 kW held across the 10:00 quarter is 1 kWh at chronological index 40. If the
    accumulator had failed to attach after an upgrade, this is where it would show.
    """
    set_sensor(hass, PV_POWER, 4000, "W", "power")
    await setup_at(hass, freezer, mock_config_entry, START)
    set_sensor(hass, PV_POWER, 4000, "W", "power")
    coordinator = mock_config_entry.runtime_data

    await advance(hass, freezer, 960)

    record = coordinator.store.days[START.date()]
    assert record.pv[40] == pytest.approx(1.0, rel=1e-3)
    assert record.pv_sample_count == 1
    assert coordinator.open_pv_coverage is not None


async def test_the_pv_recorder_survives_a_reload(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """An upgrade is a reload. The accumulator must be rebuilt, not lost.

    The in-flight quarter is deliberately dropped rather than restored, because a
    partly observed quarter cannot reach the coverage threshold and guessing at the
    rest would invent generation across the gap.
    """
    set_sensor(hass, PV_POWER, 4000, "W", "power")
    await setup_at(hass, freezer, mock_config_entry, START)

    for _ in range(2):
        await hass.config_entries.async_reload(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    set_sensor(hass, PV_POWER, 4000, "W", "power")
    coordinator = mock_config_entry.runtime_data
    await advance(hass, freezer, 960)

    record = coordinator.store.days[START.date()]
    assert record.pv[40] == pytest.approx(1.0, rel=1e-3)
    # Not a multiple of it: three accumulators did not double-count.
    assert record.measured[40] == pytest.approx(0.5, rel=1e-3)
