"""The learned household load must be independent of everything else.

This is the release-critical property for every later phase. If a sunny day
teaches the model that the house consumes less simply because the panels
supplied the energy, every future optimisation decision is built on a lie.

Each scenario below holds the house-load sensor at exactly 2 kW while PV,
battery and grid move through wildly different states. The learned quarter must
come out at 0.5 kWh every single time.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.alpha_ems_manager.const import SLOTS_PER_DAY

from .conftest import (
    BATTERY_POWER,
    GRID_POWER,
    HOUSE_LOAD,
    PV_POWER,
    TEST_TIMEZONE,
    set_sensor,
)

TZ = ZoneInfo(TEST_TIMEZONE)

#: Start exactly on a quarter boundary so the first bucket is fully covered.
START = datetime(2026, 8, 17, 10, 0, 0, tzinfo=TZ)

HOUSE_LOAD_W = 2000.0
EXPECTED_QUARTER_KWH = 0.5  # 2 kW for 15 minutes

#: (label, pv_w, battery_w, grid_w). Battery is negative-is-charge, grid is
#: positive-is-import, matching the AlphaESS + Dutch P1 defaults.
SCENARIOS = [
    ("no pv, all from grid", 0.0, 0.0, 2000.0),
    ("no pv, battery discharging", 0.0, 1200.0, 800.0),
    ("pv exactly covers the house", 2000.0, 0.0, 0.0),
    ("pv surplus charging the battery", 5000.0, -3000.0, 0.0),
    ("pv surplus exported to the grid", 8000.0, 0.0, -6000.0),
    ("pv and battery both charging hard", 9000.0, -7000.0, 0.0),
    ("night, battery covers everything", 0.0, 2000.0, 0.0),
]


async def advance(hass: HomeAssistant, freezer, seconds: int, step: int = 60) -> None:
    """Move the frozen clock forward, firing Home Assistant's time triggers."""
    for _ in range(seconds // step):
        freezer.tick(timedelta(seconds=step))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


async def run_scenario(
    hass: HomeAssistant,
    freezer,
    mock_config_entry: MockConfigEntry,
    pv_w: float,
    battery_w: float,
    grid_w: float,
) -> float | None:
    """Set up with the given flows and return the learned 10:00 quarter."""
    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    freezer.move_to(START)

    set_sensor(hass, HOUSE_LOAD, HOUSE_LOAD_W, "W", "power")
    set_sensor(hass, PV_POWER, pv_w, "W", "power")
    set_sensor(hass, BATTERY_POWER, battery_w, "W", "power")
    set_sensor(hass, GRID_POWER, grid_w, "W", "power")
    set_sensor(hass, "sensor.alphaess_soc_battery", 55, "%", "battery")
    set_sensor(
        hass, "sensor.alphaess_today_s_house_load", 4.2, "kWh", "energy", "total"
    )

    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    # Past the 10:15:05 boundary trigger.
    await advance(hass, freezer, 960)

    coordinator = mock_config_entry.runtime_data
    record = coordinator.store.days.get(START.date())
    assert record is not None, "the quarter should have been stored"
    return record.measured[40]  # 10:00 -> slot 40


@pytest.mark.parametrize(
    ("label", "pv_w", "battery_w", "grid_w"),
    SCENARIOS,
    ids=[scenario[0] for scenario in SCENARIOS],
)
async def test_learned_load_is_identical_regardless_of_other_flows(
    hass: HomeAssistant,
    freezer,
    mock_config_entry: MockConfigEntry,
    label: str,
    pv_w: float,
    battery_w: float,
    grid_w: float,
) -> None:
    """2 kW of house load learns as 0.5 kWh whatever else the system is doing."""
    learned = await run_scenario(
        hass, freezer, mock_config_entry, pv_w, battery_w, grid_w
    )

    assert learned == pytest.approx(EXPECTED_QUARTER_KWH, rel=1e-3), (
        f"scenario {label!r} learned {learned} kWh instead of "
        f"{EXPECTED_QUARTER_KWH} kWh"
    )


async def test_a_sunny_day_does_not_teach_a_lower_house_load(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """The brief's worked example: PV 2 kW, house 2 kW, grid 0 W.

    A meter-based learner would see zero import and record a zero-consumption
    quarter. The house-load learner records 0.5 kWh.
    """
    learned = await run_scenario(
        hass, freezer, mock_config_entry, pv_w=2000.0, battery_w=0.0, grid_w=0.0
    )

    assert learned == pytest.approx(0.5, rel=1e-3)
    assert learned != 0.0


async def test_battery_charging_is_not_counted_as_household_consumption(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """PV 5 kW, house 2 kW, battery charging 3 kW learns 2 kW, not 5 kW."""
    learned = await run_scenario(
        hass, freezer, mock_config_entry, pv_w=5000.0, battery_w=-3000.0, grid_w=0.0
    )

    assert learned == pytest.approx(0.5, rel=1e-3)
    # 5 kW would be 1.25 kWh, 3 kW would be 0.75 kWh. Neither is what we learn.
    assert learned == pytest.approx(0.5, abs=0.05)


async def test_battery_discharge_does_not_reduce_the_learned_load(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """House 2 kW with the battery supplying 1.2 kW still learns 2 kW.

    A grid-import learner would have recorded only the 0.8 kW shortfall.
    """
    learned = await run_scenario(
        hass, freezer, mock_config_entry, pv_w=0.0, battery_w=1200.0, grid_w=800.0
    )

    assert learned == pytest.approx(0.5, rel=1e-3)
    assert learned > 0.2  # 0.8 kW would have been 0.2 kWh


async def test_every_scenario_produces_the_same_value(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """A single quarter's learning is unaffected by the surrounding system.

    Run one representative pair and assert equality directly, so a future change
    that makes learning depend on PV fails here even if the absolute figure
    happens to stay plausible.
    """
    dark = await run_scenario(
        hass, freezer, mock_config_entry, pv_w=0.0, battery_w=0.0, grid_w=2000.0
    )

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    sunny_entry = MockConfigEntry(
        domain=mock_config_entry.domain,
        title=mock_config_entry.title,
        data=dict(mock_config_entry.data),
        options={},
        version=mock_config_entry.version,
    )
    sunny = await run_scenario(
        hass, freezer, sunny_entry, pv_w=9000.0, battery_w=-7000.0, grid_w=0.0
    )

    assert dark == pytest.approx(sunny, rel=1e-9)


async def test_only_the_house_load_slot_is_populated(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """Learning writes one quarter, not a day's worth of inferred values."""
    await run_scenario(
        hass, freezer, mock_config_entry, pv_w=5000.0, battery_w=-3000.0, grid_w=0.0
    )

    coordinator = mock_config_entry.runtime_data
    record = coordinator.store.days[START.date()]
    populated = [
        index for index, value in enumerate(record.measured) if value is not None
    ]

    assert populated == [40]
    assert record.measured_valid_count == 1
    assert record.interval_count == SLOTS_PER_DAY
