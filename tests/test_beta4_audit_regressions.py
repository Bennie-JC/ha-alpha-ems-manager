"""Regressions for the defects found in the beta.4 Phase-1 closure audit.

Each test here corresponds to a specific fault that existed in v1.0.0-beta.3 and
was found by reading the code rather than by observing it fail. They are grouped
by the module they belong to and every one names the mechanism it protects, so a
future change that reintroduces the fault is told what it broke and why it
mattered.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.confidence import compute_confidence
from custom_components.alpha_ems_manager.const import (
    CONF_EV_POWER_ENTITY,
    CONF_SOLCAST_ENTRY_ID,
    CONF_USE_PV_FORECAST,
    MAX_CATCHUP_SECONDS,
    MAX_HISTORY_DAYS,
    MAX_PLAUSIBLE_LOAD_W,
)
from custom_components.alpha_ems_manager.forecast import build_forecast
from custom_components.alpha_ems_manager.quarter import QuarterAccumulator
from custom_components.alpha_ems_manager.storage import (
    DayRecord,
    LearningStore,
    index_for_start_utc,
)

from .conftest import HOUSE_LOAD, TEST_TIMEZONE, TZ, set_sensor
from .synthetic import empty_day, flat_day

TZ_KEY = TEST_TIMEZONE


# -- retention: a clock excursion must not delete a year of history ----------


async def test_creating_a_future_dated_day_does_not_wipe_the_history(
    hass: HomeAssistant,
) -> None:
    """The clock-excursion guard was inert on the only path that reaches it.

    ``get_or_create`` inserted the new day *before* calling ``prune``, so the
    clamp measured itself against a set that already contained the future date
    and ``reference > newest`` could never be true. A Pi handing Home Assistant
    a date years ahead before NTP corrects it therefore deleted every retained
    day, and the debounced save wrote the empty document to disk within the
    minute. Fails on beta.3, where only one day survives.
    """
    store = LearningStore(hass, "entry-excursion")
    start = date(2026, 8, 1)
    for offset in range(30):
        store.get_or_create(start + timedelta(days=offset), TZ)
    assert len(store.days) == 30

    store.get_or_create(date(2030, 1, 1), TZ)

    assert len(store.days) == 31
    assert start in store.days


async def test_normal_day_to_day_progression_still_retires_the_oldest_day(
    hass: HomeAssistant,
) -> None:
    """The guard must not disable retention for ordinary advancement."""
    store = LearningStore(hass, "entry-retention")
    today = date(2026, 8, 19)
    oldest = today - timedelta(days=MAX_HISTORY_DAYS - 1)
    for offset in range(MAX_HISTORY_DAYS):
        store.get_or_create(today - timedelta(days=offset), TZ)
    assert oldest in store.days

    store.get_or_create(today + timedelta(days=1), TZ)

    assert oldest not in store.days
    assert len(store.days) == MAX_HISTORY_DAYS


async def test_a_genuine_multi_day_gap_prunes_relative_to_real_recorded_time(
    hass: HomeAssistant,
) -> None:
    """Coming back after a long outage trims, but never to nothing."""
    store = LearningStore(hass, "entry-gap")
    start = date(2026, 1, 1)
    for offset in range(10):
        store.get_or_create(start + timedelta(days=offset), TZ)

    store.get_or_create(start + timedelta(days=400), TZ)

    assert len(store.days) >= 10


# -- persistence: a failed read must not become a destroyed file -------------


async def test_a_store_that_failed_to_load_refuses_to_write(
    hass: HomeAssistant,
) -> None:
    """One transient read error must not turn into permanent data loss.

    ``async_load`` degrades an unreadable document to an empty history so setup
    can continue, which is right for availability -- but that empty history was
    then written straight back over the file on the next unload or shutdown.
    Fails on beta.3, where both writes go through.
    """
    store = LearningStore(hass, "entry-unreadable")
    with patch(
        "homeassistant.helpers.storage.Store.async_load",
        side_effect=OSError("device busy"),
    ):
        await store.async_load(TZ_KEY)
    assert store.corrupt

    with (
        patch("homeassistant.helpers.storage.Store.async_save") as save,
        patch("homeassistant.helpers.storage.Store.async_delay_save") as delay_save,
    ):
        store.schedule_save()
        await store.async_save_now()

    save.assert_not_called()
    delay_save.assert_not_called()


async def test_removing_an_entry_still_deletes_an_unreadable_document(
    hass: HomeAssistant,
) -> None:
    """Refusing to write must not mean refusing to forget."""
    store = LearningStore(hass, "entry-unreadable-removed")
    with patch(
        "homeassistant.helpers.storage.Store.async_load",
        side_effect=OSError("device busy"),
    ):
        await store.async_load(TZ_KEY)

    with patch("homeassistant.helpers.storage.Store.async_remove") as remove:
        await store.async_remove()

    remove.assert_called_once()


async def test_a_healthy_store_still_writes(hass: HomeAssistant) -> None:
    """The guard must be reachable only by the failure it was written for."""
    store = LearningStore(hass, "entry-healthy")
    await store.async_load(TZ_KEY)
    assert not store.corrupt

    with patch("homeassistant.helpers.storage.Store.async_save") as save:
        await store.async_save_now()

    save.assert_called_once()


async def test_a_discarded_legacy_document_is_visible_in_diagnostics(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """ "The migration threw my history away" and "fresh install" looked alike.

    ``reset_by_migration`` was assigned in ``__init__`` and never read or set
    anywhere, so the v1 discard left nothing behind but a log line that has
    usually rotated away by the time anyone asks.
    """
    from custom_components.alpha_ems_manager.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    store = LearningStore(hass, "entry-legacy")

    async def load_a_v1_document() -> dict[str, object]:
        """Stand in for Store.async_load hitting a schema-v1 file on disk."""
        return await store._store._async_migrate_func(1, 0, {"slots": [1, 2, 3]})

    with patch(
        "homeassistant.helpers.storage.Store.async_load",
        side_effect=load_a_v1_document,
    ):
        await store.async_load(TZ_KEY)

    assert store.reset_by_migration is True
    assert store.days == {}

    # A subsequent clean load must clear it rather than latching forever.
    await store.async_load(TZ_KEY)
    assert store.reset_by_migration is False

    setup_integration.runtime_data.store.reset_by_migration = True
    payload = await async_get_config_entry_diagnostics(hass, setup_integration)
    assert payload["storage"]["reset_by_schema_migration"] is True
    assert payload["storage"]["writes_suspended"] is False


# -- timezone changes --------------------------------------------------------


def test_an_interval_is_indexed_in_the_zone_its_day_was_recorded_in() -> None:
    """Indexing under the live zone shifted writes by whole hours.

    An existing ``DayRecord`` keeps the ``tz_key`` and length it was written
    with. Filing its intervals under a zone the user has since switched to wrote
    each afternoon quarter over a morning one, while the day still looked
    complete and still counted as learned.
    """
    from zoneinfo import ZoneInfo

    day = date(2026, 8, 17)
    record = empty_day(day)
    start = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)

    own_zone = index_for_start_utc(day, start, record.tz)
    other_zone = index_for_start_utc(day, start, ZoneInfo("America/New_York"))

    assert own_zone == 40
    assert other_zone != own_zone


async def test_a_timezone_change_reloads_the_entry(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Home Assistant does not reload config entries on a timezone change.

    Without this the accumulators kept labelling buckets in the old zone while
    the storage layer created records in the new one -- two halves of one write
    path running on different calendars until the next restart.
    """
    coordinator = setup_integration.runtime_data

    with patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload:
        await hass.config.async_update(time_zone="America/New_York")
        await hass.async_block_till_done()

    schedule_reload.assert_called_once_with(setup_integration.entry_id)
    assert coordinator._tz_key == TEST_TIMEZONE


async def test_an_unrelated_core_config_change_does_not_reload(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Only the timezone matters here; anything else must be ignored."""
    with patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload:
        await hass.config.async_update(currency="USD")
        await hass.async_block_till_done()

    schedule_reload.assert_not_called()


# -- accumulator: a clock step must not blow up the event loop ---------------


def test_a_large_forward_clock_step_does_not_walk_every_quarter() -> None:
    """A host stepped from 1970 asked for two million rejected buckets.

    Every quarter inside a gap this long already fails the tolerated-gap test,
    so walking it one bucket at a time could only manufacture rejections --
    about twelve seconds of blocked event loop and several hundred megabytes of
    results that were all going to be discarded.
    """
    accumulator = QuarterAccumulator(TZ)
    accumulator.add_sample(datetime(1970, 1, 1, tzinfo=UTC), 2000.0)

    results = accumulator.add_sample(datetime(2026, 8, 17, 12, 0, tzinfo=UTC), 2000.0)

    assert results == []
    assert accumulator.open_coverage == 0.0


def test_a_gap_just_inside_the_bound_is_still_walked_normally() -> None:
    """The bound must not swallow an ordinary overnight outage."""
    accumulator = QuarterAccumulator(TZ)
    start = datetime(2026, 8, 17, 0, 0, tzinfo=UTC)
    accumulator.add_sample(start, 2000.0)

    results = accumulator.add_sample(
        start + timedelta(seconds=MAX_CATCHUP_SECONDS - 60), 2000.0
    )

    assert len(results) > 90
    # None of them may be accepted: the gap was far past the sample tolerance.
    assert not any(result.accepted for result in results)


# -- confidence: a stuck-at-zero source must not raise the score -------------


def test_a_day_of_exact_zeroes_is_visible_to_the_stability_component() -> None:
    """A house-load sensor stuck at 0 W raised confidence while degrading it.

    Such a day is perfectly covered and perfectly valid, so it counts as
    learned and lifts both maturity and coverage -- while dragging every slot
    mean toward zero. ``_stability`` filtered totals to ``> 0``, so the one
    component whose job is to notice that the daily totals disagree was the only
    one that could not see it. Fails on beta.3, where stability reads 1.0.
    """
    reference = date(2026, 8, 19)
    healthy = [flat_day(reference - timedelta(days=n), 12.0) for n in range(1, 6)]
    # The same five days, but the oldest source was stuck at zero all day. Day
    # count is held equal so maturity cannot mask the effect being measured.
    stuck = [*healthy[:-1], flat_day(reference - timedelta(days=5), 0.0)]

    with_zero = compute_confidence(stuck, reference)
    all_healthy = compute_confidence(healthy, reference)

    assert with_zero.learned_days == all_healthy.learned_days
    assert with_zero.stability is not None
    assert with_zero.stability < all_healthy.stability
    assert with_zero.percent < all_healthy.percent


def test_stability_is_still_withheld_when_every_day_is_zero() -> None:
    """A mean of zero has no coefficient of variation to report."""
    reference = date(2026, 8, 19)
    days = [flat_day(reference - timedelta(days=n), 0.0) for n in range(1, 4)]

    assert compute_confidence(days, reference).stability is None


# -- forecast reporting ------------------------------------------------------


def test_modelled_intervals_no_longer_claims_a_filled_interval_was_blended() -> None:
    """The field was overwritten with the day length on every published day.

    Its own contract is "intervals a look-back window could actually be blended
    for", so setting it to ``interval_count`` made it a constant and hid exactly
    what it was added to show. Fails on beta.3, which reports 96 of 96.
    """
    reference = date(2026, 8, 19)
    records = []
    for offset in range(1, 6):
        day = reference - timedelta(days=offset)
        record = empty_day(day)
        # Intervals 0-7 are never valid -- the overnight hours a flexible-load
        # sensor drops out for -- so they can only ever be filled.
        for index in range(8, record.interval_count):
            record.record_interval(index, 0.12, None, False)
        records.append(record)

    forecast = build_forecast(records, reference, reference + timedelta(days=1), TZ)

    assert forecast.available is True
    assert forecast.modelled_intervals == 88
    assert forecast.filled_intervals == 8
    assert forecast.modelled_intervals + forecast.filled_intervals == 96


def test_a_fully_observed_day_reports_no_filled_intervals() -> None:
    """The honest count must still read 96 when 96 really were blended."""
    reference = date(2026, 8, 19)
    records = [flat_day(reference - timedelta(days=n), 12.0) for n in range(1, 6)]

    forecast = build_forecast(records, reference, reference + timedelta(days=1), TZ)

    assert forecast.modelled_intervals == 96
    assert forecast.filled_intervals == 0


# -- entity/diagnostics parity ----------------------------------------------


async def test_a_withheld_tomorrow_forecast_publishes_no_model_metadata(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Today's attributes were gated on availability; tomorrow's were not.

    A template reading the attributes saw all five look-back windows and a
    day-type decision behind a sensor reading ``unknown``.
    """
    coordinator = setup_integration.runtime_data
    coordinator.store.days = {}
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    state = hass.states.get("sensor.alpha_ems_expected_house_load_tomorrow")
    assert state is not None
    assert state.state in ("unknown", "unavailable")
    assert state.attributes.get("forecast_total_kwh") is None
    assert state.attributes.get("windows_used_days") == []
    assert state.attributes.get("day_type_pooled") is None


# -- energy balance: the two paths must agree on a usable reading ------------


async def test_an_implausible_house_load_does_not_widen_the_balance_allowance(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The allowance grew with ``ac_power``, so a grossly wrong entity relaxed
    the very check meant to catch it. The learning path already rejected the
    same reading; the balance path accepted it.
    """
    coordinator = setup_integration.runtime_data
    set_sensor(hass, HOUSE_LOAD, MAX_PLAUSIBLE_LOAD_W + 1000, "W", "power")
    await hass.async_block_till_done()

    assert coordinator.read_flows().house_load_w is None
    coordinator._sample_balance()
    assert coordinator.balance.unavailable_samples >= 1


# -- config flow -------------------------------------------------------------


async def test_a_stale_solcast_selection_does_not_block_every_submission(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    source_entities: None,
    solcast_config_entry: MockConfigEntry,
) -> None:
    """A SelectSelector validates against its option list.

    With Solcast removed after being configured, the stored id no longer
    appears among the options, so the form rendered normally and then rejected
    *every* submission at schema validation -- before ``_validate`` could turn
    it into a field error. The user could not change any unrelated setting.
    Fails on beta.3 with ``vol.Invalid``.
    """
    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={
            **mock_config_entry.data,
            CONF_USE_PV_FORECAST: True,
            CONF_SOLCAST_ENTRY_ID: solcast_config_entry.entry_id,
        },
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    await hass.config_entries.async_remove(solcast_config_entry.entry_id)
    await hass.async_block_till_done()

    # The form must still render, and its Solcast default must not be the id
    # that is no longer selectable.
    from .test_config_flow import open_options

    result = await open_options(hass, mock_config_entry.entry_id)
    assert result["type"].value == "form"

    schema_keys = {str(key): key for key in result["data_schema"].schema}
    solcast_key = schema_keys[CONF_SOLCAST_ENTRY_ID]
    suggested = (solcast_key.description or {}).get("suggested_value")
    assert suggested != solcast_config_entry.entry_id


async def test_the_flexible_load_may_not_be_the_house_load_entity(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    frank_config_entry: MockConfigEntry,
) -> None:
    """One entity in both roles makes the baseline exactly zero, forever.

    ``baseline = max(measured - flexible, 0)``, so the intervals stay valid, the
    days stay complete, they count as learned, and the forecast is a confident
    0 kWh. Nothing downstream can tell that apart from a house that used no
    energy, so it has to be refused at selection time.
    """
    from .test_config_flow import open_options, options_payload

    result = await open_options(hass, setup_integration.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        options_payload(
            frank_config_entry.entry_id, **{CONF_EV_POWER_ENTITY: HOUSE_LOAD}
        ),
    )

    assert result["errors"] == {CONF_EV_POWER_ENTITY: "ev_entity_same_as_house_load"}


# -- determinism -------------------------------------------------------------


def test_the_forecast_is_a_pure_function_of_history_and_date() -> None:
    """A restart must reproduce the same numbers from the same storage.

    Nothing may depend on the order records happen to arrive in, or on set or
    dict iteration order, or the forecast published at 00:05 after a restart
    would differ from the one published at 00:05 without one.
    """
    reference = date(2026, 8, 19)
    records = [flat_day(reference - timedelta(days=n), 10.0 + n) for n in range(1, 8)]

    forward = build_forecast(records, reference, reference, TZ)
    backward = build_forecast(list(reversed(records)), reference, reference, TZ)

    assert forward.intervals == backward.intervals
    assert forward.total_kwh == backward.total_kwh
    assert forward.windows_used == backward.windows_used
    assert forward.source_days == backward.source_days


def test_sharing_prepared_inputs_changes_nothing_about_the_result() -> None:
    """The performance fix must be observationally identical."""
    from custom_components.alpha_ems_manager.forecast import collect_forecast_inputs

    reference = date(2026, 8, 19)
    records = [flat_day(reference - timedelta(days=n), 10.0 + n) for n in range(1, 8)]
    inputs = collect_forecast_inputs(records, reference)

    for target in (reference, reference + timedelta(days=1)):
        alone = build_forecast(records, reference, target, TZ)
        shared = build_forecast(records, reference, target, TZ, inputs)
        assert alone.intervals == shared.intervals
        assert alone.source_days == shared.source_days
        assert alone.usable_days == shared.usable_days
        assert alone.day_type_pooled == shared.day_type_pooled


def test_a_forecast_for_today_never_reads_todays_own_record() -> None:
    """Same-day contamination would make the model self-confirming."""
    reference = date(2026, 8, 19)
    history = [flat_day(reference - timedelta(days=n), 10.0) for n in range(1, 6)]
    today = flat_day(reference, 99.0)

    without = build_forecast(history, reference, reference, TZ)
    with_today = build_forecast([*history, today], reference, reference, TZ)

    assert with_today.total_kwh == pytest.approx(without.total_kwh)
    assert with_today.usable_days == without.usable_days


def test_a_forecast_for_tomorrow_never_reads_tomorrows_own_record() -> None:
    """A future-dated record from a clock excursion must not leak in either."""
    reference = date(2026, 8, 19)
    tomorrow = reference + timedelta(days=1)
    history = [flat_day(reference - timedelta(days=n), 10.0) for n in range(1, 6)]
    future = flat_day(tomorrow, 99.0)

    without = build_forecast(history, reference, tomorrow, TZ)
    with_future = build_forecast([*history, future], reference, tomorrow, TZ)

    assert with_future.total_kwh == pytest.approx(without.total_kwh)


def test_a_record_carrying_an_unresolvable_timezone_is_still_forecastable() -> None:
    """A hand-edited zone key must not take every sensor down with it."""
    reference = date(2026, 8, 19)
    records = [flat_day(reference - timedelta(days=n), 10.0) for n in range(1, 6)]
    broken = DayRecord.from_dict(
        reference - timedelta(days=6),
        {"tz": "Mars/Olympus_Mons", "n": 96, "m": [0.1] * 96},
        TZ_KEY,
    )
    assert broken is not None

    forecast = build_forecast([*records, broken], reference, reference, TZ)

    assert forecast.available is True
