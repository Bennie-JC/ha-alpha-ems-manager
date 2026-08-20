"""The three Phase-3 entities as a user meets them, and their diagnostics.

Two rules carried forward from every other entity in this integration: nothing
is published that was not measured or derived, and ``unknown`` is what "no
honest answer" looks like -- never a zero. A third is specific to this phase:
none of these three describes something that will happen. Nothing executes the
plan.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.const import (
    ACTION_DISCHARGE,
    ACTION_HOLD,
    BATTERY_ACTION_OPTIONS,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_MAX_CHARGE_KW,
    CONF_BATTERY_MIN_SOC_PERCENT,
    REASON_AT_RESERVE,
    REASON_BELOW_RESERVE,
    REASON_FORECAST_UNAVAILABLE,
    REASON_MISSING_CAPACITY,
    REASON_MISSING_POWER_LIMITS,
    REASON_MISSING_SOC,
)
from custom_components.alpha_ems_manager.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import BATTERY_SOC, HOUSE_LOAD, set_sensor
from .forecast_helpers import (
    NORMAL,
    frozen,
    history_before,
    local,
    refresh_at,
    seed,
)

pytestmark = pytest.mark.usefixtures("setup_integration")

RECOMMENDATION = "sensor.alpha_ems_battery_recommendation"
PLANNED_POWER = "sensor.alpha_ems_planned_battery_power"
USABLE_ENERGY = "sensor.alpha_ems_usable_battery_energy"
BATTERY_ENTITIES = (RECOMMENDATION, PLANNED_POWER, USABLE_ENERGY)

LEARNING_ENTITIES = (
    "sensor.alpha_ems_expected_house_load_today",
    "sensor.alpha_ems_expected_house_load_tomorrow",
    "sensor.alpha_ems_learning_confidence",
    "sensor.alpha_ems_learning_days",
)
FORECAST_ERROR_ENTITIES = (
    "sensor.alpha_ems_forecast_error_yesterday",
    "sensor.alpha_ems_forecast_error_7_days",
)

#: The closed attribute surface. A debugging field cannot become part of the
#: public contract by accident, and a removal is a visible change.
RECOMMENDATION_ATTRIBUTES = {
    "reason",
    "planned_power_kw",
    "usable_energy_kwh",
    "battery_soc_percent",
    "configured_min_soc_percent",
    "effective_min_soc_percent",
    "constraints",
    # Widened deliberately in Phase 5, from eight to nine. "Eight" was a
    # convention rather than a rule, and this is the one fact a user needs in
    # order to read the recommendation that cannot live in the prose beside it:
    # prose cannot be automated against. The hard caps -- no dicts, no list above
    # eight entries -- are untouched.
    "pv_aware",
    "basis",
}
PLANNED_POWER_ATTRIBUTES = {
    "requested_mode",
    "requested_power_kw",
    "allowed_energy_kwh",
    "limiting_constraints",
    "policy",
    "policy_version",
    "sign_convention",
    "basis",
}
USABLE_ENERGY_ATTRIBUTES = {
    "battery_soc_percent",
    "capacity_kwh",
    "stored_energy_kwh",
    "configured_min_soc_percent",
    "effective_min_soc_percent",
    "reserve_source",
    "coverage_hours",
    "basis",
}
CORE_ATTRIBUTES = {
    "state_class",
    "unit_of_measurement",
    "icon",
    "friendly_name",
    "device_class",
    "options",
}


async def drive(coordinator, *, hour: int = 12) -> None:
    """Give the model history and refresh at a fixed instant."""
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, hour, 5))


def attributes_of(hass: HomeAssistant, entity_id: str) -> dict:
    """Return one entity's attributes, asserting the entity exists."""
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return dict(state.attributes)


def reconfigure(entry: MockConfigEntry, hass: HomeAssistant, **options) -> None:
    """Replace the coordinator's effective configuration in place."""
    coordinator = entry.runtime_data
    fields = {
        name: getattr(coordinator.config, name)
        for name in coordinator.config.__dataclass_fields__
    }
    coordinator.config = coordinator.config.__class__(**{**fields, **options})


# -- the working case -------------------------------------------------------


async def test_the_three_entities_agree_with_each_other(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A 10 kWh pack at 55 % above a 20 % floor: 3.5 kWh DC, 3.32 kWh AC.

    Every published figure is derived by hand here, so an arithmetic change that
    kept the entities self-consistent while moving the numbers still fails.
    """
    await drive(setup_integration.runtime_data)

    recommendation = hass.states.get(RECOMMENDATION)
    assert recommendation.state == ACTION_DISCHARGE
    assert recommendation.attributes["battery_soc_percent"] == 55.0
    assert recommendation.attributes["usable_energy_kwh"] == 3.32
    assert recommendation.attributes["configured_min_soc_percent"] == 20.0
    assert recommendation.attributes["effective_min_soc_percent"] == 20.0

    usable = hass.states.get(USABLE_ENERGY)
    assert float(usable.state) == 3.32
    assert usable.attributes["stored_energy_kwh"] == 5.5
    assert usable.attributes["capacity_kwh"] == 10.0
    assert usable.attributes["reserve_source"] == "configured"
    # 3.32 kWh against a 0.5 kW predicted mean is 6.64 hours.
    assert usable.attributes["coverage_hours"] == 6.64

    power = hass.states.get(PLANNED_POWER)
    # Negative because the plan is to discharge; 0.125 kWh over a quarter-hour.
    assert float(power.state) == -0.5
    assert power.attributes["requested_mode"] == "discharge"
    assert power.attributes["requested_power_kw"] == 0.5
    assert power.attributes["allowed_energy_kwh"] == 0.12

    # The recommendation's own copy of the planned power cannot disagree.
    assert recommendation.attributes["planned_power_kw"] == float(power.state)


async def test_the_planned_power_is_signed_only_here(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Positive means into the battery, and it says so.

    The convention is the plan's own. The configured ``battery_power_sign``
    describes how the *user's sensor* reports and is resolved away long before
    this point, and conflating the two is the trap this attribute exists to
    close.
    """
    await drive(setup_integration.runtime_data)
    attributes = attributes_of(hass, PLANNED_POWER)

    assert float(hass.states.get(PLANNED_POWER).state) < 0.0
    assert "positive is energy into the battery" in attributes["sign_convention"]
    assert "configured battery power sign" in attributes["sign_convention"]
    # An interval average, and not an inverter setpoint.
    assert "not an instantaneous inverter setpoint" in attributes["basis"]


async def test_every_battery_entity_says_it_controls_nothing(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The promise of the phase, on the surface a user actually reads."""
    await drive(setup_integration.runtime_data)

    for entity_id in BATTERY_ENTITIES:
        basis = attributes_of(hass, entity_id)["basis"]
        assert (
            "nothing executes" in basis
            or "never sends" in basis
            or ("advisory only" in basis)
        ), entity_id


async def test_the_recommendation_is_a_closed_enum(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Three states and no more, so a dashboard can be built against it."""
    await drive(setup_integration.runtime_data)
    state = hass.states.get(RECOMMENDATION)

    assert state.attributes["device_class"] == "enum"
    assert state.attributes["options"] == list(BATTERY_ACTION_OPTIONS)
    assert state.state in BATTERY_ACTION_OPTIONS
    # An enum permits no state class, and a statistic over a category is
    # meaningless anyway.
    assert "state_class" not in state.attributes


@pytest.mark.parametrize(
    ("entity_id", "allowed"),
    [
        (RECOMMENDATION, RECOMMENDATION_ATTRIBUTES),
        (PLANNED_POWER, PLANNED_POWER_ATTRIBUTES),
        (USABLE_ENERGY, USABLE_ENERGY_ATTRIBUTES),
    ],
)
async def test_no_battery_entity_publishes_an_undeclared_attribute(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    entity_id: str,
    allowed: set,
) -> None:
    """The surface is closed, and stays closed with and without a plan."""
    published = set(attributes_of(hass, entity_id)) - CORE_ATTRIBUTES
    assert published <= allowed, published - allowed

    await drive(setup_integration.runtime_data)

    published = set(attributes_of(hass, entity_id)) - CORE_ATTRIBUTES
    assert published == allowed, published ^ allowed


async def test_no_battery_entity_exposes_a_trajectory(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Ninety-six values in an attribute would be written on every state change."""
    await drive(setup_integration.runtime_data)

    for entity_id in BATTERY_ENTITIES:
        for key, value in attributes_of(hass, entity_id).items():
            assert not isinstance(value, dict), f"{entity_id}.{key}"
            if isinstance(value, (list, tuple)):
                assert len(value) <= 8, f"{entity_id}.{key} has {len(value)}"


# -- honest degradation -----------------------------------------------------


@pytest.mark.parametrize(
    ("options", "reason"),
    [
        ({"battery_capacity_kwh": None}, REASON_MISSING_CAPACITY),
        ({"battery_max_charge_kw": None}, REASON_MISSING_POWER_LIMITS),
        ({"battery_max_discharge_kw": None}, REASON_MISSING_POWER_LIMITS),
        ({"battery_capacity_kwh": 0.0}, REASON_MISSING_CAPACITY),
    ],
)
async def test_a_missing_hardware_fact_reads_unknown_and_names_itself(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    options: dict,
    reason: str,
) -> None:
    """Never a fabricated zero, and never a plan built on a guess.

    All three entities go quiet, because none of them can be derived without the
    missing figure -- and the reason survives on the recommendation, because an
    entity reading ``unknown`` with no explanation is the thing a user cannot act
    on.
    """
    reconfigure(setup_integration, hass, **options)
    await drive(setup_integration.runtime_data)

    for entity_id in BATTERY_ENTITIES:
        assert hass.states.get(entity_id).state == "unknown", entity_id

    assert attributes_of(hass, RECOMMENDATION)["reason"] == reason
    assert attributes_of(hass, RECOMMENDATION)["usable_energy_kwh"] is None
    assert attributes_of(hass, PLANNED_POWER)["allowed_energy_kwh"] is None


@pytest.mark.parametrize(
    "options",
    [
        {"battery_capacity_kwh": None},
        {"battery_max_charge_kw": None},
        {"battery_round_trip_efficiency_percent": 0.9},
    ],
)
async def test_a_declined_decision_carries_exactly_zero_energy(
    hass: HomeAssistant, setup_integration: MockConfigEntry, options: dict
) -> None:
    """The invariant that makes ``no_decision`` safe to have at all.

    It is deliberately distinct from a hold, because "hold because that is best"
    and "hold because a hardware fact is missing" are different facts a later
    phase needs apart. What makes the distinction harmless is that the two are
    *behaviourally* identical: a consumer that ignored the action entirely would
    still do nothing.

    Asserted on the record rather than on the entity, because the entity gates on
    the action and would hide a non-zero energy behind ``unknown`` -- which is
    exactly how this gap went unnoticed until a deliberately broken build was
    tested against it.
    """
    reconfigure(setup_integration, hass, **options)
    coordinator = setup_integration.runtime_data
    await drive(coordinator)

    decision = coordinator.battery_plan.decision
    assert decision.decided is False
    assert decision.action == "no_decision"
    assert decision.allowed_energy_ac_kwh == 0.0
    assert decision.average_power_kw == 0.0
    assert decision.published_power_kw == 0.0
    assert decision.request.is_idle
    assert decision.constraints == ()


@pytest.mark.parametrize("state", ["unknown", "unavailable", "", "not-a-number"])
async def test_an_unreadable_state_of_charge_reads_unknown(
    hass: HomeAssistant, setup_integration: MockConfigEntry, state: str
) -> None:
    """No state of charge, no plan -- and certainly no discharge recommendation."""
    set_sensor(hass, BATTERY_SOC, state, "%", "battery")
    await drive(setup_integration.runtime_data)

    for entity_id in BATTERY_ENTITIES:
        assert hass.states.get(entity_id).state == "unknown", entity_id
    assert attributes_of(hass, RECOMMENDATION)["reason"] == REASON_MISSING_SOC


@pytest.mark.parametrize("value", [-20, 150, 1e9])
async def test_an_implausible_state_of_charge_is_refused(
    hass: HomeAssistant, setup_integration: MockConfigEntry, value: float
) -> None:
    """A reading outside the band is an unreadable source, not a number.

    Minus twenty per cent is the dangerous one: the charge headroom of a 10 kWh
    pack would compute as 12 kWh, so a single bad sample would permit filling it
    past its own capacity.
    """
    set_sensor(hass, BATTERY_SOC, value, "%", "battery")
    await drive(setup_integration.runtime_data)

    assert hass.states.get(RECOMMENDATION).state == "unknown"
    assert attributes_of(hass, RECOMMENDATION)["reason"] == REASON_MISSING_SOC


async def test_a_state_of_charge_sensor_in_the_wrong_unit_is_refused(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A house-load sensor selected by mistake must not read as a 2000 % battery."""
    set_sensor(hass, BATTERY_SOC, 2000, "W", "power")
    await drive(setup_integration.runtime_data)

    assert hass.states.get(RECOMMENDATION).state == "unknown"
    assert attributes_of(hass, RECOMMENDATION)["reason"] == REASON_MISSING_SOC


async def test_a_withheld_forecast_still_leaves_the_usable_energy_published(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Which is the whole reason this entity was chosen over a projected state.

    With no forecast there is no trajectory and no coverage figure, but the
    battery is fully known -- so holding is a real recommendation and the usable
    energy is a real number.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, {})
    await refresh_at(coordinator, local(NORMAL, 12, 5))

    assert hass.states.get(RECOMMENDATION).state == ACTION_HOLD
    assert attributes_of(hass, RECOMMENDATION)["reason"] == REASON_FORECAST_UNAVAILABLE
    assert float(hass.states.get(USABLE_ENERGY).state) == 3.32
    assert float(hass.states.get(PLANNED_POWER).state) == 0.0
    # No forecast, so no hours -- rather than the entity going unavailable.
    assert attributes_of(hass, USABLE_ENERGY)["coverage_hours"] is None


@pytest.mark.parametrize(
    ("soc", "expected_state", "reason"),
    [(20, ACTION_HOLD, REASON_AT_RESERVE), (15, ACTION_HOLD, REASON_BELOW_RESERVE)],
)
async def test_at_and_below_the_reserve_the_recommendation_is_hold(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    soc: int,
    expected_state: str,
    reason: str,
) -> None:
    """And the two cases are told apart, because they call for different action."""
    set_sensor(hass, BATTERY_SOC, soc, "%", "battery")
    await drive(setup_integration.runtime_data)

    assert hass.states.get(RECOMMENDATION).state == expected_state
    assert attributes_of(hass, RECOMMENDATION)["reason"] == reason
    assert float(hass.states.get(PLANNED_POWER).state) == 0.0
    assert float(hass.states.get(USABLE_ENERGY).state) == 0.0


async def test_changing_the_minimum_visibly_changes_the_usable_energy(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The setting has to be legible, or a user cannot tell it took effect.

    Fifty-five per cent of a 10 kWh pack is 5.5 kWh; raising the floor from 20 %
    to 40 % removes 2 kWh DC, so the deliverable figure falls by that times the
    conversion.
    """
    coordinator = setup_integration.runtime_data
    await drive(coordinator)
    assert float(hass.states.get(USABLE_ENERGY).state) == 3.32

    reconfigure(setup_integration, hass, battery_min_soc_percent=40.0)
    await refresh_at(coordinator, local(NORMAL, 12, 20))

    assert float(hass.states.get(USABLE_ENERGY).state) == 1.42
    assert attributes_of(hass, USABLE_ENERGY)["configured_min_soc_percent"] == 40.0
    assert attributes_of(hass, USABLE_ENERGY)["effective_min_soc_percent"] == 40.0


# -- diagnostics ------------------------------------------------------------


async def test_diagnostics_publishes_exactly_what_the_entities_show(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Disagreement between a download and a dashboard has to be impossible.

    Reported from the same plan object the entities read, rather than
    recomputed -- the pattern the forecast-error block already uses, for the same
    reason.
    """
    await drive(setup_integration.runtime_data)
    with frozen(local(NORMAL, 12, 6)):
        payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    published = payload["battery_plan"]["published"]

    assert published["battery_recommendation"] == hass.states.get(RECOMMENDATION).state
    assert published["planned_battery_power_kw"] == float(
        hass.states.get(PLANNED_POWER).state
    )
    assert published["usable_battery_energy_kwh"] == float(
        hass.states.get(USABLE_ENERGY).state
    )


async def test_diagnostics_reports_both_floors_and_where_the_effective_one_came_from(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """So a raised reserve is never mistaken for the user changing their mind."""
    await drive(setup_integration.runtime_data)
    with frozen(local(NORMAL, 12, 6)):
        payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    reserve = payload["battery_plan"]["reserve"]

    assert reserve["configured_min_soc_percent"] == 20.0
    assert reserve["effective_min_soc_percent"] == 20.0
    assert reserve["source"] == "configured"
    assert reserve["raised_above_configured"] is False
    assert "hard floor" in reserve["rule"]


async def test_diagnostics_labels_the_projection_as_pv_blind(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The projection is real and the limitation is real, so both are stated.

    Without this a user with solar reads a simulated grid import far above
    reality and files it as a bug. It is a battery-only counterfactual, and it
    has to say so wherever it appears -- which is also why it is not an entity.
    """
    await drive(setup_integration.runtime_data)
    with frozen(local(NORMAL, 12, 6)):
        payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    plan = payload["battery_plan"]

    assert plan["projected_soc_percent"] is not None
    assert "PV-blind" in plan["projected_soc_note"]
    assert "photovoltaic" in plan["trajectory"]["basis"]
    assert "counterfactual" in plan["trajectory"]["basis"]


async def test_diagnostics_reports_the_model_and_its_known_bias(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Both efficiencies, the invariant, and what the model flatters."""
    await drive(setup_integration.runtime_data)
    with frozen(local(NORMAL, 12, 6)):
        payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    model = payload["battery_plan"]["model"]

    assert model["charge_efficiency"] == model["discharge_efficiency"]
    assert model["round_trip_efficiency"] == pytest.approx(0.9, abs=1e-5)
    assert "exactly once" in model["efficiency_rule"]
    assert "upper bound" in model["known_optimistic_bias"]


async def test_diagnostics_reports_the_battery_power_as_a_coherence_note_only(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """An instantaneous power must never redefine stored energy.

    The state of charge is the only source of stored energy. The measured power
    is reported so a maintainer can see the two agree, and for nothing else --
    the same role the daily-validation entity plays for house load.
    """
    await drive(setup_integration.runtime_data)
    with frozen(local(NORMAL, 12, 6)):
        payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    inputs = payload["battery_plan"]["inputs"]

    assert inputs["battery_power_w"] == 664.0
    assert "coherence only" in inputs["battery_power_role"]
    assert "never redefines" in inputs["battery_power_role"]
    # And the stored energy is the state of charge times capacity, nothing else.
    assert payload["battery_plan"]["state"]["energy_kwh"] == 5.5


async def test_diagnostics_states_the_electrical_boundaries(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The decision that would be most expensive to get wrong, written down."""
    await drive(setup_integration.runtime_data)
    with frozen(local(NORMAL, 12, 6)):
        payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    boundaries = payload["battery_plan"]["inputs"]["boundaries"]

    assert "DC-side" in boundaries
    assert "AC-side" in boundaries


async def test_diagnostics_names_the_shipped_policies_and_the_charging_rule(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A phase boundary that is only in a commit message is not discoverable."""
    await drive(setup_integration.runtime_data)
    with frozen(local(NORMAL, 12, 6)):
        payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    catalogue = payload["battery_plan"]["policy_catalogue"]

    assert set(catalogue["shipped"]) == {"hold", "reserve_guard"}
    assert catalogue["default"] == "reserve_guard"
    assert "ever asks to charge" in catalogue["charging_rule"]


async def test_diagnostics_says_when_the_hardware_is_simply_not_configured(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A user's missing setting and a fault are different things."""
    reconfigure(setup_integration, hass, battery_capacity_kwh=None)
    await drive(setup_integration.runtime_data)
    with frozen(local(NORMAL, 12, 6)):
        payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    plan = payload["battery_plan"]

    assert plan["available"] is False
    assert plan["unavailable_reason"] == REASON_MISSING_CAPACITY
    assert plan["hardware_configured"] is False


async def test_the_battery_block_is_bounded_and_serialisable(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Home Assistant serves this over HTTP, and the whole payload is capped."""
    await drive(setup_integration.runtime_data)
    with frozen(local(NORMAL, 12, 6)):
        payload = await async_get_config_entry_diagnostics(hass, setup_integration)

    def oversized(value, path=""):
        found = []
        if isinstance(value, dict):
            for key, item in value.items():
                found += oversized(item, f"{path}.{key}")
        elif isinstance(value, (list, tuple)):
            if len(value) > 16:
                found.append((path, len(value)))
            for index, item in enumerate(value):
                found += oversized(item, f"{path}[{index}]")
        return found

    assert oversized(payload["battery_plan"]) == []
    encoded = json.dumps(payload, allow_nan=False, default=str)
    assert "NaN" not in encoded
    assert "Infinity" not in encoded


# -- failure isolation ------------------------------------------------------


async def test_a_battery_failure_leaves_every_other_sensor_alone(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Phase 3 is additive, so a fault in it must cost only Phase 3.

    Taking the whole integration unavailable because a battery calculation raised
    would trade the important half for the newest half.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))

    with patch(
        "custom_components.alpha_ems_manager.coordinator.build_plan",
        side_effect=RuntimeError("battery layer exploded"),
    ):
        await refresh_at(coordinator, local(NORMAL, 12, 5))

    # The three battery entities degrade...
    for entity_id in BATTERY_ENTITIES:
        assert hass.states.get(entity_id).state == "unknown", entity_id

    # ...and nothing else does.
    for entity_id in LEARNING_ENTITIES:
        state = hass.states.get(entity_id)
        assert state.state not in ("unavailable",), entity_id
    assert hass.states.get("sensor.alpha_ems_learning_days").state == "6"
    assert (
        float(hass.states.get("sensor.alpha_ems_expected_house_load_today").state) > 0
    )

    # Learning itself is untouched: the history is still there to be read.
    assert len(coordinator.store.days) == 6


async def test_a_battery_failure_is_reported_rather_than_hidden(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A silent degradation is indistinguishable from a missing setting."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))

    with patch(
        "custom_components.alpha_ems_manager.coordinator.build_plan",
        side_effect=RuntimeError("battery layer exploded"),
    ):
        await refresh_at(coordinator, local(NORMAL, 12, 5))
        with frozen(local(NORMAL, 12, 6)):
            payload = await async_get_config_entry_diagnostics(hass, setup_integration)

    plan = payload["battery_plan"]
    assert plan["available"] is False
    # The hardware *is* configured, so the note must say a fault rather than a
    # missing setting.
    assert plan["hardware_configured"] is True
    assert "isolated" in plan["note"]


async def test_a_refresh_failure_makes_the_battery_entities_unavailable(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A stale recommendation after a fault would be worse than none."""
    coordinator = setup_integration.runtime_data
    await drive(coordinator)
    assert hass.states.get(RECOMMENDATION).state == ACTION_DISCHARGE

    with patch.object(
        coordinator, "_async_update_data", side_effect=RuntimeError("source gone")
    ):
        await refresh_at(coordinator, local(NORMAL, 12, 20))

    for entity_id in BATTERY_ENTITIES:
        assert hass.states.get(entity_id).state == "unavailable", entity_id


# -- restart ----------------------------------------------------------------


async def test_the_battery_entities_come_back_after_a_reload(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Nothing about the plan is cached, so equality proves it recomputed."""
    coordinator = setup_integration.runtime_data
    await drive(coordinator)
    before = {
        entity_id: hass.states.get(entity_id).state for entity_id in BATTERY_ENTITIES
    }
    history = dict(coordinator.store.days)
    await coordinator.async_shutdown_store()

    with frozen(local(NORMAL, 12, 30)):
        assert await hass.config_entries.async_reload(setup_integration.entry_id)
        await hass.async_block_till_done()
    restarted = setup_integration.runtime_data
    seed(restarted, history)
    await refresh_at(restarted, local(NORMAL, 12, 35))

    for entity_id, value in before.items():
        assert hass.states.get(entity_id).state == value, entity_id


async def test_the_planning_figures_survive_an_options_edit(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Editing one figure must not clear the others, nor any unrelated option."""
    from .test_config_flow import battery_options_payload, open_options

    hass.config_entries.async_update_entry(
        setup_integration, options={"future_option": "keep me"}
    )

    result = await open_options(hass, setup_integration.entry_id, "battery")
    assert result["step_id"] == "battery"
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        battery_options_payload(**{CONF_BATTERY_MIN_SOC_PERCENT: 35.0}),
    )
    await hass.async_block_till_done()

    options = setup_integration.options
    assert options[CONF_BATTERY_MIN_SOC_PERCENT] == 35.0
    assert options[CONF_BATTERY_CAPACITY_KWH] == 10.0
    assert options[CONF_BATTERY_MAX_CHARGE_KW] == 5.0
    assert options["future_option"] == "keep me"


async def test_a_house_load_change_does_not_disturb_the_battery_figures(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The usable energy depends on the battery and on nothing else."""
    coordinator = setup_integration.runtime_data
    await drive(coordinator)
    before = hass.states.get(USABLE_ENERGY).state

    set_sensor(hass, HOUSE_LOAD, 9000, "W", "power")
    await refresh_at(coordinator, local(NORMAL, 12, 20))

    assert hass.states.get(USABLE_ENERGY).state == before


async def test_the_plan_is_rebuilt_every_refresh_rather_than_carried(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Seeded from the state of charge each time, so a stale energy cannot persist.

    That is what keeps a mid-session capacity change honest: the pack did not
    change, so re-deriving the energy from the reading is right, while carrying
    an energy across would make the state of charge jump.
    """
    coordinator = setup_integration.runtime_data
    await drive(coordinator)
    assert hass.states.get(USABLE_ENERGY).state == "3.32"

    set_sensor(hass, BATTERY_SOC, 90, "%", "battery")
    await refresh_at(coordinator, local(NORMAL, 12, 20))

    # 90 % of 10 kWh above a 20 % floor is 7 kWh DC.
    assert float(hass.states.get(USABLE_ENERGY).state) == pytest.approx(6.64, abs=0.01)
    assert attributes_of(hass, USABLE_ENERGY)["stored_energy_kwh"] == 9.0


async def test_midnight_does_not_disturb_the_battery_plan(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A day rollover changes the forecast, not the battery's own facts."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 23, 50))
    before = hass.states.get(USABLE_ENERGY).state

    await refresh_at(coordinator, local(NORMAL + timedelta(days=1), 0, 5))

    assert hass.states.get(USABLE_ENERGY).state == before
    assert hass.states.get(RECOMMENDATION).state in BATTERY_ACTION_OPTIONS
