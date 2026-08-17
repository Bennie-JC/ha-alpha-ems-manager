"""Validation of user-selected source entities.

Alpha EMS Manager reads entities it does not own, so the config flow checks that
each selection can actually be interpreted before the entry is created. The
checks lean on ``unit_of_measurement`` rather than on the entity name: a sensor
called "House Load" that reports degrees Celsius is not a house-load sensor, and
a sensor called "verbruik_nu" that reports watts is.

A unit is mandatory for every numeric source. Without one the value cannot be
normalised to the canonical internal representation, and guessing the scale
would be worse than refusing the selection.
"""

from __future__ import annotations

from homeassistant.const import PERCENTAGE, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant

from .normalization import is_energy_unit, is_power_unit, parse_numeric

#: Error keys, matching the ``config.error`` / ``options.error`` translations.
ERROR_NOT_FOUND = "entity_not_found"
ERROR_INVALID_POWER = "invalid_power_entity"
ERROR_INVALID_ENERGY = "invalid_energy_entity"
ERROR_INVALID_PERCENTAGE = "invalid_percentage_entity"
ERROR_NOT_NUMERIC = "entity_not_numeric"


def _unit_of(hass: HomeAssistant, entity_id: str) -> tuple[bool, str | None, str]:
    """Return ``(exists, unit, state)`` for ``entity_id``."""
    state = hass.states.get(entity_id)
    if state is None:
        return False, None, ""
    return True, state.attributes.get("unit_of_measurement"), state.state


def _is_numeric_or_transient(state: str) -> bool:
    """Return whether the current state blocks acceptance.

    A source that happens to be unavailable while the user is filling in the
    form is fine -- cloud-backed integrations do that regularly. A source whose
    state is a non-numeric string is not.
    """
    if state in (STATE_UNAVAILABLE, STATE_UNKNOWN, ""):
        return True
    return parse_numeric(state) is not None


def validate_power_entity(hass: HomeAssistant, entity_id: str) -> str | None:
    """Return an error key when ``entity_id`` is not a usable power sensor."""
    exists, unit, state = _unit_of(hass, entity_id)
    if not exists:
        return ERROR_NOT_FOUND
    if not is_power_unit(unit):
        return ERROR_INVALID_POWER
    if not _is_numeric_or_transient(state):
        return ERROR_NOT_NUMERIC
    return None


def validate_energy_entity(hass: HomeAssistant, entity_id: str) -> str | None:
    """Return an error key when ``entity_id`` is not a usable energy sensor."""
    exists, unit, state = _unit_of(hass, entity_id)
    if not exists:
        return ERROR_NOT_FOUND
    if not is_energy_unit(unit):
        return ERROR_INVALID_ENERGY
    if not _is_numeric_or_transient(state):
        return ERROR_NOT_NUMERIC
    return None


def validate_percentage_entity(hass: HomeAssistant, entity_id: str) -> str | None:
    """Return an error key when ``entity_id`` is not a usable percentage sensor."""
    exists, unit, state = _unit_of(hass, entity_id)
    if not exists:
        return ERROR_NOT_FOUND
    if unit != PERCENTAGE:
        return ERROR_INVALID_PERCENTAGE
    if not _is_numeric_or_transient(state):
        return ERROR_NOT_NUMERIC
    return None
