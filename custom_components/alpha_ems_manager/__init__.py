"""The Alpha EMS Manager integration.

Phase 1: source fusion, household-load learning and forecasting. This
integration deliberately issues no commands to the battery and makes no
charge, discharge or trading decisions.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryError

from .const import (
    CONFIG_ENTRY_VERSION,
    DOMAIN,
    LEGACY_CONF_MARKER,
    PLATFORMS,
)
from .coordinator import AlphaEmsCoordinator, SourceConfig
from .storage import LearningStore

_LOGGER = logging.getLogger(__name__)

type AlphaEmsConfigEntry = ConfigEntry[AlphaEmsCoordinator]


async def async_migrate_entry(hass: HomeAssistant, entry: AlphaEmsConfigEntry) -> bool:
    """Refuse to load a config entry written by the previous source model.

    Version 1 configured six individual Frank entities, a cumulative daily
    house-load counter and a battery capacity entity. Version 2 configures an
    instantaneous house-load power sensor, sign conventions and config-entry
    references. The two share **no** keys, and none of the v1 values can be
    mapped onto a v2 field without inventing a selection on the user's behalf.

    Returning ``False`` puts the entry into a visible migration-failed state.
    That is deliberately louder than the alternative: loading it would produce
    four healthy-looking sensors attached to no source at all, learning nothing
    and logging nothing.
    """
    if entry.version < CONFIG_ENTRY_VERSION:
        legacy = LEGACY_CONF_MARKER in entry.data
        _LOGGER.error(
            "Alpha EMS Manager config entry %s uses the version %s source "
            "model%s, which cannot be converted to the version %s model: the "
            "two share no configuration keys. Remove this entry and add the "
            "integration again to select the new sources. No other Home "
            "Assistant data is affected, but note that learning history is "
            "stored per config entry: the replacement entry starts a fresh "
            "history rather than inheriting this one",
            entry.title,
            entry.version,
            " (it still selects a cumulative daily house-load counter)"
            if legacy
            else "",
            CONFIG_ENTRY_VERSION,
        )
        return False

    # Nothing newer than the current version is understood either.
    return entry.version == CONFIG_ENTRY_VERSION


async def async_setup_entry(hass: HomeAssistant, entry: AlphaEmsConfigEntry) -> bool:
    """Set up one Alpha EMS Manager instance."""
    config = SourceConfig.from_entry(entry)

    # Nothing downstream can learn without this, and an integration that loads
    # cleanly while quietly measuring nothing is the worst possible failure.
    if not config.house_load_entity:
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="house_load_source_missing",
        )

    coordinator = AlphaEmsCoordinator(hass, entry)
    await coordinator.async_prepare()
    entry.runtime_data = coordinator

    await coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Listeners and timers are registered after the platforms exist, and every
    # one of them is unregistered by async_on_unload on reload. Nothing is
    # tracked in a module-level structure, so two instances never interfere and
    # a reload cannot leave a duplicate behind.
    coordinator.async_start()

    async def _flush_on_stop(_event: Event) -> None:
        """Persist learning data when Home Assistant shuts down."""
        await coordinator.async_shutdown_store()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _flush_on_stop)
    )
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AlphaEmsConfigEntry) -> bool:
    """Tear down one Alpha EMS Manager instance."""
    coordinator = entry.runtime_data
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        # Cheap insurance. Recent cores register this themselves, but on the
        # declared 2025.1.0 floor a refresh debounced by the quarter-hour tick
        # could otherwise fire after teardown and run against a dead entry.
        await coordinator.async_shutdown()
        # Flush before the entry goes away so a reload resumes from the last
        # finalised interval rather than from the last debounced write.
        await coordinator.async_shutdown_store()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: AlphaEmsConfigEntry) -> None:
    """Delete the learning history belonging to a removed entry.

    The store key is scoped to the entry id, so without this every removed entry
    leaves a document behind in ``.storage`` that nothing can ever reach again --
    up to a year of quarter-hour history per orphan. A removal is an explicit
    instruction to forget this instance, so the history goes with it.
    """
    await LearningStore(hass, entry.entry_id).async_remove()


async def async_reload_entry(hass: HomeAssistant, entry: AlphaEmsConfigEntry) -> None:
    """Reload the entry after an options change."""
    await hass.config_entries.async_reload(entry.entry_id)
