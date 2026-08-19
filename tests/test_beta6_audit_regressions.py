"""Regression cover for every defect the v1.0.0-beta.6 audit found.

Each test in this file fails against v1.0.0-beta.5. They are grouped by the
defect rather than by the module, because the point of the file is to be read
next to the changelog.

The four defects
----------------

1. **A data gap read as a changed baseline definition.** ``_definition_changed``
   judged the day from every entry of ``DayRecord.ev_expected``, but that list
   is only written for *accepted* quarters -- so on an installation with a
   flexible load, one quarter lost to a restart made ``any(expected)`` true and
   ``all(expected)`` false, and the whole day was excluded from every statistic,
   permanently. This is what left 19 August 2026 unscored on the maintainer's
   live system after the beta.5 upgrade restart.

2. **A forward clock excursion deleted raw evidence.** ``async_prune`` clamps
   its reference to one day past the newest recorded target, but the recorder
   issued snapshots *before* pruning, so the bogus future target was already
   inside the set the clamp measures against. Exactly the beta.4
   ``get_or_create`` ordering bug, one store along. The store-level test that
   was supposed to cover this called ``async_prune`` directly and so never
   exercised the real path.

3. **A corrected matching rule could not reach the days already matched.**
   Finalisation only ever looks at days that were never matched, so defect 1
   would have scarred 19 August for as long as the record survived.

4. **The rolling sensor published energy it had not measured.** Below the
   minimum-sample threshold the window dropped its two energy totals to the
   ``0.0`` dataclass defaults, so the sensor advertised ``predicted_kwh: 0.0``
   and ``actual_kwh: 0.0`` beside an ``intervals_compared`` of ninety-six.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.const import (
    FLAG_DEFINITION_CHANGED,
    FLAG_NO_RECORD,
    FORECAST_MATCHER_VERSION,
    FORECAST_RAW_RETENTION_DAYS,
    STATUS_MEASURED_MISSING,
    STATUS_VALID,
)
from custom_components.alpha_ems_manager.forecast_history import (
    LIFECYCLE_UNMATCHED,
    LIFECYCLE_VALIDATED,
    DayOutcome,
    ForecastSnapshot,
    lifecycle_from_summary,
)
from custom_components.alpha_ems_manager.metrics import matcher_version
from custom_components.alpha_ems_manager.storage import DayRecord

from .conftest import EV_POWER, set_sensor
from .forecast_helpers import (
    NORMAL,
    history_before,
    local,
    refresh_at,
    reseed,
    seed,
)
from .synthetic import empty_day, flat_day

pytestmark = pytest.mark.usefixtures("setup_integration")

DAY_ONE = NORMAL
DAY_TWO = NORMAL + timedelta(days=1)

ERROR_YESTERDAY = "sensor.alpha_ems_forecast_error_yesterday"
ERROR_WINDOW = "sensor.alpha_ems_forecast_error_7_days"


def with_flexible_load(coordinator, hass: HomeAssistant) -> None:
    """Configure a flexible-load source on a live coordinator."""
    coordinator.config = coordinator.config.__class__(
        **{
            **{
                name: getattr(coordinator.config, name)
                for name in coordinator.config.__dataclass_fields__
            },
            "ev_power_entity": EV_POWER,
        }
    )
    set_sensor(hass, EV_POWER, 0, "W", "power")


def day_with_gap(day: date, *, missing: set[int], ev: bool) -> DayRecord:
    """Return a day recorded exactly as the coordinator would record it.

    A quarter that never reached coverage is *never handed to*
    ``record_interval``, so it keeps every padded default -- including
    ``ev_expected=False``. Filing it with a flexible-load expectation, as the
    test helpers used to, describes a state no live installation can reach.
    """
    record = empty_day(day)
    for index in range(record.interval_count):
        if index in missing:
            continue
        record.record_interval(
            index,
            measured_kwh=0.125,
            ev_kwh=0.0 if ev else None,
            ev_expected=ev,
        )
    return record


# -- defect 1: a data gap is not a changed definition ------------------------


async def test_a_restart_gap_is_not_a_changed_baseline_definition(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The live 19 August 2026 case, reproduced exactly.

    A charger is configured and reading all day. Home Assistant restarts for the
    beta.5 upgrade at noon, so the two quarters spanning the restart never reach
    coverage and are never filed. Every other quarter has the same flexible-load
    expectation as every other, and the definition of "baseline" did not move an
    inch -- so the day must be scored, on the ninety-four intervals that were
    actually observed.
    """
    coordinator = setup_integration.runtime_data
    with_flexible_load(coordinator, hass)

    base = history_before(DAY_ONE)
    seed(coordinator, base)
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))

    day = day_with_gap(DAY_ONE, missing={48, 49}, ev=True)
    assert any(day.ev_expected) and not all(day.ev_expected)
    reseed(coordinator, {**base, DAY_ONE: day})
    await refresh_at(coordinator, local(DAY_TWO, 0, 5))

    outcome = coordinator.history.outcome(DAY_ONE)
    assert outcome is not None
    assert outcome.flags == ()
    assert outcome.comparable is True
    assert outcome.status[48:50] == STATUS_MEASURED_MISSING * 2
    assert outcome.status[:48] == STATUS_VALID * 48

    facts = coordinator.last_record.yesterday
    assert facts is not None
    assert facts["intervals_compared"] == 94
    assert facts["intervals_in_day"] == 96
    # 94 predictions of 0.125 against 94 measurements of 0.125.
    assert facts["predicted_kwh"] == 11.75
    assert facts["actual_kwh"] == 11.75
    assert facts["signed_error_kwh"] == 0.0
    assert state_of(hass, ERROR_YESTERDAY).state == "0.0"

    row = coordinator.history.days[DAY_ONE]
    assert (
        lifecycle_from_summary(
            DAY_ONE,
            DAY_TWO,
            finalized=row.finalized_at is not None,
            summary=row.summary,
        )
        == LIFECYCLE_VALIDATED
    )


def state_of(hass: HomeAssistant, entity_id: str):
    """Return one sensor's state object, asserting it exists."""
    state = hass.states.get(entity_id)
    assert state is not None
    return state


async def test_a_day_with_nothing_observed_claims_no_definition_change(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """No observation has an opinion about what "baseline" meant.

    The day is unmatched either way -- it has no comparable interval -- so
    asserting a definition change on top of that adds a reason that was never
    established, and it is the reason a maintainer would go and investigate.
    """
    coordinator = setup_integration.runtime_data
    with_flexible_load(coordinator, hass)

    base = history_before(DAY_ONE)
    seed(coordinator, base)
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))
    reseed(coordinator, {**base, DAY_ONE: empty_day(DAY_ONE)})
    await refresh_at(coordinator, local(DAY_TWO, 0, 5))

    outcome = coordinator.history.outcome(DAY_ONE)
    assert outcome is not None
    assert FLAG_DEFINITION_CHANGED not in outcome.flags
    assert outcome.flags == ()
    # Still unmatched, for the honest reason: nothing to compare.
    row = coordinator.history.days[DAY_ONE]
    assert (
        lifecycle_from_summary(
            DAY_ONE,
            DAY_TWO,
            finalized=row.finalized_at is not None,
            summary=row.summary,
        )
        == LIFECYCLE_UNMATCHED
    )
    assert coordinator.last_record.yesterday is None


async def test_a_flexible_load_arriving_mid_day_is_still_excluded(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The legitimate half of the matrix, and it must survive the fix.

    The charger is selected at noon, so the morning's baseline is measured load
    and the afternoon's is measured load minus charging. Those are two different
    quantities and no single prediction can be scored against both -- even
    though a gap elsewhere in the same day changes nothing about that.
    """
    coordinator = setup_integration.runtime_data
    with_flexible_load(coordinator, hass)

    day = empty_day(DAY_ONE)
    for index in range(day.interval_count):
        if index in {70, 71}:
            continue
        day.record_interval(
            index,
            measured_kwh=0.125,
            ev_kwh=0.0 if index >= 48 else None,
            ev_expected=index >= 48,
        )

    base = history_before(DAY_ONE)
    seed(coordinator, base)
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))
    reseed(coordinator, {**base, DAY_ONE: day})
    await refresh_at(coordinator, local(DAY_TWO, 0, 5))

    outcome = coordinator.history.outcome(DAY_ONE)
    assert outcome is not None
    assert outcome.flags == (FLAG_DEFINITION_CHANGED,)
    assert outcome.comparable is False
    # The record is kept -- both halves are true facts -- and never scored.
    assert coordinator.last_record.yesterday is None
    assert coordinator.last_record.window.days_compared == 0
    assert state_of(hass, ERROR_YESTERDAY).state == "unknown"


async def test_a_charger_removed_before_the_day_still_excludes_it(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A prediction of one quantity is never scored against another.

    The forecast for the day was issued while a charger was configured, and the
    day itself was measured without one. Both are consistent within themselves,
    so nothing in the day's own record is partial -- the incompatibility is
    between the prediction and the measurement, and it is caught by comparing
    the two definitions rather than by looking for a mid-day split.
    """
    coordinator = setup_integration.runtime_data
    with_flexible_load(coordinator, hass)

    base = history_before(DAY_ONE)
    seed(coordinator, base)
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))
    assert coordinator.history.snapshots(DAY_ONE)[0].ev_configured is True

    # The charger is removed from the configuration, and the whole day is
    # recorded with no flexible-load expectation at all.
    coordinator.config = coordinator.config.__class__(
        **{
            **{
                name: getattr(coordinator.config, name)
                for name in coordinator.config.__dataclass_fields__
            },
            "ev_power_entity": None,
        }
    )
    reseed(coordinator, {**base, DAY_ONE: flat_day(DAY_ONE, 12.0)})
    await refresh_at(coordinator, local(DAY_TWO, 0, 5))

    outcome = coordinator.history.outcome(DAY_ONE)
    assert outcome is not None
    assert outcome.flags == (FLAG_DEFINITION_CHANGED,)
    assert coordinator.last_record.yesterday is None


async def test_an_options_reload_that_preserves_the_definition_still_validates(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Reloading the entry is not a change of meaning.

    The entry is reloaded mid-day -- which resets both accumulators and so
    normally costs a quarter -- with the flexible-load configuration untouched.
    The day must still be scored.
    """
    coordinator = setup_integration.runtime_data
    with_flexible_load(coordinator, hass)

    base = history_before(DAY_ONE)
    seed(coordinator, base)
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))

    day = day_with_gap(DAY_ONE, missing={49}, ev=True)
    reseed(coordinator, {**base, DAY_ONE: day})
    await refresh_at(coordinator, local(DAY_TWO, 0, 5))

    outcome = coordinator.history.outcome(DAY_ONE)
    assert outcome is not None
    assert outcome.flags == ()
    assert coordinator.last_record.yesterday["intervals_compared"] == 95


def test_the_definition_is_judged_from_observations_only() -> None:
    """The rule itself, isolated from the pipeline that carries it."""
    from custom_components.alpha_ems_manager.forecast_history import build_outcome

    def outcome_for(record: DayRecord) -> DayOutcome:
        return build_outcome(
            DAY_ONE,
            record,
            [],
            finalized_at=datetime(2026, 8, 20, 0, 5, tzinfo=UTC),
            fallback_tz_key="Europe/Amsterdam",
            fallback_interval_count=96,
        )

    # A charger all day, with two quarters never filed: no change.
    gap = day_with_gap(DAY_ONE, missing={10, 11}, ev=True)
    assert outcome_for(gap).flags == ()

    # A charger arriving mid-day among observed intervals: a change.
    split = empty_day(DAY_ONE)
    for index in range(split.interval_count):
        split.record_interval(
            index, measured_kwh=0.1, ev_kwh=0.0, ev_expected=index >= 48
        )
    assert outcome_for(split).flags == (FLAG_DEFINITION_CHANGED,)

    # No charger at all: no change.
    assert outcome_for(flat_day(DAY_ONE, 12.0)).flags == ()

    # Nothing observed: no claim either way.
    assert outcome_for(empty_day(DAY_ONE)).flags == ()


# -- defect 2: a forward clock excursion must not delete raw evidence --------


async def test_a_forward_clock_excursion_keeps_the_raw_forecast_evidence(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A host without a real-time clock reports a date years ahead.

    The clamp in ``async_prune`` exists for exactly this, and it was inert:
    issuance ran first and put the bogus target inside the set the clamp
    measures against. One refresh under a five-year excursion dropped every
    retained prediction array in the history.
    """
    coordinator = setup_integration.runtime_data
    base = history_before(DAY_ONE)
    seed(coordinator, base)
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))

    real_targets = sorted(coordinator.history.days)
    assert real_targets == [DAY_ONE, DAY_TWO]
    assert coordinator.history.snapshots(DAY_ONE)

    # NTP has not corrected the clock yet.
    excursion = DAY_ONE + timedelta(days=365 * 5)
    await refresh_at(coordinator, local(excursion, 3, 5))

    for day in real_targets:
        row = coordinator.history.days[day]
        assert row.raw_pruned is False, day
        assert row.fingerprints != [], day
    await coordinator.history.async_ensure_days(real_targets)
    assert coordinator.history.snapshots(DAY_ONE)


async def test_the_clamp_does_not_freeze_pruning_for_ever(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """It advances with recorded time, so real ageing still expires evidence."""
    coordinator = setup_integration.runtime_data
    store = coordinator.history
    seed(coordinator, {})

    old = DAY_ONE - timedelta(days=FORECAST_RAW_RETENTION_DAYS + 5)
    await store.async_ensure_days([old])
    store.add_snapshot(
        ForecastSnapshot(
            issued_at=datetime(old.year, old.month, old.day, 10, tzinfo=UTC),
            target_day=old,
            tz_key="Europe/Amsterdam",
            interval_count=96,
            horizon_days=0,
            available=True,
            unavailable_reason=None,
            predicted=(0.125,) * 96,
            filled=(False,) * 96,
            fingerprint="a" * 16,
            model_version=1,
            model_params="0" * 16,
            baseline_definition="none",
        )
    )
    store.set_outcome(
        DayOutcome(
            target_day=old,
            finalized_at=datetime(old.year, old.month, old.day, 23, tzinfo=UTC),
            tz_key="Europe/Amsterdam",
            interval_count=96,
            actual=(0.125,) * 96,
            status=STATUS_VALID * 96,
            flexible_total_kwh=None,
        ),
        {"n": 96, "c": 96, "ps": 12.0, "as": 12.0, "ae": 0.0, "fg": [], "mr": 2},
    )

    seed_days = history_before(DAY_ONE)
    reseed(coordinator, seed_days)
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))

    # Pruning runs before issuance, so this first pass still measures the clamp
    # against the ancient day alone and correctly declines to expire it. That is
    # the same one-day lag the learning store has, and for the same reason: a
    # reference far ahead of the recorded history is indistinguishable from a
    # wrong clock until real days start arriving.
    assert store.days[old].raw_pruned is False

    reseed(coordinator, seed_days)
    await refresh_at(coordinator, local(DAY_TWO, 0, 5))

    # Now the newest recorded target is current, the clamp has nothing to do,
    # and the ancient day expires against real recorded time.
    assert store.days[old].raw_pruned is True
    assert store.days[old].fingerprints == []
    assert store.days[old].summary is not None


# -- defect 3: a corrected rule reaches the days already matched -------------


async def beta5_style_exclusion(hass: HomeAssistant, coordinator) -> DayRecord:
    """Drive one day and rewrite its match as v1.0.0-beta.5 would have left it."""
    with_flexible_load(coordinator, hass)
    base = history_before(DAY_ONE)
    seed(coordinator, base)
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))

    day = day_with_gap(DAY_ONE, missing={48, 49}, ev=True)
    reseed(coordinator, {**base, DAY_ONE: day})
    await refresh_at(coordinator, local(DAY_TWO, 0, 5))

    # Put the day back into the state beta.5 wrote: excluded, and stamped with
    # the older matching generation.
    outcome = coordinator.history.outcome(DAY_ONE)
    assert outcome is not None
    partition = coordinator.history._partitions["2026-08"]
    partition.outcomes[DAY_ONE] = DayOutcome(
        target_day=outcome.target_day,
        finalized_at=outcome.finalized_at,
        tz_key=outcome.tz_key,
        interval_count=outcome.interval_count,
        actual=outcome.actual,
        status=outcome.status,
        flexible_total_kwh=outcome.flexible_total_kwh,
        flags=(FLAG_DEFINITION_CHANGED,),
    )
    coordinator.history.days[DAY_ONE].summary = {
        "n": 96,
        "c": 0,
        "fg": [FLAG_DEFINITION_CHANGED],
    }
    return day


async def test_a_match_written_under_older_rules_is_re_derived(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The upgrade repairs 19 August rather than only sparing 20 August.

    Matching is a pure recomputation from a retained snapshot and a retained
    learning record, so re-deriving it is idempotent and loses nothing. Leaving
    it alone would keep a verdict this release knows to be wrong, for ever, in
    the one dataset the project cannot regenerate.
    """
    coordinator = setup_integration.runtime_data
    day = await beta5_style_exclusion(hass, coordinator)
    assert matcher_version(coordinator.history.days[DAY_ONE].summary) == 1

    before = coordinator.history.snapshots(DAY_ONE)[0]
    reseed(coordinator, {**history_before(DAY_ONE), DAY_ONE: day})
    await refresh_at(coordinator, local(DAY_TWO, 0, 20))

    assert coordinator.last_record.restated == (DAY_ONE,)
    assert coordinator.last_record.finalized == ()
    outcome = coordinator.history.outcome(DAY_ONE)
    assert outcome is not None
    assert outcome.flags == ()
    row = coordinator.history.days[DAY_ONE]
    assert matcher_version(row.summary) == FORECAST_MATCHER_VERSION
    assert row.summary["c"] == 94

    # The evidence itself is untouched: only the reading of it was restated.
    after = coordinator.history.snapshots(DAY_ONE)[0]
    assert after.fingerprint == before.fingerprint
    assert after.predicted == before.predicted
    assert after.issued_at == before.issued_at

    assert coordinator.last_record.yesterday["intervals_compared"] == 94
    assert state_of(hass, ERROR_YESTERDAY).state == "0.0"


async def test_restatement_happens_once_and_then_costs_nothing(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A sweep that re-ran every refresh would rewrite the document for ever."""
    coordinator = setup_integration.runtime_data
    day = await beta5_style_exclusion(hass, coordinator)
    history = {**history_before(DAY_ONE), DAY_ONE: day}

    reseed(coordinator, history)
    await refresh_at(coordinator, local(DAY_TWO, 0, 20))
    assert coordinator.last_record.restated == (DAY_ONE,)
    finalized_at = coordinator.history.days[DAY_ONE].finalized_at

    for minute in (35, 50):
        reseed(coordinator, history)
        await refresh_at(coordinator, local(DAY_TWO, 0, minute))
        assert coordinator.last_record.restated == ()
    assert coordinator.history.days[DAY_ONE].finalized_at == finalized_at


async def test_a_match_is_not_re_derived_once_its_learning_record_is_gone(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Re-deriving without the record would write "no record" over a real match.

    That is the one way this sweep could destroy evidence, so the record's
    presence is a precondition rather than something the sweep discovers.
    """
    coordinator = setup_integration.runtime_data
    await beta5_style_exclusion(hass, coordinator)

    # The learning history for the day has since been pruned.
    reseed(coordinator, history_before(DAY_ONE))
    await refresh_at(coordinator, local(DAY_TWO, 0, 20))

    assert coordinator.last_record.restated == ()
    outcome = coordinator.history.outcome(DAY_ONE)
    assert outcome is not None
    assert outcome.flags == (FLAG_DEFINITION_CHANGED,)
    assert FLAG_NO_RECORD not in outcome.flags
    assert matcher_version(coordinator.history.days[DAY_ONE].summary) == 1


async def test_a_match_is_not_re_derived_once_its_raw_evidence_is_pruned(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Past the raw horizon there is no prediction left to re-derive against."""
    coordinator = setup_integration.runtime_data
    day = await beta5_style_exclusion(hass, coordinator)
    coordinator.history.days[DAY_ONE].raw_pruned = True

    reseed(coordinator, {**history_before(DAY_ONE), DAY_ONE: day})
    await refresh_at(coordinator, local(DAY_TWO, 0, 20))

    assert coordinator.last_record.restated == ()
    assert matcher_version(coordinator.history.days[DAY_ONE].summary) == 1


async def test_restatement_is_suspended_while_the_learning_store_is_unreadable(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The same rule finalisation obeys, for the same reason."""
    coordinator = setup_integration.runtime_data
    day = await beta5_style_exclusion(hass, coordinator)

    reseed(coordinator, {**history_before(DAY_ONE), DAY_ONE: day})
    coordinator.store.corrupt = True
    await refresh_at(coordinator, local(DAY_TWO, 0, 20))

    assert coordinator.last_record.finalization_suspended is True
    assert coordinator.last_record.restated == ()
    assert coordinator.history.outcome(DAY_ONE).flags == (FLAG_DEFINITION_CHANGED,)

    coordinator.store.corrupt = False
    await refresh_at(coordinator, local(DAY_TWO, 0, 35))
    assert coordinator.last_record.restated == (DAY_ONE,)


def test_a_row_without_a_version_is_the_first_generation() -> None:
    """Every beta.5 row is exactly that, and must be recognised as one."""
    assert matcher_version(None) == 1
    assert matcher_version({}) == 1
    assert matcher_version({"c": 96}) == 1
    assert matcher_version({"mr": True}) == 1
    assert matcher_version({"mr": "2"}) == 1
    assert matcher_version({"mr": 2}) == 2


# -- defect 4: the window never reports energy it did not measure ------------


async def test_the_window_never_publishes_energy_it_did_not_measure(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Below the minimum sample the rates are withheld; the facts are not.

    ``predicted_kwh: 0.0`` beside ``intervals_compared: 96`` is a claim that the
    house consumed nothing, published by a sensor whose whole purpose is to
    refuse exactly that substitution one layer down.
    """
    coordinator = setup_integration.runtime_data
    base = history_before(DAY_ONE)
    seed(coordinator, base)
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))
    reseed(coordinator, {**base, DAY_ONE: flat_day(DAY_ONE, 9.6)})
    await refresh_at(coordinator, local(DAY_TWO, 0, 5))

    window = coordinator.last_record.window
    assert window.intervals_compared == 96
    assert window.wape_percent is None
    assert window.predicted_kwh == 12.0
    assert window.actual_kwh == 9.6

    attributes = state_of(hass, ERROR_WINDOW).attributes
    assert attributes["predicted_kwh"] == 12.0
    assert attributes["actual_kwh"] == 9.6


async def test_an_empty_window_publishes_no_energy_at_all(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Nothing compared means nothing to report, including no zero."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(DAY_ONE))
    await refresh_at(coordinator, local(DAY_ONE, 12, 5))

    window = coordinator.last_record.window
    assert window.days_compared == 0
    assert window.predicted_kwh is None
    assert window.actual_kwh is None

    attributes = state_of(hass, ERROR_WINDOW).attributes
    assert attributes["predicted_kwh"] is None
    assert attributes["actual_kwh"] is None
    assert state_of(hass, ERROR_WINDOW).state == "unknown"


def test_the_deep_statistics_report_no_energy_for_an_empty_window() -> None:
    """The diagnostics half of the same substitution."""
    from custom_components.alpha_ems_manager.metrics import compute_window

    empty = compute_window([]).as_dict()
    assert empty["days_compared"] == 0
    assert empty["intervals_compared"] == 0
    assert empty["predicted_kwh"] is None
    assert empty["actual_kwh"] is None


# -- hardening: a non-finite stored number is missing data -------------------


def test_a_non_finite_stored_measurement_is_read_as_missing() -> None:
    """``NaN`` compares false against every guard that might have caught it."""
    record = DayRecord.from_dict(
        DAY_ONE,
        {
            "tz": "Europe/Amsterdam",
            "n": 4,
            "m": [0.1, float("nan"), float("inf"), 0.2],
        },
        "Europe/Amsterdam",
    )
    assert record is not None
    assert record.measured == [0.1, None, None, 0.2]
    assert record.measured_valid_count == 2
    assert record.measured_total_kwh == pytest.approx(0.3)


def test_a_non_finite_stored_prediction_is_read_as_missing() -> None:
    """The forecast half, where it would travel straight into a sensor state."""
    snapshot = ForecastSnapshot.from_dict(
        DAY_ONE,
        {
            "iat": "2026-08-19T10:05:00+00:00",
            "tz": "Europe/Amsterdam",
            "n": 3,
            "h": 0,
            "av": True,
            "p": [0.1, float("nan"), 0.2],
            "f": "000",
            "fp": "b" * 16,
        },
    )
    assert snapshot is not None
    assert snapshot.predicted == (0.1, None, 0.2)
    assert snapshot.total_kwh() == pytest.approx(0.3)

    outcome = DayOutcome.from_dict(
        DAY_ONE,
        {
            "fin": "2026-08-20T00:05:00+00:00",
            "tz": "Europe/Amsterdam",
            "n": 3,
            "a": [0.1, float("-inf"), 0.2],
            "s": STATUS_VALID * 3,
            "ev": float("nan"),
        },
    )
    assert outcome is not None
    assert outcome.actual == (0.1, None, 0.2)
    # The status is reconciled downwards: no value, no valid claim.
    assert outcome.valid_indices() == [0, 2]
    assert outcome.flexible_total_kwh is None


def test_a_non_finite_summary_row_is_skipped_rather_than_poisoning_a_window() -> None:
    """One damaged row must not void the sound rows beside it."""
    from custom_components.alpha_ems_manager.metrics import (
        day_error_from_summary,
        window_from_summaries,
    )

    sound = {"n": 96, "c": 96, "ps": 12.0, "as": 9.6, "ae": 2.4, "fg": []}
    damaged = {
        "n": 96,
        "c": 96,
        "ps": float("nan"),
        "as": 9.6,
        "ae": 2.4,
        "fg": [],
    }
    window = window_from_summaries([sound, damaged])
    assert window.days_compared == 1
    assert window.intervals_compared == 96
    assert window.wape_percent == 25.0
    assert day_error_from_summary(damaged) is None
