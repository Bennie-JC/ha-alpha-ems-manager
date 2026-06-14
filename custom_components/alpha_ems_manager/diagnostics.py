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
        "coordinator_data": coordinator.data,
    }
