"""Which Solcast sites belong to this installation, declared rather than assumed.

A Solcast account can hold rooftop sites that have nothing to do with the AlphaESS
system a config entry manages: a second property, a neighbour's array, a retired
system. Consuming the aggregate unconditionally folds those into the plan, and no
amount of provenance recovers a number that was already wrong when it was summed.

So the user is asked exactly one question -- which sites belong here -- and never
asked to classify a site as AC- or DC-coupled, hybrid-side or grid-inverter-side.
That correspondence is not reliably known to a user, and a guessed topology
recorded as fact is worse than the declared unknown stored instead.

The three states this file keeps apart, because they are three different facts:
no answer stored yet, a stored answer, and a stored *empty* answer.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.const import (
    CONF_SELECTED_SOLCAST_SITE_IDS,
    CONF_SOLCAST_ENTRY_ID,
    CONF_USE_PV_FORECAST,
    PV_QUERY_MODE_AGGREGATE,
    PV_QUERY_MODE_PER_SITE,
    PV_SELECTION_ORIGIN_AUTO,
    PV_SELECTION_ORIGIN_STORED,
    PV_UNAVAILABLE_EMPTY_SELECTION,
    PV_UNAVAILABLE_NO_SITES_DISCOVERED,
    PV_UNAVAILABLE_NOT_CONFIGURED,
    PV_UNAVAILABLE_SERVICE_FAILED,
    PV_UNAVAILABLE_SERVICE_MISSING,
)

from .conftest import ACHTERKANT, VOORKANT, FakeSolcast
from .forecast_helpers import NORMAL, history_before, local, refresh_at, seed


def enable_forecast(
    entry: MockConfigEntry,
    hass: HomeAssistant,
    solcast_entry: MockConfigEntry,
    **options: Any,
) -> None:
    """Point the entry at the Solcast entry and turn the forecast on."""
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_USE_PV_FORECAST: True,
            CONF_SOLCAST_ENTRY_ID: solcast_entry.entry_id,
            **options,
        },
    )


async def drive(
    coordinator, *, hour: int = 12, solcast: FakeSolcast | None = None
) -> None:
    """Give the model history and refresh at a fixed instant.

    Clears the recorded requests immediately before the refresh being measured.
    Setting the entry up performs a refresh of its own at the real clock, so a
    call count taken across the whole helper would describe two refreshes on two
    different days rather than the one under test.
    """
    seed(coordinator, history_before(NORMAL))
    if solcast is not None:
        solcast.forecast_calls.clear()
    await refresh_at(coordinator, local(NORMAL, hour, 5))


def today_forecast(coordinator):
    """Return the forecast for the driven day."""
    return coordinator.pv_forecasts[NORMAL]


# -- the boundary is not configured ------------------------------------------


async def test_the_forecast_is_unavailable_when_the_user_has_not_enabled_it(
    hass: HomeAssistant, setup_integration: MockConfigEntry, solcast: FakeSolcast
) -> None:
    """Named, not empty, and no action is called at all."""
    coordinator = setup_integration.runtime_data
    await drive(coordinator)

    forecast = today_forecast(coordinator)

    assert forecast.available is False
    assert forecast.unavailable_reason == PV_UNAVAILABLE_NOT_CONFIGURED
    assert solcast.diagnostic_calls == 0
    assert solcast.forecast_calls == []


async def test_a_missing_query_action_is_reported_rather_than_raised(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
) -> None:
    """Solcast loaded but its action absent: a fact, not an exception.

    Deliberately does not use the ``solcast`` fixture, so the entry is loaded and
    the actions are simply not registered -- which is what an older Solcast
    release looks like.
    """
    solcast_config_entry.mock_state(hass, ConfigEntryState.LOADED)
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    await drive(setup_integration.runtime_data)

    forecast = setup_integration.runtime_data.pv_forecasts[NORMAL]

    assert forecast.available is False
    assert forecast.unavailable_reason == PV_UNAVAILABLE_SERVICE_MISSING


# -- first discovery: the default, persisted once -----------------------------


async def test_first_discovery_selects_every_site_and_stores_the_answer(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """A default is fine. A default that is never written down is not.

    Resolving "all of them" afresh on every refresh would mean a site added to
    Solcast next year silently joined this installation's plan, which is the exact
    failure this option exists to prevent.
    """
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await drive(coordinator)

    stored = setup_integration.options[CONF_SELECTED_SOLCAST_SITE_IDS]

    assert sorted(stored) == [ACHTERKANT, VOORKANT]
    assert coordinator.config.solcast_selection_stored is True

    forecast = today_forecast(coordinator)
    assert forecast.available is True
    assert forecast.provenance.selection_complete is True
    # By this refresh the resolved set has already been written down, so the
    # origin is what is actually known: it was read from the entry. Only the one
    # refresh that resolved it reports ``auto_initial``, and the two are not the
    # same claim.
    assert forecast.provenance.selection_origin == PV_SELECTION_ORIGIN_STORED
    assert PV_SELECTION_ORIGIN_AUTO != PV_SELECTION_ORIGIN_STORED


async def test_a_site_added_after_the_answer_was_stored_does_not_join_it(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """Available, reported, and not selected. This is the whole point.

    An installation that grew a second property in Solcast must not have that
    property's generation quietly added to this house's plan.
    """
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await drive(coordinator)

    solcast.sites.append(
        {
            "resource_id": "site-elsewhere",
            "name": "Holiday house",
            "capacity": 4.0,
            "capacity_dc": 4.0,
            "azimuth": 0.0,
            "tilt": 30.0,
            "loss_factor": 0.9,
        }
    )
    solcast.power_by_site["site-elsewhere"] = 99.0
    await drive(coordinator)

    forecast = today_forecast(coordinator)

    assert forecast.provenance.available_site_count == 3
    assert forecast.provenance.selected_site_count == 2
    assert "site-elsewhere" not in forecast.provenance.selected_site_ids
    assert forecast.provenance.selection_complete is False
    # And the third site's generation appears nowhere.
    assert forecast.provenance.query_mode == PV_QUERY_MODE_PER_SITE
    assert all(call.get("site") != "site-elsewhere" for call in solcast.forecast_calls)


async def test_a_failed_discovery_stores_nothing_and_guesses_nothing(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """Writing a selection from a failed read would be inventing a declaration."""
    solcast.fail_diagnostic = True
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await drive(coordinator)

    forecast = today_forecast(coordinator)

    assert forecast.available is False
    assert forecast.unavailable_reason == PV_UNAVAILABLE_NO_SITES_DISCOVERED
    assert CONF_SELECTED_SOLCAST_SITE_IDS not in setup_integration.options


async def test_a_failed_discovery_does_not_overwrite_an_existing_selection(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """A Solcast reload must not silently change which roofs the plan is about."""
    enable_forecast(
        setup_integration,
        hass,
        solcast_config_entry,
        **{CONF_SELECTED_SOLCAST_SITE_IDS: [VOORKANT]},
    )
    await hass.config_entries.async_reload(setup_integration.entry_id)
    solcast.fail_diagnostic = True
    coordinator = setup_integration.runtime_data
    await drive(coordinator)

    assert setup_integration.options[CONF_SELECTED_SOLCAST_SITE_IDS] == [VOORKANT]
    assert coordinator.config.selected_solcast_site_ids == (VOORKANT,)


# -- a stored answer ---------------------------------------------------------


async def test_all_sites_selected_uses_one_aggregate_request(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """One call, no site key, and the source's own percentile bands."""
    enable_forecast(
        setup_integration,
        hass,
        solcast_config_entry,
        **{
            CONF_SELECTED_SOLCAST_SITE_IDS: [
                ACHTERKANT,
                VOORKANT,
            ]
        },
    )
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await drive(coordinator, solcast=solcast)

    forecast = today_forecast(coordinator)

    assert len(solcast.forecast_calls) == 1
    assert "site" not in solcast.forecast_calls[0]
    assert forecast.provenance.query_mode == PV_QUERY_MODE_AGGREGATE
    assert forecast.available is True
    # 5 kW aggregate for the whole day: 96 quarters of 1.25 kWh.
    assert forecast.total_kwh == pytest.approx(96 * 5.0 * 0.25)


async def test_a_subset_queries_each_selected_site_and_sums_them(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """Two of three selected: two calls, and the third contributes nothing."""
    solcast.sites.append(
        {
            "resource_id": "site-third",
            "name": "Shed",
            "capacity": 2.0,
            "capacity_dc": 2.0,
            "azimuth": 0.0,
            "tilt": 20.0,
            "loss_factor": 0.9,
        }
    )
    solcast.power_by_site["site-third"] = 50.0
    enable_forecast(
        setup_integration,
        hass,
        solcast_config_entry,
        **{
            CONF_SELECTED_SOLCAST_SITE_IDS: [
                ACHTERKANT,
                VOORKANT,
            ]
        },
    )
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await drive(coordinator)

    forecast = today_forecast(coordinator)
    queried = {call.get("site") for call in solcast.forecast_calls}

    assert queried == {ACHTERKANT, VOORKANT}
    assert forecast.provenance.query_mode == PV_QUERY_MODE_PER_SITE
    # 2 kW plus 3 kW, per interval, and nothing from the 50 kW shed.
    assert forecast.intervals[48] == pytest.approx(5.0 * 0.25)


async def test_one_site_selected_works_normally(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """A single-site installation is not a special case."""
    enable_forecast(
        setup_integration,
        hass,
        solcast_config_entry,
        **{CONF_SELECTED_SOLCAST_SITE_IDS: [ACHTERKANT]},
    )
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await drive(coordinator)

    forecast = today_forecast(coordinator)

    assert forecast.available is True
    assert forecast.intervals[48] == pytest.approx(2.0 * 0.25)
    assert forecast.provenance.selected_site_count == 1


async def test_a_selected_site_that_vanished_is_reported_and_never_dropped(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """A Solcast outage cannot quietly narrow a declaration.

    The identifier stays in the stored selection and in provenance, so the fact
    that a declared roof is missing is visible rather than being absorbed as a
    smaller forecast.
    """
    enable_forecast(
        setup_integration,
        hass,
        solcast_config_entry,
        **{
            CONF_SELECTED_SOLCAST_SITE_IDS: [
                ACHTERKANT,
                VOORKANT,
            ]
        },
    )
    await hass.config_entries.async_reload(setup_integration.entry_id)
    solcast.sites = [site for site in solcast.sites if site["resource_id"] != VOORKANT]
    coordinator = setup_integration.runtime_data
    await drive(coordinator)

    forecast = today_forecast(coordinator)

    assert VOORKANT in forecast.provenance.selected_site_ids
    assert forecast.provenance.selected_site_count == 2
    assert forecast.provenance.available_site_count == 1
    assert forecast.provenance.selection_complete is False
    assert setup_integration.options[CONF_SELECTED_SOLCAST_SITE_IDS] == [
        ACHTERKANT,
        VOORKANT,
    ]


async def test_a_stored_empty_selection_is_a_named_unavailability(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """A decision, not an absence. Falling back to all of them would overrule it."""
    enable_forecast(
        setup_integration,
        hass,
        solcast_config_entry,
        **{CONF_SELECTED_SOLCAST_SITE_IDS: []},
    )
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await drive(coordinator)

    forecast = today_forecast(coordinator)

    assert forecast.available is False
    assert forecast.unavailable_reason == PV_UNAVAILABLE_EMPTY_SELECTION
    assert solcast.forecast_calls == []


# -- partial coverage --------------------------------------------------------


async def test_a_silent_selected_site_leaves_a_partial_sum_never_a_zero(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """The sum of what reported, and the count that says how many did."""
    solcast.sites.append(
        {
            "resource_id": "site-third",
            "name": "Shed",
            "capacity": 2.0,
            "capacity_dc": 2.0,
            "azimuth": 0.0,
            "tilt": 20.0,
            "loss_factor": 0.9,
        }
    )
    solcast.power_by_site["site-third"] = 4.0
    solcast.silent_sites = {"site-third"}
    # A strict subset: two of the three discovered sites are declared, so the
    # per-site path is taken. Selecting all three would make the selection
    # complete and the source's own aggregate would be used instead, which has no
    # per-site coverage to be partial about.
    enable_forecast(
        setup_integration,
        hass,
        solcast_config_entry,
        **{
            CONF_SELECTED_SOLCAST_SITE_IDS: [
                ACHTERKANT,
                "site-third",
            ]
        },
    )
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await drive(coordinator, solcast=solcast)

    forecast = today_forecast(coordinator)

    assert forecast.available is True
    # One of the two reported, so the interval carries 2.0 and not 2.0 + 0.
    assert forecast.intervals[48] == pytest.approx(2.0 * 0.25)
    assert forecast.sites_contributing[48] == 1
    assert forecast.partial_site_intervals > 0


async def test_every_site_failing_is_unavailable_rather_than_empty(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """A forecast of nothing must never be published as a forecast."""
    solcast.fail_forecast = True
    enable_forecast(
        setup_integration,
        hass,
        solcast_config_entry,
        **{CONF_SELECTED_SOLCAST_SITE_IDS: [ACHTERKANT]},
    )
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await drive(coordinator)

    forecast = today_forecast(coordinator)

    assert forecast.available is False
    assert forecast.unavailable_reason == PV_UNAVAILABLE_SERVICE_FAILED
    assert set(forecast.intervals) == {None}


# -- provenance --------------------------------------------------------------


async def test_provenance_records_what_the_source_said(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """Read, never inferred -- including the two capacity totals."""
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await drive(coordinator)

    provenance = today_forecast(coordinator).provenance

    assert provenance.integration_version == "v4.6.1"
    assert provenance.estimate_key == "estimate"
    assert provenance.auto_dampening_active is False
    assert provenance.dampened is False
    assert provenance.get_actuals is False
    assert provenance.use_actuals == 0
    assert provenance.api_limit == 10
    assert provenance.api_used == 8
    assert provenance.forecast_health == "fresh"
    # 3.65 + 2.43, the live figures.
    assert provenance.selected_capacity_dc_total_kw == pytest.approx(6.08)
    assert provenance.selected_capacity_ac_total_kw == pytest.approx(10.0)
    assert provenance.electrical_correspondence == "unknown"
    assert provenance.membership_declared is True


async def test_the_hard_limit_is_recorded_as_a_value_and_a_judgement(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """100.0 against a six-kilowatt array cannot clip it, and a boolean would lie.

    A bare "configured: true" would have implied the source models this
    installation's clipping. It demonstrably does not, and the difference matters
    to the phase that eventually reads forecast-versus-actual error.
    """
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await drive(coordinator)

    provenance = today_forecast(coordinator).provenance

    assert provenance.hard_limit_raw == 100.0
    assert provenance.hard_limit_binding is False


async def test_a_hard_limit_below_the_array_is_recorded_as_binding(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """The other side of the judgement, so it is not a constant dressed as one."""
    solcast.configuration["hard_limit"] = 3.0
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await drive(coordinator)

    assert today_forecast(coordinator).provenance.hard_limit_binding is True


async def test_no_api_key_material_reaches_provenance_or_diagnostics(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """The response carries a key. Nothing downstream may.

    Asserted against the serialised forms rather than the dataclass fields,
    because serialisation is where a stray copy would actually escape.
    """
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await drive(coordinator)

    rendered = repr(today_forecast(coordinator).provenance.as_dict())
    facts = repr(coordinator.pv_facts.as_dict())

    assert "SECRET-KEY-VALUE" not in rendered
    assert "SECRET-KEY-VALUE" not in facts
    assert "api_key" not in rendered
    assert "api_key" not in facts


async def test_renaming_a_site_changes_neither_fingerprint(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """The same roof under a different label is the same roof.

    Driven end to end rather than against the fingerprint function alone, so a
    display name leaking into identity anywhere along the path is caught.
    """
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await drive(coordinator)
    before = today_forecast(coordinator).provenance

    solcast.sites[0]["name"] = "Back roof"
    await drive(coordinator)
    after = today_forecast(coordinator).provenance

    assert after.selected_sites_identity == before.selected_sites_identity
    assert after.selected_sites_model == before.selected_sites_model
    # The new name is visible, though: it is display information, not identity.
    assert "Back roof" in after.selected_site_display_names


async def test_changing_membership_changes_the_identity_fingerprint(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """The hard barrier: evidence either side of this is never pooled."""
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await drive(coordinator)
    before = today_forecast(coordinator).provenance.selected_sites_identity

    hass.config_entries.async_update_entry(
        setup_integration,
        options={
            **setup_integration.options,
            CONF_SELECTED_SOLCAST_SITE_IDS: [ACHTERKANT],
        },
    )
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await drive(coordinator)

    assert today_forecast(coordinator).provenance.selected_sites_identity != before


async def test_a_physical_model_change_changes_the_model_fingerprint(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """A re-tilted roof produces a different series while keeping its identity."""
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await drive(coordinator)
    before = today_forecast(coordinator).provenance

    solcast.sites[0]["tilt"] = 15.0
    await drive(coordinator)
    after = today_forecast(coordinator).provenance

    assert after.selected_sites_identity == before.selected_sites_identity
    assert after.selected_sites_model != before.selected_sites_model


# -- the query window --------------------------------------------------------


async def test_one_request_covers_both_days(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """The same rows are mapped twice rather than fetched twice."""
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await drive(coordinator, solcast=solcast)

    assert len(solcast.forecast_calls) == 1
    assert coordinator.pv_forecasts[NORMAL].available is True
    assert coordinator.pv_forecasts[NORMAL + timedelta(days=1)].available is True


async def test_the_request_is_undampened_explicitly(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """Asked for, not left to a default that could change under us."""
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    await drive(setup_integration.runtime_data)

    assert solcast.forecast_calls[0]["undampened"] is False


# -- no new entities ---------------------------------------------------------


async def test_no_entity_is_created_per_site(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """Site detail belongs in options and diagnostics, not in the state machine."""
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    await drive(setup_integration.runtime_data)

    ours = [
        entity_id
        for entity_id in hass.states.async_entity_ids()
        if entity_id.startswith(("sensor.alpha_ems", "select.alpha_ems"))
    ]

    assert not any("site" in entity_id for entity_id in ours)
    assert not any("achterkant" in entity_id.lower() for entity_id in ours)
    assert not any("solcast" in entity_id for entity_id in ours)


# -- daylight ----------------------------------------------------------------


async def test_a_daylight_window_is_computed_for_both_days(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """Advisory, and it must exist for tomorrow as well as today.

    Tomorrow is the half that ``sun.sun`` cannot supply, since it exposes only the
    next sunrise and sunset.
    """
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await drive(coordinator)

    for day in (NORMAL, NORMAL + timedelta(days=1)):
        window = coordinator.pv_forecasts[day].daylight
        assert len(window) == coordinator.pv_forecasts[day].interval_count
        assert any(window), day
        assert not all(window), day


async def test_a_flat_forecast_across_the_night_is_counted_as_an_anomaly(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """The detector for a timezone or offset bug, on the live path.

    The fake source returns constant power around the clock, which is physically
    impossible and exactly the shape an offset error produces. It is counted and
    the values are left completely alone.
    """
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await drive(coordinator)

    forecast = today_forecast(coordinator)

    assert forecast.non_daylight_generation_intervals > 0
    # Reported, never corrected.
    assert forecast.intervals[0] == pytest.approx(5.0 * 0.25)
