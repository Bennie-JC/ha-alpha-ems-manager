"""Diagnostics for Alpha EMS Manager.

This is where everything that does *not* justify an entity goes: per-source
availability, normalised readings, sign conventions, coverage statistics, the
confidence derivation and the energy-balance residual.

The payload carries no credentials, tokens or account data -- this integration
holds none, because it never talks to an external service. Nor does it dump the
full year of learned history; that would be megabytes of quarter buckets. Only
the summary a support conversation actually needs is included.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.core import HomeAssistant

from . import AlphaEmsConfigEntry
from .const import (
    CONFIG_ENTRY_VERSION,
    MAX_HISTORY_DAYS,
    MIN_DAY_COMPLETENESS,
    MIN_QUARTER_COVERAGE,
    SLOTS_PER_DAY,
    STORAGE_VERSION,
)
from .coordinator import AlphaEmsCoordinator
from .energy_balance import infer_balance_mode


def _source_report(hass: HomeAssistant, entity_id: str | None) -> dict[str, Any]:
    """Summarise one configured source entity."""
    if not entity_id:
        return {"configured": False}
    state = hass.states.get(entity_id)
    if state is None:
        return {"configured": True, "entity_id": entity_id, "exists": False}
    return {
        "configured": True,
        "entity_id": entity_id,
        "exists": True,
        "state": state.state,
        "unit": state.attributes.get("unit_of_measurement"),
        "device_class": state.attributes.get("device_class"),
        "state_class": state.attributes.get("state_class"),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AlphaEmsConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for one config entry."""
    # Home Assistant deletes ``runtime_data`` on unload and offers the diagnostics
    # download regardless of entry state, so this must not assume a loaded entry.
    # The state that matters most is the one this release's migration guard
    # produces: a legacy v1 entry sits in MIGRATION_ERROR, and "download
    # diagnostics" is the first thing anyone asks such a user for. Reaching for
    # ``entry.runtime_data`` there raised AttributeError and returned HTTP 500.
    coordinator: AlphaEmsCoordinator | None = getattr(entry, "runtime_data", None)
    if coordinator is None:
        return {
            "integration": {
                "loaded": False,
                "state": entry.state.value,
                "config_entry_version": entry.version,
                "expected_config_entry_version": CONFIG_ENTRY_VERSION,
                "note": (
                    "This entry is not loaded, so no runtime data exists. A "
                    "config-entry version below the expected one means the entry "
                    "predates the Phase 1 source model and cannot be migrated; "
                    "remove the integration and add it again."
                ),
            }
        }

    config = coordinator.config
    store = coordinator.store
    oldest, newest = store.span

    records = list(store.days.values())
    real_intervals = sum(record.interval_count for record in records)
    measured_valid = sum(record.measured_valid_count for record in records)
    baseline_valid = sum(record.baseline_valid_count for record in records)

    today = coordinator.today_forecast
    tomorrow = coordinator.tomorrow_forecast
    confidence = coordinator.confidence

    return {
        "integration": {
            "version": entry.version,
            "entry_title": entry.title,
            "learning_interval_minutes": 15,
            "slots_per_day": SLOTS_PER_DAY,
        },
        "sources": {
            "house_load": _source_report(hass, config.house_load_entity),
            "daily_house_load_validation": _source_report(
                hass, config.daily_house_load_entity
            ),
            "ev_power": _source_report(hass, config.ev_power_entity),
            "battery_soc": _source_report(hass, config.battery_soc_entity),
            "battery_power": _source_report(hass, config.battery_power_entity),
            "pv_power": _source_report(hass, config.pv_power_entity),
            "grid_power": _source_report(hass, config.grid_power_entity),
        },
        "sign_conventions": {
            "battery_power": config.battery_power_sign,
            "grid_power": config.grid_power_sign,
            "canonical": (
                "house_load >= 0, pv >= 0, battery_charge >= 0, "
                "battery_discharge >= 0, grid_import >= 0, grid_export >= 0"
            ),
        },
        "normalized_flows_now": asdict(coordinator.read_flows()),
        "daily_validation_kwh": coordinator.read_daily_house_load_kwh(),
        "learning": {
            "learned_days": len(store.learned_days()),
            "retained_days": len(store.days),
            # Real quarter-hours, so a fall-back day contributes 100 and a
            # spring-forward day 92.
            "retained_real_intervals": real_intervals,
            "history_start": None if oldest is None else oldest.isoformat(),
            "history_end": None if newest is None else newest.isoformat(),
            "measured_valid_intervals": measured_valid,
            "measured_missing_intervals": max(0, real_intervals - measured_valid),
            "measured_coverage": (
                None
                if real_intervals == 0
                else round(measured_valid / real_intervals, 4)
            ),
            "baseline_valid_intervals": baseline_valid,
            "baseline_coverage": (
                None
                if real_intervals == 0
                else round(baseline_valid / real_intervals, 4)
            ),
            "rejected_quarters": coordinator.rejected_quarters,
            "open_quarter_coverage": round(coordinator.open_quarter_coverage, 3),
            "last_finalized_quarter": store.last_finalized,
            "min_quarter_coverage": MIN_QUARTER_COVERAGE,
            "min_day_completeness": MIN_DAY_COMPLETENESS,
        },
        "flexible_load": {
            "kind": "ev_charging",
            "configured": coordinator.ev_configured,
            "entity": _source_report(hass, config.ev_power_entity),
            "available_now": coordinator.ev_available,
            "current_power_w": coordinator.current_ev_power_w,
            "open_interval_coverage": coordinator.ev_open_quarter_coverage,
            "intervals_without_valid_data": coordinator.invalid_ev_quarters,
            "baseline_rule": (
                "baseline = max(measured - flexible, 0); an interval with a "
                "configured but unreadable flexible load has no valid baseline"
            ),
        },
        "forecast": {
            "today_total_kwh": (
                None if today is None else round(today.forecast_total_kwh, 3)
            ),
            "today_actual_so_far_kwh": (
                None if today is None else round(today.actual_so_far_kwh, 3)
            ),
            "today_adaptation_ratio": (
                None if today is None else round(today.adaptation_ratio, 3)
            ),
            "tomorrow_total_kwh": (
                None
                if tomorrow is None or tomorrow.total_kwh is None
                else round(tomorrow.total_kwh, 3)
            ),
            "tomorrow_day_type": None if tomorrow is None else tomorrow.day_type,
            "tomorrow_day_type_pooled": (
                None if tomorrow is None else tomorrow.day_type_pooled
            ),
            "windows_used_days": (
                [] if tomorrow is None else list(tomorrow.windows_used)
            ),
        },
        "confidence": None if confidence is None else confidence.as_dict(),
        # Session-scoped counters plus the persisted tally. The session view is
        # what shows whether the sources are updating coherently right now; the
        # persisted pass rate is what feeds the confidence score.
        "energy_balance": {
            **coordinator.balance.as_dict(),
            "source_entities": coordinator.balance_source_entities,
            # The mode the identity is being evaluated in right now, lifted out
            # of the last sample so a residual can be attributed to an operating
            # mode without digging through the nested payload.
            "active_balance_mode": infer_balance_mode(coordinator.read_flows()),
            "source_time_skew_seconds": (
                None
                if coordinator.last_balance is None
                or coordinator.last_balance.coherence is None
                else round(coordinator.last_balance.coherence.skew_seconds, 1)
            ),
            "persisted_pass_rate": store.balance.score,
            "persisted_samples": store.balance.total_samples,
        },
        "storage": {
            "schema_version": STORAGE_VERSION,
            "interval_identity": (
                "chronological index from local midnight; 92/96/100 per civil "
                "day so daylight-saving transitions are represented exactly"
            ),
            "retention_days": MAX_HISTORY_DAYS,
            "corrupt_on_load": store.corrupt,
        },
        "consumed_integrations": {
            "frank_entry_id": config.frank_entry_id,
            "frank_available": coordinator.frank_available,
            "pv_forecast_enabled": config.use_pv_forecast,
            "solcast_entry_id": config.solcast_entry_id,
            "solcast_available": coordinator.solcast_available,
        },
    }
