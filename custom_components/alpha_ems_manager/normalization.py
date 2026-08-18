"""Unit and sign normalisation for externally provided source entities.

Alpha EMS Manager consumes entities owned by other integrations, so every value
that enters the learning pipeline passes through here first. Two rules matter:

* An unusable reading normalises to ``None`` -- never to ``0``. Turning an
  unavailable sensor into a zero would teach the model that the house consumed
  nothing, which is the single most damaging thing this integration could learn.
* Exactly one internal sign convention exists (see :class:`PowerFlows`). Source
  conventions are configuration, not assumptions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfEnergy,
    UnitOfPower,
)

from .const import (
    SIGN_BATTERY_NEGATIVE_IS_CHARGE,
    SIGN_BATTERY_POSITIVE_IS_CHARGE,
    SIGN_GRID_NEGATIVE_IS_IMPORT,
    SIGN_GRID_POSITIVE_IS_IMPORT,
)

#: States that explicitly carry no measurement.
_NON_NUMERIC_STATES: frozenset[str] = frozenset(
    {STATE_UNAVAILABLE, STATE_UNKNOWN, "none", "null", ""}
)

#: Multipliers converting a source power unit into watts.
_POWER_TO_W: dict[str, float] = {
    UnitOfPower.WATT: 1.0,
    UnitOfPower.KILO_WATT: 1_000.0,
    UnitOfPower.MEGA_WATT: 1_000_000.0,
}

#: Multipliers converting a source energy unit into kilowatt-hours.
_ENERGY_TO_KWH: dict[str, float] = {
    UnitOfEnergy.WATT_HOUR: 0.001,
    UnitOfEnergy.KILO_WATT_HOUR: 1.0,
    UnitOfEnergy.MEGA_WATT_HOUR: 1_000.0,
}


#: Why a source reading could not be turned into a number.
#:
#: These exist because "the quarter was rejected" is not a diagnosis. A user
#: whose learning has frozen needs to know whether the entity is unavailable,
#: publishing text, or simply carrying a unit this integration cannot read --
#: three quite different mistakes with three quite different fixes.

#: The entity explicitly carries no measurement (``unavailable``/``unknown``).
PROBLEM_STATE_UNAVAILABLE: str = "state_unavailable"
#: The state is present but is not a finite number: text, a boolean, NaN, inf.
PROBLEM_STATE_NOT_NUMERIC: str = "state_not_numeric"
#: The entity reports no ``unit_of_measurement`` at all.
PROBLEM_UNIT_MISSING: str = "unit_missing"
#: The unit is present but is not a power unit -- typically a kWh meter picked
#: where an instantaneous W sensor was wanted.
PROBLEM_UNIT_NOT_POWER: str = "unit_not_power"


def describe_power_problem(value: Any, unit: str | None) -> str | None:
    """Return why ``value``/``unit`` is not a usable power reading, or ``None``.

    Mirrors :func:`normalize_power_w` exactly: it returns ``None`` for precisely
    the inputs that function accepts. Kept as a separate pass rather than folded
    into the return type because the normalisation call sits on the hot sampling
    path and is made on every state change of a fast-publishing sensor, while
    the reason is only ever wanted once something has already gone wrong.
    """
    if parse_numeric(value) is None:
        if isinstance(value, str) and value.strip().lower() in _NON_NUMERIC_STATES:
            return PROBLEM_STATE_UNAVAILABLE
        if value is None:
            return PROBLEM_STATE_UNAVAILABLE
        return PROBLEM_STATE_NOT_NUMERIC
    if not unit:
        return PROBLEM_UNIT_MISSING
    if unit not in _POWER_TO_W:
        return PROBLEM_UNIT_NOT_POWER
    return None


def parse_numeric(value: Any) -> float | None:
    """Return ``value`` as a finite float, or ``None`` when it is not usable.

    Handles ``None``, ``unknown``/``unavailable``, empty strings, arbitrary
    non-numeric text, ``NaN`` and infinities. Booleans are rejected: a boolean
    state reaching a power sensor means the wrong entity was selected.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        candidate = value.strip()
        if candidate.lower() in _NON_NUMERIC_STATES:
            return None
        try:
            number = float(candidate)
        except ValueError:
            return None
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        return None

    if not math.isfinite(number):
        return None
    return number


def normalize_power_w(value: Any, unit: str | None) -> float | None:
    """Convert a source power reading to watts.

    ``unit`` is the source entity's ``unit_of_measurement``. An unrecognised
    unit yields ``None`` rather than an unscaled guess.
    """
    number = parse_numeric(value)
    if number is None:
        return None
    factor = _POWER_TO_W.get(unit or "")
    if factor is None:
        return None
    return number * factor


def normalize_energy_kwh(value: Any, unit: str | None) -> float | None:
    """Convert a source energy reading to kilowatt-hours."""
    number = parse_numeric(value)
    if number is None:
        return None
    factor = _ENERGY_TO_KWH.get(unit or "")
    if factor is None:
        return None
    return number * factor


def is_power_unit(unit: str | None) -> bool:
    """Return whether ``unit`` is a power unit this integration understands."""
    return (unit or "") in _POWER_TO_W


def is_energy_unit(unit: str | None) -> bool:
    """Return whether ``unit`` is an energy unit this integration understands."""
    return (unit or "") in _ENERGY_TO_KWH


def split_battery_power(
    raw_w: float | None,
    convention: str = SIGN_BATTERY_NEGATIVE_IS_CHARGE,
) -> tuple[float | None, float | None]:
    """Split a signed battery power into ``(charge_w, discharge_w)``.

    Both returned components are non-negative. ``None`` propagates so callers
    can distinguish "battery idle" from "battery reading missing".
    """
    if raw_w is None:
        return None, None
    # Compared against the *non-default* convention so that an unrecognised value
    # -- a renamed constant, or a hand-edited config entry -- lands on the shipped
    # default rather than on its exact inverse. Written the other way round, a
    # typo silently reported charging as discharging, which is the single fault
    # the energy-balance check exists to catch.
    if convention == SIGN_BATTERY_POSITIVE_IS_CHARGE:
        charging = raw_w
    else:
        charging = -raw_w
    return max(0.0, charging), max(0.0, -charging)


def split_grid_power(
    raw_w: float | None,
    convention: str = SIGN_GRID_POSITIVE_IS_IMPORT,
) -> tuple[float | None, float | None]:
    """Split a signed grid power into ``(import_w, export_w)``.

    Both returned components are non-negative.
    """
    if raw_w is None:
        return None, None
    # Compared against the non-default convention, for the reason given in
    # ``split_battery_power``: an unknown value must fall back to the shipped
    # default, not to its inverse.
    if convention == SIGN_GRID_NEGATIVE_IS_IMPORT:
        importing = -raw_w
    else:
        importing = raw_w
    return max(0.0, importing), max(0.0, -importing)


@dataclass(frozen=True, slots=True)
class PowerFlows:
    """A single instantaneous snapshot in the canonical internal convention.

    Every field is either ``None`` (not available) or ``>= 0``. Mixed source
    sign conventions are resolved before a :class:`PowerFlows` is constructed,
    so nothing downstream ever has to reason about signs again.
    """

    house_load_w: float | None = None
    pv_w: float | None = None
    battery_charge_w: float | None = None
    battery_discharge_w: float | None = None
    grid_import_w: float | None = None
    grid_export_w: float | None = None
