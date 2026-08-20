"""Shared fixtures for the Alpha EMS Manager test suite."""

from __future__ import annotations

import asyncio
import sys

# On Windows the default ProactorEventLoop uses a socket for its self-pipe,
# which pytest_socket (enabled by pytest-homeassistant-custom-component) blocks.
# Force the selector loop before any loop is created so the HA test harness can
# run locally on Windows. No effect on Linux/macOS (CI).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import pytest_socket
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
)

from custom_components.alpha_ems_manager.const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_MAX_CHARGE_KW,
    CONF_BATTERY_MAX_DISCHARGE_KW,
    CONF_BATTERY_MIN_SOC_PERCENT,
    CONF_BATTERY_POWER_ENTITY,
    CONF_BATTERY_POWER_SIGN,
    CONF_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
    CONF_BATTERY_SOC_ENTITY,
    CONF_DAILY_HOUSE_LOAD_ENTITY,
    CONF_FRANK_ENTRY_ID,
    CONF_GRID_POWER_ENTITY,
    CONF_GRID_POWER_SIGN,
    CONF_HAS_PV,
    CONF_HOUSE_LOAD_ENTITY,
    CONF_NAME,
    CONF_PV_POWER_ENTITY,
    CONF_USE_PV_FORECAST,
    CONFIG_ENTRY_VERSION,
    DEFAULT_BATTERY_MIN_SOC_PERCENT,
    DEFAULT_BATTERY_POWER_SIGN,
    DEFAULT_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
    DEFAULT_GRID_POWER_SIGN,
    DOMAIN,
    DOMAIN_FRANK,
    DOMAIN_SOLCAST,
)

# On Windows every asyncio event loop creates an ``AF_INET`` socket for its
# self-pipe, which pytest_socket blocks. On Linux/macOS CI the self-pipe uses an
# allowed unix socket, so blocking is fine there. Neutralise the blocker for
# local Windows runs only.
if sys.platform == "win32":
    pytest_socket.disable_socket = lambda *args, **kwargs: None


TEST_TIMEZONE = "Europe/Amsterdam"
TZ = ZoneInfo(TEST_TIMEZONE)

#: A Monday, well inside a quarter and past the morning ramp, so weekday
#: behaviour and mid-day adaptation are both exercisable from the default clock.
SETUP_NOW = datetime(2026, 8, 17, 10, 7, 0, tzinfo=TZ)

#: Dutch DST transition dates. A spring-forward day contains 92 quarters and a
#: fall-back day 100, which the storage and accumulator layers must both honour.
SPRING_FORWARD = datetime(2026, 3, 29, 12, 0, tzinfo=TZ)
FALL_BACK = datetime(2026, 10, 25, 12, 0, tzinfo=TZ)

# Source entities used throughout the suite. They are deliberately named after
# the real AlphaESS Modbus / HomeWizard entities on the maintainer's system.
HOUSE_LOAD = "sensor.alphaess_current_house_load"
DAILY_HOUSE_LOAD = "sensor.alphaess_today_s_house_load"
BATTERY_SOC = "sensor.alphaess_soc_battery"
BATTERY_POWER = "sensor.alphaess_power_battery"
PV_POWER = "sensor.alphaess_current_pv_production"
GRID_POWER = "sensor.p1_meter_active_power"
EV_POWER = "sensor.ev_charger_power"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading the custom integration in every test."""
    return


def set_sensor(
    hass: HomeAssistant,
    entity_id: str,
    state: object,
    unit: str | None,
    device_class: str | None = None,
    state_class: str | None = "measurement",
) -> None:
    """Write a source sensor state with the attributes validation looks at."""
    attributes: dict[str, object] = {}
    if unit is not None:
        attributes["unit_of_measurement"] = unit
    if device_class is not None:
        attributes["device_class"] = device_class
    if state_class is not None:
        attributes["state_class"] = state_class
    hass.states.async_set(entity_id, state, attributes)


@pytest.fixture
def source_entities(hass: HomeAssistant) -> None:
    """Populate the state machine with a plausible, valid source set."""
    set_sensor(hass, HOUSE_LOAD, 2000, "W", "power")
    set_sensor(hass, DAILY_HOUSE_LOAD, 4.2, "kWh", "energy", "total")
    set_sensor(hass, BATTERY_SOC, 55, "%", "battery")
    set_sensor(hass, BATTERY_POWER, -664, "W", "power")
    set_sensor(hass, PV_POWER, 3000, "W", "power")
    set_sensor(hass, GRID_POWER, -336, "W", "power")


@pytest.fixture
def frank_config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Register a Frank Quarter Prices entry for Alpha EMS to reference.

    Alpha EMS requires one, and the options form renders the selection as a
    dropdown built from the entries that actually exist -- so without this the
    stored id would not be a selectable value.
    """
    entry = MockConfigEntry(
        domain=DOMAIN_FRANK, title="Frank Quarter Prices (NL)", unique_id="NL"
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def solcast_config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Register a Solcast PV Forecast entry."""
    entry = MockConfigEntry(domain=DOMAIN_SOLCAST, title="Solcast PV Forecast")
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def config_data(frank_config_entry: MockConfigEntry) -> dict[str, object]:
    """Return a complete, valid config-entry payload."""
    return {
        CONF_NAME: "Alpha EMS",
        CONF_HOUSE_LOAD_ENTITY: HOUSE_LOAD,
        CONF_DAILY_HOUSE_LOAD_ENTITY: DAILY_HOUSE_LOAD,
        CONF_BATTERY_SOC_ENTITY: BATTERY_SOC,
        CONF_BATTERY_POWER_ENTITY: BATTERY_POWER,
        CONF_BATTERY_POWER_SIGN: DEFAULT_BATTERY_POWER_SIGN,
        # Phase-3 planning figures, as the config flow requires them of a new
        # installation: a 10 kWh pack behind a 5 kW inverter. An installation
        # upgrading from an earlier release has none of these, which is what
        # ``test_beta6_upgrade.py`` builds explicitly.
        CONF_BATTERY_CAPACITY_KWH: 10.0,
        CONF_BATTERY_MIN_SOC_PERCENT: DEFAULT_BATTERY_MIN_SOC_PERCENT,
        CONF_BATTERY_MAX_CHARGE_KW: 5.0,
        CONF_BATTERY_MAX_DISCHARGE_KW: 5.0,
        CONF_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT: (
            DEFAULT_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT
        ),
        CONF_HAS_PV: True,
        CONF_PV_POWER_ENTITY: PV_POWER,
        CONF_GRID_POWER_ENTITY: GRID_POWER,
        CONF_GRID_POWER_SIGN: DEFAULT_GRID_POWER_SIGN,
        CONF_FRANK_ENTRY_ID: frank_config_entry.entry_id,
        CONF_USE_PV_FORECAST: False,
    }


@pytest.fixture
def mock_config_entry(config_data: dict[str, object]) -> MockConfigEntry:
    """Return an unloaded Alpha EMS config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Alpha EMS",
        data=config_data,
        options={},
        version=CONFIG_ENTRY_VERSION,
    )


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    source_entities: None,
) -> MockConfigEntry:
    """Set up the integration with valid sources and a fixed timezone."""
    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry
