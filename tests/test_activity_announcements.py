"""One message per run, and the proof that the old spam cannot come back.

The live logbook showed this, every quarter of an hour, for hours:

    11:45 -> charge 11:45-15:00
    12:00 -> charge 12:00-15:00
    12:15 -> charge 12:15-15:30
    12:30 -> charge 12:30-15:45

Technically honest and completely unreadable. The plan really was recomputed each
quarter and its remaining energy really did shrink -- but nothing had been
*decided* differently, and a log that cannot tell those apart is noise.

Three causes, all fixed here and each with its own regression:

* the run's identity was keyed on ``start_index``, a horizon-relative index that
  advances every refresh while a run is under way;
* midnight rebased every index by a whole day with no change in meaning;
* the figures were bucketed and hashed, so a hundredth of a kilowatt across a
  boundary spoke while a fifth of a kilowatt inside one stayed silent.

And one policy change: a run is announced when it is **imminent**, not whenever it
exists. That single rule removes the overwhelming majority of the old traffic.

Every case here drives the real :func:`next_activity` against fixed instants. No
clock is read anywhere in the module under test, which is what makes that
possible.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.alpha_ems_manager.activity import (
    ActivityState,
    PlannedRun,
    RunContent,
    RunIdentity,
    direction_of,
    logbook_payload,
    next_activity,
)
from custom_components.alpha_ems_manager.const import (
    CONTROL_EXECUTION_AVAILABLE,
    CONTROL_MIN_POWER_KW,
    ECONOMIC_ACTION_CHARGE,
    ECONOMIC_ACTION_DISCHARGE,
    ECONOMIC_ACTION_EXPORT,
    ECONOMIC_ANNOUNCE_LEAD_MINUTES,
    ECONOMIC_BUCKET_KWH,
    ECONOMIC_CHARGE_SOURCE_MIXED,
    ECONOMIC_CHARGE_SOURCE_PRODUCTION,
    ECONOMIC_DEADBAND_ENERGY_KWH,
    ECONOMIC_DEADBAND_MINUTES,
    ECONOMIC_DEADBAND_POWER_KW,
    ECONOMIC_DIRECTION_CHARGE,
    ECONOMIC_DIRECTION_DISCHARGE,
    ECONOMIC_EVENT_CANCELLED,
    ECONOMIC_EVENT_CHANGED,
    ECONOMIC_EVENT_ENDED,
    ECONOMIC_EVENT_PLANNED,
    ECONOMIC_EVENT_REFUSED,
    ECONOMIC_EVENT_STARTED,
    ECONOMIC_REASON_CHEAP_WINDOW,
    QUARTER_MINUTES,
)

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
QUARTER = timedelta(minutes=QUARTER_MINUTES)


def make_run(
    *,
    start_minutes: float,
    duration_minutes: float = 60.0,
    action: str = ECONOMIC_ACTION_CHARGE,
    energy_kwh: float = 6.5,
    power_kw: float = 8.7,
    battery_energy_kwh: float | None = None,
    average_power_kw: float | None = None,
    charge_source: str = ECONOMIC_CHARGE_SOURCE_MIXED,
    refused: bool = False,
    now: datetime = NOW,
) -> PlannedRun:
    """Return one planned run, positioned relative to ``now``."""
    start = now + timedelta(minutes=start_minutes)
    end = start + timedelta(minutes=duration_minutes)
    return PlannedRun(
        identity=RunIdentity(direction=direction_of(action), start_utc=start),
        content=RunContent(
            action=action,
            capability_action=(ECONOMIC_ACTION_DISCHARGE if refused else action),
            reason=ECONOMIC_REASON_CHEAP_WINDOW,
            energy_kwh=energy_kwh,
            battery_energy_kwh=(
                energy_kwh if battery_energy_kwh is None else battery_energy_kwh
            ),
            power_kw=power_kw,
            average_power_kw=(
                power_kw if average_power_kw is None else average_power_kw
            ),
            end_utc=end,
            charge_source=charge_source,
            price_eur_kwh=0.12,
            value_eur=1.23,
            refused=refused,
            window=f"{start:%H:%M}-{end:%H:%M}",
        ),
    )


def announce(run: PlannedRun, *, now: datetime = NOW) -> ActivityState:
    """Return the state after announcing one run, asserting it was announced."""
    entry = next_activity(previous=None, runs=(run,), now=now)
    assert entry is not None, "the run was expected to be announced"
    return entry.state


# ===========================================================================
# A. announcement timing
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
    assert "plans to charge the battery" in entry.message
    assert "6.50 kWh" in entry.message
    assert "Advisory only" in entry.message


def test_the_lead_time_is_exactly_one_planning_interval() -> None:
    """Derived from the refresh cadence, not chosen. Pinned so it cannot drift."""
    assert ECONOMIC_ANNOUNCE_LEAD_MINUTES == QUARTER_MINUTES == 15


def test_announcing_the_same_run_again_says_nothing() -> None:
    """The plan is recomputed; the decision is not new."""
    run = make_run(start_minutes=10)
    state = announce(run)

    assert next_activity(previous=state, runs=(run,), now=NOW) is None


# ===========================================================================
# B. the in-progress regression -- the reported symptom
# ===========================================================================


def test_ten_refreshes_during_a_running_charge_produce_no_further_entries() -> None:
    """The exact symptom from the live logbook, as a regression.

    A three-hour charge run, refreshed every quarter while it runs. Its remaining
    energy shrinks and its first-interval power moves as the horizon consumes it --
    which is arithmetic, not a decision. Under the old fingerprint each of these
    produced an entry.
    """
    start = NOW + timedelta(minutes=10)
    first = make_run(start_minutes=10, duration_minutes=180, energy_kwh=12.0)
    state = announce(first)

    entries = []
    remaining = 12.0
    for step in range(1, 11):
        now = NOW + step * QUARTER
        # The horizon has consumed `step` quarters of the run.
        remaining -= 1.0
        run = PlannedRun(
            identity=RunIdentity(direction=ECONOMIC_DIRECTION_CHARGE, start_utc=start),
            content=first.content.__class__(
                **{
                    **{
                        field: getattr(first.content, field)
                        for field in first.content.__dataclass_fields__
                    },
                    "energy_kwh": remaining,
                    "power_kw": 8.7 - 0.05 * step,
                }
            ),
        )
        entry = next_activity(previous=state, runs=(run,), now=now)
        if entry is not None:
            entries.append((step, entry.kind))
            state = entry.state

    assert entries == [], f"the running run spoke {len(entries)} more times"


def test_the_identity_survives_the_horizon_advancing() -> None:
    """The root cause. The start instant does not move; an index did."""
    start = NOW + timedelta(minutes=10)
    identities = {
        RunIdentity(direction=ECONOMIC_DIRECTION_CHARGE, start_utc=start)
        for _ in range(5)
    }

    assert len(identities) == 1


def test_crossing_midnight_produces_no_entry() -> None:
    """Indices rebase at midnight; absolute instants do not.

    The old fingerprint keyed on an index counted from the target day's midnight,
    so at the day boundary every index dropped by a whole day and the hash changed
    with no change in meaning.
    """
    evening = datetime(2026, 8, 22, 23, 50, tzinfo=UTC)
    start = datetime(2026, 8, 23, 0, 0, tzinfo=UTC)
    run = PlannedRun(
        identity=RunIdentity(direction=ECONOMIC_DIRECTION_CHARGE, start_utc=start),
        content=make_run(start_minutes=10, now=evening).content,
    )
    state = announce(run, now=evening)

    after_midnight = datetime(2026, 8, 23, 0, 5, tzinfo=UTC)
    assert next_activity(previous=state, runs=(run,), now=after_midnight) is None


def test_a_run_relabelled_from_discharge_to_export_is_the_same_run() -> None:
    """One physical discharge, two labels, one decision.

    The label flips as house load rises and falls beneath a constant battery
    discharge. Identity is the *direction*, so the flip is invisible -- and the
    content comparison deliberately ignores the label too.
    """
    start = NOW + timedelta(minutes=10)
    as_discharge = make_run(start_minutes=10, action=ECONOMIC_ACTION_DISCHARGE)
    state = announce(as_discharge)

    as_export = PlannedRun(
        identity=RunIdentity(direction=ECONOMIC_DIRECTION_DISCHARGE, start_utc=start),
        content=make_run(start_minutes=10, action=ECONOMIC_ACTION_EXPORT).content,
    )

    assert as_discharge.identity == as_export.identity
    assert next_activity(previous=state, runs=(as_export,), now=NOW) is None


# ===========================================================================
# C. material change, and the deadbands
# ===========================================================================


def test_the_deadbands_are_existing_constants_not_chosen_percentages() -> None:
    """Each one is a quantity the system already could not distinguish below."""
    assert ECONOMIC_DEADBAND_ENERGY_KWH == ECONOMIC_BUCKET_KWH == 0.25
    assert ECONOMIC_DEADBAND_POWER_KW == CONTROL_MIN_POWER_KW == 0.2
    assert ECONOMIC_DEADBAND_MINUTES == QUARTER_MINUTES == 15


@pytest.mark.parametrize(
    ("energy_delta", "power_delta"),
    [(0.24, 0.0), (0.0, 0.19), (0.1, 0.1), (-0.24, -0.19)],
)
def test_drift_inside_the_deadbands_is_silent(
    energy_delta: float, power_delta: float
) -> None:
    """A plan that moves by less than the system can express has not moved."""
    run = make_run(start_minutes=10)
    state = announce(run)
    drifted = make_run(
        start_minutes=10,
        energy_kwh=6.5 + energy_delta,
        power_kw=8.7 + power_delta,
    )

    assert next_activity(previous=state, runs=(drifted,), now=NOW) is None


def test_a_boundary_cannot_flap_the_way_a_bucket_could() -> None:
    """The old failure mode, stated as a property.

    Bucket-and-hash fired whenever a value crossed a boundary, however small the
    step. A deadband is measured from the *announced* value, so repeated tiny
    drifts in the same direction stay silent until they add up to something real.
    """
    run = make_run(start_minutes=10, energy_kwh=6.5)
    state = announce(run)

    for step in range(1, 5):
        nudged = make_run(start_minutes=10, energy_kwh=6.5 + 0.05 * step)
        assert next_activity(previous=state, runs=(nudged,), now=NOW) is None


@pytest.mark.parametrize(
    ("energy_kwh", "power_kw"),
    [(6.5 + 0.3, 8.7), (6.5, 8.7 + 0.3), (6.5 - 1.0, 8.7)],
)
def test_a_change_beyond_a_deadband_speaks_exactly_once(
    energy_kwh: float, power_kw: float
) -> None:
    """One entry, and then silence until it moves again."""
    run = make_run(start_minutes=10)
    state = announce(run)
    changed = make_run(start_minutes=10, energy_kwh=energy_kwh, power_kw=power_kw)

    entry = next_activity(previous=state, runs=(changed,), now=NOW)
    assert entry is not None
    assert entry.kind == ECONOMIC_EVENT_CHANGED
    assert "has changed its plan" in entry.message

    assert next_activity(previous=entry.state, runs=(changed,), now=NOW) is None


def test_a_running_run_is_not_re_announced_for_its_shrinking_energy() -> None:
    """Content is only compared while the run has not started.

    Once it is under way its remaining energy and power decay as the horizon
    consumes it. Comparing those would reproduce the spam exactly.
    """
    start = NOW - timedelta(minutes=30)
    running = PlannedRun(
        identity=RunIdentity(direction=ECONOMIC_DIRECTION_CHARGE, start_utc=start),
        content=make_run(start_minutes=-30, duration_minutes=120).content,
    )
    state = announce(running)
    shrunk = PlannedRun(
        identity=running.identity,
        content=make_run(
            start_minutes=-30, duration_minutes=120, energy_kwh=1.0, power_kw=2.0
        ).content,
    )

    assert next_activity(previous=state, runs=(shrunk,), now=NOW) is None


def test_a_running_run_that_ends_much_earlier_does_speak() -> None:
    """The one change that matters during a run: it is being cut short."""
    start = NOW - timedelta(minutes=30)
    running = PlannedRun(
        identity=RunIdentity(direction=ECONOMIC_DIRECTION_CHARGE, start_utc=start),
        content=make_run(start_minutes=-30, duration_minutes=120).content,
    )
    state = announce(running)
    cut_short = PlannedRun(
        identity=running.identity,
        content=make_run(start_minutes=-30, duration_minutes=35).content,
    )

    entry = next_activity(previous=state, runs=(cut_short,), now=NOW)
    assert entry is not None
    assert entry.kind == ECONOMIC_EVENT_CHANGED


# ===========================================================================
# D. retraction
# ===========================================================================


def test_a_future_run_that_disappears_is_cancelled_once() -> None:
    """An announcement left standing when the plan dropped it would be a lie."""
    run = make_run(start_minutes=10)
    state = announce(run)

    entry = next_activity(previous=state, runs=(), now=NOW)
    assert entry is not None
    assert entry.kind == ECONOMIC_EVENT_CANCELLED
    assert "no longer plans to" in entry.message
    assert "before its window opened" in entry.message

    assert next_activity(previous=entry.state, runs=(), now=NOW) is None


def test_a_run_whose_window_has_passed_is_ended_once() -> None:
    """Its window elapsed rather than its advice being withdrawn.

    Announced while it was under way -- which is the only way a past run can have
    been announced at all, since a finished one is never back-dated -- and then
    evaluated after its window closed.
    """
    run = make_run(start_minutes=-30, duration_minutes=60)
    state = announce(run)
    later = NOW + timedelta(minutes=45)

    entry = next_activity(previous=state, runs=(), now=later)
    assert entry is not None
    assert entry.kind == ECONOMIC_EVENT_ENDED
    assert "has finished the planned window" in entry.message

    assert next_activity(previous=entry.state, runs=(), now=later) is None


def test_a_direction_reversal_is_a_cancellation_and_a_new_run() -> None:
    """A charge that becomes a discharge is not the same decision changed."""
    charge = make_run(start_minutes=10, action=ECONOMIC_ACTION_CHARGE)
    state = announce(charge)
    discharge = make_run(start_minutes=10, action=ECONOMIC_ACTION_DISCHARGE)

    first = next_activity(previous=state, runs=(discharge,), now=NOW)
    assert first is not None
    assert first.kind == ECONOMIC_EVENT_CANCELLED

    second = next_activity(previous=first.state, runs=(discharge,), now=NOW)
    assert second is not None
    assert second.kind == ECONOMIC_EVENT_PLANNED


# ===========================================================================
# E. never back-date, and at most one entry per refresh
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
    assert "part way through" in entry.message

    assert next_activity(previous=entry.state, runs=(running,), now=NOW) is None


def test_only_one_entry_is_produced_per_refresh() -> None:
    """Even when several things happened, and the rest self-heal next refresh."""
    # Announced ten minutes ago, when it was fifteen minutes from starting; it
    # has still not begun, so its withdrawal is a cancellation rather than an end.
    gone = make_run(start_minutes=5, duration_minutes=60)
    state = announce(gone, now=NOW - timedelta(minutes=10))
    arriving = make_run(start_minutes=5, action=ECONOMIC_ACTION_DISCHARGE)

    first = next_activity(previous=state, runs=(arriving,), now=NOW)
    assert first is not None
    assert first.kind == ECONOMIC_EVENT_CANCELLED

    second = next_activity(previous=first.state, runs=(arriving,), now=NOW)
    assert second is not None
    assert second.kind == ECONOMIC_EVENT_PLANNED


def test_a_distant_run_beside_an_imminent_one_stays_silent() -> None:
    """Only the imminent one is news."""
    imminent = make_run(start_minutes=10)
    distant = make_run(start_minutes=600, action=ECONOMIC_ACTION_DISCHARGE)

    entry = next_activity(previous=None, runs=(imminent, distant), now=NOW)
    assert entry is not None
    assert entry.kind == ECONOMIC_EVENT_PLANNED

    assert (
        next_activity(previous=entry.state, runs=(imminent, distant), now=NOW) is None
    )


def test_the_announced_set_is_bounded() -> None:
    """A plan publishes at most eight runs, so remembering more is unreachable."""
    from custom_components.alpha_ems_manager.const import MAX_ECONOMIC_RUNS_TRACKED

    state = ActivityState()
    for index in range(MAX_ECONOMIC_RUNS_TRACKED + 4):
        state = state.with_announced(make_run(start_minutes=index * 30))

    assert len(state.announced) == MAX_ECONOMIC_RUNS_TRACKED


# ===========================================================================
# F. what the sentence says
# ===========================================================================


def test_a_charge_run_says_where_its_energy_comes_from() -> None:
    """So "charge 6.5 kWh" cannot read as "buy 6.5 kWh"."""
    solar = make_run(start_minutes=10, charge_source=ECONOMIC_CHARGE_SOURCE_PRODUCTION)
    entry = next_activity(previous=None, runs=(solar,), now=NOW)

    assert entry is not None
    assert "almost entirely from your own production" in entry.message


def test_every_entry_carries_the_advisory_qualifier() -> None:
    """No wording may imply the battery did anything."""
    run = make_run(start_minutes=10)
    planned = next_activity(previous=None, runs=(run,), now=NOW)
    cancelled = next_activity(previous=planned.state, runs=(), now=NOW)
    ended = next_activity(
        previous=announce(make_run(start_minutes=-10, duration_minutes=30)),
        runs=(),
        now=NOW + timedelta(minutes=45),
    )

    for entry in (planned, cancelled, ended):
        assert entry is not None
        assert "Advisory only: this release sends no command." in entry.message
        assert "started" not in entry.message


def test_a_refused_run_says_what_it_wanted_and_what_is_possible() -> None:
    """The capability gap, in a sentence."""
    run = make_run(start_minutes=10, action=ECONOMIC_ACTION_EXPORT, refused=True)
    entry = next_activity(previous=None, runs=(run,), now=NOW)

    assert entry is not None
    assert entry.kind == ECONOMIC_EVENT_REFUSED
    assert "wants to export to the grid" in entry.message
    assert "No actuator in this release can do that" in entry.message


def test_the_execution_kind_is_still_refused() -> None:
    """``started`` remains the one thing this surface may never say."""
    from custom_components.alpha_ems_manager.activity import ActivityEntry

    assert CONTROL_EXECUTION_AVAILABLE is False
    entry = ActivityEntry(
        kind=ECONOMIC_EVENT_STARTED, message="anything", state=ActivityState()
    )

    with pytest.raises(ValueError, match="executes nothing"):
        logbook_payload(entry, domain="alpha_ems_manager", entity_id="sensor.x")


def test_cancelled_is_now_an_advice_kind_and_is_accepted() -> None:
    """Withdrawing advice that never began is advice, not execution."""
    from custom_components.alpha_ems_manager.activity import ActivityEntry

    entry = ActivityEntry(
        kind=ECONOMIC_EVENT_CANCELLED, message="withdrawn", state=ActivityState()
    )
    payload = logbook_payload(
        entry, domain="alpha_ems_manager", entity_id="sensor.alpha_ems_economic_action"
    )

    assert payload["entity_id"] == "sensor.alpha_ems_economic_action"


def test_the_module_reads_no_clock_of_its_own() -> None:
    """``now`` is a value. That is what makes every case above deterministic."""
    import inspect

    from custom_components.alpha_ems_manager import activity

    source = inspect.getsource(activity)
    for forbidden in ("utcnow", "dt_util", "datetime.now(", "time.time"):
        assert forbidden not in source, forbidden
