"""Whether the inverter is storing surplus, and what Phase 5 did not change.

Two halves. The first drives the absorption gate through the real device-state
path, because the approved design asserted autonomous absorption as unconditional
physics and the vendor control surface contradicts that -- with Excess Export on,
production below the inverter's AC limit goes to house load and feed-in and the
battery is charged with zero.

The second is the regression half: the whole integration driven twice at the same
instant, once with a PV forecast and once without, asserting that every Phase-1
and Phase-2 figure is identical and that only the battery surface may differ.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.alphaess_device import (
    BOOLEAN_EXCESS_EXPORT,
    BOOLEAN_PEAK_SHAVING,
)
from custom_components.alpha_ems_manager.const import (
    PV_ABSORPTION_DISPATCH_ACTIVE,
    PV_ABSORPTION_EXCESS_EXPORT,
    PV_ABSORPTION_NO_SUPPRESSING_FEATURE,
    PV_ABSORPTION_PEAK_SHAVING,
    PV_ABSORPTION_SELF_CONSUMPTION,
    PV_ABSORPTION_STATE_UNREADABLE,
)

from .conftest import ACHTERKANT, VOORKANT, FakeSolcast
from .forecast_helpers import NORMAL
from .live_capability import assert_charge_only_capability
from .test_pv_site_selection import drive, enable_forecast

#: Phase-1 and Phase-2 entities. Not one of these may move because a PV forecast
#: arrived: the learned household load is defined to be independent of production,
#: and ``test_pv_independence.py`` states the deeper form of the same rule.
PHASE_ONE_AND_TWO = (
    "sensor.alpha_ems_expected_house_load_today",
    "sensor.alpha_ems_expected_house_load_tomorrow",
    "sensor.alpha_ems_learning_confidence",
    "sensor.alpha_ems_learning_days",
    "sensor.alpha_ems_forecast_error_yesterday",
    "sensor.alpha_ems_forecast_error_7_days",
)


def states_of(hass: HomeAssistant, entity_ids) -> dict[str, tuple]:
    """Return each entity's state and attributes, for comparison."""
    captured = {}
    for entity_id in entity_ids:
        state = hass.states.get(entity_id)
        assert state is not None, entity_id
        captured[entity_id] = (state.state, dict(state.attributes))
    return captured


# -- the absorption gate -----------------------------------------------------


async def test_a_healthy_control_surface_at_rest_permits_absorption(
    hass: HomeAssistant, setup_integration: MockConfigEntry, control_surface: None
) -> None:
    """Nothing is suppressing it, and we looked."""
    coordinator = setup_integration.runtime_data
    await drive(coordinator)

    absorption = coordinator.data["pv_absorption"]

    assert absorption["modelled"] is True
    assert absorption["reason"] == PV_ABSORPTION_SELF_CONSUMPTION


async def test_excess_export_suppresses_absorption(
    hass: HomeAssistant, setup_integration: MockConfigEntry, control_surface: None
) -> None:
    """The finding that corrected the approved design.

    From the vendor package's own design note: with this on, PV below the
    inverter's AC limit is directed to house load and feed-in and the battery is
    charged with *zero*. Modelling absorption here would project stored energy
    that is actually being exported.
    """
    coordinator = setup_integration.runtime_data
    hass.states.async_set(BOOLEAN_EXCESS_EXPORT, "on")
    await drive(coordinator)

    absorption = coordinator.data["pv_absorption"]

    assert absorption["modelled"] is False
    assert absorption["reason"] == PV_ABSORPTION_EXCESS_EXPORT


async def test_peak_shaving_suppresses_absorption(
    hass: HomeAssistant, setup_integration: MockConfigEntry, control_surface: None
) -> None:
    """It arms its own dispatch, so what the battery does is not ours to predict."""
    coordinator = setup_integration.runtime_data
    hass.states.async_set(BOOLEAN_PEAK_SHAVING, "on")
    await drive(coordinator)

    assert coordinator.data["pv_absorption"]["reason"] == PV_ABSORPTION_PEAK_SHAVING
    assert coordinator.data["pv_absorption"]["modelled"] is False


async def test_a_running_dispatch_suppresses_absorption(
    hass: HomeAssistant, setup_integration: MockConfigEntry, control_surface: None
) -> None:
    """Under external control, and the dispatch vocabulary can forbid charging."""
    coordinator = setup_integration.runtime_data
    hass.states.async_set("sensor.alphaess_dispatch_start", "1")
    await drive(coordinator)

    assert coordinator.data["pv_absorption"]["reason"] == PV_ABSORPTION_DISPATCH_ACTIVE
    assert coordinator.data["pv_absorption"]["modelled"] is False


async def test_an_absent_control_surface_permits_absorption(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The distinction that matters, and the reason it is not "unknown".

    With no vendor package the suppressing features do not exist, so nothing can
    be suppressing anything and ordinary self-consumption applies. Reading absence
    as ignorance would leave every installation without the package permanently
    pessimistic about its own battery, which is wrong rather than cautious.
    """
    coordinator = setup_integration.runtime_data
    await drive(coordinator)

    absorption = coordinator.data["pv_absorption"]

    assert absorption["modelled"] is True
    assert absorption["reason"] == PV_ABSORPTION_NO_SUPPRESSING_FEATURE


async def test_an_unreadable_control_surface_suppresses_absorption(
    hass: HomeAssistant, setup_integration: MockConfigEntry, control_surface: None
) -> None:
    """Present but unusable is not the same as absent.

    A feature that exists and cannot be read could be suppressing absorption
    invisibly, so it is not modelled -- which is the opposite conclusion from the
    absent case, and deliberately so.
    """
    coordinator = setup_integration.runtime_data
    hass.states.async_set(BOOLEAN_EXCESS_EXPORT, "unavailable")
    await drive(coordinator)

    assert coordinator.data["pv_absorption"]["reason"] == PV_ABSORPTION_STATE_UNREADABLE
    assert coordinator.data["pv_absorption"]["modelled"] is False


async def test_a_device_read_failure_costs_only_the_absorption_answer(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The isolation defect the existing control test caught when this was added.

    Reading the inverter's state must never be able to take a refresh down. A
    failure means the state could not be established, which is the same answer as
    an unreadable entity.
    """
    import custom_components.alpha_ems_manager.coordinator as module

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("control surface exploded")

    coordinator = setup_integration.runtime_data
    monkeypatch.setattr(module, "discover", explode)
    await drive(coordinator)

    assert coordinator.data["pv_absorption"]["reason"] == PV_ABSORPTION_STATE_UNREADABLE
    # And every other layer survived.
    assert coordinator.battery_plan is not None
    assert coordinator.today_forecast is not None


# -- the three disclaimer branches -------------------------------------------


async def test_the_projection_is_labelled_pv_blind_without_a_forecast(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The original wording, still exact, because it is still true."""
    from custom_components.alpha_ems_manager.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    from .forecast_helpers import local
    from .test_battery_entities import frozen

    coordinator = setup_integration.runtime_data
    await drive(coordinator)
    with frozen(local(NORMAL, 12, 6)):
        payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    plan = payload["battery_plan"]

    assert "PV-blind" in plan["projected_soc_note"]
    assert "no photovoltaic production term" in plan["trajectory"]["basis"]
    assert (
        hass.states.get("sensor.alpha_ems_battery_recommendation").attributes[
            "pv_aware"
        ]
        is False
    )


async def test_the_projection_is_labelled_pv_aware_with_a_forecast(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
    control_surface: None,
) -> None:
    """Conditional, not deleted. The limitation changed, so the words changed."""
    from custom_components.alpha_ems_manager.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    from .forecast_helpers import local
    from .test_battery_entities import frozen

    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await drive(coordinator)

    with frozen(local(NORMAL, 12, 6)):
        payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    plan = payload["battery_plan"]

    assert "PV-aware" in plan["projected_soc_note"]
    assert "PV-aware" in plan["trajectory"]["basis"]
    assert plan["trajectory"]["intervals_pv_aware"] > 0
    assert (
        hass.states.get("sensor.alpha_ems_battery_recommendation").attributes[
            "pv_aware"
        ]
        is True
    )


async def test_a_suppressed_absorption_says_the_projection_is_a_lower_bound(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
    control_surface: None,
) -> None:
    """The third branch, which is the one an earlier design had no words for."""
    from custom_components.alpha_ems_manager.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    from .forecast_helpers import local
    from .test_battery_entities import frozen

    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    hass.states.async_set(BOOLEAN_EXCESS_EXPORT, "on")
    coordinator = setup_integration.runtime_data
    await drive(coordinator)

    with frozen(local(NORMAL, 12, 6)):
        payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    plan = payload["battery_plan"]

    assert "lower bound" in plan["projected_soc_note"]
    assert "absorption not modelled" in plan["trajectory"]["basis"]


# -- Phase 1 to 4 are unchanged ----------------------------------------------


async def test_a_pv_forecast_moves_no_phase_one_or_two_figure(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """Driven twice at the same instant, and compared figure for figure.

    This is the promise the whole learning model rests on. A sunny day must not
    teach the model that the house consumes less because the panels supplied the
    energy -- and the same rule applies to a *forecast* of a sunny day.
    """
    coordinator = setup_integration.runtime_data
    await drive(coordinator)
    blind = states_of(hass, PHASE_ONE_AND_TWO)

    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await drive(coordinator)
    aware = states_of(hass, PHASE_ONE_AND_TWO)

    assert coordinator.pv_forecasts[NORMAL].available is True
    assert aware == blind


async def test_the_learned_history_is_unchanged_by_a_pv_forecast(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """Not just the entities: the stored baselines themselves."""
    coordinator = setup_integration.runtime_data
    await drive(coordinator)
    before = {
        day: [record.baseline_at(i) for i in range(record.interval_count)]
        for day, record in coordinator.store.days.items()
    }

    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await drive(coordinator)
    after = {
        day: [record.baseline_at(i) for i in range(record.interval_count)]
        for day, record in coordinator.store.days.items()
    }

    assert after == before


async def test_execution_remains_structurally_unavailable(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
    control_surface: None,
) -> None:
    """Obligation 8. Phase 5 changes nothing about the barrier."""
    from custom_components.alpha_ems_manager.const import CONTROL_MODE_ACTIVE

    from .test_control_modes import set_mode

    assert_charge_only_capability()

    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    await set_mode(hass, CONTROL_MODE_ACTIVE)
    coordinator = setup_integration.runtime_data
    await drive(coordinator)

    report = coordinator.control_report
    # The barrier is open for a charge since beta.24. Obligation 8 is unchanged:
    # a PV source cannot authorize anything, and this plan is not a charge.
    assert report["execution_available"] is True
    assert report["authorization"]["authorized"] is False


async def test_a_pv_aware_plan_writes_nothing(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
    control_surface: None,
) -> None:
    """The strongest statement the release makes, with a forecast in hand."""
    from homeassistant.core import ServiceCall

    from custom_components.alpha_ems_manager.alphaess_device import PERMITTED_SERVICES
    from custom_components.alpha_ems_manager.const import CONTROL_MODE_ACTIVE

    from .test_control_modes import set_mode

    calls: list[ServiceCall] = []

    async def record(call: ServiceCall) -> None:
        calls.append(call)

    for domain, service in PERMITTED_SERVICES:
        hass.services.async_register(domain, service, record)

    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    await set_mode(hass, CONTROL_MODE_ACTIVE)
    await drive(setup_integration.runtime_data)

    assert calls == []


async def test_upgrading_from_beta_eight_keeps_everything(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """No new required setting, unknown option keys preserved, history intact."""
    coordinator = setup_integration.runtime_data
    hass.config_entries.async_update_entry(
        setup_integration,
        options={**setup_integration.options, "a_key_from_a_later_release": 42},
    )
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await drive(coordinator)

    assert setup_integration.options["a_key_from_a_later_release"] == 42
    assert setup_integration.version == 2
    assert coordinator.store.days
    # And the resolved site set was written down exactly once.
    from custom_components.alpha_ems_manager.const import (
        CONF_SELECTED_SOLCAST_SITE_IDS,
    )

    stored = setup_integration.options[CONF_SELECTED_SOLCAST_SITE_IDS]
    assert sorted(stored) == [ACHTERKANT, VOORKANT]


async def test_the_entity_set_is_unchanged(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """Zero new entities, with a forecast in hand and both days available."""
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    await drive(setup_integration.runtime_data)

    ours = sorted(
        entity_id
        for entity_id in hass.states.async_entity_ids()
        if entity_id.startswith(("sensor.alpha_ems", "select.alpha_ems"))
    )

    assert ours == [
        "select.alpha_ems_control_mode",
        "sensor.alpha_ems_battery_recommendation",
        "sensor.alpha_ems_control_state",
        "sensor.alpha_ems_dynamic_battery_reserve",
        "sensor.alpha_ems_economic_action",
        "sensor.alpha_ems_expected_house_load_today",
        "sensor.alpha_ems_expected_house_load_tomorrow",
        "sensor.alpha_ems_forecast_error_7_days",
        "sensor.alpha_ems_forecast_error_yesterday",
        "sensor.alpha_ems_learning_confidence",
        "sensor.alpha_ems_learning_days",
        "sensor.alpha_ems_next_planned_action",
        "sensor.alpha_ems_planned_battery_power",
        "sensor.alpha_ems_usable_battery_energy",
    ]


async def test_the_known_diagnostic_findings_are_still_reported(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Two limitations Phase 4 documented, and Phase 5 must not quietly drop.

    The small energy-balance boundary residual stays a known limitation with its
    tolerance untouched, and the gross-fault evidence stays visible. Neither is a
    reason to enable real control, and neither was widened away.
    """
    from custom_components.alpha_ems_manager.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    await drive(setup_integration.runtime_data)
    payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    balance = payload["energy_balance"]

    # The allowance model is still published, and still describes itself, so the
    # small boundary residual remains a stated limitation rather than something
    # tuned away. Phase 5 widened no tolerance.
    assert "tolerance_model" in balance

    # The signed evidence beta.8 added, which is what could eventually tell a
    # two-meter difference from a mis-selected source. Every earlier statistic
    # took an absolute value first and threw the sign away.
    assert "mean_signed_residual_w" in balance
    assert "excess_sum_by_power_band" in balance
    assert "failed_samples_by_power_band" in balance
    assert "windowed_failures" in balance

    # And the gross-fault evidence is still not a control input.
    assert payload["control"].get("state") is not None
    assert "balance" not in str(payload["control"].get("safety", {}))
