"""All four phases as one integration, with the control layer additive.

Each phase has its own suite proving it right in isolation, and the Phase-3 file
walks the chain up to the decision. This one continues it:

    ... -> Phase-3 decision -> control intent -> safety gate -> command plan
    -> execution authorization -> (nothing)

The rule that outranks everything else here: **Phase 4 is additive.** Nothing it
does may move a Phase-1, Phase-2 or Phase-3 figure, and the way that is shown is
the way the previous phase showed it -- drive the whole integration twice at the
same instant, once with the control layer running and once with it off, and
compare the earlier surfaces figure for figure.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.alphaess_device import PERMITTED_SERVICES
from custom_components.alpha_ems_manager.const import (
    ACTION_DISCHARGE,
    CONTROL_MODE_ACTIVE,
    CONTROL_MODE_OFF,
    CONTROL_MODE_SHADOW,
)
from custom_components.alpha_ems_manager.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .forecast_helpers import NORMAL, history_before, local, refresh_at, seed

pytestmark = pytest.mark.usefixtures("setup_integration")

DAY_ONE = NORMAL

#: Every entity that existed before Phase 4, in phase order.
EARLIER_ENTITIES = (
    "sensor.alpha_ems_expected_house_load_today",
    "sensor.alpha_ems_expected_house_load_tomorrow",
    "sensor.alpha_ems_learning_confidence",
    "sensor.alpha_ems_learning_days",
    "sensor.alpha_ems_forecast_error_yesterday",
    "sensor.alpha_ems_forecast_error_7_days",
    "sensor.alpha_ems_battery_recommendation",
    "sensor.alpha_ems_planned_battery_power",
    "sensor.alpha_ems_usable_battery_energy",
)

RECOMMENDATION = "sensor.alpha_ems_battery_recommendation"
CONTROL_STATE = "sensor.alpha_ems_control_state"


def surfaces(hass: HomeAssistant) -> dict[str, tuple[str, dict]]:
    """Return the state and attributes of every pre-Phase-4 entity."""
    captured: dict[str, tuple[str, dict]] = {}
    for entity_id in EARLIER_ENTITIES:
        state = hass.states.get(entity_id)
        assert state is not None, entity_id
        attributes = {
            key: value
            for key, value in state.attributes.items()
            if key != "friendly_name"
        }
        captured[entity_id] = (state.state, attributes)
    return captured


@pytest.fixture
def captured_calls(hass: HomeAssistant) -> list[ServiceCall]:
    """Capture any call to a service the control layer could make."""
    calls: list[ServiceCall] = []

    async def record(call: ServiceCall) -> None:
        calls.append(call)

    for domain, service in PERMITTED_SERVICES:
        hass.services.async_register(domain, service, record)
    return calls


async def drive(coordinator, hour: int = 12) -> None:
    """Give the model history and refresh at a fixed instant."""
    seed(coordinator, history_before(DAY_ONE))
    await refresh_at(coordinator, local(DAY_ONE, hour, 5))


async def set_mode(hass: HomeAssistant, mode: str) -> None:
    """Select a control mode through the real entity."""
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.alpha_ems_control_mode", "option": mode},
        blocking=True,
    )
    await hass.async_block_till_done()


# ===========================================================================
# Phase 4 is additive
# ===========================================================================


async def test_the_nine_earlier_entities_are_untouched_by_the_control_layer(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    control_surface: None,
) -> None:
    """Driven twice at the same instant: control off, then control active.

    Nothing but the control mode differs, so any change in the earlier nine
    entities would be the control layer reaching somewhere it must not.
    """
    coordinator = setup_integration.runtime_data

    await set_mode(hass, CONTROL_MODE_OFF)
    await drive(coordinator)
    quiet = surfaces(hass)
    assert hass.states.get(RECOMMENDATION).state == ACTION_DISCHARGE

    await set_mode(hass, CONTROL_MODE_ACTIVE)
    await drive(coordinator)

    assert surfaces(hass) == quiet


async def test_shadow_mode_changes_nothing_a_user_already_watched(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    control_surface: None,
) -> None:
    """The mode a user is asked to run for weeks must be invisible upstream."""
    coordinator = setup_integration.runtime_data

    await drive(coordinator)
    quiet = surfaces(hass)

    await set_mode(hass, CONTROL_MODE_SHADOW)
    await drive(coordinator)

    assert surfaces(hass) == quiet


async def test_a_missing_control_surface_changes_nothing_either(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The default state of any installation that never installed one.

    The control surface belongs to the user. Its absence is a capability finding,
    not a setup failure, and certainly not a reason for learning to stop.
    """
    coordinator = setup_integration.runtime_data

    await set_mode(hass, CONTROL_MODE_OFF)
    await drive(coordinator)
    quiet = surfaces(hass)

    await set_mode(hass, CONTROL_MODE_ACTIVE)
    await drive(coordinator)

    assert surfaces(hass) == quiet
    assert hass.states.get(CONTROL_STATE).state == "inhibited"


# ===========================================================================
# the chain hands over correctly
# ===========================================================================


async def test_the_decision_reaches_the_intent_without_being_re_derived(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    control_surface: None,
) -> None:
    """One authoritative chain, checked at the hand-off.

    The intent's energy is the decision's allowed energy, byte for byte, and the
    device power is that energy over the interval quantised downwards -- never a
    figure recomputed from the request the policy made.
    """
    coordinator = setup_integration.runtime_data
    await set_mode(hass, CONTROL_MODE_SHADOW)
    await drive(coordinator)

    plan = coordinator.battery_plan
    report = coordinator.control_report
    assert plan is not None and report is not None

    decision = plan.decision
    intent = report["intent"]
    command = report["command"]
    assert intent is not None and command is not None

    assert intent["action"] == decision.action
    assert command["allowed_energy_ac_kwh"] == pytest.approx(
        decision.allowed_energy_ac_kwh
    )
    assert command["commanded_energy_ac_kwh"] <= command["allowed_energy_ac_kwh"]
    assert command["power_kw"] <= decision.average_power_kw


async def test_the_policy_and_its_version_travel_all_the_way_through(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    control_surface: None,
) -> None:
    """So a later phase cannot pool commands made under different objectives."""
    coordinator = setup_integration.runtime_data
    await set_mode(hass, CONTROL_MODE_SHADOW)
    await drive(coordinator)

    decision = coordinator.battery_plan.decision
    intent = coordinator.control_report["intent"]

    assert intent["policy"] == decision.policy
    assert intent["policy_version"] == decision.policy_version
    assert intent["reason"] == decision.reason


async def test_the_configured_floor_reaches_the_device_cutoff(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    control_surface: None,
) -> None:
    """The user's own floor, raised one percent for the device's truncation.

    The *configured* floor, not the policy reserve: those are deliberately
    separate concepts, and only the first is a hard limit.
    """
    coordinator = setup_integration.runtime_data
    await set_mode(hass, CONTROL_MODE_SHADOW)
    await drive(coordinator)

    reserve = coordinator.battery_plan.reserve
    command = coordinator.control_report["command"]

    assert reserve.configured_min_soc_percent == 20.0
    assert command["cutoff_soc_percent"] == 21


async def test_the_diagnostics_hold_all_four_phases_at_once(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    control_surface: None,
) -> None:
    """Sixteen sections, and the four phase blocks agree with each other."""
    coordinator = setup_integration.runtime_data
    await set_mode(hass, CONTROL_MODE_SHADOW)
    await drive(coordinator)
    payload = await async_get_config_entry_diagnostics(hass, setup_integration)

    assert payload["learning"] is not None
    assert payload["forecast"] is not None
    assert payload["battery_plan"]["decision"]["action"] == ACTION_DISCHARGE
    assert payload["control"]["intent"]["action"] == ACTION_DISCHARGE
    # The two blocks describe the same decision, so they cannot disagree.
    assert (
        payload["control"]["intent"]["reason"]
        == (payload["battery_plan"]["decision"]["reason"])
    )


# ===========================================================================
# and still nothing is sent
# ===========================================================================


async def test_the_whole_chain_running_at_once_writes_nothing(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    control_surface: None,
    captured_calls: list[ServiceCall],
) -> None:
    """Learning, forecasting, deciding, translating, gating, planning -- and no write.

    The strongest single statement this release makes, driven through the real
    data path with a real discharge recommendation and a healthy control surface,
    in the mode that would act if it could.
    """
    from .conftest import set_absorbing_snapshot

    coordinator = setup_integration.runtime_data
    await set_mode(hass, CONTROL_MODE_ACTIVE)
    # A site that can absorb the discharge, so the gate passes and the barrier is
    # the only thing left standing between the pipeline and a write. That is the
    # claim being made here, and it is only worth making when the gate said yes.
    set_absorbing_snapshot(hass)
    await drive(coordinator)

    report = coordinator.control_report
    assert report["intent"]["action"] == ACTION_DISCHARGE
    assert report["commands_planned"] == 5
    assert report["safety"]["safe"] is True
    assert report["authorization"]["authorized"] is False
    assert captured_calls == []


async def test_the_state_of_charge_instrument_catches_a_real_incoherence(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    control_surface: None,
    freezer,
) -> None:
    """Driven on the real ingest path, with the two readings disagreeing.

    The state of charge is forced downwards while the battery-power sensor reads
    *charging*. That is exactly the shape a mis-signed or stuck sensor makes, and
    exactly the shape the energy-balance residual cannot see -- the battery term
    cancels out of it entirely.
    """
    from .conftest import BATTERY_SOC, set_sensor
    from .test_init import advance

    coordinator = setup_integration.runtime_data
    await set_mode(hass, CONTROL_MODE_SHADOW)

    # The source fixture reports the battery charging at 664 W. Move the stored
    # energy the other way and the two cannot both be right.
    for step in range(4):
        set_sensor(hass, BATTERY_SOC, 55 - step, "%", "battery")
        await advance(hass, freezer, 16 * 60)

    monitor = coordinator.soc_coherence
    assert monitor.disagree > 0
    # Nothing can agree here: where the reading moved it moved the wrong way, and
    # where it did not, one side moved and the other did not.
    assert monitor.agree == 0
    if any(sample.soc_delta_percent != 0.0 for sample in monitor.recent):
        assert monitor.observed_step_percent == pytest.approx(1.0)

    payload = coordinator.control_report["soc_coherence"]
    assert "instrumentation only" in payload["status"]
    assert payload["disagree"] == monitor.disagree
    assert len(payload["recent"]) <= 16


async def test_the_instrument_agrees_when_the_two_readings_match(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    control_surface: None,
    freezer,
) -> None:
    """A falling state of charge against a discharging battery is coherent.

    The claim is asserted over the samples where the state of charge actually
    moved, not over every sample, and that is not a weakening. This fixture does
    not freeze the clock, so a sixteen-minute advance crosses one or two quarter
    boundaries depending on where in the quarter the test happens to start -- and
    a boundary that closes without the reading having moved is legitimately a
    disagreement, which the previous test asserts on purpose. Counting boundaries
    would have made this pass or fail by the time of day, which it did.
    """
    from .conftest import BATTERY_POWER, BATTERY_SOC, set_sensor
    from .test_init import advance

    coordinator = setup_integration.runtime_data
    await set_mode(hass, CONTROL_MODE_SHADOW)

    # Positive under this installation's convention means discharging.
    for step in range(5):
        set_sensor(hass, BATTERY_SOC, 55 - step, "%", "battery")
        set_sensor(hass, BATTERY_POWER, 2000, "W", "power")
        await advance(hass, freezer, 16 * 60)

    monitor = coordinator.soc_coherence
    moved = [sample for sample in monitor.recent if sample.soc_delta_percent != 0.0]

    assert moved, "the state of charge never moved across a closed interval"
    for sample in moved:
        assert sample.soc_delta_percent < 0.0
        assert sample.expected_ac_kwh < 0.0
        assert sample.verdict == "agree", sample
    assert monitor.agree >= len(moved)


async def test_the_instrument_never_changes_a_verdict(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    control_surface: None,
    freezer,
) -> None:
    """Whatever it concluded, the gate did not consult it.

    The strong form: a run of outright disagreements leaves the safety verdict
    exactly as it was. This instrument is evidence for a later phase, not a veto.

    The sensors are republished before the comparison, and deliberately so. Two
    thirds of an hour of frozen time genuinely ages a reading past the freshness
    limit, and the gate rightly refuses on ``soc_stale`` -- which would have made
    this test pass for entirely the wrong reason.
    """
    from .conftest import (
        BATTERY_POWER,
        BATTERY_SOC,
        GRID_POWER,
        HOUSE_LOAD,
        set_absorbing_snapshot,
        set_sensor,
    )
    from .test_init import advance

    coordinator = setup_integration.runtime_data
    await set_mode(hass, CONTROL_MODE_SHADOW)
    set_absorbing_snapshot(hass)
    await drive(coordinator)
    before = dict(coordinator.control_report["safety"])
    assert before["safe"] is True

    for step in range(4):
        set_sensor(hass, BATTERY_SOC, 55 - step, "%", "battery")
        await advance(hass, freezer, 16 * 60)
    assert coordinator.soc_coherence.disagree > 0

    # Republish every reading the gate reads, so the comparison is about the
    # instrument rather than about how long the clock was frozen.
    set_sensor(hass, BATTERY_SOC, 55, "%", "battery")
    set_sensor(hass, BATTERY_POWER, 0, "W", "power")
    set_sensor(hass, HOUSE_LOAD, 2000, "W", "power")
    set_sensor(hass, GRID_POWER, 2000, "W", "power")
    await drive(coordinator)

    assert coordinator.control_report["safety"] == before
