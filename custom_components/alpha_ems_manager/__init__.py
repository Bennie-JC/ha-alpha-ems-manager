"""The Alpha EMS Manager integration.

A self-learning Energy Management System for AlphaESS battery management in
Home Assistant. This integration learns household load per 15-minute interval,
combines Solcast PV forecasts and Frank dynamic prices, and calculates the
required battery reserve between the next sell and buy windows.

This module wires up the config entry: it creates the coordinator, performs the
first refresh and forwards the supported platforms.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS
from .coordinator import AlphaEmsCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Alpha EMS Manager from a config entry."""
    coordinator = AlphaEmsCoordinator(hass, entry)

    # Load any previously learned data from persistent storage.
    await coordinator.async_load_store()

    # Fetch initial data so entities have values when they are added.
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload entities when the user updates options.
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coordinator: AlphaEmsCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_save_store()
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
