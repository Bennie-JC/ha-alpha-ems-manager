"""The evidence layer as a user and a maintainer actually meet it.

End-to-end behaviour of the two published sensors, the invariants that must hold
over any stored document, and the remaining lifecycle situations a live
installation runs into: installed mid-day, a source swapped, a shutdown before
the debounce fires, a model that changes underneath old records.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.const import (
    FORECAST_MIN_INTERVALS_FOR_METRIC,
    STATUS_VALID,
)
from custom_components.alpha_ems_manager.forecast_history import (
    LIFECYCLE_PENDING,
    LIFECYCLE_UNMATCHED,
    LIFECYCLE_UNRESOLVED,
    LIFECYCLE_VALIDATED,
    lifecycle_from_summary,
)

from .conftest import set_sensor
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

pytestmark = pytest.mark.usefixtures("setup_integration")

ERROR_YESTERDAY = "sensor.alpha_ems_forecast_error_yesterday"
ERROR_WINDOW = "sensor.alpha_ems_forecast_error_7_days"

TOMORROW = NORMAL + timedelta(days=1)


async def run_days(coordinator, *, count: int, measured_kwh: float) -> date:
    """Drive ``count`` consecutive days, each finalised the following morning.

    Returns the first day after the run. Each day issues a forecast at noon and
    is matched when the next day's first refresh happens, which is exactly the
    live sequence.
    """
    seed(coordinator, history_before(NORMAL))
    measured: dict[date, object] = {}
    for offset in range(count):
        day = NORMAL + timedelta(days=offset)
        reseed(coordinator, {**history_before(NORMAL), **measured})
        await refresh_at(coordinator, local(day, 12, 5))
        measured[day] = flat_day(day, measured_kwh)

    finish = NORMAL + timedelta(days=count)
    reseed(coordinator, {**history_before(NORMAL), **measured})
    await refresh_at(coordinator, local(finish, 0, 5))
    return finish


# -- the two published sensors -----------------------------------------------


async def test_the_sensors_read_unknown_until_a_day_has_been_scored(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Zero is the value of a perfect forecast, so it must not stand in here."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))

    for entity_id in (ERROR_YESTERDAY, ERROR_WINDOW):
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == "unknown"


async def test_yesterdays_error_appears_once_the_day_is_matched(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The model predicted 12 kWh; the house used 9.6."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))
    reseed(coordinator, {**history_before(NORMAL), NORMAL: flat_day(NORMAL, 9.6)})
    await refresh_at(coordinator, local(TOMORROW, 0, 5))

    state = hass.states.get(ERROR_YESTERDAY)
    assert state is not None
    # Positive: the model over-predicted.
    assert float(state.state) == pytest.approx(2.4, abs=0.02)
    assert state.attributes["error_percent"] == pytest.approx(25.0, rel=0.02)
    assert state.attributes["intervals_compared"] == 96
    assert state.attributes["intervals_in_day"] == 96
    assert state.attributes["horizon_days"] == 0
    assert state.attributes["predicted_kwh"] == pytest.approx(12.0, abs=0.02)
    assert state.attributes["actual_kwh"] == pytest.approx(9.6, abs=0.02)


async def test_the_rolling_sensor_waits_for_a_meaningful_sample(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """One resolved day is not a week of evidence.

    Below the minimum the figure is whichever handful of intervals happened to
    resolve, and publishing it invites a fresh installation's noise to be read
    as forecast quality.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))
    reseed(coordinator, {**history_before(NORMAL), NORMAL: flat_day(NORMAL, 9.6)})
    await refresh_at(coordinator, local(TOMORROW, 0, 5))

    window = coordinator.data["forecast_error_window"]
    assert window.intervals_compared == 96
    assert window.intervals_compared < FORECAST_MIN_INTERVALS_FOR_METRIC
    assert window.wape_percent is None
    assert hass.states.get(ERROR_WINDOW).state == "unknown"


async def test_the_rolling_sensor_publishes_once_the_sample_is_large_enough(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Three consecutive days, each coming in well under the model.

    The published figure is deliberately not pinned to the naive 25 % the first
    day alone would give. Each finished day becomes a model input for the next,
    so a household that has genuinely shifted downwards drags the forecast down
    with it and the error shrinks -- which is the model working, not the metric
    drifting. What is pinned is the sample size, the direction and a band the
    figure cannot leave without something being wrong.
    """
    coordinator = setup_integration.runtime_data
    await run_days(coordinator, count=3, measured_kwh=9.6)

    state = hass.states.get(ERROR_WINDOW)
    assert state is not None
    assert 10.0 < float(state.state) < 30.0
    assert state.attributes["days_compared"] == 3
    assert state.attributes["intervals_compared"] == 288
    # Signed, so a persistent over-prediction is visible as such.
    assert state.attributes["bias_kwh_per_interval"] > 0
    assert state.attributes["mae_kwh_per_interval"] > 0
    assert state.attributes["window_days"] == 7


async def test_the_rolling_sensor_is_never_reported_as_an_accuracy(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """It is a measurement of error, not a score out of a hundred."""
    coordinator = setup_integration.runtime_data
    await run_days(coordinator, count=3, measured_kwh=9.6)

    state = hass.states.get(ERROR_WINDOW)
    assert state is not None
    assert "accuracy" not in str(state.attributes).lower()
    assert state.attributes["comparison_basis"].startswith("baseline house load")


async def test_a_day_the_model_got_right_reports_a_small_error_not_a_missing_one(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A near-zero error must be published, unlike a missing one."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))
    reseed(coordinator, {**history_before(NORMAL), NORMAL: flat_day(NORMAL, 12.0)})
    await refresh_at(coordinator, local(TOMORROW, 0, 5))

    state = hass.states.get(ERROR_YESTERDAY)
    assert state is not None
    assert state.state != "unknown"
    assert abs(float(state.state)) < 0.05


# -- Today adaptation must not touch the evidence ----------------------------


async def test_todays_adaptation_never_rewrites_the_issued_prediction(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The stored prediction is the model's, not the dashboard's hybrid.

    Same-day adaptation blends measured energy into the remainder of the day.
    If that leaked into the evidence, every day would be scored against a
    prediction that already knew the answer.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 0, 5))
    issued = coordinator.history.snapshots(NORMAL)[0]
    captured = issued.predicted

    # The day runs well under the model, so adaptation engages hard.
    reseed(coordinator, {**history_before(NORMAL), NORMAL: flat_day(NORMAL, 4.0)})
    await refresh_at(coordinator, local(NORMAL, 18, 5))

    today = coordinator.data["today"]
    assert today.adapted is True
    assert today.adaptation_ratio < 1.0
    # And the evidence is untouched.
    assert coordinator.history.snapshots(NORMAL)[0].predicted == captured
    assert coordinator.history.snapshots(NORMAL)[0].fingerprint == issued.fingerprint


async def test_the_running_day_never_enters_its_own_provenance(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A day cannot be a model input for its own forecast."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    reseed(coordinator, {**history_before(NORMAL), NORMAL: flat_day(NORMAL, 12.0)})
    await refresh_at(coordinator, local(NORMAL, 22, 5))

    context = coordinator.history.snapshots(NORMAL)[0].context["load_model"]
    # Six prior days exist and all six are learned, but 2026-08-19 is a
    # Wednesday and only four of them are weekdays. The two counts are
    # different populations -- days complete enough to learn, versus days this
    # particular forecast was built from -- and recording both is what lets a
    # later phase tell a thin model from a narrow one. Neither includes today.
    assert context["model_days"] == 4
    assert context["learned_days"] == 6
    assert context["usable_days"] == 6


# -- lifecycle situations ----------------------------------------------------


async def test_an_installation_starting_mid_day_issues_only_the_day_of_forecast(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """No day-ahead record is invented for a day that was already running."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 14, 5))

    horizons = [s.horizon_days for s in coordinator.history.snapshots(NORMAL)]
    assert horizons == [0]
    assert [s.horizon_days for s in coordinator.history.snapshots(TOMORROW)] == [1]


async def test_changing_the_house_load_source_does_not_corrupt_the_evidence(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A swapped source changes future measurements, not past records."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))
    before = coordinator.history.snapshots(NORMAL)[0]

    set_sensor(hass, "sensor.replacement_load", 1500, "W", "power")
    coordinator.config = type(coordinator.config)(
        **{
            **{
                field: getattr(coordinator.config, field)
                for field in coordinator.config.__dataclass_fields__
            },
            "house_load_entity": "sensor.replacement_load",
        }
    )
    await refresh_at(coordinator, local(NORMAL, 12, 20))

    after = coordinator.history.snapshots(NORMAL)[0]
    assert after.predicted == before.predicted
    assert after.fingerprint == before.fingerprint


async def test_a_model_version_change_leaves_old_records_readable(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Old evidence stays legible and stays labelled with the model that made it.

    This is what lets a later phase refuse to pool two model generations into
    one error series rather than reading the discontinuity as the household
    changing its habits.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))
    original = coordinator.history.snapshots(NORMAL)[0]

    with patch(
        "custom_components.alpha_ems_manager.forecast_history.FORECAST_MODEL_VERSION",
        original.model_version + 1,
    ):
        await refresh_at(coordinator, local(NORMAL, 12, 20))

    snapshots = coordinator.history.snapshots(NORMAL)
    assert len(snapshots) == 2
    assert snapshots[0].model_version == original.model_version
    assert snapshots[1].model_version == original.model_version + 1
    assert snapshots[0].predicted == snapshots[1].predicted


async def test_a_forecast_with_no_model_days_still_leaves_a_record(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A brand-new installation records that it had nothing to say."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, {})
    await refresh_at(coordinator, local(NORMAL, 12, 5))

    snapshot = coordinator.history.snapshots(NORMAL)[0]
    assert snapshot.available is False
    assert snapshot.context["load_model"]["model_days"] == 0
    assert snapshot.context["load_model"]["learned_days"] == 0


async def test_a_pooled_day_type_is_recorded_as_pooled(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Below the day-type minimum the model pools, and must say so."""
    coordinator = setup_integration.runtime_data
    # A Saturday target with only one prior weekend day available.
    saturday = date(2026, 8, 22)
    seed(coordinator, history_before(saturday, days=3))
    await refresh_at(coordinator, local(saturday, 12, 5))

    context = coordinator.history.snapshots(saturday)[0].context["load_model"]
    assert context["day_type"] == "weekend"
    assert context["day_type_pooled"] is True


@pytest.mark.parametrize("ev_configured", [False, True])
async def test_both_flexible_load_modes_record_a_consistent_definition(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    ev_configured: bool,
) -> None:
    """The definition is recorded either way, so a change is always detectable."""
    coordinator = setup_integration.runtime_data
    coordinator.config = type(coordinator.config)(
        **{
            **{
                field: getattr(coordinator.config, field)
                for field in coordinator.config.__dataclass_fields__
            },
            "ev_power_entity": "sensor.ev_charger_power" if ev_configured else None,
        }
    )
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))

    snapshot = coordinator.history.snapshots(NORMAL)[0]
    assert snapshot.ev_configured is ev_configured
    assert snapshot.baseline_definition == (
        "ev:sensor.ev_charger_power" if ev_configured else "none"
    )


# -- writes on the way out ---------------------------------------------------


async def test_a_shutdown_before_the_debounce_still_persists_the_evidence(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """Writes are debounced, so an immediate stop must flush rather than drop."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    key = f"alpha_ems_manager.{setup_integration.entry_id}.forecast_index"
    assert "2026-08-19" in hass_storage[key]["data"]["days"]


async def test_an_unload_persists_the_evidence(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """A reload must resume from what was recorded, not from the last debounce."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))

    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()

    key = f"alpha_ems_manager.{setup_integration.entry_id}.forecast_index"
    assert "2026-08-19" in hass_storage[key]["data"]["days"]


async def test_a_forecast_history_failure_never_fails_the_refresh(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Learning and both forecasts do not depend on the evidence layer.

    Taking the whole integration unavailable because a forecast-history
    document could not be written would trade the important half for the useful
    half.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))

    with patch.object(
        coordinator.recorder, "async_record", side_effect=RuntimeError("boom")
    ):
        await refresh_at(coordinator, local(NORMAL, 12, 5))

    assert coordinator.last_update_success is True
    assert coordinator.data["today_baseline"].available is True
    today = hass.states.get("sensor.alpha_ems_expected_house_load_today")
    assert today is not None
    assert today.state not in ("unavailable", "unknown")


# -- invariants --------------------------------------------------------------


async def test_a_valid_status_always_has_a_value_beside_it(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The invariant that closes every route to a fabricated zero."""
    coordinator = setup_integration.runtime_data
    await run_days(coordinator, count=4, measured_kwh=10.0)

    for day in sorted(coordinator.history.days):
        outcome = coordinator.history.outcome(day)
        if outcome is None:
            continue
        for index, code in enumerate(outcome.status):
            has_value = outcome.actual[index] is not None
            assert (code == STATUS_VALID) is has_value


async def test_every_stored_array_is_exactly_as_long_as_its_day(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A short array would silently mis-align with the interval it labels."""
    coordinator = setup_integration.runtime_data
    await run_days(coordinator, count=3, measured_kwh=10.0)

    for day in sorted(coordinator.history.days):
        for snapshot in coordinator.history.snapshots(day):
            if snapshot.available:
                assert len(snapshot.predicted) == snapshot.interval_count
                assert len(snapshot.filled) == snapshot.interval_count
        outcome = coordinator.history.outcome(day)
        if outcome is not None:
            assert len(outcome.actual) == outcome.interval_count
            assert len(outcome.status) == outcome.interval_count


async def test_snapshots_for_one_target_are_ordered_and_distinct(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Immutable evidence, in the order it was made, with no duplicates."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    for step, daily in enumerate((12.0, 14.0, 16.0)):
        reseed(coordinator, history_before(NORMAL, daily_kwh=daily))
        await refresh_at(coordinator, local(NORMAL, 8 + step, 5))

    snapshots = coordinator.history.snapshots(NORMAL)
    assert len(snapshots) == 3
    issued = [snapshot.issued_at for snapshot in snapshots]
    assert issued == sorted(issued)
    fingerprints = [snapshot.fingerprint for snapshot in snapshots]
    assert len(set(fingerprints)) == len(fingerprints)


async def test_the_lifecycle_counts_account_for_every_target_day(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Pending, validated, unmatched and unresolved must partition the set."""
    coordinator = setup_integration.runtime_data
    finish = await run_days(coordinator, count=3, measured_kwh=10.0)

    counts = {
        LIFECYCLE_PENDING: 0,
        LIFECYCLE_VALIDATED: 0,
        LIFECYCLE_UNMATCHED: 0,
        LIFECYCLE_UNRESOLVED: 0,
    }
    for day, row in coordinator.history.days.items():
        counts[
            lifecycle_from_summary(
                day,
                finish,
                finalized=row.finalized_at is not None,
                summary=row.summary,
            )
        ] += 1

    assert sum(counts.values()) == len(coordinator.history.days)
    assert counts[LIFECYCLE_VALIDATED] == 3
    assert counts[LIFECYCLE_UNRESOLVED] == 0


async def test_the_index_row_agrees_with_the_partition_it_describes(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The index is the fast path, so it must never drift from the evidence."""
    coordinator = setup_integration.runtime_data
    await run_days(coordinator, count=3, measured_kwh=10.0)

    for day, row in coordinator.history.days.items():
        if row.raw_pruned:
            continue
        snapshots = coordinator.history.snapshots(day)
        assert row.snapshot_count == len(snapshots)
        assert row.fingerprints == [s.fingerprint for s in snapshots]
        assert (row.finalized_at is not None) == (
            coordinator.history.outcome(day) is not None
        )


async def test_storage_stays_bounded_over_a_long_run(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Raw evidence must reach a steady state rather than growing forever."""
    import json

    from .test_forecast_history_persistence import plant

    coordinator = setup_integration.runtime_data
    store = coordinator.history
    seed(coordinator, {})

    start = date(2027, 1, 1)
    for offset in range(430):
        await plant(store, start + timedelta(days=offset))
    reference = start + timedelta(days=429)
    await store.async_prune(reference)

    with_raw = [row for row in store.days.values() if not row.raw_pruned]
    assert len(with_raw) <= 365
    # Every expired day keeps its reduced summary, which is what makes the
    # long-run view affordable.
    assert len(store.days) == 430
    assert all(row.summary is not None for row in store.days.values())

    index_bytes = len(json.dumps(store._index_document()))
    assert index_bytes < 250_000


def test_the_lifecycle_partition_is_total() -> None:
    """Every combination of the four deciding facts lands in exactly one state."""
    states = set()
    for finalized in (False, True):
        for flags in ([], ["shape_mismatch"]):
            for compared in (0, 96):
                for target, today in ((NORMAL, NORMAL), (NORMAL, TOMORROW)):
                    states.add(
                        lifecycle_from_summary(
                            target,
                            today,
                            finalized=finalized,
                            summary={"fg": flags, "c": compared},
                        )
                    )

    assert states == {
        LIFECYCLE_PENDING,
        LIFECYCLE_VALIDATED,
        LIFECYCLE_UNMATCHED,
        LIFECYCLE_UNRESOLVED,
    }


async def test_a_prediction_is_never_replaced_by_its_own_actual(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Two facts, kept separately, in two different structures."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))
    predicted = coordinator.history.snapshots(NORMAL)[0].predicted

    reseed(coordinator, {**history_before(NORMAL), NORMAL: flat_day(NORMAL, 5.0)})
    await refresh_at(coordinator, local(TOMORROW, 0, 5))

    outcome = coordinator.history.outcome(NORMAL)
    assert outcome is not None
    assert coordinator.history.snapshots(NORMAL)[0].predicted == predicted
    assert outcome.actual != predicted
    assert sum(v for v in outcome.actual if v is not None) == pytest.approx(
        5.0, abs=0.01
    )


async def test_reloading_the_entry_neither_duplicates_nor_loses_evidence(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A reload is a restart. It must be indistinguishable from one here."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))
    await coordinator.history.async_save_now()
    before = coordinator.history.snapshot_total

    with frozen(local(NORMAL, 12, 30)):
        await hass.config_entries.async_reload(setup_integration.entry_id)
        await hass.async_block_till_done()

    reloaded = setup_integration.runtime_data
    await reloaded.history.async_ensure_days([NORMAL, TOMORROW])
    assert reloaded.history.snapshot_total >= before
    assert reloaded.history.snapshots(NORMAL) != []


def test_a_snapshot_round_trips_without_losing_its_context() -> None:
    """Context written by a newer release must survive an older one reading it."""
    from custom_components.alpha_ems_manager.forecast_history import ForecastSnapshot

    original = ForecastSnapshot(
        issued_at=datetime(2026, 8, 19, 10, 5, tzinfo=UTC),
        target_day=NORMAL,
        tz_key="Europe/Amsterdam",
        interval_count=4,
        horizon_days=0,
        available=True,
        unavailable_reason=None,
        predicted=(0.1, 0.2, 0.3, 0.4),
        filled=(False, True, False, False),
        fingerprint="abcdefabcdefabcd",
        model_version=1,
        model_params="1111111111111111",
        baseline_definition="none",
        context={
            "load_model": {"v": 1, "model_days": 5},
            # A block this release has never heard of.
            "pv": {"v": 3, "ghi": 412},
        },
    )

    restored = ForecastSnapshot.from_dict(NORMAL, original.to_dict())

    assert restored is not None
    assert restored.predicted == original.predicted
    assert restored.filled == original.filled
    # Preserved verbatim rather than dropped: a downgrade must not be
    # destructive.
    assert restored.context["pv"] == {"v": 3, "ghi": 412}
