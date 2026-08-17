"""Unit and sign normalisation.

The single most important property asserted here is that an unusable reading
becomes ``None`` and never ``0``. A zero would be learned as a quarter-hour of
no consumption, quietly dragging the household profile down every time a cloud
integration hiccups.
"""

from __future__ import annotations

import math

import pytest

from custom_components.alpha_ems_manager.const import (
    SIGN_BATTERY_NEGATIVE_IS_CHARGE,
    SIGN_BATTERY_POSITIVE_IS_CHARGE,
    SIGN_GRID_NEGATIVE_IS_IMPORT,
    SIGN_GRID_POSITIVE_IS_IMPORT,
)
from custom_components.alpha_ems_manager.normalization import (
    is_energy_unit,
    is_power_unit,
    normalize_energy_kwh,
    normalize_power_w,
    parse_numeric,
    split_battery_power,
    split_grid_power,
)


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (1500, "W", 1500.0),
        ("1500", "W", 1500.0),
        (1.5, "kW", 1500.0),
        ("1.5", "kW", 1500.0),
        (0.0015, "MW", 1500.0),
        (-664, "W", -664.0),
        (0, "W", 0.0),
    ],
)
def test_power_units_normalise_to_watts(value, unit, expected) -> None:
    """W, kW and MW all reduce to watts, sign preserved."""
    assert normalize_power_w(value, unit) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (2500, "Wh", 2.5),
        (2.5, "kWh", 2.5),
        (0.0025, "MWh", 2.5),
    ],
)
def test_energy_units_normalise_to_kwh(value, unit, expected) -> None:
    """Wh, kWh and MWh all reduce to kilowatt-hours."""
    assert normalize_energy_kwh(value, unit) == pytest.approx(expected)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "unknown",
        "unavailable",
        "",
        "   ",
        "none",
        "not a number",
        "1,5",
        float("nan"),
        float("inf"),
        float("-inf"),
        True,
        False,
        [1],
        {"a": 1},
    ],
)
def test_unusable_values_become_none_never_zero(value) -> None:
    """Every unusable input normalises to ``None``.

    This is the guard that stops an unavailable sensor from being learned as a
    quarter of zero household consumption.
    """
    assert parse_numeric(value) is None
    assert normalize_power_w(value, "W") is None
    assert normalize_energy_kwh(value, "kWh") is None


def test_nan_is_rejected_rather_than_propagated() -> None:
    """A NaN reading must not leak into arithmetic downstream."""
    result = normalize_power_w(float("nan"), "W")
    assert result is None
    assert not (result is not None and math.isnan(result))


@pytest.mark.parametrize("unit", [None, "", "kWh", "degC", "%", "A", "V"])
def test_unrecognised_power_unit_is_rejected(unit) -> None:
    """An unscalable unit yields ``None`` rather than an unscaled guess."""
    assert normalize_power_w(100, unit) is None


@pytest.mark.parametrize("unit", [None, "", "W", "kW", "%", "A"])
def test_unrecognised_energy_unit_is_rejected(unit) -> None:
    """Energy normalisation is equally strict about units."""
    assert normalize_energy_kwh(100, unit) is None


def test_unit_predicates_agree_with_the_converters() -> None:
    """The config-flow predicates and the converters accept the same units."""
    for unit in ("W", "kW", "MW"):
        assert is_power_unit(unit)
        assert normalize_power_w(1, unit) is not None
    for unit in ("Wh", "kWh", "MWh"):
        assert is_energy_unit(unit)
        assert normalize_energy_kwh(1, unit) is not None
    for unit in (None, "", "degC"):
        assert not is_power_unit(unit)
        assert not is_energy_unit(unit)


# -- battery sign -----------------------------------------------------------


def test_battery_negative_is_charge_matches_alphaess() -> None:
    """The observed -664 W while charging maps to 664 W of charging.

    This is the convention AlphaESS uses and the default this integration ships,
    but it stays configurable because the wider ecosystem disagrees with itself.
    """
    charge, discharge = split_battery_power(-664.0, SIGN_BATTERY_NEGATIVE_IS_CHARGE)
    assert charge == pytest.approx(664.0)
    assert discharge == pytest.approx(0.0)


def test_battery_negative_is_charge_handles_discharge() -> None:
    """A positive reading under the same convention is discharging."""
    charge, discharge = split_battery_power(1264.0, SIGN_BATTERY_NEGATIVE_IS_CHARGE)
    assert charge == pytest.approx(0.0)
    assert discharge == pytest.approx(1264.0)


def test_battery_positive_is_charge_inverts_cleanly() -> None:
    """The opposite convention produces the mirror-image split."""
    charge, discharge = split_battery_power(664.0, SIGN_BATTERY_POSITIVE_IS_CHARGE)
    assert charge == pytest.approx(664.0)
    assert discharge == pytest.approx(0.0)

    charge, discharge = split_battery_power(-1264.0, SIGN_BATTERY_POSITIVE_IS_CHARGE)
    assert charge == pytest.approx(0.0)
    assert discharge == pytest.approx(1264.0)


# -- grid sign --------------------------------------------------------------


def test_grid_positive_is_import() -> None:
    """The common Dutch meter convention splits as expected."""
    imported, exported = split_grid_power(500.0, SIGN_GRID_POSITIVE_IS_IMPORT)
    assert imported == pytest.approx(500.0)
    assert exported == pytest.approx(0.0)

    imported, exported = split_grid_power(-336.0, SIGN_GRID_POSITIVE_IS_IMPORT)
    assert imported == pytest.approx(0.0)
    assert exported == pytest.approx(336.0)


def test_grid_negative_is_import() -> None:
    """The inverted meter convention is supported too."""
    imported, exported = split_grid_power(-500.0, SIGN_GRID_NEGATIVE_IS_IMPORT)
    assert imported == pytest.approx(500.0)
    assert exported == pytest.approx(0.0)


@pytest.mark.parametrize(
    "convention", [SIGN_BATTERY_NEGATIVE_IS_CHARGE, SIGN_BATTERY_POSITIVE_IS_CHARGE]
)
def test_missing_battery_reading_stays_missing(convention) -> None:
    """``None`` propagates so idle and unavailable stay distinguishable."""
    assert split_battery_power(None, convention) == (None, None)


@pytest.mark.parametrize(
    "convention", [SIGN_GRID_POSITIVE_IS_IMPORT, SIGN_GRID_NEGATIVE_IS_IMPORT]
)
def test_missing_grid_reading_stays_missing(convention) -> None:
    """``None`` propagates for the grid split as well."""
    assert split_grid_power(None, convention) == (None, None)


@pytest.mark.parametrize("raw", [-5000.0, -1.0, 0.0, 1.0, 5000.0])
def test_split_components_are_never_negative(raw) -> None:
    """Both halves of every split are non-negative by construction."""
    for convention in (
        SIGN_BATTERY_NEGATIVE_IS_CHARGE,
        SIGN_BATTERY_POSITIVE_IS_CHARGE,
    ):
        for component in split_battery_power(raw, convention):
            assert component is not None and component >= 0
    for convention in (SIGN_GRID_POSITIVE_IS_IMPORT, SIGN_GRID_NEGATIVE_IS_IMPORT):
        for component in split_grid_power(raw, convention):
            assert component is not None and component >= 0


@pytest.mark.parametrize("raw", [-5000.0, -664.0, 0.0, 1264.0])
def test_only_one_direction_is_ever_active(raw) -> None:
    """A flow is either charging or discharging, never both at once."""
    charge, discharge = split_battery_power(raw, SIGN_BATTERY_NEGATIVE_IS_CHARGE)
    assert charge == 0.0 or discharge == 0.0
