"""Matching a prediction against what the house actually did.

Two rules dominate this file, and both are about refusing to produce a number:

* a missing actual is never zero, and never appears as a validated comparison;
* nothing is finalised while the learning store cannot be read, because the
  records written here are immutable and would permanently assert that every
  measurement was missing.

The second is the Phase-2 analogue of the beta.4 write-after-failed-read bug,
and it is worse: the learning document at least survives that failure, while a
finalised forecast outcome is final by design.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.const import (
    FLAG_DEFINITION_CHANGED,
    FLAG_NO_RECORD,
    FLAG_SHAPE_MISMATCH,
    FLAG_TIMEZONE_CHANGED,
    STATUS_FLEXIBLE_MISSING,
    STATUS_MEASURED_MISSING,
    STATUS_VALID,
)
from custom_components.alpha_ems_manager.forecast_history import (
    LIFECYCLE_PENDING,
    LIFECYCLE_UNMATCHED,
    LIFECYCLE_UNRESOLVED,
    LIFECYCLE_VALIDATED,
    DayOutcome,
    lifecycle_state,
)

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

TOMORROW = NORMAL + timedelta(days=1)


async def issue_then_turn_the_day(
    coordinator, *, actual: dict[date, object] | None = None
) -> None:
    """Issue a forecast for ``NORMAL``, then refresh on the following day."""
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))

    history = dict(history_before(NORMAL))
    if actual is not None:
        history.update(actual)
    reseed(coordinator, history)
    await refresh_at(coordinator, local(TOMORROW, 0, 5))


# -- what "actual" means -----------------------------------------------------


async def test_the_actual_is_the_baseline_not_the_raw_measured_load(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The model predicts baseline, so baseline is what it is scored against.

    Comparing against raw measured load would charge the model with the energy
    an EV drew -- precisely the load the baseline exists to exclude, and which a
    future optimiser may itself have scheduled.
    """
    coordinator = setup_integration.runtime_data
    coordinator.config = coordinator.config.__class__(
        **{
            **{
                field: getattr(coordinator.config, field)
                for field in coordinator.config.__dataclass_fields__
            },
            "ev_power_entity": EV_POWER,
        }
    )

    # 9.6 kWh measured, 4.8 kWh of it flexible, so the baseline is 4.8 kWh.
    day = flat_day(
        NORMAL,
        9.6,
        ev_kwh_per_interval=0.05,
        ev_expected=True,
    )
    await issue_then_turn_the_day(coordinator, actual={NORMAL: day})

    outcome = coordinator.history.outcome(NORMAL)
    assert outcome is not None
    assert sum(v for v in outcome.actual if v is not None) == pytest.approx(4.8)
    assert day.measured_total_kwh == pytest.approx(9.6)
    assert set(outcome.status) == {STATUS_VALID}


async def test_a_missing_measurement_is_recorded_as_missing_not_zero(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A rejected or unobserved quarter has no actual, and must say so."""
    coordinator = setup_integration.runtime_data
    partial = flat_day(NORMAL, 12.0, accepted_intervals=60)

    await issue_then_turn_the_day(coordinator, actual={NORMAL: partial})

    outcome = coordinator.history.outcome(NORMAL)
    assert outcome is not None
    assert outcome.status[:60] == STATUS_VALID * 60
    assert outcome.status[60:] == STATUS_MEASURED_MISSING * 36
    # The one thing that must never happen.
    assert all(value is None for value in outcome.actual[60:])
    assert 0.0 not in outcome.actual[60:]


async def test_an_unusable_flexible_reading_is_distinguished_from_a_missing_one(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Measured intact, baseline undefined: a different fault, named separately.

    The two call for different action -- check the house-load sensor, or check
    the charger -- so collapsing them into one "missing" code would throw away
    the only clue.
    """
    coordinator = setup_integration.runtime_data
    day = empty_day(NORMAL)
    for index in range(day.interval_count):
        day.record_interval(
            index,
            measured_kwh=0.125,
            # The charger drops out for the first eight intervals.
            ev_kwh=None if index < 8 else 0.0,
            ev_expected=True,
        )

    await issue_then_turn_the_day(coordinator, actual={NORMAL: day})

    outcome = coordinator.history.outcome(NORMAL)
    assert outcome is not None
    assert outcome.status[:8] == STATUS_FLEXIBLE_MISSING * 8
    assert outcome.status[8:] == STATUS_VALID * 88
    assert all(value is None for value in outcome.actual[:8])


async def test_a_day_with_no_record_at_all_is_still_finalised(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Home Assistant was off all day. That absence is evidence.

    Leaving the day unfinalised instead would keep a prediction dangling
    forever, retried on every refresh for as long as the record is retained.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))

    # The day turns with nothing at all recorded for NORMAL.
    reseed(coordinator, history_before(NORMAL))
    coordinator.store.days.pop(NORMAL, None)
    await refresh_at(coordinator, local(TOMORROW, 0, 5))

    outcome = coordinator.history.outcome(NORMAL)
    assert outcome is not None
    assert outcome.flags == (FLAG_NO_RECORD,)
    assert set(outcome.status) == {STATUS_MEASURED_MISSING}
    assert outcome.comparable is False
    assert coordinator.history.is_finalized(NORMAL) is True


# -- the corruption rule -----------------------------------------------------


async def test_a_corrupt_learning_store_suspends_finalisation(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """An unreadable history must not become an immutable "all missing" record.

    ``LearningStore`` degrades a failed read to an empty history so setup can
    continue, which is right for availability. Finalising against that empty
    view would write, permanently, that no interval of the day was measured --
    for days whose measurements are very probably intact on disk.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))

    # The learning document could not be read on this session's load.
    coordinator.store.corrupt = True
    coordinator.store.days = {}
    await refresh_at(coordinator, local(TOMORROW, 0, 5))

    assert coordinator.history.outcome(NORMAL) is None
    assert coordinator.history.is_finalized(NORMAL) is False
    assert coordinator.last_record.finalization_suspended is True
    assert coordinator.last_record.unresolved_days == 1


async def test_matching_resumes_correctly_once_the_history_is_readable(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Waiting costs nothing: matching is a pure recomputation.

    The day was never finalised while the store was unreadable, so once the
    real measurements are back the outcome is built from them -- not from the
    empty view that was in memory at the time.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))

    coordinator.store.corrupt = True
    coordinator.store.days = {}
    await refresh_at(coordinator, local(TOMORROW, 0, 5))
    assert coordinator.history.outcome(NORMAL) is None

    # A restart reads the document successfully this time.
    coordinator.store.corrupt = False
    reseed(coordinator, {**history_before(NORMAL), NORMAL: flat_day(NORMAL, 11.0)})
    await refresh_at(coordinator, local(TOMORROW, 0, 20))

    outcome = coordinator.history.outcome(NORMAL)
    assert outcome is not None
    assert outcome.flags == ()
    # Stored energies are rounded to 0.1 Wh per interval, so a 96-interval
    # day can differ from its nominal total by a few milliwatt-hours.
    assert sum(v for v in outcome.actual if v is not None) == pytest.approx(
        11.0, abs=0.01
    )
    assert coordinator.last_record.finalization_suspended is False
    assert coordinator.last_record.unresolved_days == 0


async def test_suspension_does_not_stop_new_forecasts_being_issued(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Issuance and matching fail independently.

    A prediction is knowable without reading the past; only the comparison
    needs the history. Suspending both would lose evidence unnecessarily.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))

    coordinator.store.corrupt = True
    await refresh_at(coordinator, local(TOMORROW, 0, 5))

    assert coordinator.history.snapshots(TOMORROW) != []
    assert coordinator.history.outcome(NORMAL) is None


# -- restart, retry and idempotency ------------------------------------------


async def test_a_restart_before_matching_loses_nothing(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """There is no in-memory queue to lose.

    Matching is a pure function over persisted data, so a restart between
    issuance and matching simply means the work happens on the next refresh.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))
    assert coordinator.history.unfinalized_days(before=TOMORROW) == [NORMAL]

    # Home Assistant restarts: the recorder's session state is gone.
    coordinator.recorder._last_day = None
    reseed(coordinator, {**history_before(NORMAL), NORMAL: flat_day(NORMAL, 12.0)})
    await refresh_at(coordinator, local(TOMORROW, 6, 5))

    assert coordinator.history.is_finalized(NORMAL) is True
    assert coordinator.history.unfinalized_days(before=TOMORROW) == []


async def test_finalisation_is_idempotent(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Refreshing again must not re-open or duplicate a matched day."""
    coordinator = setup_integration.runtime_data
    await issue_then_turn_the_day(coordinator, actual={NORMAL: flat_day(NORMAL, 12.0)})
    first = coordinator.history.outcome(NORMAL)

    for minute in (20, 35, 50):
        await refresh_at(coordinator, local(TOMORROW, 0, minute))

    second = coordinator.history.outcome(NORMAL)
    assert first is not None and second is not None
    assert second.finalized_at == first.finalized_at
    assert second.actual == first.actual
    assert coordinator.last_record.finalized == ()


async def test_a_backlog_of_unmatched_days_is_resolved_in_bounded_batches(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Ten days of suspended matching, then a clean read.

    Suspension is the only way a real backlog builds up, so it is also how one
    is built here. Every day must eventually resolve, and no single refresh may
    take the whole backlog at once: a months-long one would otherwise be one
    long synchronous burst of partition loads on the event loop.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    measured: dict[date, object] = {}

    # Ten consecutive days each issue a forecast, while the learning store
    # cannot be read, so nothing is ever matched.
    coordinator.store.corrupt = True
    for offset in range(10):
        day = NORMAL + timedelta(days=offset)
        measured[day] = flat_day(day, 12.0)
        reseed(coordinator, history_before(day))
        await refresh_at(coordinator, local(day, 12, 5))

    restart = NORMAL + timedelta(days=10)
    pending = coordinator.history.unfinalized_days(before=restart)
    assert len(pending) == 10
    assert coordinator.last_record.finalization_suspended is True

    # The document reads correctly again.
    coordinator.store.corrupt = False
    reseed(coordinator, {**history_before(NORMAL), **measured})

    await refresh_at(coordinator, local(restart, 9, 5))
    remaining = coordinator.history.unfinalized_days(before=restart)
    # Bounded: the first refresh took a batch, not the lot.
    assert 0 < len(remaining) < 10

    # Following refreshes drain the rest, oldest first.
    for step in range(1, 6):
        await refresh_at(coordinator, local(restart, 9, 5 + 15 * step))
        if not coordinator.history.unfinalized_days(before=restart):
            break

    assert coordinator.history.unfinalized_days(before=restart) == []
    assert all(coordinator.history.is_finalized(day) for day in pending)


async def test_the_backlog_is_drained_oldest_first(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """History resolves in the order it happened, not in an arbitrary one."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    measured: dict[date, object] = {}

    coordinator.store.corrupt = True
    for offset in range(10):
        day = NORMAL + timedelta(days=offset)
        measured[day] = flat_day(day, 12.0)
        reseed(coordinator, history_before(day))
        await refresh_at(coordinator, local(day, 12, 5))

    restart = NORMAL + timedelta(days=10)
    coordinator.store.corrupt = False
    reseed(coordinator, {**history_before(NORMAL), **measured})
    await refresh_at(coordinator, local(restart, 9, 5))

    resolved = [
        day
        for day in sorted(coordinator.history.days)
        if coordinator.history.is_finalized(day)
    ]
    assert resolved == sorted(resolved)
    assert resolved[0] == NORMAL


# -- flags: when the two sides are not comparable ----------------------------


async def test_a_changed_flexible_load_configuration_flags_the_day(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Baseline meant one thing at issuance and another during the day.

    The record is kept -- prediction and actual are both true facts -- but the
    day is excluded from every statistic, because the two are measuring
    different quantities.
    """
    coordinator = setup_integration.runtime_data
    set_sensor(hass, EV_POWER, 0, "W", "power")
    seed(coordinator, history_before(NORMAL))

    # Issued with no flexible load configured.
    await refresh_at(coordinator, local(NORMAL, 12, 5))

    # A charger is configured, and the day records flexible energy throughout.
    day = flat_day(NORMAL, 12.0, ev_kwh_per_interval=0.02, ev_expected=True)
    reseed(coordinator, {**history_before(NORMAL), NORMAL: day})
    await refresh_at(coordinator, local(TOMORROW, 0, 5))

    outcome = coordinator.history.outcome(NORMAL)
    assert outcome is not None
    assert FLAG_DEFINITION_CHANGED in outcome.flags
    assert outcome.comparable is False


async def test_a_flexible_load_added_mid_day_flags_the_day(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Half the day means one thing by "baseline" and half means another."""
    coordinator = setup_integration.runtime_data
    day = empty_day(NORMAL)
    for index in range(day.interval_count):
        day.record_interval(
            index,
            measured_kwh=0.125,
            ev_kwh=0.0 if index >= 48 else None,
            # The charger is selected at noon.
            ev_expected=index >= 48,
        )

    await issue_then_turn_the_day(coordinator, actual={NORMAL: day})

    outcome = coordinator.history.outcome(NORMAL)
    assert outcome is not None
    assert FLAG_DEFINITION_CHANGED in outcome.flags


async def test_a_changed_timezone_is_flagged_rather_than_matched_by_index(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Two zones give a chronological index two different meanings.

    Matching them by position would line an 18:00 prediction up against a 17:00
    measurement and look entirely plausible doing it.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))

    moved = flat_day(NORMAL, 12.0)
    moved.tz_key = "America/New_York"
    reseed(coordinator, {**history_before(NORMAL), NORMAL: moved})
    await refresh_at(coordinator, local(TOMORROW, 0, 5))

    outcome = coordinator.history.outcome(NORMAL)
    assert outcome is not None
    assert FLAG_TIMEZONE_CHANGED in outcome.flags
    assert outcome.comparable is False


async def test_a_changed_day_length_is_flagged(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A 96-interval prediction cannot be matched against a 100-interval day."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))

    reshaped = flat_day(NORMAL, 12.0)
    reshaped.interval_count = 100
    reshaped._resize()
    reseed(coordinator, {**history_before(NORMAL), NORMAL: reshaped})
    await refresh_at(coordinator, local(TOMORROW, 0, 5))

    outcome = coordinator.history.outcome(NORMAL)
    assert outcome is not None
    assert FLAG_SHAPE_MISMATCH in outcome.flags
    assert outcome.interval_count == 100


# -- lifecycle ---------------------------------------------------------------


def describe(outcome: DayOutcome | None, today: date) -> str:
    """Return the lifecycle state a target day is in, given its outcome.

    Expressed through :func:`lifecycle_state`, which is the single rule both
    production callers use, so a test cannot pass against a second copy of the
    logic that production does not run.
    """
    return lifecycle_state(
        NORMAL,
        today,
        finalized=outcome is not None,
        comparable=outcome is None or outcome.comparable,
        has_valid_interval=bool(outcome is not None and outcome.valid_indices()),
    )


def test_the_lifecycle_is_derived_from_the_facts() -> None:
    """No stored state field. A state that can disagree with the data will."""
    finalized_at = datetime(2026, 8, 20, 0, 5, tzinfo=UTC)

    assert describe(None, NORMAL) == LIFECYCLE_PENDING
    assert describe(None, TOMORROW) == LIFECYCLE_UNRESOLVED

    good = DayOutcome(
        target_day=NORMAL,
        finalized_at=finalized_at,
        tz_key="Europe/Amsterdam",
        interval_count=4,
        actual=(0.1, 0.1, 0.1, 0.1),
        status=STATUS_VALID * 4,
        flexible_total_kwh=None,
    )
    assert describe(good, TOMORROW) == LIFECYCLE_VALIDATED

    empty = DayOutcome(
        target_day=NORMAL,
        finalized_at=finalized_at,
        tz_key="Europe/Amsterdam",
        interval_count=4,
        actual=(None, None, None, None),
        status=STATUS_MEASURED_MISSING * 4,
        flexible_total_kwh=None,
        flags=(FLAG_NO_RECORD,),
    )
    assert describe(empty, TOMORROW) == LIFECYCLE_UNMATCHED

    # Finalised and unflagged, but nothing survived to compare.
    barren = DayOutcome(
        target_day=NORMAL,
        finalized_at=finalized_at,
        tz_key="Europe/Amsterdam",
        interval_count=4,
        actual=(None, None, None, None),
        status=STATUS_MEASURED_MISSING * 4,
        flexible_total_kwh=None,
    )
    assert describe(barren, TOMORROW) == LIFECYCLE_UNMATCHED


def test_a_stored_status_cannot_claim_a_validated_interval_with_no_value() -> None:
    """The reconciliation on load closes the last route to a fabricated zero.

    A hand-edited or partially written document could otherwise assert that an
    interval was valid while the value beside it is null, and every consumer
    downstream trusts the status code.
    """
    from custom_components.alpha_ems_manager.forecast_history import DayOutcome

    outcome = DayOutcome.from_dict(
        NORMAL,
        {
            "fin": "2026-08-20T00:05:00+00:00",
            "tz": "Europe/Amsterdam",
            "n": 4,
            "a": [0.1, None, 0.1, None],
            "s": "0000",
            "ev": None,
            "fl": [],
        },
    )

    assert outcome is not None
    assert outcome.status == "0101"
    assert outcome.valid_indices() == [0, 2]


async def test_an_unmatched_day_never_reaches_a_published_metric(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A flagged day is retained as evidence and excluded from statistics."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 12, 5))

    reseed(coordinator, history_before(NORMAL))
    coordinator.store.days.pop(NORMAL, None)
    await refresh_at(coordinator, local(TOMORROW, 0, 5))

    assert coordinator.history.is_finalized(NORMAL) is True
    # Recorded, but with nothing comparable, so nothing is published.
    assert coordinator.data["forecast_yesterday_error"] is None
    assert coordinator.data["forecast_error_window"].intervals_compared == 0
