"""When a plan is worth announcing, and when silence is the right answer.

The announcement *policy* -- as distinct from the lifecycle, which
``test_beta31_activity`` covers. Three rules, each with its own history:

* **Imminence.** A run more than one planning interval away is silent, however
  many times the plan is recomputed. The plan's far end moves constantly and
  announcing that produced an entry every fifteen minutes about something nobody
  could act on.
* **No back-dating.** A window that has already closed is never announced.
  Describing a decision nobody could act on is worse than saying nothing.
* **Absolute instants.** The old fingerprint keyed on an index counted from the
  target day's midnight, so at the day boundary every index dropped by a whole day
  and the announcement fired again with no change in meaning.

Every case drives the real :func:`next_activity` against fixed instants. No clock
is read anywhere in the module under test, which is what makes that possible.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.alpha_ems_manager.activity import (
    PlannedRun,
    RunContent,
    RunIdentity,
    direction_of,
    next_activity,
)
from custom_components.alpha_ems_manager.const import (
    ACTIVITY_CATEGORY_ECONOMIC_BUY,
    ACTIVITY_CATEGORY_ECONOMIC_SELL,
    ACTIVITY_CATEGORY_SAFETY_BUY,
    ECONOMIC_ACTION_DISCHARGE,
    ECONOMIC_ACTION_EXPORT,
    ECONOMIC_ANNOUNCE_LEAD_MINUTES,
    ECONOMIC_DIRECTION_CHARGE,
    ECONOMIC_DIRECTION_DISCHARGE,
    ECONOMIC_EVENT_CANCELLED,
    ECONOMIC_EVENT_PLANNED,
    QUARTER_MINUTES,
)

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
QUARTER = timedelta(minutes=QUARTER_MINUTES)


def make_run(
    *,
    start_minutes: float,
    duration_minutes: float = 60.0,
    category: str = ACTIVITY_CATEGORY_SAFETY_BUY,
    energy_kwh: float = 6.5,
    now: datetime = NOW,
) -> PlannedRun:
    """Return one planned run, positioned relative to ``now``."""
    start = now + timedelta(minutes=start_minutes)
    end = start + timedelta(minutes=duration_minutes)
    direction = (
        ECONOMIC_DIRECTION_DISCHARGE
        if category == ACTIVITY_CATEGORY_ECONOMIC_SELL
        else ECONOMIC_DIRECTION_CHARGE
    )
    return PlannedRun(
        identity=RunIdentity(direction=direction, start_utc=start),
        content=RunContent(
            category=category,
            energy_kwh=energy_kwh,
            end_utc=end,
            window=f"{start:%H:%M}-{end:%H:%M}",
        ),
    )


def announce(run: PlannedRun, *, now: datetime = NOW):
    """Return the state after announcing one run, asserting it was announced."""
    entry = next_activity(previous=None, runs=(run,), now=now)
    assert entry is not None, "the run was expected to be announced"
    return entry.state


# ===========================================================================
# A. imminence
# ===========================================================================


@pytest.mark.parametrize("lead_minutes", [180, 120, 60, 30, 16])
def test_a_run_further_out_than_one_interval_is_silent(lead_minutes: float) -> None:
    """The rule that removes the spam.

    A run three hours away is a forecast, not news. The plan is rebuilt every
    quarter and its far end moves constantly; announcing that produced an entry
    every fifteen minutes about something nobody could act on yet.
    """
    entry = next_activity(
        previous=None, runs=(make_run(start_minutes=lead_minutes),), now=NOW
    )

    assert entry is None


@pytest.mark.parametrize("lead_minutes", [15, 14, 10, 5, 1])
def test_a_run_within_one_interval_is_announced_once(lead_minutes: float) -> None:
    """The last refresh before it begins, which is when it is worth reading."""
    run = make_run(start_minutes=lead_minutes)
    entry = next_activity(previous=None, runs=(run,), now=NOW)

    assert entry is not None
    assert entry.kind == ECONOMIC_EVENT_PLANNED
    assert "Safety Buy Planned" in entry.message
    assert "6.50 kWh" in entry.message
    # It must not read as though the battery had already moved.
    assert "Started" not in entry.message


def test_the_lead_time_is_exactly_one_planning_interval() -> None:
    """Derived from the refresh cadence, not chosen. Pinned so it cannot drift."""
    assert ECONOMIC_ANNOUNCE_LEAD_MINUTES == QUARTER_MINUTES == 15


def test_a_distant_run_beside_an_imminent_one_stays_silent() -> None:
    """Only the imminent one is news, and it speaks exactly once."""
    imminent = make_run(start_minutes=10)
    distant = make_run(start_minutes=600, category=ACTIVITY_CATEGORY_ECONOMIC_SELL)

    entry = next_activity(previous=None, runs=(imminent, distant), now=NOW)
    assert entry is not None
    assert entry.kind == ECONOMIC_EVENT_PLANNED
    assert "Safety Buy" in entry.message

    assert (
        next_activity(previous=entry.state, runs=(imminent, distant), now=NOW) is None
    )


# ===========================================================================
# B. the in-progress regression -- the reported symptom
# ===========================================================================


def test_ten_refreshes_during_a_running_charge_produce_no_further_entries() -> None:
    """The exact symptom from the live logbook, as a regression.

    A three-hour charge run, refreshed every quarter while it runs. Its start
    advances as the horizon consumes it and its remaining energy shrinks -- which
    is arithmetic, not a decision. Under the old fingerprint each of these
    produced an entry; under beta.30's identity the *start* moving produced two,
    a false "finished" and a fresh announcement.
    """
    start = NOW + timedelta(minutes=10)
    end = start + timedelta(minutes=180)
    state = announce(make_run(start_minutes=10, duration_minutes=180, energy_kwh=12.0))

    entries = []
    remaining = 12.0
    for step in range(1, 11):
        now = NOW + step * QUARTER
        # The horizon has consumed `step` quarters of the run: its head advances
        # and its remaining energy falls. Its end does not move.
        remaining -= 1.0
        run = PlannedRun(
            identity=RunIdentity(
                direction=ECONOMIC_DIRECTION_CHARGE, start_utc=start + step * QUARTER
            ),
            content=RunContent(
                category=ACTIVITY_CATEGORY_SAFETY_BUY,
                energy_kwh=remaining,
                end_utc=end,
                window=f"{start + step * QUARTER:%H:%M}-{end:%H:%M}",
            ),
        )
        entry = next_activity(previous=state, runs=(run,), now=now)
        if entry is not None:
            entries.append((step, entry.kind, entry.message))
            state = entry.state

    assert entries == [], f"the running run spoke {len(entries)} more times"


def test_crossing_midnight_produces_no_entry() -> None:
    """Indices rebase at midnight; absolute instants do not.

    The old fingerprint keyed on an index counted from the target day's midnight,
    so at the day boundary every index dropped by a whole day and the hash changed
    with no change in meaning.
    """
    evening = datetime(2026, 8, 22, 23, 50, tzinfo=UTC)
    run = make_run(start_minutes=10, now=evening)
    state = announce(run, now=evening)

    after_midnight = datetime(2026, 8, 23, 0, 5, tzinfo=UTC)
    assert next_activity(previous=state, runs=(run,), now=after_midnight) is None


def test_a_run_relabelled_from_discharge_to_export_is_the_same_direction() -> None:
    """One physical discharge, two labels, one decision.

    The label alternates as house load rises and falls beneath a discharge; the
    direction does not, and only the direction belongs in an identity.
    """
    assert direction_of(ECONOMIC_ACTION_DISCHARGE) == ECONOMIC_DIRECTION_DISCHARGE
    assert direction_of(ECONOMIC_ACTION_EXPORT) == ECONOMIC_DIRECTION_DISCHARGE


# ===========================================================================
# C. never back-date
# ===========================================================================


def test_a_run_that_already_finished_is_never_announced() -> None:
    """Describing a window nobody could act on is worse than saying nothing."""
    finished = make_run(start_minutes=-120, duration_minutes=60)

    assert next_activity(previous=None, runs=(finished,), now=NOW) is None


def test_a_run_already_under_way_is_announced_once_after_a_reload() -> None:
    """State resets on reload, and the standing decision deserves one line."""
    running = make_run(start_minutes=-30, duration_minutes=120)

    entry = next_activity(previous=None, runs=(running,), now=NOW)
    assert entry is not None
    assert entry.kind == ECONOMIC_EVENT_PLANNED

    assert next_activity(previous=entry.state, runs=(running,), now=NOW) is None


def test_a_future_run_that_disappears_is_cancelled_once() -> None:
    """An announcement left standing when the plan dropped it would be a lie."""
    run = make_run(start_minutes=10)
    state = announce(run)

    entry = next_activity(previous=state, runs=(), now=NOW)
    assert entry is not None
    assert entry.kind == ECONOMIC_EVENT_CANCELLED
    assert "Plan Replaced" in entry.message

    assert next_activity(previous=entry.state, runs=(), now=NOW) is None


def test_a_run_whose_window_has_passed_reads_as_expired_not_as_replaced() -> None:
    """The distinction beta.30 could not make, and the one a reader needs.

    "has finished the planned window" was said about both, so a plan superseded
    three hours early and a plan whose window genuinely elapsed produced the same
    line. They are different events and now they read differently.
    """
    run = make_run(start_minutes=-30, duration_minutes=60)
    state = announce(run)
    later = NOW + timedelta(minutes=45)

    entry = next_activity(previous=state, runs=(), now=later)
    assert entry is not None
    assert entry.kind == ECONOMIC_EVENT_CANCELLED
    assert "Window Expired" in entry.message

    assert next_activity(previous=entry.state, runs=(), now=later) is None


def test_a_direction_reversal_is_a_cancellation_and_a_new_plan() -> None:
    """A charge that becomes a discharge is not the same decision changed."""
    charge = make_run(start_minutes=10)
    state = announce(charge)
    sell = make_run(start_minutes=10, category=ACTIVITY_CATEGORY_ECONOMIC_SELL)

    first = next_activity(previous=state, runs=(sell,), now=NOW)
    assert first is not None
    assert first.kind == ECONOMIC_EVENT_CANCELLED

    second = next_activity(previous=first.state, runs=(sell,), now=NOW)
    assert second is not None
    assert second.kind == ECONOMIC_EVENT_PLANNED
    assert "Economic Sell Planned" in second.message


def test_a_closed_plan_is_never_announced_a_second_time() -> None:
    """The rule that makes "Planned appears exactly once" hold across a gap.

    A plan id is derived from its identity, so a plan that terminates and then
    reappears in a later solve would otherwise be announced again under the same
    id -- "Finished, then Planned" for one plan, which the lifecycle forbids.
    """
    run = make_run(start_minutes=10, category=ACTIVITY_CATEGORY_ECONOMIC_BUY)
    state = announce(run)

    cancelled = next_activity(previous=state, runs=(), now=NOW)
    assert cancelled is not None
    assert cancelled.kind == ECONOMIC_EVENT_CANCELLED

    # The same plan, back in the plan, at the same window.
    assert next_activity(previous=cancelled.state, runs=(run,), now=NOW) is None
