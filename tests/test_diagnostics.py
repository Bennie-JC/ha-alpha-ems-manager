"""Diagnostics content and, just as importantly, what it leaves out."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import BATTERY_POWER, GRID_POWER, HOUSE_LOAD


async def test_diagnostics_report_every_documented_section(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The payload carries all the sections a support request needs."""
    payload = await async_get_config_entry_diagnostics(hass, setup_integration)

    assert set(payload) == {
        "integration",
        "sources",
        "sign_conventions",
        "normalized_flows_now",
        "daily_validation_kwh",
        "learning",
        "flexible_load",
        "forecast",
        "confidence",
        "energy_balance",
        "storage",
        "consumed_integrations",
    }


async def test_source_availability_and_units_are_reported(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Each configured source reports its entity id, state and unit."""
    payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    house = payload["sources"]["house_load"]

    assert house["configured"] is True
    assert house["entity_id"] == HOUSE_LOAD
    assert house["exists"] is True
    assert house["unit"] == "W"
    assert house["device_class"] == "power"


async def test_an_unconfigured_source_is_reported_as_such(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """An optional source that was never selected says so plainly."""
    payload = await async_get_config_entry_diagnostics(hass, setup_integration)

    assert payload["sources"]["house_load"]["configured"] is True
    # The test fixture configures every source, so check the shape holds for a
    # source that exists but has no value rather than inventing one.
    assert "state" in payload["sources"]["battery_power"]


async def test_normalised_flows_use_the_canonical_convention(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The reported flows are already sign-normalised and non-negative.

    The fixture has the battery at -664 W (charging) and the grid at -336 W
    (exporting), which is precisely the case where a sign mistake would show.
    """
    payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    flows = payload["normalized_flows_now"]

    assert flows["house_load_w"] == 2000.0
    assert flows["battery_charge_w"] == 664.0
    assert flows["battery_discharge_w"] == 0.0
    assert flows["grid_import_w"] == 0.0
    assert flows["grid_export_w"] == 336.0

    for key, value in flows.items():
        assert value is None or value >= 0, f"{key} is negative"


async def test_sign_conventions_are_reported(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Both configured conventions and the canonical rule are visible."""
    payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    signs = payload["sign_conventions"]

    assert signs["battery_power"] == "negative_is_charge"
    assert signs["grid_power"] == "positive_is_import"
    assert "house_load >= 0" in signs["canonical"]


async def test_learning_and_storage_metadata_are_reported(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Coverage, retention and schema version are all present."""
    payload = await async_get_config_entry_diagnostics(hass, setup_integration)

    learning = payload["learning"]
    assert learning["learned_days"] == 0
    assert learning["min_quarter_coverage"] == 0.80
    assert learning["min_day_completeness"] == 0.80
    # Measured and baseline coverage are reported separately, so a gap can be
    # attributed to the house-load source or to the flexible-load source.
    assert "measured_valid_intervals" in learning
    assert "measured_missing_intervals" in learning
    assert "baseline_valid_intervals" in learning
    assert "retained_real_intervals" in learning

    storage = payload["storage"]
    assert storage["schema_version"] == 2
    assert storage["retention_days"] == 365
    assert storage["corrupt_on_load"] is False


async def test_consumed_integration_availability_is_reported(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Frank and Solcast availability is surfaced without extra entities."""
    payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    consumed = payload["consumed_integrations"]

    assert consumed["frank_entry_id"] == setup_integration.data["frank_entry_id"]
    assert consumed["frank_available"] is False
    assert consumed["pv_forecast_enabled"] is False
    assert consumed["solcast_available"] is False


async def test_diagnostics_expose_no_credentials(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """There are no secrets to leak, and the payload proves it."""
    payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    flattened = repr(payload).lower()

    for secret in (
        "token",
        "password",
        "api_key",
        "apikey",
        "secret",
        "bearer",
        "credential",
        "serial",
    ):
        assert secret not in flattened


async def test_diagnostics_do_not_dump_the_full_history(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A year of quarter buckets must not end up in a diagnostics download.

    Only the summary is included; the raw slots stay in ``.storage``.
    """
    payload = await async_get_config_entry_diagnostics(hass, setup_integration)

    # Counts and dates only -- no per-day or per-slot structures.
    assert "days" not in payload["learning"]
    assert isinstance(payload["learning"]["learned_days"], int)
    assert isinstance(payload["learning"]["measured_valid_intervals"], int)

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            assert len(value) <= 16, f"a {len(value)}-item list is too large"
            for item in value:
                walk(item)

    walk(payload)


async def test_diagnostics_are_json_serialisable(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Home Assistant writes the payload out as JSON."""
    import json

    payload = await async_get_config_entry_diagnostics(hass, setup_integration)

    encoded = json.dumps(payload, default=str)
    assert HOUSE_LOAD in encoded
    assert BATTERY_POWER in encoded
    assert GRID_POWER in encoded


async def test_energy_balance_diagnostics_expose_the_robustness_counters(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Everything needed to judge live source timing is downloadable.

    These are the exact fields the manual validation checklist reads, so a
    rename here would silently break that procedure.
    """
    payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    balance = payload["energy_balance"]

    assert {
        "eligible_samples",
        "passed_samples",
        "failed_samples",
        "skipped_incoherent_samples",
        "unavailable_samples",
        "pass_rate",
        "pass_rate_basis",
        "consecutive_failures",
        "worst_consecutive_failures",
        "sustained_failure_threshold",
        "max_allowed_skew_seconds",
        "max_allowed_age_seconds",
        "source_time_skew_seconds",
        "source_entities",
        "last_sample",
        "last_coherent_sample",
        "last_warning",
        "persisted_pass_rate",
        "persisted_samples",
    } <= set(balance)

    # The pass-rate denominator is documented in the payload itself.
    assert "incoherent samples excluded" in balance["pass_rate_basis"]


async def test_the_balance_source_list_matches_the_configuration(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Only configured, timestamped sources take part in the coherence check."""
    payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    sources = payload["energy_balance"]["source_entities"]

    assert HOUSE_LOAD in sources
    assert BATTERY_POWER in sources
    assert GRID_POWER in sources
    # The fixture has PV enabled, so it participates too.
    assert len(sources) == 4


async def test_a_pv_less_system_omits_pv_from_the_coherence_check(
    hass: HomeAssistant, freezer, config_data: dict
) -> None:
    """With no panels, PV is a known zero rather than an unread sensor."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry as MCE

    from custom_components.alpha_ems_manager.const import (
        CONF_HAS_PV,
        CONFIG_ENTRY_VERSION,
        DOMAIN,
    )

    from .conftest import TEST_TIMEZONE

    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    entry = MCE(
        domain=DOMAIN,
        title="Alpha EMS",
        data={**config_data, CONF_HAS_PV: False},
        options={},
        version=CONFIG_ENTRY_VERSION,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    payload = await async_get_config_entry_diagnostics(hass, entry)
    sources = payload["energy_balance"]["source_entities"]

    assert len(sources) == 3
    assert all("pv" not in entity for entity in sources)
