"""What Phase 7 publishes, and everything it must leave alone.

One sensor and one diagnostics section. The sensor's state is an **energy**,
because that is the quantity the model conserves and the one a person compares
against ``Usable Battery Energy``; the state of charge it implies is derived and
travels as an attribute.

The other half of this file is the promise that matters more than the figure:
**nothing changed.** Phase 7 computes and reports. The recommendation, the planned
power, the usable energy and the control state are what beta.12 published for the
same inputs, the reserve is never written into the floor the policy obeys, and a
healthy price source still moves none of it.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.const import (
    CONTROL_MODE_ACTIVE,
    RESERVE_CONFIGURED,
)
from custom_components.alpha_ems_manager.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .forecast_helpers import NORMAL, history_before, local, refresh_at, seed
from .live_capability import assert_charge_only_capability

RESERVE_ENTITY = "sensor.alpha_ems_dynamic_battery_reserve"

#: Exactly the eight the contract allows, and no more. A requirement with six
#: ways of being wrong must not unpack all six into an entity.
RESERVE_ATTRIBUTES = {
    "required_reserve_soc_percent",
    "configured_min_soc_percent",
    "reserve_shortfall_kwh",
    "reserve_reachable",
    "replenishment_dependency_kwh",
    "lower_bound_reason",
    "intervals_evaluated",
    "basis",
}
CORE_ATTRIBUTES = {
    "state_class",
    "unit_of_measurement",
    "icon",
    "friendly_name",
    "device_class",
}

#: The four figures beta.12 published. Named rather than counted, so a change to
#: any one of them is visible in a diff.
PUBLISHED = (
    "sensor.alpha_ems_battery_recommendation",
    "sensor.alpha_ems_planned_battery_power",
    "sensor.alpha_ems_usable_battery_energy",
    "sensor.alpha_ems_control_state",
)


async def drive(coordinator, *, hour: int = 12) -> None:
    """Give the model history and refresh at a fixed instant."""
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, hour, 5))


def attributes_of(hass: HomeAssistant, entity_id: str) -> dict:
    """Return one entity's attributes, asserting the entity exists."""
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return dict(state.attributes)


async def diagnostics_at_noon(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    """Read diagnostics on the day the refresh was driven on.

    Diagnostics reads its own clock, so a payload taken after a driven refresh
    would otherwise describe whatever day it really is -- which is how a sibling
    module's tests passed on the day they were written and failed two days later.
    """
    from unittest.mock import patch

    with patch(
        "custom_components.alpha_ems_manager.diagnostics.dt_util.now",
        return_value=local(NORMAL, 12, 5),
    ):
        return await async_get_config_entry_diagnostics(hass, entry)


# --- the entity -------------------------------------------------------------


async def test_the_state_is_an_energy_in_kilowatt_hours(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The requirement itself, not the state of charge it implies.

    An energy is what the model conserves and what a person compares against
    ``Usable Battery Energy``. Publishing the percentage instead would make the
    two entities incomparable and would put a derived quantity in the place of
    the primitive.
    """
    await drive(setup_integration.runtime_data)

    state = hass.states.get(RESERVE_ENTITY)
    attributes = attributes_of(hass, RESERVE_ENTITY)

    assert attributes["unit_of_measurement"] == "kWh"
    assert attributes["device_class"] == "energy_storage"
    assert attributes["state_class"] == "measurement"
    assert float(state.state) >= 0.0
    # And the percentage is present, derived, and consistent with the energy.
    plan = setup_integration.runtime_data.battery_plan
    capacity = plan.inputs.capacity_kwh
    assert attributes["required_reserve_soc_percent"] == pytest.approx(
        float(state.state) / capacity * 100.0, abs=0.1
    )


async def test_the_reserve_entity_carries_exactly_eight_attributes(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The cap, asserted as a set rather than a count.

    A count could be satisfied by swapping a useful attribute for a useless one,
    so the names are pinned. Everything else the phase computes -- both
    counterfactuals, the peak, the constraint tallies, the horizon edges and the
    provenance the calculation ignores -- is in diagnostics.
    """
    await drive(setup_integration.runtime_data)

    attributes = attributes_of(hass, RESERVE_ENTITY)

    assert set(attributes) - CORE_ATTRIBUTES == RESERVE_ATTRIBUTES
    assert len(RESERVE_ATTRIBUTES) == 8


async def test_the_reserve_entity_exposes_no_array_at_all(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A hundred and ninety-two values would be written on every state change."""
    await drive(setup_integration.runtime_data)

    for key, value in attributes_of(hass, RESERVE_ENTITY).items():
        assert not isinstance(value, dict), key
        if isinstance(value, (list, tuple)):
            assert len(value) <= 8, f"{key} has {len(value)}"


async def test_the_basis_says_that_nothing_obeys_the_figure(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The most important sentence the phase publishes.

    A protective-sounding number without its limitation costs more trust than it
    buys. This one has to say three things: that it is advisory, that the
    configured minimum is still the floor the planner obeys, and that it carries
    no margin for forecast error.
    """
    await drive(setup_integration.runtime_data)

    basis = attributes_of(hass, RESERVE_ENTITY)["basis"].lower()

    assert "advisory" in basis
    assert "neither enforces nor executes" in basis
    assert "hard floor" in basis
    assert "no forecast-error margin" in basis


async def test_a_withheld_forecast_reads_unknown_rather_than_zero(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Zero is the value of a satisfied requirement, so it must not mean "unknown".

    A young installation has no publishable forecast, so there is no horizon to
    walk. The honest answer is that there is no requirement -- not that nothing is
    needed.
    """
    coordinator = setup_integration.runtime_data
    await refresh_at(coordinator, local(NORMAL, 12, 5))

    state = hass.states.get(RESERVE_ENTITY)

    assert state.state == "unknown"


async def test_a_missing_hardware_fact_reads_unknown_too(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """No capacity, no limits, no requirement, and no invented one."""
    coordinator = setup_integration.runtime_data
    fields = {
        name: getattr(coordinator.config, name)
        for name in coordinator.config.__dataclass_fields__
    }
    coordinator.config = coordinator.config.__class__(
        **{**fields, "battery_capacity_kwh": None}
    )
    await drive(coordinator)

    assert hass.states.get(RESERVE_ENTITY).state == "unknown"


# --- the diagnostics section ------------------------------------------------


async def test_the_reserve_section_reports_the_documented_blocks(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """One authoritative figure, the counterfactuals, the horizon and the bounds."""
    await drive(setup_integration.runtime_data)

    reserve = (await diagnostics_at_noon(hass, setup_integration))["reserve"]

    assert reserve["available"] is True
    assert set(reserve) >= {
        "authoritative",
        "counterfactuals",
        "floor",
        "horizon",
        "headroom",
        "shortfall",
        "provenance",
        "replenishment_note",
        "decides_nothing",
    }
    assert reserve["authoritative"]["required_reserve_kwh"] is not None
    assert reserve["counterfactuals"]["required_same_interval_only_kwh"] is not None
    assert reserve["counterfactuals"]["peak_required_reserve_kwh"] is not None


async def test_no_list_in_the_reserve_section_exceeds_the_ceiling(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Sixteen entries, recursively, like every other list in the payload.

    A hundred and ninety-two requirements truncated to sixteen would be worse
    than absent: it would read as a short horizon rather than as a clipped
    payload.
    """
    await drive(setup_integration.runtime_data)
    reserve = (await diagnostics_at_noon(hass, setup_integration))["reserve"]

    def walk(value, path: str = "reserve") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{path}.{key}")
        elif isinstance(value, (list, tuple)):
            assert len(value) <= 16, f"{path} has {len(value)} entries"
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(reserve)


async def test_the_reserve_section_names_no_economic_field(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Checked on key names, not on prose: the note explains the boundary.

    A price cannot reach the requirement, so it must not appear beside it either
    -- a reader finding one there would reasonably conclude it had been consulted.
    """
    await drive(setup_integration.runtime_data)
    reserve = (await diagnostics_at_noon(hass, setup_integration))["reserve"]
    forbidden = ("price", "tariff", "cost", "arbitrage", "cheap", "expensive", "eur")

    def keys(value) -> set[str]:
        found: set[str] = set()
        if isinstance(value, dict):
            for key, item in value.items():
                found.add(key)
                found |= keys(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                found |= keys(item)
        return found

    for key in keys(reserve):
        for term in forbidden:
            assert term not in key.lower(), key


async def test_the_provenance_records_the_absorption_pair_and_the_bias(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Recorded, and consumed by nothing.

    The absorption pair is here so it can be checked afterwards that the figure
    did not move with it. The measured load bias is here because the requirement
    carries no margin for forecast error, and a negative bias means the estimate
    may be low -- naming the direction is this phase's obligation; correcting for
    it is a later one.
    """
    await drive(setup_integration.runtime_data)

    provenance = (await diagnostics_at_noon(hass, setup_integration))["reserve"][
        "provenance"
    ]

    assert "pv_absorption_modelled" in provenance
    assert "pv_absorption_reason" in provenance
    assert "forecast_bias_kwh_per_interval" in provenance
    assert "forecast_days_compared" in provenance
    assert "replenishment_assumption" in provenance
    assert "point estimate" in provenance["forecast_basis"]
    assert "under-predicting" in provenance["forecast_basis"]


async def test_the_reserve_section_says_it_decides_nothing(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The same sentence every observation-only layer in this project repeats."""
    await drive(setup_integration.runtime_data)

    reserve = (await diagnostics_at_noon(hass, setup_integration))["reserve"]

    assert "never enforces" in reserve["decides_nothing"]
    assert "never consults a price" in reserve["decides_nothing"]
    assert "does not prove" in reserve["replenishment_note"]


# --- nothing changed --------------------------------------------------------


async def test_the_published_battery_figures_are_untouched(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The whole promise of compute-and-report-only, in four states.

    The reserve is computed on this refresh and is above the configured floor, and
    the four figures beta.12 published are what they were: the policy still reads
    an effective minimum equal to the user's own setting, so it still discharges
    to the same place.
    """
    coordinator = setup_integration.runtime_data
    await drive(coordinator)

    plan = coordinator.battery_plan
    assert plan.reserve_projection is not None
    assert plan.reserve_projection.required_now_dc_kwh > plan.state.floor_energy_kwh

    assert hass.states.get(PUBLISHED[0]).state == plan.decision.action
    assert float(hass.states.get(PUBLISHED[1]).state) == pytest.approx(
        plan.decision.published_power_kw, abs=0.001
    )
    assert float(hass.states.get(PUBLISHED[2]).state) == pytest.approx(
        plan.usable_energy_kwh, abs=0.01
    )
    # And the reserve the plan was built against is still the configured one.
    assert plan.reserve.source == RESERVE_CONFIGURED
    assert plan.reserve.effective_min_soc_percent == (
        plan.reserve.configured_min_soc_percent
    )
    assert plan.reserve.raised_above_configured is False


async def test_the_requirement_does_not_move_the_trajectories_it_is_compared_to(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """One-way dependency, asserted on the published trajectory figures.

    The requirement is computed from limits and demands; the comparison reads the
    candidate trajectory and writes nothing back. So the projected state of charge
    and the what-if are exactly the counterfactuals Phase 3 produced.
    """
    coordinator = setup_integration.runtime_data
    await drive(coordinator)
    plan = coordinator.battery_plan

    reference_end = plan.reference.end_soc_percent
    candidate_end = plan.candidate.end_soc_percent
    comparison = plan.reserve_comparison

    assert comparison is not None
    assert plan.reference.end_soc_percent == reference_end
    assert plan.candidate.end_soc_percent == candidate_end
    assert plan.what_if["candidate_end_soc_percent"] == pytest.approx(
        round(candidate_end, 1)
    )


async def test_a_reserve_shortfall_commands_nothing_in_active_mode(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Phase 7 identifies need. It cannot express an intention to satisfy it.

    Driven in ``active`` with a floor high enough that the requirement cannot be
    met, so the shortfall is real -- and still no service call leaves the
    integration, because there is no path from a requirement to a command.
    """
    from unittest.mock import patch

    coordinator = setup_integration.runtime_data
    coordinator.control_mode = CONTROL_MODE_ACTIVE
    fields = {
        name: getattr(coordinator.config, name)
        for name in coordinator.config.__dataclass_fields__
    }
    coordinator.config = coordinator.config.__class__(
        **{**fields, "battery_min_soc_percent": 90.0}
    )

    with patch("homeassistant.core.ServiceRegistry.async_call") as call:
        await drive(coordinator)

    plan = coordinator.battery_plan
    report = (await diagnostics_at_noon(hass, setup_integration))["reserve"]

    assert_charge_only_capability()
    assert report["shortfall"]["reserve_shortfall_kwh"] > 0.0
    assert plan.decision.action != "charge"
    assert call.await_count == 0
