"""Settings the audit found unreachable, mislabelled or undiagnosable.

Three unrelated gaps with one thing in common: **the code was right and the
surface was missing.** The optimiser really does charge a throughput cost, the
staleness gates really do refuse an old reading, and the default really is a named
constant -- but a user could not set the first, could not see the second in a
diagnostics download, and could not rely on the third staying in step with the
form.
"""

from __future__ import annotations

import pathlib

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.const import (
    CONF_ALLOW_BATTERY_EXPORT,
    CONF_ALLOW_GRID_CHARGING,
    CONF_BATTERY_THROUGHPUT_COST_EUR_PER_KWH,
    CONF_CONTROL_EXECUTION_ENABLED,
    CONF_GRID_CHARGE_MARGIN_EUR_PER_KWH,
    CONF_MINIMUM_TRADE_GAIN_EUR,
    DEFAULT_BATTERY_THROUGHPUT_COST_EUR_PER_KWH,
    DEFAULT_CONTROL_EXECUTION_ENABLED,
    MAX_BATTERY_THROUGHPUT_COST_EUR_PER_KWH,
    MIN_BATTERY_THROUGHPUT_COST_EUR_PER_KWH,
)
from custom_components.alpha_ems_manager.diagnostics import (
    async_get_config_entry_diagnostics,
)

pytestmark = pytest.mark.usefixtures("control_surface")


def flow_source() -> str:
    """Return the config-flow module's own text."""
    from custom_components.alpha_ems_manager import config_flow

    return pathlib.Path(config_flow.__file__).read_text(encoding="utf-8")


async def economics_step(hass: HomeAssistant, entry: MockConfigEntry):
    """Open the Economy options page and return the form result."""
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "economics"}
    )
    assert flow["step_id"] == "economics"
    return flow


# ===========================================================================
# CFG-004 / CFG-005 -- the throughput cost is settable, and bounded
# ===========================================================================


async def test_cfg_004_the_throughput_cost_has_a_field_at_last(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """It changed the plan from beta.18 and could only be set by hand until now.

    *Mutation: remove the field from ``_economics_schema`` and this fails.*
    """
    flow = await economics_step(hass, setup_integration)
    keys = {str(marker) for marker in flow["data_schema"].schema}

    assert CONF_BATTERY_THROUGHPUT_COST_EUR_PER_KWH in keys
    # And the three economic terms are offered together, because their bases are
    # different and a user choosing between them has to see all three.
    assert CONF_MINIMUM_TRADE_GAIN_EUR in keys
    assert CONF_GRID_CHARGE_MARGIN_EUR_PER_KWH in keys


async def test_cfg_005_the_declared_bounds_are_the_ones_enforced(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The MIN/MAX constants existed and were referenced nowhere.

    A constant that nothing reads is not a bound; it is a comment shaped like one.
    The selector must be built from these two and no other numbers.
    """
    flow = await economics_step(hass, setup_integration)
    selector = next(
        value
        for marker, value in flow["data_schema"].schema.items()
        if str(marker) == CONF_BATTERY_THROUGHPUT_COST_EUR_PER_KWH
    )
    config = selector.config

    assert config["min"] == MIN_BATTERY_THROUGHPUT_COST_EUR_PER_KWH
    assert config["max"] == MAX_BATTERY_THROUGHPUT_COST_EUR_PER_KWH


async def test_a_saved_throughput_cost_reaches_the_solver(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The whole point: the value typed is the value the plan is charged.

    A field that saves into options and never reaches ``SourceConfig`` would be a
    worse defect than the one being fixed -- a setting that looks live and is not.
    """
    flow = await economics_step(hass, setup_integration)
    await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            CONF_MINIMUM_TRADE_GAIN_EUR: 0.10,
            CONF_GRID_CHARGE_MARGIN_EUR_PER_KWH: 0.0,
            CONF_BATTERY_THROUGHPUT_COST_EUR_PER_KWH: 0.037,
            CONF_ALLOW_GRID_CHARGING: False,
            CONF_ALLOW_BATTERY_EXPORT: False,
        },
    )
    await hass.async_block_till_done()

    assert setup_integration.options[CONF_BATTERY_THROUGHPUT_COST_EUR_PER_KWH] == 0.037
    config = setup_integration.runtime_data.config
    assert config.battery_throughput_cost_eur_per_kwh == pytest.approx(0.037)


# ===========================================================================
# CFG-006 -- the default is the constant, not a literal beside it
# ===========================================================================


async def test_cfg_006_the_execution_default_comes_from_its_constant(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A hardcoded ``False`` beside a ``DEFAULT_*`` constant is a silent desync.

    Nothing is wrong today because both read ``False``. The failure appears the day
    someone changes the constant, which is exactly when nobody rereads the form.
    """
    assert "current(CONF_CONTROL_EXECUTION_ENABLED, False)" not in flow_source()
    assert "DEFAULT_CONTROL_EXECUTION_ENABLED" in flow_source()

    flow = await hass.config_entries.options.async_init(setup_integration.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "control"}
    )
    marker = next(
        marker
        for marker in flow["data_schema"].schema
        if str(marker) == CONF_CONTROL_EXECUTION_ENABLED
    )
    assert marker.default() is DEFAULT_CONTROL_EXECUTION_ENABLED


def test_the_shipped_defaults_are_unchanged_by_exposing_the_field() -> None:
    """Withdrawing a field changed no behaviour; adding one must not either.

    An install that never opens the Economy page must plan exactly as it did.
    """
    assert DEFAULT_BATTERY_THROUGHPUT_COST_EUR_PER_KWH == 0.0
    assert DEFAULT_CONTROL_EXECUTION_ENABLED is False


# ===========================================================================
# CFG-008 -- a staleness refusal must be diagnosable from the download
# ===========================================================================


async def test_cfg_008_every_source_publishes_how_old_its_reading_is(
    hass: HomeAssistant, setup_integration: MockConfigEntry, source_entities: None
) -> None:
    """``INHIBIT_SOC_STALE`` names a family, not which member went quiet.

    The control path refuses a source older than its window, and the download
    published the value without the timestamp -- so the one question a reader has
    could not be answered from it.

    *Mutation: drop ``age_seconds`` from ``_source_report`` and this fails.*
    """
    payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    sources = payload["sources"]

    present = [
        (name, report)
        for name, report in sources.items()
        if isinstance(report, dict) and report.get("exists")
    ]
    assert present, "the fixture must configure at least one live source"

    for name, report in present:
        assert "age_seconds" in report, name
        assert isinstance(report["age_seconds"], float), name
        assert report["age_seconds"] >= 0.0, name
        assert "last_updated" in report, name
        assert "last_changed" in report, name
        assert "unchanged_for_seconds" in report, name
