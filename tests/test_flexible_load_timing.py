"""Unsynchronised sources: house load and EV updating at different rates.

Nothing guarantees the two sensors publish together. A P1-derived house load may
update every second while a cloud-backed charger updates once a minute, or the
reverse. Both signals are integrated over real elapsed time independently, so
neither has to wait for the other and neither is approximated as
``power * 0.25`` -- the mistake the previous implementation made, which
under-corrected badly whenever a quarter spanned more than one interval.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.alpha_ems_manager.const import (
    CONF_EV_POWER_ENTITY,
    CONFIG_ENTRY_VERSION,
    DOMAIN,
)
from custom_components.alpha_ems_manager.quarter import (
    QuarterAccumulator,
    sanitize_ev_w,
)

from .conftest import EV_POWER, HOUSE_LOAD, TEST_TIMEZONE, set_sensor

TZ = ZoneInfo(TEST_TIMEZONE)
START = datetime(2026, 8, 17, 10, 0, 0, tzinfo=TZ)
TODAY = START.date()
SLOT = 40


def integrate(
    samples: list[tuple[int, float | None]], ev: bool = False
) -> tuple[float, float]:
    """Drive one accumulator and return ``(energy_kwh, coverage)`` of the quarter."""
    accumulator = (
        QuarterAccumulator(TZ, sanitizer=sanitize_ev_w)
        if ev
        else QuarterAccumulator(TZ)
    )
    base = START.astimezone(UTC)
    results = []
    for offset, value in samples:
        results.extend(accumulator.add_sample(base + timedelta(seconds=offset), value))
    assert len(results) == 1
    return results[0].energy_kwh, results[0].coverage


def steady(watts: float, step: int) -> list[tuple[int, float | None]]:
    """Return a constant-power train covering one quarter at ``step`` seconds."""
    return [(offset, watts) for offset in range(0, 901, step)]


# -- integration is time-weighted, not sample-weighted -----------------------


@pytest.mark.parametrize("step", [1, 5, 30, 60, 300])
def test_the_sampling_rate_does_not_change_the_energy(step: int) -> None:
    """A quarter at constant power integrates identically at any update rate."""
    energy, coverage = integrate(steady(2000.0, step))

    assert energy == pytest.approx(0.5, rel=1e-6)
    assert coverage == pytest.approx(1.0)


def test_a_slow_ev_sensor_still_integrates_correctly() -> None:
    """A charger publishing once every five minutes is fully covered.

    Five minutes is exactly the tolerated gap, so the held value carries the
    whole interval.
    """
    energy, coverage = integrate(steady(7000.0, 300), ev=True)

    assert energy == pytest.approx(7000 * 0.25 / 1000, rel=1e-6)
    assert coverage == pytest.approx(1.0)


def test_a_mid_quarter_change_is_weighted_by_duration() -> None:
    """A charger that starts halfway through contributes half the energy.

    ``power * 0.25`` would have charged the full quarter at 7 kW.
    """
    samples: list[tuple[int, float | None]] = [
        (offset, 0.0) for offset in range(0, 451, 30)
    ]
    samples += [(offset, 7000.0) for offset in range(450, 901, 30)]

    energy, coverage = integrate(samples, ev=True)

    assert energy == pytest.approx(7000 * 0.125 / 1000, rel=1e-3)
    assert coverage == pytest.approx(1.0)


def test_a_session_ending_mid_quarter_is_not_extrapolated() -> None:
    """Charging that stops at minute five only counts for five minutes."""
    samples: list[tuple[int, float | None]] = [
        (offset, 11000.0) for offset in range(0, 301, 30)
    ]
    samples += [(offset, 0.0) for offset in range(300, 901, 30)]

    energy, _ = integrate(samples, ev=True)

    assert energy == pytest.approx(11000 * (300 / 3600) / 1000, rel=1e-3)


def test_an_ev_sensor_going_unavailable_mid_quarter_loses_coverage() -> None:
    """The unread portion is missing coverage, not zero charging."""
    samples: list[tuple[int, float | None]] = [
        (offset, 7000.0) for offset in range(0, 451, 60)
    ]
    samples += [(450, None), (900, None)]

    energy, coverage = integrate(samples, ev=True)

    assert coverage == pytest.approx(0.5, abs=0.02)
    assert energy == pytest.approx(7000 * 0.125 / 1000, rel=1e-2)


# -- end to end with genuinely unsynchronised sources ------------------------


async def drive(
    hass: HomeAssistant,
    freezer,
    entry: MockConfigEntry,
    seconds: int,
    house_every: int,
    ev_every: int,
    house_w: float,
    ev_w: float,
) -> None:
    """Advance the clock, publishing each source on its own cadence."""
    for elapsed in range(0, seconds, 10):
        freezer.tick(timedelta(seconds=10))
        if elapsed % house_every == 0:
            set_sensor(hass, HOUSE_LOAD, house_w, "W", "power")
        if elapsed % ev_every == 0:
            set_sensor(hass, EV_POWER, ev_w, "W", "power")
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


async def setup_entry(
    hass: HomeAssistant, freezer, config_data: dict, house_w: float, ev_w: float
) -> MockConfigEntry:
    """Set up with both sources present at the start of a quarter."""
    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    freezer.move_to(START)
    set_sensor(hass, HOUSE_LOAD, house_w, "W", "power")
    set_sensor(hass, EV_POWER, ev_w, "W", "power")

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Alpha EMS",
        data={**config_data, CONF_EV_POWER_ENTITY: EV_POWER},
        options={},
        version=CONFIG_ENTRY_VERSION,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_fast_house_load_with_a_slow_charger(
    hass: HomeAssistant, freezer, config_data: dict
) -> None:
    """House updates every 10 s, charger every 4 minutes."""
    entry = await setup_entry(hass, freezer, config_data, 9000.0, 7000.0)
    await drive(
        hass,
        freezer,
        entry,
        960,
        house_every=10,
        ev_every=240,
        house_w=9000.0,
        ev_w=7000.0,
    )

    record = entry.runtime_data.store.days[TODAY]
    assert record.measured[SLOT] == pytest.approx(2.25, rel=1e-2)
    assert record.ev[SLOT] == pytest.approx(1.75, rel=1e-2)
    assert record.baseline_at(SLOT) == pytest.approx(0.5, rel=1e-2)


async def test_fast_charger_with_a_slow_house_load(
    hass: HomeAssistant, freezer, config_data: dict
) -> None:
    """The reverse cadence produces the same baseline."""
    entry = await setup_entry(hass, freezer, config_data, 9000.0, 7000.0)
    await drive(
        hass,
        freezer,
        entry,
        960,
        house_every=240,
        ev_every=10,
        house_w=9000.0,
        ev_w=7000.0,
    )

    record = entry.runtime_data.store.days[TODAY]
    assert record.baseline_at(SLOT) == pytest.approx(0.5, rel=1e-2)


async def test_both_sources_changing_inside_one_quarter(
    hass: HomeAssistant, freezer, config_data: dict
) -> None:
    """Independent mid-quarter changes are each weighted by their own duration."""
    entry = await setup_entry(hass, freezer, config_data, 2000.0, 0.0)

    # First half: 2 kW house, no charging.
    for _ in range(45):
        freezer.tick(timedelta(seconds=10))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    # Second half: charging starts and the house load rises with it.
    set_sensor(hass, HOUSE_LOAD, 9000.0, "W", "power")
    set_sensor(hass, EV_POWER, 7000.0, "W", "power")
    for _ in range(51):
        freezer.tick(timedelta(seconds=10))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    record = entry.runtime_data.store.days[TODAY]
    # Measured: half at 2 kW, half at 9 kW -> 1.375 kWh.
    assert record.measured[SLOT] == pytest.approx(1.375, rel=5e-2)
    # Flexible: half at 0, half at 7 kW -> 0.875 kWh.
    assert record.ev[SLOT] == pytest.approx(0.875, rel=5e-2)
    # Baseline stays at a steady 2 kW throughout.
    assert record.baseline_at(SLOT) == pytest.approx(0.5, rel=5e-2)


async def test_a_reload_mid_quarter_drops_only_the_open_interval(
    hass: HomeAssistant, freezer, config_data: dict
) -> None:
    """Reloading discards the in-flight interval and resumes cleanly."""
    entry = await setup_entry(hass, freezer, config_data, 9000.0, 7000.0)
    await drive(
        hass,
        freezer,
        entry,
        480,
        house_every=60,
        ev_every=60,
        house_w=9000.0,
        ev_w=7000.0,
    )

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    # Finish this quarter (partial, so rejected) and run a full clean one.
    await drive(
        hass,
        freezer,
        entry,
        1440,
        house_every=60,
        ev_every=60,
        house_w=9000.0,
        ev_w=7000.0,
    )

    record = entry.runtime_data.store.days[TODAY]
    # The interrupted 10:00 interval never reached the coverage threshold.
    assert record.measured[SLOT] is None
    # The next full interval is measured and its baseline is correct.
    assert record.measured[SLOT + 1] == pytest.approx(2.25, rel=1e-2)
    assert record.baseline_at(SLOT + 1) == pytest.approx(0.5, rel=1e-2)
