"""Diagnostics support for Alpha EMS Manager."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import AlphaEmsCoordinator

# Configuration values are entity ids, not secrets, but redact nothing sensitive
# by default. Kept here so it is easy to extend later.
TO_REDACT: set[str] = set()


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: AlphaEmsCoordinator = hass.data[DOMAIN][entry.entry_id]
    model = coordinator.model

    learned_slots_count = sum(1 for value in model.global_profile() if value > 0.0)

    profile_summary = {
        "learned_slots_count": learned_slots_count,
        "learned_days": model.learned_days,
        "update_count": model.update_count,
        "profile_keys": sorted(model.profiles.keys()),
        "latest_deltas": {
            "last_raw_delta": model.last_raw_delta,
            "last_delta_per_slot": model.last_delta_per_slot,
            "distributed_slots": model.distributed_slots,
            "last_learned_slots": model.last_learned_slots,
        },
        "previous_house_load": model.previous_house_load,
        "previous_slot": model.previous_slot,
        "last_update": model.last_update,
        "ev_exclusion": {
            "ev_charger_power_sensor": coordinator.entry.options.get(
                "ev_charger_power_sensor",
                coordinator.entry.data.get("ev_charger_power_sensor"),
            ),
            "last_ev_power_kw": model.last_ev_power_kw,
            "ev_excluded_last_quarter_kwh": model.ev_excluded_last_quarter_kwh,
            "ev_excluded_today_kwh": model.ev_excluded_today_kwh,
            "house_load_raw_last_quarter_kwh": model.house_load_raw_last_quarter_kwh,
            "house_load_corrected_last_quarter_kwh": (
                model.house_load_corrected_last_quarter_kwh
            ),
            "ev_exclusion_active": model.ev_exclusion_active,
            "ev_today_date": model.ev_today_date,
        },
    }

    pv_model = coordinator.pv_model
    pv_summary = {
        "global_factor": pv_model.global_factor,
        "season_factors": pv_model.season_factors,
        "pv_learning_days": pv_model.pv_learning_days,
        "update_count": pv_model.update_count,
        "last_pv_error": pv_model.last_pv_error,
        "last_pv_error_factor": pv_model.last_pv_error_factor,
        "current_date": pv_model.current_date,
        "current_season": pv_model.current_season,
        "last_actual_today": pv_model.last_actual_today,
        "last_forecast_today": pv_model.last_forecast_today,
        "last_update": pv_model.last_update,
    }

    reserve_model = coordinator.reserve_model
    reserve_summary = {
        "reserve_correction_factor": reserve_model.reserve_correction_factor,
        "reserve_learning_days": reserve_model.reserve_learning_days,
        "reserve_miss_count": reserve_model.reserve_miss_count,
        "reserve_success_count": reserve_model.reserve_success_count,
        "reserve_learning_status": reserve_model.reserve_learning_status,
        "last_reserve_miss": reserve_model.last_reserve_miss,
        "last_reserve_success": reserve_model.last_reserve_success,
        "last_battery_energy": reserve_model.last_battery_energy,
        "day_min_battery_energy": reserve_model.day_min_battery_energy,
        "last_miss_date": reserve_model.last_miss_date,
        "last_success_date": reserve_model.last_success_date,
        "current_date": reserve_model.current_date,
        "update_count": reserve_model.update_count,
        "last_update": reserve_model.last_update,
    }

    return {
        "entry": {
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "configured_source_entities": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "profile_summary": profile_summary,
        "pv_profile_summary": pv_summary,
        "reserve_profile_summary": reserve_summary,
        "coordinator_data": coordinator.data,
    }
