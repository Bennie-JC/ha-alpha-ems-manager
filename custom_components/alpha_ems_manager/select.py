"""Select platform: the one control the user sets.

The integration's first writable entity. What it writes is this integration's own
runtime state -- never a battery, and in this release nothing writes to a battery
at all.

A select rather than an options-flow field, for one reason: it is the control a
user reaches for when they want the integration to stop trying, and that must not
require opening a configuration dialog or reloading the entry. It restores its
last value across a restart, and falls back to ``off`` if it cannot -- which is
the safe direction.

What ``off`` means here is worth stating precisely, because the obvious reading is
wrong. It means **this integration stops attempting control**. It does not mean
the inverter reverts, and in this release the distinction never arises: nothing
can start a dispatch, so there is never one of ours to revert. A later release
that can execute will have to state which of the two it promises, and it cannot
promise the second until it can prove which dispatch is its own.
"""

from __future__ import annotations

from typing import ClassVar

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AlphaEmsConfigEntry
from .const import (
    CONTROL_MODE_OFF,
    CONTROL_MODE_OPTIONS,
    DOMAIN,
    NAME,
    SELECT_CONTROL_MODE,
)
from .coordinator import AlphaEmsCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AlphaEmsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Alpha EMS control-mode select."""
    coordinator: AlphaEmsCoordinator = entry.runtime_data
    async_add_entities([AlphaEmsControlModeSelect(coordinator)])


class AlphaEmsControlModeSelect(
    CoordinatorEntity[AlphaEmsCoordinator], RestoreEntity, SelectEntity
):
    """Off, shadow or active -- shown as Off, Shadow and Live.

    **The displayed label and the stored value are deliberately different.**
    ``active`` is what a restored entity, every stored document and every test
    already say, so moving it would rename a value with history behind it for the
    sake of a caption. "Live" is the clearer word for what the mode will mean once
    it can act, so the translation says that and the value does not move.
    """

    _attr_has_entity_name = True
    _attr_name = "Control Mode"
    _attr_icon = "mdi:tune-variant"
    #: Resolves the option labels through ``entity.select.control_mode.state``.
    #: Without it Home Assistant renders the raw values, which is what every enum
    #: in this integration did before beta.19.
    _attr_translation_key = SELECT_CONTROL_MODE
    _attr_options: ClassVar[list[str]] = list(CONTROL_MODE_OPTIONS)

    def __init__(self, coordinator: AlphaEmsCoordinator) -> None:
        """Bind the select to its coordinator."""
        super().__init__(coordinator)
        entry = coordinator.entry
        self._attr_unique_id = f"{entry.entry_id}_{SELECT_CONTROL_MODE}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Alpha EMS",
            model=NAME,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def current_option(self) -> str:
        """Return the selected mode.

        Read from the coordinator rather than held here, so the entity and the
        pipeline cannot disagree about which mode is in force.
        """
        return self.coordinator.control_mode

    async def async_added_to_hass(self) -> None:
        """Restore the previously selected mode, defaulting to off.

        Anything unrecognised -- a value from a future release, or a damaged
        restore -- falls back to ``off`` rather than being carried forward. A
        control whose stored value cannot be interpreted must not be assumed to
        have been permissive.
        """
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        restored = last.state if last is not None else None
        if restored in CONTROL_MODE_OPTIONS:
            self.coordinator.set_control_mode(restored)
        else:
            self.coordinator.set_control_mode(CONTROL_MODE_OFF)

    async def async_select_option(self, option: str) -> None:
        """Change the mode and re-evaluate immediately.

        The refresh matters. The plan is rebuilt on the quarter-hour, so without
        it a mode change would sit unreflected for up to fifteen minutes -- and a
        control that appears to have done nothing for a quarter of an hour is one
        a user reasonably stops trusting.

        ``async_refresh`` rather than ``async_request_refresh``, for exactly the
        reason ``AlphaEmsCoordinator._handle_started`` gives for the startup path:
        the requesting form is debounced on a ten-second cooldown, so a quarter-
        hour tick and a user action arriving inside that window collapse into one
        deferred refresh. The mode is applied either way -- it is set before the
        refresh is asked for -- but the *re-evaluation* then waits out the
        remainder of the cooldown, and "immediately" stops being true.

        A user pressing a control is the last thing that should be rate-limited
        against a background timer. This is a human-speed action, and it is the
        one path where collapsing two refreshes into the earlier one is precisely
        wrong.
        """
        if option not in CONTROL_MODE_OPTIONS:
            raise ValueError(f"unknown control mode: {option}")
        self.coordinator.set_control_mode(option)
        self.async_write_ha_state()
        await self.coordinator.async_refresh()
