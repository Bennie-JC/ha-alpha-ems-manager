"""Turning a live diagnostic response into discovered sites.

The beta.10 defect, exactly: an account with two rooftop sites reported

    pv.capability.usable:      true
    pv.capability.discoverable: true
    pv.source.site_count:      0
    pv.sites.discovered:       0
    forecast_today reason:     no_solcast_sites_discovered

Capability was fine. The response was fine. The *parse* was wrong: both Solcast
actions wrap their result, and beta.10 unwrapped ``data`` for the forecast query
but read the diagnostic at the top level. Every field therefore came back absent
at once -- no sites, no estimate key, no version -- which is what the download
showed.

The fake in ``conftest`` now returns the wrapped shape, so the whole suite
exercises the live response rather than the assumption that caused this. The tests
here pin the shape itself, and the site-identity rules that must survive it.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.const import (
    CONF_SELECTED_SOLCAST_SITE_IDS,
    PV_QUERY_MODE_AGGREGATE,
    PV_QUERY_MODE_PER_SITE,
    PV_UNAVAILABLE_NO_SITES_DISCOVERED,
    RESPONSE_SHAPE_FLAT,
    RESPONSE_SHAPE_NESTED,
    RESPONSE_SHAPE_UNUSABLE,
)
from custom_components.alpha_ems_manager.pv_forecast import (
    PvSite,
    sites_identity,
    sites_model,
)
from custom_components.alpha_ems_manager.solcast_source import (
    parse_diagnostic,
    unwrap_response,
)

from .conftest import ACHTERKANT, LIVE_SITES, VOORKANT, FakeSolcast
from .forecast_helpers import NORMAL
from .test_pv_site_selection import drive, enable_forecast

LIVE_CONFIG: dict[str, Any] = {
    "key_estimate": "estimate",
    "get_actuals": False,
    "use_actuals": 0,
    "auto_dampen": False,
    "hard_limit": 100.0,
    "excluded_sites": [],
    "api_key": "SECRET-KEY-VALUE",
}


def live_payload(sites: Any = None) -> dict[str, Any]:
    """Return the diagnostic payload as the live action reports it, unwrapped.

    ``sites=[]`` means an account with no sites, which is different from not
    specifying them -- so the default is checked against ``None`` rather than
    truthiness.
    """
    if sites is None:
        sites = LIVE_SITES
    return {
        "version": "v4.6.1",
        "api_limit": 10,
        "api_used": 8,
        "forecast_health": "fresh",
        "sites": [dict(site) for site in sites],
        "configuration": dict(LIVE_CONFIG),
        "dampening": {"enabled": False, "auto_dampening": False},
    }


# -- the response shape, which is the whole defect ---------------------------


def test_the_live_wrapped_response_yields_both_sites() -> None:
    """The failing case. beta.10 returned zero sites from exactly this input."""
    facts = parse_diagnostic({"data": live_payload()})

    assert facts.response_shape == RESPONSE_SHAPE_NESTED
    assert len(facts.sites) == 2
    assert set(facts.site_ids) == {ACHTERKANT, VOORKANT}
    assert facts.estimate_key == "estimate"
    assert facts.integration_version == "v4.6.1"
    assert facts.api_limit == 10
    assert sum(site.capacity_dc_kw for site in facts.sites) == pytest.approx(6.08)


def test_reading_the_payload_at_the_top_level_finds_nothing() -> None:
    """The beta.10 behaviour, reproduced, so the test above cannot pass vacuously.

    Every field absent at once is the signature. It is worth recognising, because
    that pattern means "looked in the wrong place", not "the source said nothing".
    """
    wrapped = {"data": live_payload()}

    assert wrapped.get("sites") is None
    assert wrapped.get("configuration") is None
    assert wrapped.get("version") is None


def test_the_flat_shape_is_still_accepted() -> None:
    """Tolerated deliberately: one branch, and it prevents a total loss of PV.

    If a future release stopped wrapping, refusing the flat shape would turn a
    convention change into every field vanishing again.
    """
    facts = parse_diagnostic(live_payload())

    assert facts.response_shape == RESPONSE_SHAPE_FLAT
    assert len(facts.sites) == 2


@pytest.mark.parametrize(
    "response", [None, "a string", 42, [], {"data": []}, {"data": "text"}]
)
def test_an_unusable_response_yields_no_sites_and_says_so(response: Any) -> None:
    """Never a fabricated site, and never an exception."""
    facts = parse_diagnostic(response)

    assert facts.sites == ()
    if response is None or not isinstance(response, dict):
        assert facts.response_shape == RESPONSE_SHAPE_UNUSABLE


def test_the_shape_is_reported_so_a_future_change_is_visible() -> None:
    """The lesson from the defect, made into a field.

    Everything coming back empty at once looked like "the account has no sites".
    Naming the shape means the next reader sees *where* it looked.
    """
    assert unwrap_response({"data": {"sites": []}})[1] == RESPONSE_SHAPE_NESTED
    assert unwrap_response({"sites": []})[1] == RESPONSE_SHAPE_FLAT
    assert unwrap_response(None)[1] == RESPONSE_SHAPE_UNUSABLE


def test_fields_alpha_ems_does_not_read_are_ignored_rather_than_copied() -> None:
    """The live response carries more than is needed, including a key.

    Only named fields are read out, which is what makes "no key material escapes"
    a property of the code rather than a promise.
    """
    facts = parse_diagnostic({"data": live_payload()})
    rendered = repr(facts.as_dict()) + repr([site.as_dict() for site in facts.sites])

    assert "SECRET-KEY-VALUE" not in rendered
    assert "install_date" not in rendered
    assert "compass_direction" not in rendered
    assert "tags" not in rendered


def test_an_empty_site_list_is_not_a_success() -> None:
    """An account with no sites is a named unavailability, not a working source."""
    facts = parse_diagnostic({"data": live_payload(sites=[])})

    assert facts.sites == ()
    assert facts.site_ids == ()


# -- how many sites, and what they are called --------------------------------


def sites_from(*specs: tuple[str, str]) -> list[dict[str, Any]]:
    """Return site records from ``(resource_id, name)`` pairs."""
    return [
        {
            "resource_id": resource_id,
            "name": name,
            "capacity": 5,
            "capacity_dc": 3.0,
            "azimuth": 0,
            "tilt": 30,
            "loss_factor": 0.9,
        }
        for resource_id, name in specs
    ]


@pytest.mark.parametrize("count", [1, 2, 3, 5, 9])
def test_any_number_of_sites_is_discovered(count: int) -> None:
    """One site is not a special case, and neither is nine.

    The UI must not assume two, which is all this installation happens to have.
    """
    specs = [(f"id-{index}", f"Roof {index}") for index in range(count)]
    facts = parse_diagnostic({"data": live_payload(sites=sites_from(*specs))})

    assert len(facts.sites) == count
    assert len(set(facts.site_ids)) == count


def test_two_sites_sharing_a_display_name_stay_two_sites() -> None:
    """Identity is the resource id. Names are decoration and may collide.

    A user with two arrays both called "Roof" must not silently lose one.
    """
    facts = parse_diagnostic(
        {"data": live_payload(sites=sites_from(("id-a", "Roof"), ("id-b", "Roof")))}
    )

    assert len(facts.sites) == 2
    assert set(facts.site_ids) == {"id-a", "id-b"}
    assert sites_identity(["id-a", "id-b"]) != sites_identity(["id-a"])


@pytest.mark.parametrize(
    "name",
    ["Achterkant", "Zuid­dak", "屋根", "Toit arrière", "Крыша", "roof 🌞", "  "],
)
def test_an_unusual_display_name_does_not_break_discovery(name: str) -> None:
    """Names are user text in whatever language, and are never parsed."""
    facts = parse_diagnostic({"data": live_payload(sites=sites_from(("id-a", name)))})

    assert len(facts.sites) == 1
    assert facts.sites[0].resource_id == "id-a"


def test_a_site_with_no_resource_id_is_dropped_and_the_rest_survive() -> None:
    """A record with no identity cannot be stored as membership."""
    broken = sites_from(("id-a", "Good"))
    broken.append({"name": "No id", "capacity": 5})

    facts = parse_diagnostic({"data": live_payload(sites=broken)})

    assert facts.site_ids == ("id-a",)


def test_the_order_the_source_returns_sites_in_changes_nothing() -> None:
    """Both fingerprints sort internally, so a reordered response is the same
    roof set."""
    forward = parse_diagnostic({"data": live_payload()})
    backward = parse_diagnostic(
        {"data": live_payload(sites=list(reversed(LIVE_SITES)))}
    )

    assert forward.site_ids == backward.site_ids
    assert sites_identity(forward.site_ids) == sites_identity(backward.site_ids)
    assert sites_model(forward.sites) == sites_model(backward.sites)


def test_a_rename_changes_neither_fingerprint_but_is_visible() -> None:
    """Membership is by id, so a renamed roof is the same roof."""
    renamed = [dict(site) for site in LIVE_SITES]
    renamed[0]["name"] = "Back roof"

    original = parse_diagnostic({"data": live_payload()})
    after = parse_diagnostic({"data": live_payload(sites=renamed)})

    assert sites_identity(original.site_ids) == sites_identity(after.site_ids)
    assert sites_model(original.sites) == sites_model(after.sites)
    assert "Back roof" in [site.name for site in after.sites]


def test_a_capacity_change_does_change_the_model_fingerprint() -> None:
    """The same roofs, re-rated, produce a different series and must not pool."""
    rescaled = [dict(site) for site in LIVE_SITES]
    rescaled[0]["capacity_dc"] = 4.5

    original = parse_diagnostic({"data": live_payload()})
    after = parse_diagnostic({"data": live_payload(sites=rescaled)})

    assert sites_identity(original.site_ids) == sites_identity(after.site_ids)
    assert sites_model(original.sites) != sites_model(after.sites)


def test_the_model_key_carries_no_display_name() -> None:
    """Asserted on the key itself, since this is the likeliest accidental change."""
    site = PvSite(resource_id="id-a", name="Achterkant", capacity_dc_kw=3.65)

    assert "Achterkant" not in str(site.model_key)


# -- discovery through the live path -----------------------------------------


async def test_the_two_live_sites_reach_diagnostics(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """The success criteria, read back out of a diagnostics download."""
    from custom_components.alpha_ems_manager.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    from .forecast_helpers import local
    from .test_battery_entities import frozen

    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    await drive(setup_integration.runtime_data)
    await hass.async_block_till_done()

    # Generated at the same civil day the refresh was driven at. Diagnostics reads
    # the clock to decide which day is "today", and the forecast blocks are keyed
    # by day -- so a download taken on a different date looks up a day the refresh
    # never produced. The pattern the other diagnostics tests use.
    with frozen(local(NORMAL, 12, 6)):
        payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    pv = payload["pv"]

    assert pv["capability"]["usable"] is True
    assert pv["capability"]["discoverable"] is True
    assert pv["source"]["site_count"] == 2
    assert set(pv["source"]["site_ids"]) == {ACHTERKANT, VOORKANT}
    assert pv["sites"]["discovered"] == 2
    assert pv["sites"]["selected"] == 2
    assert len(pv["sites"]["sites"]) == 2
    assert pv["selection_stored"] is True
    assert pv["provenance"]["selection_complete"] is True
    assert pv["provenance"]["selected_capacity_dc_total_kw"] == pytest.approx(6.08)
    assert pv["forecast_today"]["available"] is True
    assert pv["forecast_tomorrow"]["available"] is True
    assert pv["mapping"]["period_minutes"] == 30
    assert pv["source"]["response_shape"] == RESPONSE_SHAPE_NESTED


async def test_no_sites_discovered_is_named_rather_than_silent(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """The reason the live install reported, reachable only when it is true."""
    solcast.sites = []
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await drive(coordinator)

    forecast = coordinator.pv_forecasts[NORMAL]

    assert forecast.available is False
    assert forecast.unavailable_reason == PV_UNAVAILABLE_NO_SITES_DISCOVERED
    assert CONF_SELECTED_SOLCAST_SITE_IDS not in setup_integration.options


async def test_a_three_site_account_selects_all_three_initially(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """No assumption of two anywhere on the path."""
    solcast.sites.append(
        {
            "resource_id": "7777-8888-9999-c123",
            "name": "Schuur",
            "capacity": 2,
            "capacity_dc": 1.8,
            "azimuth": 0,
            "tilt": 20,
            "loss_factor": 0.9,
        }
    )
    solcast.power_by_site["7777-8888-9999-c123"] = 1.0
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await hass.async_block_till_done()
    await drive(coordinator, solcast=solcast)

    forecast = coordinator.pv_forecasts[NORMAL]

    assert len(setup_integration.options[CONF_SELECTED_SOLCAST_SITE_IDS]) == 3
    assert forecast.provenance.selected_site_count == 3
    assert forecast.provenance.selection_complete is True
    # All selected, so the source's own aggregate is used: one request.
    assert forecast.provenance.query_mode == PV_QUERY_MODE_AGGREGATE
    assert len(solcast.forecast_calls) == 1


async def test_deselecting_one_site_keeps_only_the_other(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """What the user does in the form, and what must survive it."""
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    await hass.async_block_till_done()

    hass.config_entries.async_update_entry(
        setup_integration,
        options={
            **setup_integration.options,
            CONF_SELECTED_SOLCAST_SITE_IDS: [VOORKANT],
        },
    )
    await hass.async_block_till_done()
    coordinator = setup_integration.runtime_data
    await drive(coordinator, solcast=solcast)

    forecast = coordinator.pv_forecasts[NORMAL]

    assert setup_integration.options[CONF_SELECTED_SOLCAST_SITE_IDS] == [VOORKANT]
    assert forecast.provenance.selected_site_ids == (VOORKANT,)
    assert forecast.provenance.selection_complete is False
    assert forecast.provenance.query_mode == PV_QUERY_MODE_PER_SITE
    # Only the kept site is queried, and only its production appears.
    assert {call.get("site") for call in solcast.forecast_calls} == {VOORKANT}
    assert forecast.intervals[48] == pytest.approx(3.0 * 0.25)


# -- the end-to-end PV-aware regression --------------------------------------


async def test_discovery_makes_the_plan_pv_aware_end_to_end(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    solcast_config_entry: MockConfigEntry,
    solcast: FakeSolcast,
) -> None:
    """The whole chain, which beta.10 could not reach because discovery returned none.

    Sites discovered, selection resolved, rows mapped, and the battery simulation
    actually receiving production. beta.10's live diagnostics showed
    ``intervals_pv_aware: 0`` and ``forecast_pv_kwh: 0.0`` -- correct given zero
    sites, and exactly what this asserts is no longer the case.
    """
    enable_forecast(setup_integration, hass, solcast_config_entry)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    coordinator = setup_integration.runtime_data
    await hass.async_block_till_done()
    await drive(coordinator)

    plan = coordinator.battery_plan
    assert plan is not None
    trajectory = plan.candidate
    assert trajectory is not None

    assert trajectory.intervals_pv_aware > 0
    assert trajectory.forecast_pv_kwh > 0.0
    assert trajectory.pv_aware is True
    assert "PV-aware" in trajectory._basis()

    recommendation = hass.states.get("sensor.alpha_ems_battery_recommendation")
    assert recommendation.attributes["pv_aware"] is True


async def test_the_pv_blind_path_is_unchanged_when_the_forecast_is_off(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The legacy path, bit for bit. Most installations are still on it."""
    coordinator = setup_integration.runtime_data
    await drive(coordinator)

    trajectory = coordinator.battery_plan.candidate

    assert trajectory.intervals_pv_aware == 0
    assert trajectory.forecast_pv_kwh == 0.0
    assert trajectory.pv_aware is False
    assert "no photovoltaic production term" in trajectory._basis()
    assert (
        hass.states.get("sensor.alpha_ems_battery_recommendation").attributes[
            "pv_aware"
        ]
        is False
    )
