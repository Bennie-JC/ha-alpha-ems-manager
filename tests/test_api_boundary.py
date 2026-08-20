"""The Phase-3 contract, and the wall around the storage behind it.

Two things are frozen here. The first is what a later phase may read. The
second, and the one that will actually stop a mistake, is what it may *import*:
if Phase 3 reaches into a partition dictionary directly, the storage layout can
never change again without breaking battery logic -- and the partitioning is
exactly the sort of thing that will want to change.

Modelled on ``tests/test_no_external_polling.py``, which enforces the other
architectural boundary this project cares about the same way: statically, over
the real source files.
"""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager import api

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

COMPONENT_DIR = Path("custom_components/alpha_ems_manager")

#: Modules that implement the evidence layer. Nothing outside the layer itself
#: may import them -- consumers go through ``api``.
PRIVATE_MODULES = (
    "forecast_history",
    "history_store",
    "forecast_recorder",
    "metrics",
)

#: Modules permitted to touch the private set: the layer itself, the runtime
#: that drives it, the two surfaces that report it, the entry lifecycle that
#: creates and deletes its documents, and the public interface.
PERMITTED_IMPORTERS = {
    "__init__",
    "api",
    "coordinator",
    "diagnostics",
    "forecast_history",
    "forecast_recorder",
    "history_store",
    "metrics",
    "sensor",
}

TOMORROW = NORMAL + timedelta(days=1)


def local_imports(path: Path) -> set[str]:
    """Return the sibling modules a source file imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            found.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("custom_components.alpha_ems_manager."):
                    found.add(alias.name.rsplit(".", 1)[-1])
    return found


def test_the_boundary_check_sees_the_real_modules() -> None:
    """Guard the glob against silently matching nothing."""
    names = {path.stem for path in COMPONENT_DIR.glob("*.py")}

    assert set(PRIVATE_MODULES) <= names
    assert "api" in names


@pytest.mark.parametrize(
    "path", sorted(COMPONENT_DIR.glob("*.py")), ids=lambda p: p.name
)
def test_no_unexpected_module_reaches_into_the_evidence_layer(path: Path) -> None:
    """Only the layer, its runtime and its two reporting surfaces may."""
    if path.stem in PERMITTED_IMPORTERS:
        return
    leaked = local_imports(path) & set(PRIVATE_MODULES)

    assert not leaked, f"{path.name} imports {sorted(leaked)}; go through api.py"


def test_the_public_interface_exposes_only_what_it_promises() -> None:
    """A frozen surface, so a later phase can be written against it.

    Compared as an *exact* set, over the names this module actually defines
    rather than over everything reachable through it. The previous form asserted
    a subset, which meant it could not fail: a new public name -- or a decision
    accidentally exposed here -- would have passed a test whose name says it
    would not. Imported symbols are excluded because they are reachable as
    attributes of any module and say nothing about the contract.
    """
    tree = ast.parse((COMPONENT_DIR / "api.py").read_text(encoding="utf-8"))
    imported = {
        (alias.asname or alias.name).split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    defined = {
        name for name in vars(api) if not name.startswith("_") and name not in imported
    }
    expected = {
        "API_VERSION",
        "LoadForecast",
        "ForecastUncertainty",
        "current_forecast",
        "async_issued_forecast",
        "async_uncertainty",
        # Phase 3 needs the live forecast, not the one refresh-stale copy sitting
        # on ``coordinator.data``. Converts and copies; decides nothing.
        "load_forecast_from",
    }

    assert defined == expected


def test_the_band_mapping_is_declared_as_read_only() -> None:
    """Annotated ``Mapping``, not ``dict``, so the intent is in the signature.

    The promise at the top of the module is that everything *returned here* is
    frozen and copied. ``mae_by_band`` was a plain ``dict`` on a frozen
    dataclass, so the reference could not be swapped but a caller could edit a
    band average in place and hand the altered object on. The annotation states
    the contract; the test below proves the accessor honours it.
    """
    import typing

    hints = typing.get_type_hints(api.ForecastUncertainty)

    assert typing.get_origin(hints["mae_by_band"]) is not dict
    assert "Mapping" in str(hints["mae_by_band"])


async def test_every_returned_mapping_is_read_only(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Through the real accessor, which is what the promise actually covers."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))

    with frozen(local(NORMAL, 12, 6)):
        uncertainty = await api.async_uncertainty(coordinator)

    with pytest.raises(TypeError):
        uncertainty.mae_by_band["night"] = 99.0  # type: ignore[index]


def test_an_out_of_range_interval_index_is_never_silently_accepted() -> None:
    """``index_for_start_utc`` is unclamped on purpose, so every caller guards.

    Clamping inside the helper would be worse than leaving it: an index outside
    the day means the stored day's shape and the instant being filed disagree,
    and beta.4 added ``REJECT_INTERVAL_OUT_OF_RANGE`` precisely so that is
    counted and named rather than absorbed. Silently folding it to the nearest
    valid index would restore the defect that fix removed.

    So the guarantee lives at the call sites, and this is what holds it there.
    """
    from custom_components.alpha_ems_manager.storage import (
        DayRecord,
        index_for_start_utc,
        utc_midnight,
    )

    tz = ZoneInfo("Europe/Amsterdam")
    before = utc_midnight(NORMAL, tz) - timedelta(minutes=15)
    assert index_for_start_utc(NORMAL, before, tz) == -1

    record = DayRecord(day=NORMAL, tz_key="Europe/Amsterdam", interval_count=96)
    for index in (-1, 96, 500):
        assert (
            record.record_interval(
                index, measured_kwh=1.0, ev_kwh=None, ev_expected=False
            )
            is False
        )
    assert record.measured_valid_count == 0
    assert record.soc_sample_count == 0

    users = {
        path.stem
        for path in COMPONENT_DIR.glob("*.py")
        if "index_for_start_utc" in path.read_text(encoding="utf-8")
        and path.stem != "storage"
    }
    assert users == {"coordinator"}
    coordinator_source = (COMPONENT_DIR / "coordinator.py").read_text(encoding="utf-8")
    assert "REJECT_INTERVAL_OUT_OF_RANGE" in coordinator_source
    assert "if not record.record_interval(" in coordinator_source
    for name in ("battery", "simulation", "policy", "plan"):
        source = (COMPONENT_DIR / f"{name}.py").read_text(encoding="utf-8")
        assert "index_for_start_utc" not in source, name


def test_the_public_types_are_immutable() -> None:
    """A consumer must not be able to mutate evidence through a returned object."""
    from dataclasses import FrozenInstanceError

    forecast = api.LoadForecast(
        day=NORMAL,
        tz_key="Europe/Amsterdam",
        interval_count=96,
        intervals=(0.1,) * 96,
        filled=(False,) * 96,
        available=True,
        unavailable_reason=None,
        model_days=5,
        confidence_percent=42.0,
    )

    with pytest.raises(FrozenInstanceError):
        forecast.available = False
    assert isinstance(forecast.intervals, tuple)
    assert isinstance(forecast.filled, tuple)


def test_the_api_makes_no_battery_decision() -> None:
    """Phase 2 reports evidence. What to do about it is not its business."""
    source = (COMPONENT_DIR / "api.py").read_text(encoding="utf-8").lower()

    for forbidden in (
        "def charge",
        "def discharge",
        "def recommend",
        "def schedule",
        "reserve_soc",
        "buy_price",
        "sell_price",
    ):
        assert forbidden not in source


# -- behaviour ---------------------------------------------------------------

pytestmark_integration = pytest.mark.usefixtures("setup_integration")


async def test_current_forecast_returns_the_unadapted_model_prediction(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The planner needs the forecast, not the dashboard's hybrid figure.

    The Today *entity* blends energy already measured today into its total,
    which is useful to a person and wrong to plan against: it is part
    measurement, so it shrinks as the day goes on for reasons that have nothing
    to do with the prediction.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    reseed(coordinator, {**history_before(NORMAL), NORMAL: flat_day(NORMAL, 6.0)})
    await refresh_at(coordinator, local(NORMAL, 18, 5))

    forecast = api.current_forecast(coordinator)
    baseline = coordinator.data["today_baseline"]

    assert forecast is not None
    assert forecast.day == NORMAL
    assert forecast.available is True
    assert forecast.total_kwh == pytest.approx(baseline.total_kwh)
    # Deliberately different from the adapted entity figure.
    assert coordinator.data["today"].forecast_total_kwh != pytest.approx(
        forecast.total_kwh
    )


async def test_current_forecast_serves_tomorrow_and_refuses_anything_else(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The model produces two days; inventing a third would hide that."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))

    assert api.current_forecast(coordinator, TOMORROW) is not None
    assert api.current_forecast(coordinator, NORMAL + timedelta(days=2)) is None
    assert api.current_forecast(coordinator, NORMAL - timedelta(days=1)) is None


async def test_remaining_energy_uses_chronological_indices(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Monotonic through a fold, unlike a wall-clock index."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))
    forecast = api.current_forecast(coordinator)

    assert forecast is not None
    whole = forecast.remaining_kwh(0)
    half = forecast.remaining_kwh(48)
    assert whole is not None and half is not None
    assert whole == pytest.approx(forecast.total_kwh)
    assert half < whole
    # Clamped rather than raising, in both directions.
    assert forecast.remaining_kwh(-5) == pytest.approx(whole)
    assert forecast.remaining_kwh(500) == pytest.approx(0.0)


async def test_a_withheld_forecast_reaches_the_api_as_withheld(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A consumer must be able to tell "no forecast" from "zero load"."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, {})
    await refresh_at(coordinator, local(NORMAL, 12, 5))

    forecast = api.current_forecast(coordinator)
    assert forecast is not None
    assert forecast.available is False
    assert forecast.unavailable_reason is not None
    assert forecast.total_kwh is None
    assert forecast.remaining_kwh(0) is None
    assert forecast.intervals == ()


async def test_an_issued_forecast_can_be_read_back_by_horizon(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Reconstructing what was known at decision time, not what is known now."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 23, 50))
    reseed(coordinator, history_before(TOMORROW))
    await refresh_at(coordinator, local(TOMORROW, 0, 5))

    day_ahead = await api.async_issued_forecast(coordinator, TOMORROW, 1)
    day_of = await api.async_issued_forecast(coordinator, TOMORROW, 0)
    headline = await api.async_issued_forecast(coordinator, TOMORROW)

    assert day_ahead is not None and day_of is not None
    assert day_ahead.horizon_days == 1
    assert day_of.horizon_days == 0
    assert day_ahead.issued_at is not None
    assert day_ahead.issued_at < day_of.issued_at
    # With no horizon asked for, the model's final word is returned.
    assert headline is not None
    assert headline.horizon_days == 0


async def test_uncertainty_reports_no_evidence_rather_than_no_error(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A fresh installation must not look like a perfect forecaster."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))

    uncertainty = await api.async_uncertainty(coordinator, window_days=30)

    assert uncertainty.days_compared == 0
    assert uncertainty.mae_kwh is None
    assert uncertainty.bias_kwh is None
    assert uncertainty.wape_percent is None
    # The margin helper must pass the absence through, not substitute a zero.
    assert uncertainty.interval_margin_kwh("evening") is None


async def test_uncertainty_measures_real_error_once_days_resolve(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The number a later phase should size a margin from."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))

    # The day comes in materially under the model.
    reseed(coordinator, {**history_before(NORMAL), NORMAL: flat_day(NORMAL, 9.6)})
    await refresh_at(coordinator, local(TOMORROW, 0, 5))

    # Read under the same clock: the window ends at yesterday, so a consumer
    # asking on the following day is what makes NORMAL eligible at all.
    with frozen(local(TOMORROW, 9, 0)):
        uncertainty = await api.async_uncertainty(coordinator, window_days=30)

    assert uncertainty.days_compared == 1
    assert uncertainty.intervals_compared == 96
    assert uncertainty.mae_kwh is not None
    assert uncertainty.bias_kwh is not None
    # Predicted 12 kWh against a measured 9.6, so the model over-predicted.
    assert uncertainty.bias_kwh > 0
    assert uncertainty.wape_percent == pytest.approx(25.0, rel=0.02)
    assert uncertainty.interval_margin_kwh("evening") is not None


async def test_the_margin_helper_falls_back_to_the_window_figure(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A band with no evidence of its own borrows the whole-window MAE."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))
    reseed(coordinator, {**history_before(NORMAL), NORMAL: flat_day(NORMAL, 9.6)})
    await refresh_at(coordinator, local(TOMORROW, 0, 5))

    with frozen(local(TOMORROW, 9, 0)):
        uncertainty = await api.async_uncertainty(coordinator, window_days=30)

    assert uncertainty.mae_kwh is not None
    assert uncertainty.interval_margin_kwh("no-such-band") == pytest.approx(
        uncertainty.mae_kwh
    )
    assert uncertainty.interval_margin_kwh(None) == pytest.approx(uncertainty.mae_kwh)


async def test_a_corrupt_history_yields_no_evidence_rather_than_raising(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A consumer must get a safe answer, not an exception."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))
    coordinator.history.corrupt = True

    uncertainty = await api.async_uncertainty(coordinator)
    issued = await api.async_issued_forecast(coordinator, NORMAL)

    assert uncertainty.intervals_compared == 0
    assert uncertainty.mae_kwh is None
    assert issued is None


async def test_the_band_helper_maps_intervals_by_their_own_wall_clock(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The band a consumer widens by must follow local time, not the index.

    A fall-back day has a hundred intervals, so index 80 is 19:00 there and
    20:00 on a normal day. Reading the band off the index would put the evening
    peak in the wrong bucket twice a year.
    """
    from .forecast_helpers import FALL_BACK

    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))
    with frozen(local(NORMAL, 12, 5)):
        uncertainty = await api.async_uncertainty(coordinator)

    assert uncertainty.band_of(NORMAL, 0, "Europe/Amsterdam") == "night"
    assert uncertainty.band_of(NORMAL, 30, "Europe/Amsterdam") == "morning"
    assert uncertainty.band_of(NORMAL, 60, "Europe/Amsterdam") == "afternoon"
    assert uncertainty.band_of(NORMAL, 90, "Europe/Amsterdam") == "evening"

    # The repeated hour is still the night on a hundred-interval day, and the
    # evening arrives four indices later than it would on a normal one.
    assert uncertainty.band_of(FALL_BACK, 12, "Europe/Amsterdam") == "night"
    assert uncertainty.band_of(FALL_BACK, 74, "Europe/Amsterdam") == "afternoon"
    assert uncertainty.band_of(FALL_BACK, 80, "Europe/Amsterdam") == "evening"


async def test_the_band_helper_declines_an_unusable_zone(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A renamed or missing zone yields no band rather than an exception."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))
    with frozen(local(NORMAL, 12, 5)):
        uncertainty = await api.async_uncertainty(coordinator)

    assert uncertainty.band_of(NORMAL, 0, "Mars/Olympus") is None
    assert uncertainty.band_of(NORMAL, 0, None) is None
    assert uncertainty.band_of(NORMAL, 0, "") is None
