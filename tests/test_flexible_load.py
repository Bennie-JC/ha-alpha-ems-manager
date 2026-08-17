"""Flexible loads: separating EV charging from the learned baseline.

The measured house-load sensor reports everything behind the meter, EV charging
included. A future optimiser must not reserve battery energy to cover a load it
may itself end up scheduling, so the learned demand curve is the **baseline**:
measured minus separately measured flexible load.

Measured energy is always ground truth and is always stored. Baseline is derived
and only valid when both halves are known -- a missing EV reading invalidates
the baseline for that interval rather than being read as "no charging".
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
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
    MAX_PLAUSIBLE_EV_W,
)
from custom_components.alpha_ems_manager.quarter import sanitize_ev_w
from custom_components.alpha_ems_manager.storage import DayRecord

from .conftest import EV_POWER, HOUSE_LOAD, TEST_TIMEZONE, set_sensor

TZ = ZoneInfo(TEST_TIMEZONE)
START = datetime(2026, 8, 17, 10, 0, 0, tzinfo=TZ)
TODAY = START.date()
SLOT = 40  # 10:00

TZ_KEY = TEST_TIMEZONE


def record_with(
    measured: float | None, ev: float | None, ev_expected: bool
) -> DayRecord:
    """Return a one-interval day carrying the given values."""
    record = DayRecord(day=TODAY, tz_key=TZ_KEY, interval_count=96)
    record.record_interval(0, measured_kwh=measured, ev_kwh=ev, ev_expected=ev_expected)
    return record


# -- the baseline rule -------------------------------------------------------


def test_without_a_flexible_load_baseline_equals_measured() -> None:
    """No EV configured means nothing is subtracted."""
    record = record_with(0.5, None, ev_expected=False)

    assert record.baseline_at(0) == pytest.approx(0.5)
    assert record.baseline_total_kwh == pytest.approx(0.5)
    assert record.measured_total_kwh == pytest.approx(0.5)


def test_an_idle_charger_reporting_zero_leaves_baseline_untouched() -> None:
    """A numeric zero is a valid measurement, not missing data."""
    record = record_with(0.5, 0.0, ev_expected=True)

    assert record.baseline_at(0) == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("measured_kw", "ev_kw", "expected_kw"),
    [
        (2.0, 0.0, 2.0),
        (9.0, 7.0, 2.0),
        (11.0, 11.0, 0.0),
        (2.5, 2.0, 0.5),
    ],
)
def test_baseline_is_measured_minus_flexible(
    measured_kw: float, ev_kw: float, expected_kw: float
) -> None:
    """The documented arithmetic, in energy terms for one quarter."""
    record = record_with(measured_kw * 0.25, ev_kw * 0.25, ev_expected=True)

    assert record.baseline_at(0) == pytest.approx(expected_kw * 0.25)
    # Measured is never destructively replaced by the corrected figure.
    assert record.measured[0] == pytest.approx(measured_kw * 0.25)


def test_flexible_load_larger_than_measured_clamps_to_zero() -> None:
    """Baseline never goes negative, however the two sensors disagree."""
    record = record_with(0.25, 1.00, ev_expected=True)

    assert record.baseline_at(0) == pytest.approx(0.0)
    assert record.measured[0] == pytest.approx(0.25)


def test_measured_history_is_kept_when_baseline_is_invalid() -> None:
    """A missing EV reading must not discard the measured ground truth."""
    record = record_with(0.5, None, ev_expected=True)

    assert record.measured[0] == pytest.approx(0.5)
    assert record.measured_valid_count == 1
    assert record.baseline_at(0) is None
    assert record.baseline_valid_count == 0


def test_a_missing_flexible_reading_is_not_treated_as_zero() -> None:
    """The dangerous shortcut, ruled out explicitly.

    Assuming zero would fold a whole charging session into the baseline, which
    is exactly the contamination this feature exists to prevent.
    """
    unknown = record_with(2.75, None, ev_expected=True)
    idle = record_with(2.75, 0.0, ev_expected=True)

    assert idle.baseline_at(0) == pytest.approx(2.75)
    assert unknown.baseline_at(0) is None
    assert unknown.baseline_at(0) != idle.baseline_at(0)


# -- sanitisation ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0.0, 0.0),
        (7400.0, 7400.0),
        (22000.0, 22000.0),
        (-5.0, 0.0),  # noise around zero
        (-500.0, None),  # a real negative is an invalid sample
        (MAX_PLAUSIBLE_EV_W + 1, None),
        (None, None),
    ],
)
def test_ev_values_are_sanitised(raw: float | None, expected: float | None) -> None:
    """Negative charging power is refused rather than reinterpreted."""
    assert sanitize_ev_w(raw) == expected


def test_a_negative_reading_never_inflates_the_baseline() -> None:
    """Subtracting a negative would push baseline above measured."""
    assert sanitize_ev_w(-500.0) is None
    # And an invalid sample yields no baseline at all, rather than a bigger one.
    record = record_with(0.5, None, ev_expected=True)
    assert record.baseline_at(0) is None


# -- day-level accounting ----------------------------------------------------


def test_partial_flexible_coverage_keeps_measured_but_fails_baseline() -> None:
    """A day whose EV sensor died keeps measured data and stops being learned."""
    record = DayRecord(day=TODAY, tz_key=TZ_KEY, interval_count=96)
    for index in range(96):
        record.record_interval(
            index,
            measured_kwh=0.1,
            ev_kwh=0.0 if index < 40 else None,
            ev_expected=True,
        )

    assert record.measured_valid_count == 96
    assert record.measured_completeness == pytest.approx(1.0)
    assert record.baseline_valid_count == 40
    assert record.completeness == pytest.approx(40 / 96)
    assert not record.is_learned


def test_good_flexible_coverage_still_counts_as_learned() -> None:
    """Losing a few EV samples does not disqualify the day."""
    record = DayRecord(day=TODAY, tz_key=TZ_KEY, interval_count=96)
    for index in range(96):
        record.record_interval(
            index,
            measured_kwh=0.1,
            ev_kwh=None if index < 5 else 0.0,
            ev_expected=True,
        )

    assert record.completeness == pytest.approx(91 / 96)
    assert record.is_learned


def test_the_day_totals_are_reported_separately() -> None:
    """Measured, flexible and baseline totals are each available."""
    record = DayRecord(day=TODAY, tz_key=TZ_KEY, interval_count=96)
    for index in range(96):
        record.record_interval(index, measured_kwh=0.2, ev_kwh=0.05, ev_expected=True)

    assert record.measured_total_kwh == pytest.approx(19.2)
    assert record.ev_total_kwh == pytest.approx(4.8)
    assert record.baseline_total_kwh == pytest.approx(14.4)


async def test_the_three_series_round_trip_through_storage(
    hass: HomeAssistant,
) -> None:
    """Measured and flexible are both persisted; baseline stays derived."""
    from custom_components.alpha_ems_manager.storage import LearningStore

    store = LearningStore(hass, "entry-flex")
    record = store.get_or_create(TODAY, TZ)
    record.record_interval(0, measured_kwh=0.9, ev_kwh=0.4, ev_expected=True)
    record.record_interval(1, measured_kwh=0.3, ev_kwh=None, ev_expected=True)
    await store.async_save_now()

    reloaded = LearningStore(hass, "entry-flex")
    await reloaded.async_load(TZ_KEY)
    restored = reloaded.days[TODAY]

    assert restored.measured[0] == pytest.approx(0.9)
    assert restored.ev[0] == pytest.approx(0.4)
    assert restored.baseline_at(0) == pytest.approx(0.5)
    # The interval with no EV reading keeps its measured value but no baseline.
    assert restored.measured[1] == pytest.approx(0.3)
    assert restored.baseline_at(1) is None


# -- end to end through Home Assistant ---------------------------------------


async def advance(hass: HomeAssistant, freezer, seconds: int, step: int = 60) -> None:
    """Move the frozen clock forward, firing Home Assistant's time triggers."""
    for _ in range(seconds // step):
        freezer.tick(timedelta(seconds=step))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


async def setup_with_ev(
    hass: HomeAssistant,
    freezer,
    config_data: dict,
    house_w: float,
    ev_w: object,
    ev_configured: bool = True,
) -> MockConfigEntry:
    """Set the integration up with a flexible load and run one full interval."""
    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    freezer.move_to(START)
    set_sensor(hass, HOUSE_LOAD, house_w, "W", "power")
    if ev_configured:
        set_sensor(hass, EV_POWER, ev_w, "W", "power")

    data = dict(config_data)
    if ev_configured:
        data[CONF_EV_POWER_ENTITY] = EV_POWER

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Alpha EMS",
        data=data,
        options={},
        version=CONFIG_ENTRY_VERSION,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await advance(hass, freezer, 960)
    return entry


async def test_ev_charging_is_removed_from_the_learned_baseline(
    hass: HomeAssistant, freezer, config_data: dict
) -> None:
    """9 kW measured with 7 kW of charging learns a 2 kW baseline."""
    entry = await setup_with_ev(hass, freezer, config_data, 9000.0, 7000.0)
    record = entry.runtime_data.store.days[TODAY]

    assert record.measured[SLOT] == pytest.approx(9000 * 0.25 / 1000, rel=1e-3)
    assert record.ev[SLOT] == pytest.approx(7000 * 0.25 / 1000, rel=1e-3)
    assert record.baseline_at(SLOT) == pytest.approx(0.5, rel=1e-3)


async def test_without_an_ev_entity_baseline_equals_measured_end_to_end(
    hass: HomeAssistant, freezer, config_data: dict
) -> None:
    """An installation with no EV behaves exactly as before."""
    entry = await setup_with_ev(
        hass, freezer, config_data, 2000.0, None, ev_configured=False
    )
    record = entry.runtime_data.store.days[TODAY]

    assert record.measured[SLOT] == pytest.approx(0.5, rel=1e-3)
    assert record.baseline_at(SLOT) == pytest.approx(0.5, rel=1e-3)
    assert not entry.runtime_data.ev_configured


async def test_an_idle_charger_does_not_reduce_confidence(
    hass: HomeAssistant, freezer, config_data: dict
) -> None:
    """A charger reporting a numeric zero is fully valid data."""
    entry = await setup_with_ev(hass, freezer, config_data, 2000.0, 0.0)
    record = entry.runtime_data.store.days[TODAY]

    assert record.baseline_at(SLOT) == pytest.approx(0.5, rel=1e-3)
    assert record.baseline_valid_count == 1
    assert entry.runtime_data.invalid_ev_quarters == 0


@pytest.mark.parametrize("bad", ["unavailable", "unknown", "not a number"])
async def test_an_unusable_ev_state_invalidates_only_the_baseline(
    hass: HomeAssistant, freezer, config_data: dict, bad: str
) -> None:
    """Measured energy survives; the baseline for that interval does not."""
    entry = await setup_with_ev(hass, freezer, config_data, 2000.0, bad)
    record = entry.runtime_data.store.days[TODAY]

    assert record.measured[SLOT] == pytest.approx(0.5, rel=1e-3)
    assert record.baseline_at(SLOT) is None
    assert entry.runtime_data.invalid_ev_quarters >= 1


async def test_a_kilowatt_ev_sensor_is_normalised(
    hass: HomeAssistant, freezer, config_data: dict
) -> None:
    """kW and W both reduce to the same baseline.

    The previous implementation assumed kW without checking, so a watt-reporting
    charger silently subtracted a thousand times too much.
    """
    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    freezer.move_to(START)
    set_sensor(hass, HOUSE_LOAD, 9000.0, "W", "power")
    set_sensor(hass, EV_POWER, 7.0, "kW", "power")

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
    await advance(hass, freezer, 960)

    record = entry.runtime_data.store.days[TODAY]
    assert record.ev[SLOT] == pytest.approx(1.75, rel=1e-3)
    assert record.baseline_at(SLOT) == pytest.approx(0.5, rel=1e-3)


async def test_a_negative_ev_reading_invalidates_rather_than_inflates(
    hass: HomeAssistant, freezer, config_data: dict
) -> None:
    """A negative charger reading never pushes baseline above measured."""
    entry = await setup_with_ev(hass, freezer, config_data, 2000.0, -3000.0)
    record = entry.runtime_data.store.days[TODAY]

    assert record.measured[SLOT] == pytest.approx(0.5, rel=1e-3)
    assert record.baseline_at(SLOT) is None


async def test_the_forecast_is_built_from_baseline_not_measured(
    hass: HomeAssistant, freezer, config_data: dict
) -> None:
    """Learned demand excludes EV charging.

    Two identical measured days differing only in EV content must produce
    different learned baselines -- otherwise the separation is cosmetic.
    """
    from custom_components.alpha_ems_manager.forecast import build_forecast

    reference = date(2026, 8, 17)
    with_ev: list[DayRecord] = []
    without_ev: list[DayRecord] = []
    for offset in range(1, 31):
        day = reference - timedelta(days=offset)
        charged = DayRecord(day=day, tz_key=TZ_KEY, interval_count=96)
        plain = DayRecord(day=day, tz_key=TZ_KEY, interval_count=96)
        for index in range(96):
            charged.record_interval(
                index, measured_kwh=0.2, ev_kwh=0.1, ev_expected=True
            )
            plain.record_interval(
                index, measured_kwh=0.2, ev_kwh=None, ev_expected=False
            )
        with_ev.append(charged)
        without_ev.append(plain)

    charged_forecast = build_forecast(with_ev, reference, reference, TZ)
    plain_forecast = build_forecast(without_ev, reference, reference, TZ)

    assert plain_forecast.total_kwh == pytest.approx(19.2, rel=1e-3)
    assert charged_forecast.total_kwh == pytest.approx(9.6, rel=1e-3)
    assert charged_forecast.total_kwh < plain_forecast.total_kwh
