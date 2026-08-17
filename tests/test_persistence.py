"""Persistence, retention and recovery of the learning history.

History lives in Home Assistant's ``Store``, not in entity attributes and not in
the recorder. These tests cover the round trip, the 365-day retention window,
and what happens when the stored document is damaged.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.alpha_ems_manager.const import (
    MAX_HISTORY_DAYS,
    SLOTS_PER_DAY,
    STORAGE_VERSION,
)
from custom_components.alpha_ems_manager.storage import (
    DayRecord,
    LearningStore,
    expected_quarters_for,
)

from .conftest import HOUSE_LOAD, TEST_TIMEZONE, set_sensor

TZ = ZoneInfo(TEST_TIMEZONE)
START = datetime(2026, 8, 17, 10, 0, 0, tzinfo=TZ)
TODAY = START.date()
TZ_KEY = TEST_TIMEZONE


def storage_key(entry_id: str) -> str:
    """Return the ``.storage`` key used for one config entry."""
    return f"alpha_ems_manager.{entry_id}.learning"


async def advance(hass: HomeAssistant, freezer, seconds: int, step: int = 60) -> None:
    """Move the frozen clock forward, firing Home Assistant's time triggers."""
    for _ in range(seconds // step):
        freezer.tick(timedelta(seconds=step))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


# -- round trip --------------------------------------------------------------


async def test_a_learned_day_round_trips_through_the_store(
    hass: HomeAssistant,
) -> None:
    """What is written is what comes back."""
    store = LearningStore(hass, "entry-a")
    record = store.get_or_create(TODAY, TZ)
    record.record_interval(40, measured_kwh=0.5, ev_kwh=None, ev_expected=False)
    record.record_interval(41, measured_kwh=0.25, ev_kwh=None, ev_expected=False)
    store.balance.record(True)
    store.balance.record(False)
    await store.async_save_now()

    reloaded = LearningStore(hass, "entry-a")
    await reloaded.async_load(TZ_KEY)

    restored = reloaded.days[TODAY]
    assert restored.measured[40] == pytest.approx(0.5)
    assert restored.measured[41] == pytest.approx(0.25)
    assert restored.measured_total_kwh == pytest.approx(0.75)
    assert restored.measured_valid_count == 2
    assert reloaded.balance.total_samples == 2
    assert reloaded.balance.ok_samples == 1


async def test_two_instances_keep_separate_history(hass: HomeAssistant) -> None:
    """Storage is keyed per config entry, so two houses never mix."""
    first = LearningStore(hass, "entry-house-a")
    first.get_or_create(TODAY, TZ).record_interval(
        0, measured_kwh=1.0, ev_kwh=None, ev_expected=False
    )
    await first.async_save_now()

    second = LearningStore(hass, "entry-house-b")
    second.get_or_create(TODAY, TZ).record_interval(
        0, measured_kwh=9.0, ev_kwh=None, ev_expected=False
    )
    await second.async_save_now()

    reloaded = LearningStore(hass, "entry-house-a")
    await reloaded.async_load(TZ_KEY)

    assert reloaded.days[TODAY].measured[0] == pytest.approx(1.0)


async def test_the_stored_document_has_the_documented_schema(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """The on-disk shape is versioned and compact."""
    store = LearningStore(hass, "entry-schema")
    store.get_or_create(TODAY, TZ).record_interval(
        3, measured_kwh=0.4, ev_kwh=None, ev_expected=False
    )
    await store.async_save_now()

    raw = hass_storage[storage_key("entry-schema")]
    assert raw["version"] == STORAGE_VERSION
    assert set(raw["data"]) == {"days", "balance", "last_finalized"}

    day = raw["data"]["days"][TODAY.isoformat()]
    # The flexible-load arrays are omitted entirely when no interval expected
    # one, which keeps the document small for installations without an EV.
    assert set(day) == {"tz", "n", "m"}
    assert day["tz"] == TZ_KEY
    assert day["n"] == SLOTS_PER_DAY
    assert len(day["m"]) == SLOTS_PER_DAY
    # Unmeasured intervals are null, never zero: a gap must stay a gap.
    assert day["m"][0] is None
    assert day["m"][3] == pytest.approx(0.4)


# -- retention ---------------------------------------------------------------


async def test_history_is_capped_at_the_retention_window(
    hass: HomeAssistant,
) -> None:
    """Exactly 365 days are retained."""
    store = LearningStore(hass, "entry-retention")
    for offset in range(MAX_HISTORY_DAYS):
        store.get_or_create(TODAY - timedelta(days=offset), TZ)

    assert len(store.days) == MAX_HISTORY_DAYS
    assert (TODAY - timedelta(days=MAX_HISTORY_DAYS - 1)) in store.days


async def test_day_three_hundred_and_sixty_six_expires_the_oldest(
    hass: HomeAssistant,
) -> None:
    """Adding one more day drops the oldest, not an arbitrary one."""
    store = LearningStore(hass, "entry-rollover")
    oldest = TODAY - timedelta(days=MAX_HISTORY_DAYS - 1)
    for offset in range(MAX_HISTORY_DAYS):
        store.get_or_create(TODAY - timedelta(days=offset), TZ)
    assert oldest in store.days

    store.get_or_create(TODAY + timedelta(days=1), TZ)

    assert oldest not in store.days
    assert len(store.days) == MAX_HISTORY_DAYS
    assert (TODAY + timedelta(days=1)) in store.days


# -- corruption --------------------------------------------------------------


async def test_corrupt_storage_does_not_break_setup(hass: HomeAssistant) -> None:
    """An unreadable store degrades to empty history rather than failing."""
    store = LearningStore(hass, "entry-corrupt")
    with patch(
        "homeassistant.helpers.storage.Store.async_load",
        side_effect=ValueError("damaged json"),
    ):
        await store.async_load(TZ_KEY)

    assert store.corrupt
    assert store.days == {}


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "not a dict",
        {"days": "not a dict"},
        {"days": {"not-a-date": {"m": []}}},
        {"days": {"2026-08-17": "not a dict"}},
        {"days": {"2026-08-17": {"m": "not a list"}}},
        {"days": {"2026-08-17": {}}},
        {"days": {"2026-08-17": {"m": [0.1], "n": "not an int"}}},
        {"days": {"2026-08-17": {"m": [0.1], "n": 0}}},
        {"balance": "nonsense"},
        {"days": {}, "balance": {"ok": "x", "total": None}},
    ],
)
async def test_malformed_documents_are_survived(hass: HomeAssistant, payload) -> None:
    """Every shape of damaged document loads without raising."""
    store = LearningStore(hass, "entry-malformed")
    with patch("homeassistant.helpers.storage.Store.async_load", return_value=payload):
        await store.async_load(TZ_KEY)

    assert isinstance(store.days, dict)
    assert store.balance.total_samples >= 0


async def test_partially_damaged_days_keep_the_good_ones(
    hass: HomeAssistant,
) -> None:
    """One bad day does not discard the rest of the year."""
    good = DayRecord(day=TODAY, tz_key=TZ_KEY, interval_count=SLOTS_PER_DAY)
    good.record_interval(5, measured_kwh=0.3, ev_kwh=None, ev_expected=False)
    payload = {
        "days": {
            TODAY.isoformat(): good.to_dict(),
            "garbage-date": {"m": [1, 2, 3]},
            "2026-08-18": {"m": None},
        },
        "balance": {"ok": 3, "total": 4},
    }

    store = LearningStore(hass, "entry-partial")
    with patch("homeassistant.helpers.storage.Store.async_load", return_value=payload):
        await store.async_load(TZ_KEY)

    assert set(store.days) == {TODAY}
    assert store.days[TODAY].measured[5] == pytest.approx(0.3)


async def test_non_numeric_slot_values_are_dropped_not_zeroed(
    hass: HomeAssistant,
) -> None:
    """A corrupted slot becomes missing data, never a zero-consumption quarter."""
    payload = {
        "days": {
            TODAY.isoformat(): {
                "tz": TZ_KEY,
                "n": SLOTS_PER_DAY,
                "m": ["bad", None, 0.25, True] + [None] * (SLOTS_PER_DAY - 4),
            }
        }
    }
    store = LearningStore(hass, "entry-badslots")
    with patch("homeassistant.helpers.storage.Store.async_load", return_value=payload):
        await store.async_load(TZ_KEY)

    measured = store.days[TODAY].measured
    assert measured[0] is None  # "bad" dropped
    assert measured[2] == pytest.approx(0.25)
    assert measured[3] is None  # True is not a measurement


# -- DST-aware day length ----------------------------------------------------


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 8, 17), 96),
        (date(2026, 3, 29), 92),  # spring forward, 23 hours
        (date(2026, 10, 25), 100),  # fall back, 25 hours
    ],
)
def test_expected_quarters_follows_the_civil_day(day: date, expected: int) -> None:
    """Day length comes from real timezone arithmetic, not a hard-coded 96."""
    assert expected_quarters_for(day, TZ) == expected


def test_completeness_uses_the_real_day_length() -> None:
    """A 23-hour day is complete at 92 quarters, not short of 96."""
    record = DayRecord(day=date(2026, 3, 29), tz_key=TZ_KEY, interval_count=92)
    for index in range(92):
        record.record_interval(index, measured_kwh=0.1, ev_kwh=None, ev_expected=False)

    assert record.completeness == pytest.approx(1.0)
    assert record.is_learned


# -- lifecycle ---------------------------------------------------------------


async def test_history_survives_a_reload(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """Reloading the entry keeps everything that was already learned."""
    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    freezer.move_to(START)
    set_sensor(hass, HOUSE_LOAD, 2000, "W", "power")

    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    await advance(hass, freezer, 960)

    before = mock_config_entry.runtime_data.store.days[TODAY].measured[40]
    assert before == pytest.approx(0.5, rel=1e-3)

    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    after = mock_config_entry.runtime_data.store.days[TODAY].measured[40]
    assert after == pytest.approx(before)


async def test_history_survives_a_simulated_restart(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """Unloading and setting up again -- what a restart looks like -- keeps data."""
    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    freezer.move_to(START)
    set_sensor(hass, HOUSE_LOAD, 2000, "W", "power")

    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    await advance(hass, freezer, 960)
    before = dict(mock_config_entry.runtime_data.store.days)

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    after = mock_config_entry.runtime_data.store.days
    assert set(after) == set(before)
    assert after[TODAY].measured[40] == pytest.approx(before[TODAY].measured[40])


async def test_downtime_creates_no_phantom_consumption(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """Hours of downtime leave gaps, not invented load.

    The clock jumps forward while the integration is unloaded. On setup the
    accumulator restarts conservatively, so the skipped quarters stay empty.
    """
    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    freezer.move_to(START)
    set_sensor(hass, HOUSE_LOAD, 2000, "W", "power")

    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    await advance(hass, freezer, 960)

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    # Four hours pass with Home Assistant down.
    freezer.move_to(START + timedelta(hours=4))
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    record = mock_config_entry.runtime_data.store.days[TODAY]
    populated = [
        index for index, value in enumerate(record.measured) if value is not None
    ]

    # Only the quarter that was genuinely measured before the outage.
    assert populated == [40]
    assert record.measured_valid_count == 1
