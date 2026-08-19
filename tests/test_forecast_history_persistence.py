"""Persistence, retention and failure behaviour of the forecast evidence.

The rules under test are the two the learning store learned the hard way, plus
the partitioning that keeps them affordable:

* a failed read never leads to a write, so a transient I/O error cannot be
  promoted into permanent loss;
* pruning clamps its reference against known history, so a clock excursion
  cannot delete the retention window behind it;
* a corrupt month costs that month and nothing else.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.const import (
    FORECAST_RAW_RETENTION_DAYS,
    FORECAST_STORAGE_VERSION,
    FORECAST_SUMMARY_RETENTION_DAYS,
)
from custom_components.alpha_ems_manager.forecast_history import (
    DayOutcome,
    ForecastSnapshot,
)
from custom_components.alpha_ems_manager.history_store import (
    ForecastHistoryStore,
    month_key,
)

from .forecast_helpers import (
    FALL_BACK,
    NORMAL,
    SPRING_FORWARD,
    history_before,
    local,
    refresh_at,
    reseed,
    seed,
)
from .synthetic import flat_day

pytestmark = pytest.mark.usefixtures("setup_integration")

TOMORROW = NORMAL + timedelta(days=1)


def index_key(entry: MockConfigEntry) -> str:
    """Return the storage key of the index document."""
    return f"alpha_ems_manager.{entry.entry_id}.forecast_index"


def month_store_key(entry: MockConfigEntry, day: date) -> str:
    """Return the storage key of the partition holding ``day``."""
    return f"alpha_ems_manager.{entry.entry_id}.forecast.{month_key(day)}"


async def reload_store(
    hass: HomeAssistant, entry: MockConfigEntry, days: list[date]
) -> ForecastHistoryStore:
    """Return a freshly loaded store, as a restart would produce."""
    store = ForecastHistoryStore(hass, entry.entry_id)
    await store.async_load()
    await store.async_ensure_days(days)
    return store


# -- round trip --------------------------------------------------------------


async def test_evidence_survives_a_restart(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Snapshots and outcomes come back from disk byte for byte."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))
    reseed(coordinator, {**history_before(NORMAL), NORMAL: flat_day(NORMAL, 12.0)})
    await refresh_at(coordinator, local(TOMORROW, 0, 5))
    await coordinator.history.async_save_now()

    original_snapshot = coordinator.history.snapshots(NORMAL)[0]
    original_outcome = coordinator.history.outcome(NORMAL)

    restarted = await reload_store(hass, setup_integration, [NORMAL, TOMORROW])
    restored_snapshot = restarted.snapshots(NORMAL)[0]
    restored_outcome = restarted.outcome(NORMAL)

    assert restored_snapshot.fingerprint == original_snapshot.fingerprint
    assert restored_snapshot.predicted == original_snapshot.predicted
    assert restored_snapshot.filled == original_snapshot.filled
    assert restored_snapshot.issued_at == original_snapshot.issued_at
    assert restored_snapshot.context == original_snapshot.context
    assert original_outcome is not None and restored_outcome is not None
    assert restored_outcome.actual == original_outcome.actual
    assert restored_outcome.status == original_outcome.status


async def test_a_restart_does_not_re_issue_a_snapshot_it_already_holds(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The fingerprint must survive the process, or every restart duplicates.

    This is why the digest is SHA-256 over canonical JSON rather than the
    built-in ``hash()``, which is salted per process.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))
    await coordinator.history.async_save_now()
    before = coordinator.history.snapshot_total

    restarted = await reload_store(hass, setup_integration, [NORMAL, TOMORROW])
    coordinator.history = restarted
    coordinator.recorder.store = restarted
    coordinator.recorder._last_day = None
    await refresh_at(coordinator, local(NORMAL, 12, 20))

    assert coordinator.history.snapshot_total == before
    assert coordinator.last_record.issued == ()


async def test_the_document_uses_the_documented_layout(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """Index plus one partition per month of target days."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))
    await coordinator.history.async_save_now()

    index = hass_storage[index_key(setup_integration)]
    assert index["version"] == FORECAST_STORAGE_VERSION
    assert index["data"]["months"] == ["2026-08"]
    row = index["data"]["days"]["2026-08-19"]
    assert row["n"] == 96
    assert len(row["fp"]) == 1

    partition = hass_storage[month_store_key(setup_integration, NORMAL)]
    day = partition["data"]["days"]["2026-08-19"]
    assert len(day["s"]) == 1
    assert len(day["s"][0]["p"]) == 96
    assert len(day["s"][0]["f"]) == 96
    assert set(day["s"][0]["f"]) <= {"0", "1"}


async def test_targets_in_different_months_use_different_partitions(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """A month boundary between today and tomorrow must not lose either."""
    coordinator = setup_integration.runtime_data
    last_of_month = date(2026, 8, 31)
    seed(coordinator, history_before(last_of_month))
    await refresh_at(coordinator, local(last_of_month, 12, 5))
    await coordinator.history.async_save_now()

    assert set(coordinator.history.months) == {"2026-08", "2026-09"}
    assert month_store_key(setup_integration, last_of_month) in hass_storage
    assert month_store_key(setup_integration, date(2026, 9, 1)) in hass_storage


# -- failure behaviour -------------------------------------------------------


async def test_a_failed_index_read_suspends_every_write(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The rule that cost the learning store a year of history once.

    With no index there is no way to distinguish an empty history from an
    unreadable one, and writing either guess would destroy the other.
    """
    store = ForecastHistoryStore(hass, setup_integration.entry_id)
    with patch(
        "homeassistant.helpers.storage.Store.async_load",
        side_effect=OSError("disk gone"),
    ):
        await store.async_load()

    assert store.corrupt is True
    assert store.writable(NORMAL) is False

    with (
        patch("homeassistant.helpers.storage.Store.async_save") as save,
        patch("homeassistant.helpers.storage.Store.async_delay_save") as delay_save,
    ):
        store.schedule_save()
        await store.async_save_now()

    save.assert_not_called()
    delay_save.assert_not_called()


async def test_a_failed_read_leaves_the_document_exactly_as_it_was(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """The whole point: the file on disk must be untouched."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))
    await coordinator.history.async_save_now()
    intact = dict(hass_storage[index_key(setup_integration)]["data"])

    store = ForecastHistoryStore(hass, setup_integration.entry_id)
    with patch(
        "homeassistant.helpers.storage.Store.async_load",
        side_effect=OSError("transient"),
    ):
        await store.async_load()
    await store.async_save_now()

    assert hass_storage[index_key(setup_integration)]["data"] == intact


async def test_a_corrupt_month_partition_does_not_take_the_rest_with_it(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Partitioning exists so one bad document costs one month."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))
    await coordinator.history.async_save_now()

    store = ForecastHistoryStore(hass, setup_integration.entry_id)
    await store.async_load()
    with patch(
        "homeassistant.helpers.storage.Store.async_load",
        side_effect=OSError("bad month"),
    ):
        partition = await store.async_partition("2026-08")

    assert partition.corrupt is True
    assert store.corrupt is False
    assert store.writable(NORMAL) is False
    # The index still knows the day exists; only its arrays are unreachable.
    assert store.row(NORMAL) is not None
    report = {entry["month"]: entry for entry in store.partition_report()}
    assert report["2026-08"]["corrupt"] is True


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "not a document",
        {"days": "not a mapping"},
        {"days": {"not-a-date": {}}},
        {"months": "nope", "days": {}},
    ],
)
async def test_a_malformed_index_is_survived(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_storage: dict,
    payload: object,
) -> None:
    """Damage degrades to an empty view rather than an exception."""
    hass_storage[index_key(setup_integration)] = {
        "version": FORECAST_STORAGE_VERSION,
        "minor_version": 1,
        "key": index_key(setup_integration),
        "data": payload,
    }

    store = ForecastHistoryStore(hass, setup_integration.entry_id)
    await store.async_load()

    assert store.corrupt is False
    assert store.days == {}


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"iat": "not a date", "n": 96, "av": True, "p": []},
        {"iat": "2026-08-19T10:05:00+00:00", "n": 0},
        {"iat": "2026-08-19T10:05:00+00:00", "n": 96, "av": True},
        {"iat": "2026-08-19T10:05:00", "n": 96, "av": False},
    ],
)
def test_a_malformed_snapshot_is_dropped_not_guessed(raw: dict) -> None:
    """A record that cannot be trusted is not a record."""
    assert ForecastSnapshot.from_dict(NORMAL, raw) is None


def test_a_corrupt_actual_becomes_missing_never_zero() -> None:
    """The single most important deserialisation rule in the project."""
    outcome = DayOutcome.from_dict(
        NORMAL,
        {
            "fin": "2026-08-20T00:05:00+00:00",
            "tz": "Europe/Amsterdam",
            "n": 4,
            "a": [0.1, "broken", None, True],
            "s": "0000",
            "fl": [],
        },
    )

    assert outcome is not None
    assert outcome.actual == (0.1, None, None, None)
    assert outcome.valid_indices() == [0]


def test_a_short_stored_array_is_padded_with_missing_not_zero() -> None:
    """A truncated write must not shorten the day or invent measurements."""
    outcome = DayOutcome.from_dict(
        NORMAL,
        {
            "fin": "2026-08-20T00:05:00+00:00",
            "tz": "Europe/Amsterdam",
            "n": 96,
            "a": [0.1, 0.1],
            "s": "00",
            "fl": [],
        },
    )

    assert outcome is not None
    assert len(outcome.actual) == 96
    assert len(outcome.status) == 96
    assert outcome.valid_indices() == [0, 1]
    assert all(value is None for value in outcome.actual[2:])


# -- retention ---------------------------------------------------------------


def a_snapshot(day: date, *, fingerprint: str) -> ForecastSnapshot:
    """Return a minimal but complete snapshot for ``day``."""
    return ForecastSnapshot(
        issued_at=datetime(day.year, day.month, day.day, 12, tzinfo=UTC),
        target_day=day,
        tz_key="Europe/Amsterdam",
        interval_count=96,
        horizon_days=0,
        available=True,
        unavailable_reason=None,
        predicted=tuple([0.125] * 96),
        filled=tuple([False] * 96),
        fingerprint=fingerprint,
        model_version=1,
        model_params="0000000000000000",
        baseline_definition="none",
    )


def an_outcome(day: date) -> DayOutcome:
    """Return a finalised, fully valid outcome for ``day``."""
    return DayOutcome(
        target_day=day,
        finalized_at=datetime(day.year, day.month, day.day, 23, 59, tzinfo=UTC)
        + timedelta(minutes=6),
        tz_key="Europe/Amsterdam",
        interval_count=96,
        actual=tuple([0.125] * 96),
        status="0" * 96,
        flexible_total_kwh=None,
    )


async def plant(
    store: ForecastHistoryStore, day: date, *, finalize: bool = True
) -> None:
    """Write real evidence for ``day`` straight into the store.

    Used where a test needs history spanning years, which no amount of driving
    the coordinator can produce in reasonable time. Everything goes through the
    same public methods a refresh uses, so nothing here can pass while the real
    write path is broken.
    """
    await store.async_ensure_days([day])
    store.add_snapshot(a_snapshot(day, fingerprint=f"{day.toordinal():016x}"))
    if finalize:
        store.set_outcome(
            an_outcome(day),
            {"n": 96, "c": 96, "ps": 12.0, "as": 12.0, "ae": 0.5, "fg": []},
        )


async def test_raw_evidence_expires_at_the_retention_horizon(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Aligned with the learning history it would otherwise be unjoinable to.

    Past this horizon the inputs that produced the forecast are gone, so the raw
    arrays could no longer answer *why* a forecast was wrong -- only that it
    was, which is what the summary row already says at a fraction of the size.
    """
    coordinator = setup_integration.runtime_data
    store = coordinator.history
    seed(coordinator, {})

    old = NORMAL - timedelta(days=FORECAST_RAW_RETENTION_DAYS + 5)
    recent = NORMAL - timedelta(days=3)
    for day in (old, recent, NORMAL):
        await plant(store, day)

    assert store.snapshots(old) != []
    await store.async_prune(NORMAL)

    assert store.days[old].raw_pruned is True
    assert store.days[old].fingerprints == []
    assert store.snapshots(old) == []
    assert store.outcome(old) is None
    # The reduced summary facts outlive the arrays by design.
    assert store.days[old].summary is not None
    assert store.days[recent].raw_pruned is False
    assert store.snapshots(recent) != []


async def test_summary_rows_expire_at_their_own_much_longer_horizon(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Two hundred bytes a day is worth keeping for years; a kilobyte is not."""
    coordinator = setup_integration.runtime_data
    store = coordinator.history
    seed(coordinator, {})

    ancient = NORMAL - timedelta(days=FORECAST_SUMMARY_RETENTION_DAYS + 1)
    inside = NORMAL - timedelta(days=FORECAST_SUMMARY_RETENTION_DAYS - 1)
    for day in (ancient, inside, NORMAL):
        await plant(store, day)

    await store.async_prune(NORMAL)

    assert ancient not in store.days
    assert inside in store.days
    assert store.days[inside].summary is not None


async def test_a_future_dated_record_cannot_delete_the_retention_window(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The beta.4 clock-excursion bug, refused a second home.

    A host without a real-time clock reports a date years ahead until NTP
    corrects it. Left unclamped, that single reference would define "now" and
    take every retained day with it.
    """
    coordinator = setup_integration.runtime_data
    store = coordinator.history
    seed(coordinator, {})

    for offset in range(1, 6):
        await plant(store, NORMAL - timedelta(days=offset))
    retained = set(store.days)

    # The clock jumps five years ahead.
    await store.async_prune(NORMAL + timedelta(days=365 * 5))

    assert set(store.days) == retained
    assert all(not row.raw_pruned for row in store.days.values())


async def test_normal_progression_still_prunes_after_a_clock_excursion(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The clamp must not become a permanent freeze.

    It advances with the newest stored day, so once real days are being
    recorded again the window trims against recorded time rather than against
    whatever the clock momentarily claimed.
    """
    coordinator = setup_integration.runtime_data
    store = coordinator.history
    seed(coordinator, {})

    old = NORMAL - timedelta(days=FORECAST_RAW_RETENTION_DAYS + 5)
    await plant(store, old)
    await store.async_prune(NORMAL)
    assert store.days[old].raw_pruned is False

    # Time genuinely advances: a current day is recorded.
    await plant(store, NORMAL)
    await store.async_prune(NORMAL)

    assert store.days[old].raw_pruned is True


async def test_an_unfinalised_day_is_never_pruned(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Work in progress is not stale data.

    Dropping the prediction would leave a record that can never be answered,
    and the day would vanish from the evidence instead of resolving.
    """
    coordinator = setup_integration.runtime_data
    store = coordinator.history
    seed(coordinator, {})

    stranded = NORMAL - timedelta(days=FORECAST_RAW_RETENTION_DAYS + 5)
    await plant(store, stranded, finalize=False)
    await plant(store, NORMAL)

    await store.async_prune(NORMAL)

    assert store.days[stranded].raw_pruned is False
    assert store.snapshots(stranded) != []


async def test_an_emptied_month_partition_is_deleted(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """Pruning must reclaim files, not merely empty them."""
    coordinator = setup_integration.runtime_data
    store = coordinator.history
    seed(coordinator, {})

    ancient = NORMAL - timedelta(days=FORECAST_SUMMARY_RETENTION_DAYS + 1)
    await plant(store, ancient)
    await plant(store, NORMAL)
    await store.async_save_now()
    assert month_store_key(setup_integration, ancient) in hass_storage

    await store.async_prune(NORMAL)
    await store.async_drop_empty_months()
    await store.async_save_now()

    assert month_store_key(setup_integration, ancient) not in hass_storage
    assert month_key(ancient) not in store.months
    # The current month is untouched.
    assert month_store_key(setup_integration, NORMAL) in hass_storage


async def test_pruning_survives_a_leap_day(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The window is counted in days, so February has no special case."""
    coordinator = setup_integration.runtime_data
    store = coordinator.history
    seed(coordinator, {})

    reference = date(2029, 3, 1)
    # 2028 is a leap year, so the horizon lands on 29 February.
    boundary = reference - timedelta(days=FORECAST_RAW_RETENTION_DAYS - 1)
    assert boundary == date(2028, 3, 2)

    outside = boundary - timedelta(days=1)
    for day in (outside, boundary, reference):
        await plant(store, day)

    await store.async_prune(reference)

    assert store.days[outside].raw_pruned is True
    assert store.days[boundary].raw_pruned is False


async def test_removing_the_entry_deletes_every_partition(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """Otherwise each removed entry orphans a year of files nothing can reach."""
    from custom_components.alpha_ems_manager import async_remove_entry

    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(date(2026, 8, 31)))
    await refresh_at(coordinator, local(date(2026, 8, 31), 12, 5))
    await coordinator.history.async_save_now()
    assert len(coordinator.history.months) == 2

    await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()
    await async_remove_entry(hass, setup_integration)

    leftovers = [
        key
        for key in hass_storage
        if key.startswith(f"alpha_ems_manager.{setup_integration.entry_id}")
    ]
    assert leftovers == []


# -- daylight saving ---------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "length"),
    [(SPRING_FORWARD, 92), (NORMAL, 96), (FALL_BACK, 100)],
)
async def test_a_snapshot_is_sized_to_the_real_civil_day(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    target: date,
    length: int,
) -> None:
    """92 and 100 must round-trip as faithfully as 96."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(target))
    await refresh_at(coordinator, local(target, 12, 5))
    await coordinator.history.async_save_now()

    restarted = await reload_store(hass, setup_integration, [target])
    snapshot = restarted.snapshots(target)[0]

    assert snapshot.interval_count == length
    assert len(snapshot.predicted) == length
    assert len(snapshot.filled) == length


async def test_the_repeated_fall_back_hour_keeps_both_predictions(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Chronological identity, not a wall-clock label.

    Intervals 8-11 and 12-15 both read 02:00-02:59. Keying on the wall clock
    would silently overwrite the first pass with the second.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(FALL_BACK))
    await refresh_at(coordinator, local(FALL_BACK, 12, 5))
    await coordinator.history.async_save_now()

    restarted = await reload_store(hass, setup_integration, [FALL_BACK])
    snapshot = restarted.snapshots(FALL_BACK)[0]

    assert snapshot.interval_count == 100
    assert all(value is not None for value in snapshot.predicted[8:16])


async def test_a_fall_back_day_matches_all_hundred_intervals(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The outcome must be as long as the day, or the tail goes unscored."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(FALL_BACK))
    await refresh_at(coordinator, local(FALL_BACK, 12, 5))

    day_after = FALL_BACK + timedelta(days=1)
    reseed(
        coordinator,
        {**history_before(FALL_BACK), FALL_BACK: flat_day(FALL_BACK, 12.5)},
    )
    await refresh_at(coordinator, local(day_after, 0, 5))

    outcome = coordinator.history.outcome(FALL_BACK)
    assert outcome is not None
    assert outcome.interval_count == 100
    assert len(outcome.actual) == 100
    assert len(outcome.status) == 100
    assert len(outcome.valid_indices()) == 100


async def test_a_spring_forward_day_matches_its_ninety_two_intervals(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The skipped hour has no index, so it cannot be reported as missing."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(SPRING_FORWARD))
    await refresh_at(coordinator, local(SPRING_FORWARD, 12, 5))

    day_after = SPRING_FORWARD + timedelta(days=1)
    reseed(
        coordinator,
        {
            **history_before(SPRING_FORWARD),
            SPRING_FORWARD: flat_day(SPRING_FORWARD, 11.5),
        },
    )
    await refresh_at(coordinator, local(day_after, 0, 5))

    outcome = coordinator.history.outcome(SPRING_FORWARD)
    assert outcome is not None
    assert outcome.interval_count == 92
    assert len(outcome.valid_indices()) == 92


def test_a_naive_timestamp_is_refused() -> None:
    """An instant with no zone cannot be ordered against the rest of history."""
    assert (
        ForecastSnapshot.from_dict(
            NORMAL, {"iat": "2026-08-19T10:05:00", "n": 96, "av": False}
        )
        is None
    )
    assert (
        DayOutcome.from_dict(
            NORMAL, {"fin": "2026-08-20T00:05:00", "n": 96, "a": [], "s": ""}
        )
        is None
    )


def test_stored_instants_are_utc() -> None:
    """Local-time storage would shift under a timezone change."""
    snapshot = ForecastSnapshot(
        issued_at=datetime(2026, 8, 19, 10, 5, tzinfo=UTC),
        target_day=NORMAL,
        tz_key="Europe/Amsterdam",
        interval_count=96,
        horizon_days=0,
        available=False,
        unavailable_reason="no_history",
        predicted=(),
        filled=(),
        fingerprint="abcdefabcdefabcd",
        model_version=1,
        model_params="0000000000000000",
        baseline_definition="none",
    )

    assert snapshot.to_dict()["iat"].endswith("+00:00")


async def test_partitions_are_written_before_the_index(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The order is the only thing standing between a power cut and a lost
    prediction.

    The index is what dedup consults: once a fingerprint is recorded there, that
    forecast is never issued again. Index last means a crash between the two
    writes leaves an already-written array the index has not claimed, which the
    next refresh rewrites. Index first would leave a fingerprint claiming an
    array that was never written -- and dedup would then refuse to ever produce
    it again.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))

    written: list[str] = []

    async def record(self, data):
        written.append(self.key)

    with patch("homeassistant.helpers.storage.Store.async_save", new=record):
        await coordinator.history.async_save_now()

    assert written, "nothing was written at all"
    index = index_key(setup_integration)
    assert index in written
    assert written[-1] == index
    assert all(key != index for key in written[:-1])
