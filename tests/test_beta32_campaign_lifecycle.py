"""One economic campaign: Planned once, Started at most once, exactly one terminal.

Whatever the segment structure inside it. A discharge campaign spanning thirteen
intervals contains export stretches and ``serve_load`` gaps; Stage B necessarily
executes it as several windows; the user made **one** decision and should read one
story about it.

**Why this needed a new layer rather than a tighter tolerance.** Since beta.29 the
hardware is armed from the admitted quarter and stopped from the 60-second tick.
``Decision`` stopped being the executor two releases ago, and the Activity terminal
was still derived from it -- so the ending happened on a tick that wipes the
carriers and publishes no coordinator data at all, and the next refresh had
``intent: None`` and returned no view. The measured 17:30-17:45 export therefore
terminated in **silence**, with its Planned line left standing as though still
true. That is R10/R11, and no adjustment to a string or a tolerance could reach it.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.alpha_ems_manager import activity as activity_module
from custom_components.alpha_ems_manager.activity import (
    ActivityState,
    ExecutionView,
    Lifecycle,
    PlanIdentity,
    PlannedRun,
    RunContent,
    RunIdentity,
    TerminalView,
    next_activity,
    plan_id_for,
)
from custom_components.alpha_ems_manager.const import (
    ACTIVITY_CATEGORY_ECONOMIC_SELL,
    ECONOMIC_ACTION_DISCHARGE,
    ECONOMIC_ACTION_EXPORT,
    ECONOMIC_DIRECTION_DISCHARGE,
    ECONOMIC_EVENT_CANCELLED,
    ECONOMIC_EVENT_ERROR,
    ECONOMIC_EVENT_FINISHED,
    ECONOMIC_EVENT_PLANNED,
    ECONOMIC_EVENT_STARTED,
    ECONOMIC_FINGERPRINT_CHARS,
    EXECUTION_INTENT_NET_EXPORT,
    EXECUTION_STOP_PLAN_REPLACED,
    OUTCOME_CANCELED,
    OUTCOME_FAILED,
    OUTCOME_PARTIAL,
    OUTCOME_SUCCESS,
)
from custom_components.alpha_ems_manager.economic import campaign_identity

NOW = datetime(2026, 8, 28, 17, 30, tzinfo=UTC)
QUARTER = timedelta(minutes=15)
#: The campaign from the live 17:45 diagnostic: intervals 0-12, meter 2.648 kWh.
CAMPAIGN_END = NOW + 13 * QUARTER
CAMPAIGN_METER_KWH = 2.648


def sell_run(*, end: datetime = CAMPAIGN_END, meter: float = CAMPAIGN_METER_KWH):
    """Return the campaign as Activity sees it: one run, at the meter boundary."""
    return PlannedRun(
        identity=RunIdentity(direction=ECONOMIC_DIRECTION_DISCHARGE, start_utc=NOW),
        content=RunContent(
            category=ACTIVITY_CATEGORY_ECONOMIC_SELL,
            energy_kwh=meter,
            end_utc=end,
            window="17:30-20:45",
            executable=True,
        ),
    )


def view(
    *,
    end: datetime = CAMPAIGN_END,
    realized: float = 0.0,
    target: float = CAMPAIGN_METER_KWH,
    running: bool = True,
    activation_confirmed: bool = False,
    stop_reason: str | None = None,
    terminal: TerminalView | None = None,
    run_id: str | None = "run-1",
    campaign_open: bool = True,
) -> ExecutionView:
    """Return Stage B's narrow view of the campaign.

    ``campaign_open`` defaults to true because that is what a refresh inside a
    campaign looks like, and it is the flag that stops a ``serve_load`` gap from
    being read as an ending. The closing refreshes below pass it false, exactly as
    the coordinator does once it latches the outcome.
    """
    return ExecutionView(
        identity=RunIdentity(direction=ECONOMIC_DIRECTION_DISCHARGE, start_utc=NOW),
        end_utc=end,
        running=running,
        executed=True,
        objective_target_kwh=target,
        objective_realized_kwh=realized,
        intent=EXECUTION_INTENT_NET_EXPORT,
        stop_reason=stop_reason,
        run_id=run_id,
        activation_confirmed=activation_confirmed,
        terminal=terminal,
        campaign_open=campaign_open and terminal is None,
    )


def feed(steps) -> list[tuple[str, str]]:
    """Drive the lifecycle through a sequence, returning the lines it filed."""
    state: ActivityState | None = None
    lines: list[tuple[str, str]] = []
    for moment, runs, execution in steps:
        entry = next_activity(
            previous=state, runs=runs, now=moment, execution=execution
        )
        if entry is not None:
            lines.append((entry.kind, entry.message))
            state = entry.state
        elif state is None:
            state = ActivityState()
    return lines


def terminal_of(outcome: str, *, realized: float, reason: str | None = None):
    """Return a latched campaign outcome, as the coordinator computes it."""
    return TerminalView(
        campaign_id="camp01",
        outcome=outcome,
        objective_target_kwh=CAMPAIGN_METER_KWH,
        objective_realized_kwh=realized,
        objective_boundary="meter",
        reason=reason,
    )


# ===========================================================================
# A. one lifecycle, whatever the segment structure
# ===========================================================================


def test_three_export_segments_with_two_gaps_produce_exactly_three_lines() -> None:
    """Planned, Started, one terminal. Not nine, and not three per segment.

    The campaign runs 17:30-20:45 with export stretches separated by two
    ``serve_load`` quarters where the house eats everything the pack gives it. Each
    gap is a refresh where Stage B holds no target and commands nothing, and each
    later segment is a fresh activation. **None of them is an event**: ``started``
    is already true for this lifecycle, so ``_started_entry`` returns ``None`` --
    the same structural deduplication beta.31 already relies on.
    """
    run = sell_run()
    lines = feed(
        [
            # announced one interval ahead
            (NOW - QUARTER, (run,), None),
            # segment one activates
            (NOW, (run,), view(activation_confirmed=True)),
            # ...continues
            (NOW + QUARTER, (run,), view(realized=0.6)),
            # a serve_load gap: no target, nothing commanded, no line
            (NOW + 2 * QUARTER, (run,), view(realized=0.6, running=False)),
            # segment two activates -- a fresh activation, and still silent
            (NOW + 3 * QUARTER, (run,), view(realized=0.6, activation_confirmed=True)),
            (NOW + 4 * QUARTER, (run,), view(realized=1.4)),
            # the second gap
            (NOW + 5 * QUARTER, (run,), view(realized=1.4, running=False)),
            # segment three, and the campaign closes on its own figures
            (NOW + 6 * QUARTER, (run,), view(realized=1.4, activation_confirmed=True)),
            (
                NOW + 13 * QUARTER,
                (run,),
                view(
                    realized=CAMPAIGN_METER_KWH,
                    running=False,
                    terminal=terminal_of(OUTCOME_SUCCESS, realized=CAMPAIGN_METER_KWH),
                ),
            ),
        ]
    )

    kinds = [kind for kind, _ in lines]
    assert kinds == [
        ECONOMIC_EVENT_PLANNED,
        ECONOMIC_EVENT_STARTED,
        ECONOMIC_EVENT_FINISHED,
    ]
    assert "2.65 kWh" in lines[0][1]
    # The Started line quotes the **meter** target, not a battery ceiling. beta.31
    # published "Tracking 0.25 kWh" beside "Planned ... 0.11 kWh" for one run.
    assert "Tracking 2.65 kWh" in lines[1][1]
    assert "Success — Target Reached — 2.65 / 2.65 kWh" in lines[2][1]


def test_a_started_campaign_always_receives_exactly_one_terminal() -> None:
    """Over every outcome the coordinator can latch. A property, not four cases.

    A start that goes unrecorded leaves the later terminal referring to a plan id
    the history has never seen; a terminal that never arrives leaves the Planned
    line standing as though still true. Both were live faults, so the invariant is
    asserted for the whole outcome vocabulary at once.
    """
    for outcome, realized, reason in (
        (OUTCOME_SUCCESS, CAMPAIGN_METER_KWH, None),
        (OUTCOME_PARTIAL, 1.80, None),
        (OUTCOME_CANCELED, 1.80, EXECUTION_STOP_PLAN_REPLACED),
        (OUTCOME_FAILED, 0.40, None),
    ):
        run = sell_run()
        lines = feed(
            [
                (NOW - QUARTER, (run,), None),
                (NOW, (run,), view(activation_confirmed=True)),
                (
                    NOW + QUARTER,
                    (run,),
                    view(
                        realized=realized,
                        running=False,
                        terminal=terminal_of(outcome, realized=realized, reason=reason),
                    ),
                ),
                # And a hundred further refreshes carrying the same latch: silence.
                (
                    NOW + 2 * QUARTER,
                    (run,),
                    view(
                        realized=realized,
                        running=False,
                        terminal=terminal_of(outcome, realized=realized, reason=reason),
                    ),
                ),
            ]
        )
        kinds = [kind for kind, _ in lines]
        assert len(kinds) == 3, (outcome, lines)
        assert kinds[0] == ECONOMIC_EVENT_PLANNED
        assert kinds[1] == ECONOMIC_EVENT_STARTED
        assert kinds[2] in {
            ECONOMIC_EVENT_FINISHED,
            ECONOMIC_EVENT_CANCELLED,
            ECONOMIC_EVENT_ERROR,
        }


def test_a_partial_delivery_is_named_partial_and_quotes_the_frozen_target() -> None:
    """A vanishing later segment is not a retroactive success.

    **The target is frozen at Started and may never shrink** -- the beta.32
    immutability invariant, applied here. A plan that promised 2.65 kWh and
    delivered 1.80 because Stage A changed its mind is ``Partial -- 1.80 / 2.65``,
    not a satisfied ``1.80 / 1.80``. Revisions may add information; they may never
    reduce the target or reset the realised figure.
    """
    run = sell_run()
    lines = feed(
        [
            (NOW - QUARTER, (run,), None),
            (NOW, (run,), view(activation_confirmed=True)),
            (
                NOW + QUARTER,
                (run,),
                view(
                    realized=1.80,
                    running=False,
                    terminal=terminal_of(OUTCOME_PARTIAL, realized=1.80),
                ),
            ),
        ]
    )
    assert lines[-1][0] == ECONOMIC_EVENT_FINISHED
    assert "Partial — 1.80 / 2.65 kWh" in lines[-1][1]
    assert "Success" not in lines[-1][1]


def test_a_failure_reads_as_failed_rather_than_finished_with_an_error() -> None:
    """``Finished ... — Error`` was self-contradictory in four words.

    The event *kind* is unchanged, so ``logbook_payload``'s refusal guard and every
    enum option are untouched: what changed is the sentence.
    """
    run = sell_run()
    lines = feed(
        [
            (NOW - QUARTER, (run,), None),
            (NOW, (run,), view(activation_confirmed=True)),
            (
                NOW + QUARTER,
                (run,),
                view(
                    realized=0.4,
                    running=False,
                    terminal=TerminalView(
                        campaign_id="camp01",
                        outcome=OUTCOME_FAILED,
                        objective_target_kwh=CAMPAIGN_METER_KWH,
                        objective_realized_kwh=0.4,
                        measurable=False,
                    ),
                ),
            ),
        ]
    )
    assert lines[-1][0] == ECONOMIC_EVENT_ERROR
    assert lines[-1][1].startswith("Failed Plan ID:")
    # An untrustworthy measurement outranks even a met objective, and says so.
    assert "Measurement Unavailable" in lines[-1][1]
    assert "Finished" not in lines[-1][1]


def test_a_terminal_arrives_with_no_live_intent_at_all() -> None:
    """The incident refresh, reproduced: nothing running, and it still speaks.

    A campaign ends on the 60-second tick, which wipes ``_carried``, ``_quarter``
    and ``_plan`` and publishes no coordinator data. The next refresh therefore has
    no intent, and every beta.31 path returned ``None`` there -- which is precisely
    why the 17:30-17:45 export terminated in silence.
    """
    run = sell_run()
    lines = feed(
        [
            (NOW - QUARTER, (run,), None),
            (NOW, (run,), view(activation_confirmed=True)),
            # No identity, no end, no intent -- a terminal-only view.
            (
                NOW + QUARTER,
                (),
                ExecutionView(
                    executed=True,
                    identity=RunIdentity(
                        direction=ECONOMIC_DIRECTION_DISCHARGE, start_utc=NOW
                    ),
                    end_utc=CAMPAIGN_END,
                    run_id="run-1",
                    terminal=terminal_of(OUTCOME_SUCCESS, realized=CAMPAIGN_METER_KWH),
                ),
            ),
        ]
    )
    assert [kind for kind, _ in lines] == [
        ECONOMIC_EVENT_PLANNED,
        ECONOMIC_EVENT_STARTED,
        ECONOMIC_EVENT_FINISHED,
    ]


def test_the_observed_0_096_of_0_11_kwh_is_a_success() -> None:
    """The measured shortfall was 0.56 of one actuator step. It is a Success.

    beta.31 filed it as ``Canceled -- Plan Replaced -- 0.00 / 0.19 kWh -- Advisory``
    after real execution: a wrong class, a wrong pair of figures and a false
    marker on one line. The class is now computed where the energy was measured,
    against a tolerance scaled per admitted quarter -- so a residue no command
    could have closed does not become a report of failure.
    """
    run = sell_run(meter=0.11)
    lines = feed(
        [
            (NOW - QUARTER, (run,), None),
            (NOW, (run,), view(target=0.11, activation_confirmed=True)),
            (
                NOW + QUARTER,
                (run,),
                view(
                    target=0.11,
                    realized=0.096,
                    running=False,
                    terminal=TerminalView(
                        campaign_id="camp01",
                        outcome=OUTCOME_SUCCESS,
                        objective_target_kwh=0.11,
                        objective_realized_kwh=0.096,
                        objective_boundary="meter",
                    ),
                ),
            ),
        ]
    )
    assert lines[-1][0] == ECONOMIC_EVENT_FINISHED
    assert "Success — Target Reached — 0.10 / 0.11 kWh" in lines[-1][1]
    assert "Advisory" not in lines[-1][1]


# ===========================================================================
# B. identity that survives twenty re-solves
# ===========================================================================


def test_the_campaign_id_is_stable_across_twenty_refreshes() -> None:
    """The head advances every refresh; the end does not.

    ``EconomicInterval.index`` is day-absolute within the plan's *target day* and
    rebases at midnight, so an index-derived identity is stable within a day and
    silently different across the boundary. And anchoring on the *start* is what
    made the beta.29/beta.30 plan ids churn: the horizon head is
    ``elapsed_intervals + 1``, so a campaign already under way loses its leading
    interval every fifteen minutes.
    """
    ids = {
        campaign_identity(ECONOMIC_DIRECTION_DISCHARGE, CAMPAIGN_END) for _ in range(20)
    }
    assert len(ids) == 1
    # A start that has advanced by eight quarters changes nothing.
    assert campaign_identity(ECONOMIC_DIRECTION_DISCHARGE, CAMPAIGN_END) in ids
    # A different direction over the same window is a different campaign: the money
    # is moving the other way.
    assert campaign_identity("charge", CAMPAIGN_END) not in ids
    # And seconds cannot enter it -- the end is a quarter boundary, so sub-minute
    # resolution could only carry noise from whichever clock resolved the instant.
    assert (
        campaign_identity(ECONOMIC_DIRECTION_DISCHARGE, CAMPAIGN_END.replace(second=37))
        in ids
    )


def test_a_restart_recomputes_the_same_campaign_id() -> None:
    """Derived from the identity, never minted from a counter.

    A reload mid-campaign recovers the same id, so a history does not show one sale
    under two names -- and a reader with the direction and the end instant can
    compute it from a diagnostic and confirm which lines belong together.
    """
    before = campaign_identity(ECONOMIC_DIRECTION_DISCHARGE, CAMPAIGN_END)
    after = campaign_identity(ECONOMIC_DIRECTION_DISCHARGE, CAMPAIGN_END)
    assert before == after
    # The same fingerprint width every economic identity in this codebase uses, so
    # a reader comparing a campaign id against a plan id is comparing like with
    # like rather than wondering which one was truncated.
    assert len(before) == ECONOMIC_FINGERPRINT_CHARS


def test_one_trimmed_interval_keeps_one_lifecycle() -> None:
    """A tail trimmed by one interval is the same campaign, not a new one.

    One interval is precisely the drift a re-solve introduces as demand is revised.
    The tolerance is what absorbs it -- and the ``campaign_id`` is what stops the
    tolerance from being widened until it starts matching *different* campaigns,
    which is how a 900-second window came to self-match the next sale.
    """
    first = sell_run()
    trimmed = sell_run(end=CAMPAIGN_END - QUARTER)
    lines = feed(
        [
            (NOW - QUARTER, (first,), None),
            (NOW, (trimmed,), None),
            (NOW + QUARTER, (trimmed,), None),
        ]
    )
    assert [kind for kind, _ in lines] == [ECONOMIC_EVENT_PLANNED]


def test_a_campaign_ending_an_hour_later_is_a_different_lifecycle() -> None:
    """The other half of the tolerance: a genuinely different window is separate.

    ``<= 900 s`` was the beta.31 tolerance, and 900 s is exactly one interval -- so a
    lifecycle anchored on 17:45 matched the next campaign ending at 18:00 and never
    retracted. Tightening the number would split a legitimately trimmed campaign;
    the fix is an identity, and this is the half that proves it still discriminates.
    """
    identity = PlanIdentity(
        category=ACTIVITY_CATEGORY_ECONOMIC_SELL, end_utc=CAMPAIGN_END
    )
    later = PlanIdentity(
        category=ACTIVITY_CATEGORY_ECONOMIC_SELL, end_utc=CAMPAIGN_END + 4 * QUARTER
    )
    state = ActivityState(
        open=(
            Lifecycle(
                plan_id=plan_id_for(identity),
                identity=identity,
                direction=ECONOMIC_DIRECTION_DISCHARGE,
                energy_kwh=CAMPAIGN_METER_KWH,
                window="17:30-20:45",
            ),
        )
    )
    assert state.find(identity) is not None
    assert state.find(later) is None


# ===========================================================================
# C. one category per campaign
# ===========================================================================


def test_a_campaign_that_sells_gets_one_category_whatever_the_label_does() -> None:
    """The label alternates; the campaign does not.

    ``runs_from`` splits on the action label and the label flips between
    ``discharge`` and ``export`` as house load rises and falls beneath one physical
    discharge. Deriving the Activity category from the label therefore gave one
    campaign two categories -- and a category is half of ``PlanIdentity``, so it gave
    one sale two lifecycles, two Planned lines and two terminals.
    """
    selling = activity_module.category_of(ECONOMIC_ACTION_DISCHARGE, None, sells=True)
    exporting = activity_module.category_of(ECONOMIC_ACTION_EXPORT, None, sells=True)
    assert selling == exporting == ACTIVITY_CATEGORY_ECONOMIC_SELL

    # And a campaign that sells nothing is not a sale: it is self-consumption,
    # which is inverter behaviour and not an event.
    consuming = activity_module.category_of(
        ECONOMIC_ACTION_DISCHARGE, None, sells=False
    )
    assert consuming != ACTIVITY_CATEGORY_ECONOMIC_SELL

    # ``None`` keeps the label-derived answer, so a pre-campaign caller still
    # describes the behaviour it was written for.
    assert activity_module.category_of(
        ECONOMIC_ACTION_DISCHARGE, None
    ) == activity_module.category_of(ECONOMIC_ACTION_DISCHARGE, None, sells=False)


def test_the_completion_tolerance_has_left_the_activity_surface() -> None:
    """Stated as its own test, because it is the architectural claim.

    A presentation layer holding ``TARGET_TOLERANCE_KWH`` is a renderer deciding
    whether 0.014 kWh of residue was a success. The outcome class is now computed
    where the energy was measured; this module renders four shapes and decides
    nothing.
    """
    source = activity_module.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    # Named in a docstring explaining its removal is fine; imported is not.
    assert "from .execution import" not in text
    assert not hasattr(activity_module, "TARGET_TOLERANCE_KWH")


def test_activity_still_carries_no_power_price_or_reserve_arithmetic() -> None:
    """The beta.31 guarantee, re-asserted over the beta.32 fields.

    ``RunContent`` and ``TerminalView`` between them carry a category, one energy
    pair, a window, an instant and an outcome. What is missing is the design: no
    power, no price, no expected value, no charge-source prose. Adding a field here
    is how the old three-clause sentence comes back.
    """
    content_fields = set(RunContent.__dataclass_fields__)
    assert content_fields == {
        "category",
        "energy_kwh",
        "end_utc",
        "window",
        "executable",
    }
    terminal_fields = set(TerminalView.__dataclass_fields__)
    assert terminal_fields == {
        "campaign_id",
        "outcome",
        "objective_target_kwh",
        "objective_realized_kwh",
        "objective_boundary",
        "reason",
        "measurable",
        "started",
    }
    # And the pair that used to let a renderer choose a boundary is gone.
    view_fields = set(ExecutionView.__dataclass_fields__)
    assert "target_kwh" not in view_fields
    assert "delivered_kwh" not in view_fields


def test_no_campaign_line_mentions_a_power_or_a_price() -> None:
    """Over every outcome, and over the whole rendered sentence."""
    run = sell_run()
    for outcome in (OUTCOME_SUCCESS, OUTCOME_PARTIAL, OUTCOME_CANCELED, OUTCOME_FAILED):
        lines = feed(
            [
                (NOW - QUARTER, (run,), None),
                (NOW, (run,), view(activation_confirmed=True)),
                (
                    NOW + QUARTER,
                    (run,),
                    view(
                        realized=1.8,
                        running=False,
                        terminal=terminal_of(outcome, realized=1.8),
                    ),
                ),
            ]
        )
        for _kind, message in lines:
            lowered = message.lower()
            # ``kw`` is a substring of ``kwh``, so the power unit is matched as a
            # whole word -- the energy figure is the one thing a line *may* carry.
            assert not re.search(r"\bkw\b", lowered), message
            for term in ("eur", "€", "price", "expected", "reserve"):
                assert term not in lowered, message


def test_an_immaterial_campaign_is_never_announced() -> None:
    """A campaign that sells nothing is not an event, so it produces no line.

    The correct reading of "one meaningful sell campaign per day": campaigns that
    sell nothing are not sells. And the suppression touches announcements only --
    if such a campaign is executable it still executes, because the energy was
    already removed at source by the export-label deadband and the actuator rule
    is what decides executability.
    """
    quiet = PlannedRun(
        identity=RunIdentity(direction=ECONOMIC_DIRECTION_DISCHARGE, start_utc=NOW),
        content=RunContent(
            category=ACTIVITY_CATEGORY_ECONOMIC_SELL,
            energy_kwh=0.0,
            end_utc=CAMPAIGN_END,
            window="17:30-20:45",
            executable=True,
        ),
    )
    # The sensor filters an immaterial campaign out before Activity ever sees it,
    # so the invariant asserted here is that Activity is handed nothing -- an empty
    # run tuple must produce silence rather than a line about zero kilowatt-hours.
    assert next_activity(previous=None, runs=(), now=NOW, execution=None) is None
    # And with the campaign present it *would* be announced, which is what makes
    # the filter the load-bearing part rather than a coincidence.
    entry = next_activity(previous=None, runs=(quiet,), now=NOW, execution=None)
    assert entry is not None
    assert entry.kind == ECONOMIC_EVENT_PLANNED
    assert "0.00 kWh" in entry.message


@pytest.mark.parametrize(
    "outcome", [OUTCOME_SUCCESS, OUTCOME_PARTIAL, OUTCOME_CANCELED, OUTCOME_FAILED]
)
def test_a_campaign_that_never_started_receives_no_terminal(outcome: str) -> None:
    """Nothing physical happened, so there is nothing to have finished.

    A plan withdrawn before its window opened moved no energy, and quoting
    ``0.00 / 2.65 kWh`` beside it would invite a reader to look for a fault where
    there is only a change of mind.
    """
    run = sell_run()
    lines = feed(
        [
            (NOW - QUARTER, (run,), None),
            # A latch arrives, but the lifecycle was never started.
            (
                NOW,
                (run,),
                view(
                    running=False,
                    terminal=TerminalView(
                        campaign_id="camp01",
                        outcome=outcome,
                        objective_target_kwh=CAMPAIGN_METER_KWH,
                        objective_realized_kwh=0.0,
                        started=False,
                    ),
                ),
            ),
        ]
    )
    assert [kind for kind, _ in lines] == [ECONOMIC_EVENT_PLANNED]
