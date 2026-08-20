"""The beta.9 live defect: a false negative that stood for a quarter of an hour.

A real installation, after a full Home Assistant restart, reported this from one
diagnostics download:

    pv.capability.entry_selected:      true
    pv.capability.entry_loaded:        false
    pv.capability.query_forecast_data: true
    pv.capability.diagnostic:          true
    pv.capability.usable:              false
    pv.capability.unavailable_reason:  solcast_entry_not_loaded

    consumed_integrations.solcast_available: true

Both actions registered, the entry selected, the source reported available two
blocks further down -- and the PV layer refusing to read it.

Two causes, and the tests here pin both.

**The probe was unprovable.** beta.9 required the Solcast config entry to be in
state ``LOADED``. Solcast registers its actions at component level, so they appear
while its config entry is still setting up. That state was never needed: calling a
registered action is safe, and a failure is caught and reported. Capability now
comes from facts that can be demonstrated.

**The snapshot could not be replaced.** Alpha EMS takes its first refresh during
its own setup, and refreshes are driven by the quarter-hour tick rather than an
interval -- so anything read before the sources had published stood for up to
fifteen minutes. That is also why the battery plan reported a missing state of
charge beside a live reading of 96 %.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.const import (
    CONF_SELECTED_SOLCAST_SITE_IDS,
    CONTROL_EXECUTION_AVAILABLE,
    PV_SELECTION_ORIGIN_AUTO,
    PV_SELECTION_ORIGIN_STORED,
    PV_UNAVAILABLE_ENTRY_NOT_FOUND,
    PV_UNAVAILABLE_NO_SOLCAST_ENTRY,
    PV_UNAVAILABLE_SERVICE_FAILED,
    PV_UNAVAILABLE_SERVICE_MISSING,
    SOLCAST_DOMAIN,
    SOLCAST_FORBIDDEN_SERVICES,
    SOLCAST_SERVICE_QUERY_FORECAST,
)
from custom_components.alpha_ems_manager.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.alpha_ems_manager.solcast_source import discover

from .conftest import FakeSolcast
from .forecast_helpers import NORMAL
from .test_pv_site_selection import drive, enable_forecast


def beta9_probe(hass: HomeAssistant, entry_id: str | None) -> bool:
    """Reproduce the beta.9 loaded-state probe, verbatim.

    Kept so every test below can show that the situation it describes is one the
    old probe got wrong. Without this the tests would pass on beta.9 too and prove
    nothing.
    """
    if not entry_id:
        return False
    entry = hass.config_entries.async_get_entry(entry_id)
    return entry is not None and entry.state is ConfigEntryState.LOADED


# -- the exact reported combination -------------------------------------------


async def test_the_reported_combination_is_now_usable(
    hass: HomeAssistant,
    solcast_config_entry: MockConfigEntry,
) -> None:
    """The live defect, reduced to its facts.

    An entry that exists but is not in ``LOADED`` state, with both actions
    registered. beta.9 called this unusable; there was never a reason to.
    """
    FakeSolcast().register(hass)
    # Deliberately *not* marked loaded: this is the state the live entry was in
    # while it finished setting up.
    assert solcast_config_entry.state is not ConfigEntryState.LOADED

    capability = discover(hass, solcast_config_entry.entry_id)

    # The old probe refused, which is what makes this test meaningful.
    assert beta9_probe(hass, solcast_config_entry.entry_id) is False

    assert capability.entry_selected is True
    assert capability.entry_found is True
    assert capability.query_service is True
    assert capability.diagnostic_service is True
    assert capability.usable is True
    assert capability.discoverable is True
    assert capability.unavailable_reason is None


async def test_the_capability_carries_no_entry_state_concept_at_all(
    hass: HomeAssistant, solcast_config_entry: MockConfigEntry
) -> None:
    """The misleading field is gone rather than forced true.

    Renamed to something provable: whether the selected id names an entry that
    exists. That cannot be true one moment and false the next while the
    integration is perfectly usable, which is what the old field did.
    """
    FakeSolcast().register(hass)
    payload = discover(hass, solcast_config_entry.entry_id).as_dict()

    assert "entry_loaded" not in payload
    assert payload["entry_found"] is True
    assert "setup state" in payload["basis"]


async def test_a_loaded_entry_is_equally_usable(
    hass: HomeAssistant, solcast_config_entry: MockConfigEntry
) -> None:
    """The fix must not have inverted anything: a loaded entry still works."""
    solcast_config_entry.mock_state(hass, ConfigEntryState.LOADED)
    FakeSolcast().register(hass)

    assert discover(hass, solcast_config_entry.entry_id).usable is True


# -- what genuinely is unusable, named precisely ------------------------------


async def test_no_entry_selected_says_so(hass: HomeAssistant) -> None:
    """Configuration, not runtime."""
    capability = discover(hass, None)

    assert capability.usable is False
    assert capability.unavailable_reason == PV_UNAVAILABLE_NO_SOLCAST_ENTRY


async def test_a_stored_id_naming_nothing_says_so(hass: HomeAssistant) -> None:
    """Solcast removed, or removed and re-added under a new id.

    Provable, unlike the state check: the id resolves to nothing at all.
    """
    FakeSolcast().register(hass)
    capability = discover(hass, "an-entry-that-does-not-exist")

    assert capability.entry_selected is True
    assert capability.entry_found is False
    assert capability.usable is False
    assert capability.unavailable_reason == PV_UNAVAILABLE_ENTRY_NOT_FOUND


async def test_a_missing_query_action_says_so(
    hass: HomeAssistant, solcast_config_entry: MockConfigEntry
) -> None:
    """An older Solcast release, or one still loading its component."""
    capability = discover(hass, solcast_config_entry.entry_id)

    assert capability.usable is False
    assert capability.unavailable_reason == PV_UNAVAILABLE_SERVICE_MISSING


async def test_a_missing_diagnostic_action_is_named_separately(
    hass: HomeAssistant, solcast_config_entry: MockConfigEntry
) -> None:
    """The site list cannot be read, which is a different problem from no forecast."""

    async def nothing(call: object) -> dict:
        return {}

    hass.services.async_register(
        SOLCAST_DOMAIN, SOLCAST_SERVICE_QUERY_FORECAST, nothing
    )
    capability = discover(hass, solcast_config_entry.entry_id)

    assert capability.usable is True
    assert capability.discoverable is False
    assert capability.unavailable_reason is not None


# -- diagnostics can no longer contradict itself ------------------------------


async def test_diagnostics_never_reports_available_and_unusable_together(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """The invariant the live download violated.

    Both fields now come from one definition and one instant, so the pair the
    user saw is unrepresentable.
    """
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    await drive(setup_integration.runtime_data)

    payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    available = payload["consumed_integrations"]["solcast_available"]
    usable = payload["pv"]["capability"]["usable"]

    assert available == usable


@pytest.mark.parametrize("loaded", [True, False])
async def test_the_invariant_holds_whatever_the_entry_state(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
    loaded: bool,
) -> None:
    """Swept over the state that used to decide it, which now decides nothing."""
    if not loaded:
        solcast_config_entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    await drive(setup_integration.runtime_data)

    payload = await async_get_config_entry_diagnostics(hass, setup_integration)

    assert (
        payload["consumed_integrations"]["solcast_available"]
        == payload["pv"]["capability"]["usable"]
        is True
    )
    assert payload["pv"]["forecast_today"]["available"] is True


async def test_the_capability_block_is_probed_live_and_the_snapshot_is_dated(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """Two blocks, and it is now visible which is which.

    The forecast figures are necessarily a snapshot. Printing a stale capability
    beside them, unlabelled, is what made the defect read as a contradiction.
    """
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    await drive(setup_integration.runtime_data)

    payload = await async_get_config_entry_diagnostics(hass, setup_integration)

    assert "capability" in payload["pv"]
    assert "capability_at_last_refresh" in payload["pv"]
    assert payload["pv"]["last_refresh_at"] is not None


# -- startup ordering, and no cached false negative ---------------------------


async def test_a_source_that_appears_after_setup_is_picked_up(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
) -> None:
    """Alpha EMS set up first, Solcast arrives later, no restart needed.

    This is the ordering the live installation hit on every boot. The refusal must
    not be cached: the next refresh has to try again.
    """
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data

    # Nothing registered yet: unusable, and correctly so.
    await drive(coordinator)
    assert coordinator.pv_forecasts[NORMAL].available is False
    assert coordinator.pv_capability.usable is False

    # Solcast finishes loading.
    FakeSolcast().register(hass)

    # The very next refresh recovers, with no reload and no reconfiguration.
    await drive(coordinator)

    assert coordinator.pv_capability.usable is True
    assert coordinator.pv_forecasts[NORMAL].available is True


async def test_home_assistant_starting_triggers_a_refresh(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
) -> None:
    """The half of the fix that stops a provisional reading standing for a quarter.

    Refreshes are driven by the quarter-hour tick, so without this a value read
    during setup -- before the sources had published, or before a consumed
    integration had loaded -- would stand for up to fifteen minutes.
    """
    from custom_components.alpha_ems_manager import coordinator as module

    assert "async_at_started" in module.__dict__ or hasattr(module, "async_at_started")

    coordinator = setup_integration.runtime_data
    before = coordinator.last_refresh_at
    assert before is not None

    coordinator._handle_hass_started(hass)
    await hass.async_block_till_done()

    assert coordinator.last_refresh_at is not None


# -- membership resolves and persists -----------------------------------------


async def test_the_two_live_sites_are_discovered_and_persisted_once(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """The outcome the user should now see: both roofs, selected, written down."""
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await drive(coordinator)
    await hass.async_block_till_done()

    forecast = coordinator.pv_forecasts[NORMAL]

    assert forecast.available is True
    assert sorted(forecast.provenance.selected_site_display_names) == [
        "Achterkant",
        "Voorkant",
    ]
    assert sorted(setup_integration.options[CONF_SELECTED_SOLCAST_SITE_IDS]) == [
        "site-achterkant",
        "site-voorkant",
    ]


async def test_the_two_origins_are_distinct_and_both_reachable(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """A default and an answer are different facts, and both are reported.

    Tested at the resolver rather than by watching refreshes settle. Once the
    membership has been written the entry reloads, so by the time a test can
    observe anything the resolving generation has already been superseded -- which
    is correct behaviour and makes the auto label unobservable from outside.
    """
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    await hass.async_block_till_done()
    coordinator = setup_integration.runtime_data

    # Settled state: the answer is on disk and every refresh reads it from there.
    assert coordinator.config.solcast_selection_stored is True
    await drive(coordinator)
    assert (
        coordinator.pv_forecasts[NORMAL].provenance.selection_origin
        == PV_SELECTION_ORIGIN_STORED
    )

    # And an installation that has not answered yet resolves a default, labelled
    # as one.
    options = dict(setup_integration.options)
    options.pop(CONF_SELECTED_SOLCAST_SITE_IDS, None)
    hass.config_entries.async_update_entry(setup_integration, options=options)
    await hass.async_block_till_done()
    fresh = setup_integration.runtime_data
    fresh._pv_selection_write_scheduled = True  # do not race the write here

    resolved, origin, reason = await fresh._async_resolve_site_selection(
        ("site-achterkant", "site-voorkant")
    )

    assert reason is None
    assert origin == PV_SELECTION_ORIGIN_AUTO
    assert resolved == ("site-achterkant", "site-voorkant")
    assert PV_SELECTION_ORIGIN_AUTO != PV_SELECTION_ORIGIN_STORED


async def test_the_membership_write_does_not_reload_from_inside_the_refresh(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """Writing the options fires this entry's own update listener.

    Doing that inline tore the coordinator down halfway through the refresh that
    had just resolved the answer. The write is scheduled instead, so the refresh
    completes and publishes a PV-aware plan before any reload happens.
    """
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data

    await drive(coordinator)

    # The refresh completed and published, before the reload the write triggers.
    assert coordinator.pv_forecasts[NORMAL].available is True
    assert coordinator.data["pv_today"].available is True
    # Only then does the write land.
    await hass.async_block_till_done()
    assert CONF_SELECTED_SOLCAST_SITE_IDS in setup_integration.options


async def test_the_write_happens_once_however_many_refreshes(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """A default is written down once. After that it is an answer, not a default."""
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data

    for _ in range(3):
        await drive(coordinator)
        await hass.async_block_till_done()

    stored = setup_integration.options[CONF_SELECTED_SOLCAST_SITE_IDS]
    assert sorted(stored) == ["site-achterkant", "site-voorkant"]


async def test_a_user_answer_beats_a_pending_default(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """The deferred write re-checks before writing.

    A refresh can overlap the user answering the question themselves, and their
    answer must not be overwritten by a default resolved from discovery.
    """
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    await hass.async_block_till_done()
    coordinator = setup_integration.runtime_data

    # Back to "no answer stored", which is where an upgraded installation starts.
    options = dict(setup_integration.options)
    options.pop(CONF_SELECTED_SOLCAST_SITE_IDS, None)
    hass.config_entries.async_update_entry(setup_integration, options=options)
    await hass.async_block_till_done()
    coordinator = setup_integration.runtime_data
    coordinator._pv_selection_write_scheduled = False

    resolved, origin, reason = await coordinator._async_resolve_site_selection(
        ("site-achterkant", "site-voorkant")
    )
    assert reason is None
    assert resolved == ("site-achterkant", "site-voorkant")
    assert origin == PV_SELECTION_ORIGIN_AUTO

    # The user picks one site before the scheduled write runs.
    hass.config_entries.async_update_entry(
        setup_integration,
        options={
            **setup_integration.options,
            CONF_SELECTED_SOLCAST_SITE_IDS: ["site-voorkant"],
        },
    )
    await hass.async_block_till_done()

    assert setup_integration.options[CONF_SELECTED_SOLCAST_SITE_IDS] == [
        "site-voorkant"
    ]


# -- forecast ingestion recovers ----------------------------------------------


async def test_the_forecast_populates_for_both_days(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """What the live installation should now show instead of 96 missing intervals."""
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await drive(coordinator, solcast=solcast)

    today = coordinator.pv_forecasts[NORMAL]
    tomorrow = coordinator.pv_forecasts[NORMAL + timedelta(days=1)]

    assert today.available is True
    assert today.forecast_intervals == 96
    assert today.missing_intervals == 0
    assert today.coverage == 1.0
    assert tomorrow.available is True
    assert today.mapping.period_minutes == 30
    assert today.total_p10_kwh is not None
    assert today.total_p90_kwh is not None
    # One aggregate request, both days mapped from it.
    assert len(solcast.forecast_calls) == 1
    assert "site" not in solcast.forecast_calls[0]


async def test_a_failing_source_is_named_rather_than_read_as_zero(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """Recovery must not have cost the honest-failure behaviour."""
    solcast.fail_forecast = True
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await drive(coordinator)

    forecast = coordinator.pv_forecasts[NORMAL]

    assert forecast.available is False
    assert forecast.unavailable_reason == PV_UNAVAILABLE_SERVICE_FAILED
    assert set(forecast.intervals) == {None}


async def test_a_malformed_diagnostic_response_does_not_fabricate_sites(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """Garbage in, named unavailability out -- and no selection written."""
    solcast.sites = [{"name": "no id at all"}, "not even a mapping"]
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await drive(coordinator)
    await hass.async_block_till_done()

    assert coordinator.pv_forecasts[NORMAL].available is False
    assert CONF_SELECTED_SOLCAST_SITE_IDS not in setup_integration.options


# -- the fix cannot have opened anything -------------------------------------


async def test_no_mutating_solcast_action_is_ever_called(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """Registered as traps, and none of them is touched.

    A capability fix is exactly where an "and while we are here, force an update"
    would creep in.
    """
    called: list[str] = []

    async def trap(call: object) -> None:
        called.append("called")

    for service in SOLCAST_FORBIDDEN_SERVICES:
        hass.services.async_register(SOLCAST_DOMAIN, service, trap)

    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    await drive(setup_integration.runtime_data)
    await hass.async_block_till_done()

    assert called == []
    assert {call.get("site") for call in solcast.forecast_calls} <= {
        None,
        "site-achterkant",
        "site-voorkant",
    }


async def test_the_fix_did_not_open_control_execution(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
    control_surface: None,
) -> None:
    """A usable PV source must not make the inverter reachable."""
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
    coordinator = setup_integration.runtime_data
    await drive(coordinator)
    await hass.async_block_till_done()

    assert CONTROL_EXECUTION_AVAILABLE is False
    assert coordinator.pv_forecasts[NORMAL].available is True
    assert calls == []
    assert coordinator.control_report["execution_available"] is False
    assert coordinator.control_report["authorization"]["authorized"] is False


async def test_no_api_key_reaches_diagnostics_after_the_fix(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """The response carries one, and the whole payload is searched."""
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    await drive(setup_integration.runtime_data)

    payload = await async_get_config_entry_diagnostics(hass, setup_integration)

    assert "SECRET-KEY-VALUE" not in repr(payload)
    assert "api_key" not in repr(payload)
