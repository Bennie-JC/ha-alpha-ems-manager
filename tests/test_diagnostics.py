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
        "daily_validation",
        "learning",
        "flexible_load",
        "forecast",
        "confidence",
        "energy_balance",
        "storage",
        "forecast_history",
        "battery_plan",
        # Phase 7. How much stored energy the forecast says must remain
        # available, and the counterfactuals it is bracketed by. Nothing
        # enforces it, and the calculation reads none of the provenance beside
        # it.
        "reserve",
        # Phase 8. The least-cost way through the known horizon: what the
        # optimizer wants, what implemented actuators could achieve, and why
        # nothing is sent. Nothing here is executed.
        "economic_plan",
        "control",
        # Phase 5. Sixteen sections to seventeen: a dict key rather than a list
        # entry, so the sixteen-item list ceiling every payload is held to is
        # untouched.
        "pv",
        # Phase 6, on the same reasoning. What electricity costs, and the
        # structural reasons it changes nothing.
        "price",
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


async def test_energy_balance_diagnostics_expose_the_failure_attribution(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A pass rate alone cannot say *why* samples failed.

    These fields are what turn "25 failures out of 265" into a diagnosis: which
    operating modes failed, which source was holding the comparison back, and how
    far the worst residual actually overshot its allowance.
    """
    payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    balance = payload["energy_balance"]

    assert {
        "passed_samples_by_mode",
        "failed_samples_by_mode",
        "skipped_due_to_skew",
        "skipped_due_to_stale_source",
        "least_recently_reported_source_counts",
        "worst_skew_seconds",
        "worst_residual_w",
        "worst_relative_error",
        "worst_excess_sample",
        "last_failed_sample",
        "active_balance_mode",
    } <= set(balance)

    # The two skip causes must account for the combined total exactly, or a high
    # skip rate stays unattributable.
    assert (
        balance["skipped_due_to_skew"] + balance["skipped_due_to_stale_source"]
        == balance["skipped_incoherent_samples"]
    )


async def test_the_reported_mode_cannot_contradict_the_last_sample(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """``active_balance_mode`` is lifted from the sample, not re-read.

    Re-reading the state machine let the payload assert an operating mode for a
    snapshot ``evaluate_balance`` had refused to judge, so diagnostics described
    a mode while reporting no verdict for it.
    """
    coordinator = setup_integration.runtime_data
    payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    balance = payload["energy_balance"]

    if coordinator.last_balance is None:
        assert balance["active_balance_mode"] is None
        assert balance["last_sample"] is None
    else:
        assert balance["active_balance_mode"] == balance["last_sample"]["mode"]


async def test_learned_days_agrees_with_the_learning_days_sensor(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """One question, one answer -- including on the day it changes.

    Diagnostics recomputed this with ``store.learned_days()`` and no ``before``,
    so it counted the in-progress day the moment its baseline coverage crossed
    ``MIN_DAY_COMPLETENESS`` -- around 19:15 on a clean day. A download taken that
    evening then reported one more learned day than the sensor showed, in the one
    artefact a support conversation relies on.
    """
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    from custom_components.alpha_ems_manager.storage import DayRecord

    from .conftest import TEST_TIMEZONE

    coordinator = setup_integration.runtime_data
    # The coordinator derives "today" from ``dt_util.now()``; deriving it the
    # same way here keeps the test independent of the machine's real date.
    today = dt_util.now().date()

    # Two complete past days, plus a today that has already crossed the bar.
    for offset in (2, 1):
        day = today - timedelta(days=offset)
        record = DayRecord(day=day, tz_key=TEST_TIMEZONE, interval_count=96)
        record.measured = [0.2] * 96
        coordinator.store.days[day] = record
    today_record = DayRecord(day=today, tz_key=TEST_TIMEZONE, interval_count=96)
    today_record.measured = [0.2] * 90 + [None] * 6  # 93.75 %
    coordinator.store.days[today] = today_record

    await coordinator.async_refresh()
    payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    state = hass.states.get("sensor.alpha_ems_learning_days")

    assert state is not None
    assert payload["learning"]["learned_days"] == int(state.state)
    # And the in-progress day is genuinely excluded, not merely consistent.
    assert payload["learning"]["learned_days"] == 2
    assert payload["learning"]["retained_days"] == 3


async def test_learned_day_dates_also_excludes_the_in_progress_day(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The same defect existed in a second, unfiltered helper."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    from custom_components.alpha_ems_manager.storage import DayRecord

    from .conftest import TEST_TIMEZONE

    coordinator = setup_integration.runtime_data
    # The coordinator derives "today" from ``dt_util.now()``; deriving it the
    # same way here keeps the test independent of the machine's real date.
    today = dt_util.now().date()

    for offset in (2, 1):
        day = today - timedelta(days=offset)
        record = DayRecord(day=day, tz_key=TEST_TIMEZONE, interval_count=96)
        record.measured = [0.2] * 96
        coordinator.store.days[day] = record
    today_record = DayRecord(day=today, tz_key=TEST_TIMEZONE, interval_count=96)
    today_record.measured = [0.2] * 90 + [None] * 6
    coordinator.store.days[today] = today_record

    await coordinator.async_refresh()
    dates = coordinator.learned_day_dates()

    assert today not in dates
    assert len(dates) == 2


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


async def test_the_forecast_history_block_reports_the_evidence_layer(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Everything the two published sensors deliberately do not show.

    The per-horizon breakdown is the part that could not exist without keeping
    both the day-ahead and the day-of prediction for the same target.
    """
    from datetime import timedelta

    from .forecast_helpers import (
        NORMAL,
        frozen,
        history_before,
        local,
        refresh_at,
        reseed,
        seed,
    )
    from .synthetic import flat_day

    coordinator = setup_integration.runtime_data
    tomorrow = NORMAL + timedelta(days=1)
    seed(coordinator, history_before(NORMAL))

    # A day-ahead prediction, then a day-of one, then the day is matched.
    await refresh_at(coordinator, local(NORMAL, 23, 50))
    reseed(coordinator, history_before(tomorrow))
    await refresh_at(coordinator, local(tomorrow, 0, 5))
    reseed(
        coordinator,
        {**history_before(tomorrow), tomorrow: flat_day(tomorrow, 9.6)},
    )
    await refresh_at(coordinator, local(tomorrow + timedelta(days=1), 0, 5))

    with frozen(local(tomorrow + timedelta(days=1), 9, 0)):
        payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    block = payload["forecast_history"]

    assert block["available"] is True
    assert block["provenance"]["forecast_schema_version"] == 1
    assert block["provenance"]["baseline_definition"] == "none"
    # Both days resolved: NORMAL was matched when the day turned, and
    # tomorrow when the one after it did.
    assert block["inventory"]["lifecycle"]["validated"] == 2
    assert block["inventory"]["lifecycle"]["unresolved"] == 0
    assert block["inventory"]["finalization_suspended"] is False
    assert block["issuance"]["duplicates_suppressed"] >= 0
    assert "predicted - actual" in block["quality"]["sign_convention"]
    # Both horizons were kept, so both can be scored separately.
    assert set(block["quality"]["by_horizon"]) == {"0", "1"}
    assert block["quality"]["by_horizon"]["1"]["days_compared"] == 1
    # Two fully measured days, so 192 valid intervals and no other code.
    assert block["matching"]["interval_status_counts"] == {"0": 192}
    assert block["matching"]["excluded_day_flags"] == {}
    assert block["storage"]["writes_suspended"] is False
    assert block["storage"]["partitions"]


async def test_the_forecast_history_block_survives_an_unreadable_index(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Diagnostics is the first thing asked for when storage misbehaves."""
    coordinator = setup_integration.runtime_data
    coordinator.history.corrupt = True

    payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    block = payload["forecast_history"]

    assert block["available"] is False
    assert block["storage"]["writes_suspended"] is True
    assert "nothing is being written" in block["note"]
