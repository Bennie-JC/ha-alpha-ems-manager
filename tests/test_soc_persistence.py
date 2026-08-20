"""Recording the measured state of charge, and the promise that it changes nothing.

This is the highest-risk change in Phase 3, because it touches the learning
store -- the only data in this project that cannot be regenerated. It is worth
the risk for one reason: a prediction can be recomputed from the stored forecast
and the stored configuration, but where the battery actually *was* at 03:15 last
Tuesday cannot be recovered from anything. Every day without it is a day the
physical model can never be checked against reality.

So the whole file is organised around one invariant. Recorded state of charge is
additive evidence for a later phase, never a learning input: adding it, removing
it or corrupting it must not move a single Phase-1 or Phase-2 figure.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.const import (
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
)
from custom_components.alpha_ems_manager.storage import DayRecord, LearningStore

from .conftest import BATTERY_SOC, TEST_TIMEZONE, set_sensor
from .forecast_helpers import NORMAL, history_before, local, refresh_at, reseed, seed
from .synthetic import empty_day, flat_day

TOMORROW = NORMAL + timedelta(days=1)


def record_with_soc(day: date, samples: dict[int, float]) -> DayRecord:
    """Return a fully measured day carrying state-of-charge samples."""
    record = flat_day(day, 12.0)
    for index, value in samples.items():
        record.soc[index] = value
    return record


# -- the shape ---------------------------------------------------------------


def test_a_fresh_record_has_an_empty_array_of_the_right_length() -> None:
    """Sized like every other parallel array, and never zero-filled."""
    record = empty_day(NORMAL)

    assert len(record.soc) == record.interval_count == 96
    assert record.soc == [None] * 96
    assert record.soc_sample_count == 0


@pytest.mark.parametrize("day", [date(2026, 3, 29), NORMAL, date(2026, 10, 25)])
def test_the_array_matches_the_real_length_of_the_civil_day(day: date) -> None:
    """92, 96 or 100, like the arrays beside it."""
    record = empty_day(day)

    assert len(record.soc) == record.interval_count
    assert len(record.soc) == len(record.measured) == len(record.ev)


def test_the_array_is_resized_with_the_others() -> None:
    """A shorter or longer stored array is padded or trimmed, not left ragged."""
    record = DayRecord(day=NORMAL, tz_key=TEST_TIMEZONE, interval_count=96)
    record.soc = [55.0, 54.0]
    record._resize()

    assert len(record.soc) == 96
    assert record.soc[:2] == [55.0, 54.0]
    assert record.soc[2:] == [None] * 94


def test_a_sample_is_stored_at_the_precision_the_sensor_actually_has() -> None:
    """One decimal, because the source reports whole percent.

    Four decimals would advertise a resolution the sensor does not have, and the
    seed quantisation already dominates every other error in the model.
    """
    record = empty_day(NORMAL)
    record.record_interval(
        0, measured_kwh=0.125, ev_kwh=None, ev_expected=False, soc_percent=55.55555
    )

    assert record.soc[0] == 55.6


def test_an_out_of_range_index_stores_nothing() -> None:
    """The same guard the measured arrays already have."""
    record = empty_day(NORMAL)

    assert (
        record.record_interval(
            200, measured_kwh=0.1, ev_kwh=None, ev_expected=False, soc_percent=55.0
        )
        is False
    )
    assert record.soc_sample_count == 0
    assert record.soc_at(200) is None
    assert record.soc_at(-1) is None


def test_recording_an_interval_without_a_sample_leaves_it_missing() -> None:
    """Absent is absent. A quarter with no reading is not a quarter at zero."""
    record = empty_day(NORMAL)
    record.record_interval(0, measured_kwh=0.125, ev_kwh=None, ev_expected=False)

    assert record.soc[0] is None
    assert record.measured[0] == 0.125


# -- serialisation -----------------------------------------------------------


def test_the_array_is_omitted_entirely_when_there_is_nothing_to_store() -> None:
    """An installation with no usable reading pays no bytes for the field.

    Exactly what the flexible-load arrays already do, and for the same reason.
    """
    payload = flat_day(NORMAL, 12.0).to_dict()

    assert "s" not in payload
    assert set(payload) == {"tz", "n", "m"}


def test_a_day_with_samples_round_trips_exactly() -> None:
    """Written, read back, and identical -- including the gaps."""
    original = record_with_soc(NORMAL, {0: 55.0, 47: 42.5, 95: 20.0})
    rebuilt = DayRecord.from_dict(NORMAL, original.to_dict(), TEST_TIMEZONE)

    assert rebuilt is not None
    assert rebuilt.soc == original.soc
    assert rebuilt.soc_at(0) == 55.0
    assert rebuilt.soc_at(47) == 42.5
    assert rebuilt.soc_at(95) == 20.0
    assert rebuilt.soc_at(1) is None
    assert rebuilt.soc_sample_count == 3


def test_a_document_written_before_the_field_existed_loads_cleanly() -> None:
    """Every day recorded up to v1.0.0-beta.6 has no such array."""
    rebuilt = DayRecord.from_dict(
        NORMAL, {"tz": TEST_TIMEZONE, "n": 96, "m": [0.125] * 96}, TEST_TIMEZONE
    )

    assert rebuilt is not None
    assert rebuilt.soc == [None] * 96
    assert rebuilt.is_learned is True
    assert rebuilt.completeness == 1.0


@pytest.mark.parametrize(
    "stored",
    [
        "not a list",
        None,
        [],
        [float("nan")] * 96,
        [float("inf"), float("-inf")] + [None] * 94,
        [True, False] + [None] * 94,
        ["55", {}, []] + [None] * 93,
        [55.0] * 200,
    ],
)
def test_a_damaged_array_degrades_to_missing_samples(stored) -> None:
    """Never a plausible-looking number, and never an exception.

    ``_numeric_list`` already refuses a non-finite or non-numeric entry -- booleans
    included, because ``True`` is an ``int`` -- so this inherits the hardening the
    measured arrays got in beta.6 rather than reimplementing it.
    """
    rebuilt = DayRecord.from_dict(
        NORMAL,
        {"tz": TEST_TIMEZONE, "n": 96, "m": [0.125] * 96, "s": stored},
        TEST_TIMEZONE,
    )

    assert rebuilt is not None
    assert len(rebuilt.soc) == 96
    for value in rebuilt.soc:
        assert value is None or (isinstance(value, float) and 0.0 <= value <= 100.0)
    # And the day is still exactly as learnable as it was.
    assert rebuilt.is_learned is True


@pytest.mark.usefixtures("setup_integration")
async def test_the_store_round_trips_samples_through_disk(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Through the real store, at the real version, as a restart would."""
    coordinator = setup_integration.runtime_data
    coordinator.store.days = {NORMAL: record_with_soc(NORMAL, {0: 55.0, 1: 54.0})}
    await coordinator.store.async_save_now()

    reloaded = LearningStore(hass, setup_integration.entry_id)
    await reloaded.async_load(TEST_TIMEZONE)

    assert reloaded.days[NORMAL].soc_at(0) == 55.0
    assert reloaded.days[NORMAL].soc_at(1) == 54.0
    assert reloaded.days[NORMAL].soc_sample_count == 2


@pytest.mark.usefixtures("setup_integration")
async def test_the_document_declares_the_new_minor_version(
    hass: HomeAssistant, setup_integration: MockConfigEntry, hass_storage: dict
) -> None:
    """Minor, not major: every earlier document is read unchanged."""
    coordinator = setup_integration.runtime_data
    coordinator.store.days = {NORMAL: record_with_soc(NORMAL, {0: 55.0})}
    await coordinator.store.async_save_now()

    document = hass_storage[f"alpha_ems_manager.{setup_integration.entry_id}.learning"]

    assert document["version"] == STORAGE_VERSION == 2
    assert document["minor_version"] == STORAGE_MINOR_VERSION == 3
    assert document["data"]["days"][NORMAL.isoformat()]["s"][0] == 55.0


# -- THE invariant -----------------------------------------------------------


#: The Phase-1 and Phase-2 figures that must not move. Everything a state of
#: charge could plausibly have leaked into.
def _phase_one_two_figures(record: DayRecord) -> dict[str, object]:
    """Return every derived figure a day record publishes."""
    return {
        "completeness": record.completeness,
        "measured_completeness": record.measured_completeness,
        "is_learned": record.is_learned,
        "measured_valid_count": record.measured_valid_count,
        "baseline_valid_count": record.baseline_valid_count,
        "measured_total_kwh": record.measured_total_kwh,
        "baseline_total_kwh": record.baseline_total_kwh,
        "ev_total_kwh": record.ev_total_kwh,
        "baselines": [
            record.baseline_at(index) for index in range(record.interval_count)
        ],
    }


@pytest.mark.parametrize(
    "samples",
    [
        {},
        {0: 55.0},
        dict.fromkeys(range(96), 55.0),
        {index: float(index) for index in range(96)},
        {0: 0.0, 95: 100.0},
    ],
)
def test_no_state_of_charge_sample_changes_any_learning_figure(samples: dict) -> None:
    """The invariant, stated as bluntly as it can be.

    Adding, removing or changing recorded state-of-charge samples must not move
    completeness, learnability, the baseline, or any total. If this ever fails,
    a level has been mistaken for a flow somewhere.
    """
    without = flat_day(NORMAL, 12.0)
    baseline = _phase_one_two_figures(without)

    with_samples = record_with_soc(NORMAL, samples)

    assert _phase_one_two_figures(with_samples) == baseline
    # And the day serialises to the same document apart from the added key.
    stripped = {
        key: value for key, value in with_samples.to_dict().items() if key != "s"
    }
    assert stripped == without.to_dict()


def test_a_day_with_only_state_of_charge_samples_is_still_not_learned() -> None:
    """Evidence for a later phase is not a substitute for a measurement.

    A day whose house-load sensor was down all day but whose battery reported
    happily must not count as a learned day.
    """
    record = empty_day(NORMAL)
    for index in range(96):
        record.soc[index] = 55.0

    assert record.soc_sample_count == 96
    assert record.is_learned is False
    assert record.completeness == 0.0
    assert record.baseline_total_kwh == 0.0


@pytest.mark.usefixtures("setup_integration")
async def test_recording_samples_does_not_move_the_published_figures(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """End to end, through the real coordinator and the real sensors."""
    coordinator = setup_integration.runtime_data

    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))
    before = {
        entity_id: hass.states.get(entity_id).state
        for entity_id in (
            "sensor.alpha_ems_learning_days",
            "sensor.alpha_ems_learning_confidence",
            "sensor.alpha_ems_expected_house_load_today",
            "sensor.alpha_ems_expected_house_load_tomorrow",
        )
    }

    # The same history at the same instant, now carrying a full day of
    # state-of-charge samples. The instant matters: refreshing at a later minute
    # would change the *adapted* Today figure for reasons that have nothing to do
    # with the battery, and this test would then be measuring the clock.
    with_samples = {
        day: record_with_soc(day, dict.fromkeys(range(96), 55.0))
        for day in history_before(NORMAL)
    }
    reseed(coordinator, with_samples)
    await refresh_at(coordinator, local(NORMAL, 12, 5))

    for entity_id, value in before.items():
        assert hass.states.get(entity_id).state == value, entity_id


@pytest.mark.usefixtures("setup_integration")
async def test_samples_do_not_disturb_phase_two_scoring(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A scored day scores identically with and without a battery trace."""
    coordinator = setup_integration.runtime_data

    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))
    reseed(coordinator, {**history_before(NORMAL), NORMAL: flat_day(NORMAL, 9.6)})
    await refresh_at(coordinator, local(TOMORROW, 0, 5))
    without = dict(coordinator.history.days[NORMAL].summary)

    # The same timeline again, with the actual day carrying a battery trace.
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))
    actual = record_with_soc(NORMAL, dict.fromkeys(range(96), 42.0))
    for index in range(96):
        actual.measured[index] = 0.1
    reseed(coordinator, {**history_before(NORMAL), NORMAL: actual})
    await refresh_at(coordinator, local(TOMORROW, 0, 5))

    with_samples = dict(coordinator.history.days[NORMAL].summary)
    assert with_samples == without


# -- the live sampling path -------------------------------------------------


async def test_the_coordinator_records_a_sample_when_a_quarter_closes(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry, config_data: dict
) -> None:
    """The first live read of the battery state of charge in the project.

    Sampled at the boundary because a state of charge is a level, not a flow --
    it does not pass through the quarter accumulator.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from .conftest import HOUSE_LOAD

    tz = ZoneInfo(TEST_TIMEZONE)
    start = datetime(2026, 8, 17, 10, 0, 0, tzinfo=tz)
    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    freezer.move_to(start)
    set_sensor(hass, HOUSE_LOAD, 2000, "W", "power")
    set_sensor(hass, BATTERY_SOC, 61, "%", "battery")
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    for _ in range(24):
        freezer.tick(timedelta(seconds=60))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    record = mock_config_entry.runtime_data.store.days[start.date()]
    assert record.soc_sample_count >= 1
    assert 61.0 in [value for value in record.soc if value is not None]


async def test_an_unusable_reading_records_no_sample(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """Never a fabricated zero, at the newest write path in the project."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from .conftest import HOUSE_LOAD

    tz = ZoneInfo(TEST_TIMEZONE)
    start = datetime(2026, 8, 17, 10, 0, 0, tzinfo=tz)
    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    freezer.move_to(start)
    set_sensor(hass, HOUSE_LOAD, 2000, "W", "power")
    set_sensor(hass, BATTERY_SOC, "unavailable", "%", "battery")
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    for _ in range(24):
        freezer.tick(timedelta(seconds=60))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    record = mock_config_entry.runtime_data.store.days[start.date()]
    # Measured energy was recorded; the state of charge simply was not.
    assert record.measured_valid_count >= 1
    assert record.soc_sample_count == 0
    assert 0.0 not in [value for value in record.soc if value is not None]


def test_only_the_interval_that_just_closed_takes_the_sample() -> None:
    """A catch-up after a restart must not repeat today's reading into the past.

    Where the battery was two hours ago is genuinely unknown, and writing the
    current reading across a backlog would be inventing history -- the same rule
    the measured arrays follow when a quarter never reached coverage.
    """
    from custom_components.alpha_ems_manager import coordinator as coordinator_module

    source = coordinator_module.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()

    assert "latest_start" in text
    assert "soc_percent=soc_percent" in text
    assert "if result.start_utc == latest_start" in text
