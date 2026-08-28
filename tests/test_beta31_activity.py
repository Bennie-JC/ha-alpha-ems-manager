"""One plan, one lifecycle: the Activity feed rebuilt from the live export.

**The evidence.** An export of the real Activity history held 185 rows, 79 of them
logbook messages, and roughly 47 of those 79 were churn about plans that had not
changed. The clearest case, and the one several tests here are built from: a
single charge campaign ending at 16:15 announced itself planned and then
"finished" **six times** while its start slid 08:45 -> 09:15 -> 10:30 -> 11:15 ->
11:45 -> 12:15 and its energy shrank 13.33 -> 13.06 -> 11.67 -> 11.11 -> 10.83 ->
11.11 kWh.

Every one of those twelve lines was false in the same way. Nothing new had been
planned -- the horizon head had advanced, which is what a horizon head does -- and
nothing had *finished*: the announcement had been superseded, which is not the
same thing as a campaign completing.

**The measurement.** Identity was ``(direction, start_utc)`` and the horizon head
is ``elapsed_intervals + 1``, so the start of a running campaign advances on every
refresh. The end does not. Anchoring the lifecycle on the end collapses all six
announcements into one, and that is the substance of the change.

Every case here drives the real :func:`next_activity` against fixed instants. The
module reads no clock, which is what makes that possible.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.alpha_ems_manager.activity import (
    ActivityState,
    ExecutionView,
    PlanIdentity,
    PlannedRun,
    RunContent,
    RunIdentity,
    category_of,
    logbook_payload,
    materially_changed,
    next_activity,
    plan_id_for,
)
from custom_components.alpha_ems_manager.const import (
    ACTIVITY_CATEGORIES,
    ACTIVITY_CATEGORY_ECONOMIC_BUY,
    ACTIVITY_CATEGORY_ECONOMIC_SELL,
    ACTIVITY_CATEGORY_MIXED_BUY,
    ACTIVITY_CATEGORY_SAFETY_BUY,
    BATTERY_ACTION_OPTIONS,
    CONTROL_STATE_OPTIONS,
    ECONOMIC_ACTION_CHARGE,
    ECONOMIC_ACTION_EXPORT,
    ECONOMIC_ACTION_OPTIONS,
    ECONOMIC_ACTION_SAFETY_BUY,
    ECONOMIC_DEADBAND_ENERGY_KWH,
    ECONOMIC_DIRECTION_CHARGE,
    ECONOMIC_DIRECTION_DISCHARGE,
    ECONOMIC_EVENT_CANCELLED,
    ECONOMIC_EVENT_ERROR,
    ECONOMIC_EVENT_FINISHED,
    ECONOMIC_EVENT_PLANNED,
    ECONOMIC_EVENT_STARTED,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_NET_EXPORT,
    EXECUTION_STOP_EXECUTION_ERROR,
    EXECUTION_STOP_OWNERSHIP_CONFLICT,
    EXECUTION_STOP_PLAN_REPLACED,
    EXECUTION_STOP_SWITCHED_TO_SHADOW,
    EXECUTION_STOP_TARGET_REACHED,
    EXECUTION_STOP_WINDOW_ENDED,
    QUARTER_MINUTES,
)

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
QUARTER = timedelta(minutes=QUARTER_MINUTES)

#: The campaign from the export, in the terms the module reads. Its end is the
#: only thing about it that never moved.
CAMPAIGN_END = datetime(2026, 8, 22, 16, 15, tzinfo=UTC)


def run_at(
    *,
    start: datetime,
    end: datetime,
    category: str = ACTIVITY_CATEGORY_SAFETY_BUY,
    energy_kwh: float = 2.22,
    executable: bool = True,
) -> PlannedRun:
    """Return one planned run at absolute instants."""
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
            executable=executable,
        ),
    )


def dispatch(
    *,
    end: datetime,
    direction: str = ECONOMIC_DIRECTION_CHARGE,
    intent: str = EXECUTION_INTENT_GRID_CHARGE,
    objective_target_kwh: float = 2.22,
    objective_realized_kwh: float = 0.0,
    run_id: str = "run-1",
    executed: bool = True,
    activation_confirmed: bool = False,
    running: bool = True,
    stop_reason: str | None = None,
    start: datetime = NOW,
) -> ExecutionView:
    """Return Stage B's narrow view, matched to a plan by direction and end."""
    return ExecutionView(
        identity=RunIdentity(direction=direction, start_utc=start),
        end_utc=end,
        running=running,
        executed=executed,
        objective_target_kwh=objective_target_kwh,
        objective_realized_kwh=objective_realized_kwh,
        intent=intent,
        stop_reason=stop_reason,
        run_id=run_id,
        activation_confirmed=activation_confirmed,
    )


def feed(
    steps: list[tuple[datetime, tuple[PlannedRun, ...], ExecutionView | None]],
    *,
    shadow: bool = False,
) -> list[tuple[str, str]]:
    """Return every ``(kind, message)`` a sequence of refreshes produces.

    One refresh per step, carrying the state forward exactly as the entity does,
    so what comes back is the Activity history a user would actually see.
    """
    state: ActivityState | None = None
    lines: list[tuple[str, str]] = []
    for now, runs, execution in steps:
        entry = next_activity(
            previous=state, runs=runs, now=now, execution=execution, shadow=shadow
        )
        if entry is None:
            continue
        state = entry.state
        lines.append((entry.kind, entry.message))
    return lines


# ===========================================================================
# A. the live failure: the same plan, refresh after refresh
# ===========================================================================


def test_the_export_sequence_now_produces_one_planned_line() -> None:
    """**Test A**, taken verbatim from the exported history.

    Six refreshes, the campaign's start advancing and its energy shrinking on
    each, ending at the same 16:15 throughout. beta.30 produced twelve lines --
    six "plans to charge" and six "has finished the planned window". This produces
    one.
    """
    observed = [
        (datetime(2026, 8, 22, 8, 45, tzinfo=UTC), 13.33),
        (datetime(2026, 8, 22, 9, 15, tzinfo=UTC), 13.06),
        (datetime(2026, 8, 22, 10, 30, tzinfo=UTC), 11.67),
        (datetime(2026, 8, 22, 11, 15, tzinfo=UTC), 11.11),
        (datetime(2026, 8, 22, 11, 45, tzinfo=UTC), 10.83),
        (datetime(2026, 8, 22, 12, 15, tzinfo=UTC), 11.11),
    ]
    steps = [
        (
            start,
            (run_at(start=start, end=CAMPAIGN_END, energy_kwh=energy),),
            None,
        )
        for start, energy in observed
    ]

    lines = feed(steps)

    assert len(lines) == 1, lines
    kind, message = lines[0]
    assert kind == ECONOMIC_EVENT_PLANNED
    # The first refresh's figures, and no later revision restates them.
    assert "13.33 kWh" in message
    assert "08:45-16:15" in message


@pytest.mark.parametrize("refreshes", [2, 5, 10])
def test_identical_refreshes_produce_one_planned_event(refreshes: int) -> None:
    """**Test A**, in the degenerate form: nothing changes at all."""
    run = run_at(start=NOW + QUARTER, end=NOW + 4 * QUARTER)
    steps = [(NOW + timedelta(minutes=i), (run,), None) for i in range(refreshes)]

    assert len(feed(steps)) == 1


def test_a_small_target_revision_is_not_a_new_plan() -> None:
    """**Test B**: the kWh moves, the plan does not.

    Half a bucket, which is inside the deadband, and then a full bucket, which is
    outside it -- and neither speaks, because energy is not part of the identity.
    A revised target for the same campaign in the same window is the optimizer
    doing its job, and the current figure is on the entity where a reader can see
    it move continuously rather than in a logbook where they cannot.
    """
    end = NOW + 4 * QUARTER
    steps = [
        (NOW, (run_at(start=NOW + QUARTER, end=end, energy_kwh=2.22),), None),
        (
            NOW + QUARTER,
            (
                run_at(
                    start=NOW + QUARTER,
                    end=end,
                    energy_kwh=2.22 + ECONOMIC_DEADBAND_ENERGY_KWH / 2,
                ),
            ),
            None,
        ),
        (
            NOW + 2 * QUARTER,
            (
                run_at(
                    start=NOW + QUARTER,
                    end=end,
                    energy_kwh=2.22 + 2 * ECONOMIC_DEADBAND_ENERGY_KWH,
                ),
            ),
            None,
        ),
    ]

    lines = feed(steps)
    assert len(lines) == 1, lines


def test_a_materially_moved_window_replaces_the_plan() -> None:
    """**Test C**: two hours later is a different plan, and says so.

    Old id cancelled as replaced, new id planned exactly once, and the two ids
    differ -- so a history reader can see that one campaign gave way to another
    rather than that one campaign was described twice.
    """
    first_end = NOW + 4 * QUARTER
    second_end = first_end + timedelta(hours=2)
    steps = [
        (NOW, (run_at(start=NOW + QUARTER, end=first_end),), None),
        (NOW + QUARTER, (run_at(start=NOW + 2 * QUARTER, end=second_end),), None),
        (NOW + 2 * QUARTER, (run_at(start=NOW + 2 * QUARTER, end=second_end),), None),
    ]

    lines = feed(steps)

    kinds = [kind for kind, _ in lines]
    assert kinds == [
        ECONOMIC_EVENT_PLANNED,
        ECONOMIC_EVENT_CANCELLED,
        ECONOMIC_EVENT_PLANNED,
    ], lines
    assert "Plan Replaced" in lines[1][1]

    old_id = plan_id_for(
        PlanIdentity(category=ACTIVITY_CATEGORY_SAFETY_BUY, end_utc=first_end)
    )
    new_id = plan_id_for(
        PlanIdentity(category=ACTIVITY_CATEGORY_SAFETY_BUY, end_utc=second_end)
    )
    assert old_id != new_id
    assert old_id in lines[0][1] and old_id in lines[1][1]
    assert new_id in lines[2][1]


@pytest.mark.parametrize("drift_minutes", [0, 5, QUARTER_MINUTES])
def test_a_window_end_drifting_within_one_interval_is_the_same_plan(
    drift_minutes: float,
) -> None:
    """The tolerance, stated directly rather than through a scenario.

    One planning interval, because that is exactly the drift a re-solve
    introduces: the head advances by one and a run's last interval can be trimmed
    or extended by one as demand is revised.
    """
    end = NOW + 8 * QUARTER
    steps = [
        (NOW, (run_at(start=NOW + QUARTER, end=end),), None),
        (
            NOW + QUARTER,
            (run_at(start=NOW + QUARTER, end=end + timedelta(minutes=drift_minutes)),),
            None,
        ),
    ]

    assert len(feed(steps)) == 1


def test_a_category_change_is_a_new_plan_even_in_the_same_window() -> None:
    """**Test C's other half**: the money is being spent for a different reason.

    Same window, same direction, same energy -- and a Safety Buy becoming an
    Economic Buy is a genuinely different decision: one was compulsory and the
    other cleared the economic gates. A history that showed them as one plan would
    hide the only thing about them worth knowing.
    """
    end = NOW + 4 * QUARTER
    steps = [
        (
            NOW,
            (
                run_at(
                    start=NOW + QUARTER, end=end, category=ACTIVITY_CATEGORY_SAFETY_BUY
                ),
            ),
            None,
        ),
        (
            NOW + QUARTER,
            (
                run_at(
                    start=NOW + QUARTER,
                    end=end,
                    category=ACTIVITY_CATEGORY_ECONOMIC_BUY,
                ),
            ),
            None,
        ),
        (
            NOW + 2 * QUARTER,
            (
                run_at(
                    start=NOW + QUARTER,
                    end=end,
                    category=ACTIVITY_CATEGORY_ECONOMIC_BUY,
                ),
            ),
            None,
        ),
    ]

    lines = feed(steps)
    assert [kind for kind, _ in lines] == [
        ECONOMIC_EVENT_PLANNED,
        ECONOMIC_EVENT_CANCELLED,
        ECONOMIC_EVENT_PLANNED,
    ], lines
    assert "Safety Buy Planned" in lines[0][1]
    assert "Economic Buy Planned" in lines[2][1]


# ===========================================================================
# B. the clean lifecycles
# ===========================================================================


def _campaign(
    category: str,
    *,
    direction: str = ECONOMIC_DIRECTION_CHARGE,
    intent: str = EXECUTION_INTENT_GRID_CHARGE,
) -> list[tuple[str, str]]:
    """Return the lines one complete, successful campaign produces."""
    end = NOW + 4 * QUARTER
    run = run_at(start=NOW + QUARTER, end=end, category=category)
    return feed(
        [
            (NOW, (run,), None),
            (
                NOW + QUARTER,
                (run,),
                dispatch(
                    end=end,
                    direction=direction,
                    intent=intent,
                    activation_confirmed=True,
                ),
            ),
            (
                NOW + 2 * QUARTER,
                (run,),
                dispatch(
                    end=end,
                    direction=direction,
                    intent=intent,
                    objective_realized_kwh=1.10,
                ),
            ),
            (
                NOW + 3 * QUARTER,
                (),
                dispatch(
                    end=end,
                    direction=direction,
                    intent=intent,
                    objective_realized_kwh=2.22,
                    running=False,
                    stop_reason=EXECUTION_STOP_TARGET_REACHED,
                ),
            ),
        ]
    )


def test_a_safety_buy_campaign_says_exactly_three_things() -> None:
    """**Test D**: Planned, Buy Started, Finished Success. Four refreshes."""
    lines = _campaign(ACTIVITY_CATEGORY_SAFETY_BUY)

    assert [kind for kind, _ in lines] == [
        ECONOMIC_EVENT_PLANNED,
        ECONOMIC_EVENT_STARTED,
        ECONOMIC_EVENT_FINISHED,
    ], lines
    assert "Safety Buy Planned" in lines[0][1]
    assert "Buy Started — Tracking 2.22 kWh" in lines[1][1]
    assert "Success — Target Reached — 2.22 / 2.22 kWh" in lines[2][1]


def test_an_economic_buy_campaign_reads_the_same_way() -> None:
    """**Test E**: the same clean lifecycle, a different first word."""
    lines = _campaign(ACTIVITY_CATEGORY_ECONOMIC_BUY)

    assert [kind for kind, _ in lines] == [
        ECONOMIC_EVENT_PLANNED,
        ECONOMIC_EVENT_STARTED,
        ECONOMIC_EVENT_FINISHED,
    ], lines
    assert "Economic Buy Planned" in lines[0][1]
    assert "Buy Started" in lines[1][1]


def test_a_sell_campaign_says_sell() -> None:
    """**Test F**: Economic Sell Planned, Sell Started, Finished Success."""
    lines = _campaign(
        ACTIVITY_CATEGORY_ECONOMIC_SELL,
        direction=ECONOMIC_DIRECTION_DISCHARGE,
        intent=EXECUTION_INTENT_NET_EXPORT,
    )

    assert [kind for kind, _ in lines] == [
        ECONOMIC_EVENT_PLANNED,
        ECONOMIC_EVENT_STARTED,
        ECONOMIC_EVENT_FINISHED,
    ], lines
    assert "Economic Sell Planned" in lines[0][1]
    assert "Sell Started" in lines[1][1]


def test_a_mixed_buy_is_named_as_one() -> None:
    """A run that is part compulsory and part economic says so, in two words.

    The internal attribution is a pair of kilowatt-hour figures and stays in
    diagnostics; the Activity line carries the one word that summarises it.
    """
    lines = _campaign(ACTIVITY_CATEGORY_MIXED_BUY)
    assert "Mixed Buy Planned" in lines[0][1]


def test_a_window_that_expires_before_execution_is_cancelled_as_expired() -> None:
    """**Test G**: Planned, then Canceled — Window Expired. Two lines."""
    end = NOW + 2 * QUARTER
    run = run_at(start=NOW + QUARTER, end=end)
    lines = feed(
        [
            (NOW, (run,), None),
            (NOW + QUARTER, (run,), None),
            (end + QUARTER, (), None),
            (end + 2 * QUARTER, (), None),
        ]
    )

    assert [kind for kind, _ in lines] == [
        ECONOMIC_EVENT_PLANNED,
        ECONOMIC_EVENT_CANCELLED,
    ], lines
    assert "Window Expired" in lines[1][1]
    # No figures: nothing was delivered, so quoting 0.00 against the target would
    # invite a reader to look for a fault where there is only an elapsed window.
    assert "kWh" not in lines[1][1]


def test_shadow_never_says_a_dispatch_started() -> None:
    """**Test H**: switching to Shadow before the start emits no physical line.

    And the Planned line marks itself, in one word rather than the sentence
    beta.30 repeated on every entry.
    """
    end = NOW + 4 * QUARTER
    run = run_at(start=NOW + QUARTER, end=end)
    lines = feed(
        [
            (NOW, (run,), None),
            (
                NOW + QUARTER,
                (run,),
                # Everything a Live start needs, except permission.
                dispatch(end=end, executed=False, activation_confirmed=True),
            ),
            (
                NOW + 2 * QUARTER,
                (run,),
                dispatch(end=end, executed=False, activation_confirmed=True),
            ),
        ],
        shadow=True,
    )

    assert [kind for kind, _ in lines] == [ECONOMIC_EVENT_PLANNED], lines
    assert lines[0][1].endswith("— Shadow")
    for _, message in lines:
        assert "Started" not in message
        assert "Success" not in message


def test_ownership_lost_during_execution_ends_the_plan_once() -> None:
    """**Test I**: one terminal event, and the refreshes after it are silent."""
    end = NOW + 4 * QUARTER
    run = run_at(start=NOW + QUARTER, end=end)
    lost = dispatch(
        end=end,
        objective_realized_kwh=0.80,
        running=False,
        stop_reason=EXECUTION_STOP_OWNERSHIP_CONFLICT,
    )
    lines = feed(
        [
            (NOW, (run,), None),
            (NOW + QUARTER, (run,), dispatch(end=end, activation_confirmed=True)),
            (NOW + 2 * QUARTER, (run,), lost),
            (NOW + 3 * QUARTER, (run,), lost),
            (NOW + 4 * QUARTER, (), lost),
        ]
    )

    assert [kind for kind, _ in lines] == [
        ECONOMIC_EVENT_PLANNED,
        ECONOMIC_EVENT_STARTED,
        ECONOMIC_EVENT_CANCELLED,
    ], lines
    assert "Ownership Lost" in lines[2][1]
    # It had started, so the figures answer "how much did it manage".
    assert "0.80 / 2.22 kWh" in lines[2][1]


def test_a_command_failure_is_an_error_and_not_a_cancellation() -> None:
    """The distinction a reader most needs, and it has its own kind."""
    end = NOW + 4 * QUARTER
    run = run_at(start=NOW + QUARTER, end=end)
    lines = feed(
        [
            (NOW, (run,), None),
            (NOW + QUARTER, (run,), dispatch(end=end, activation_confirmed=True)),
            (
                NOW + 2 * QUARTER,
                (run,),
                dispatch(
                    end=end,
                    running=False,
                    stop_reason=EXECUTION_STOP_EXECUTION_ERROR,
                ),
            ),
        ]
    )

    assert lines[-1][0] == ECONOMIC_EVENT_ERROR
    # **``Failed``, since beta.32.** ``Finished ... — Error`` was
    # self-contradictory in four words: a plan does not finish by failing. The
    # event *kind* is unchanged, so nothing a consumer subscribes to moved.
    assert lines[-1][1].startswith("Failed Plan ID:")
    assert "Command Failed" in lines[-1][1]
    assert "Finished" not in lines[-1][1]


def test_refreshes_after_completion_produce_nothing() -> None:
    """**Test J**: no duplicate Finished, and no Planned again for a closed id."""
    end = NOW + 4 * QUARTER
    run = run_at(start=NOW + QUARTER, end=end)
    done = dispatch(
        end=end,
        objective_realized_kwh=2.22,
        running=False,
        stop_reason=EXECUTION_STOP_TARGET_REACHED,
    )
    lines = feed(
        [
            (NOW, (run,), None),
            (NOW + QUARTER, (run,), dispatch(end=end, activation_confirmed=True)),
            (NOW + 2 * QUARTER, (run,), done),
            # Four more refreshes, the stop reason still standing, and the run
            # still in the plan -- which is what a stale publication looks like.
            (NOW + 3 * QUARTER, (run,), done),
            (NOW + 4 * QUARTER, (run,), done),
            (NOW + 5 * QUARTER, (run,), done),
            (NOW + 6 * QUARTER, (run,), done),
        ]
    )

    assert [kind for kind, _ in lines] == [
        ECONOMIC_EVENT_PLANNED,
        ECONOMIC_EVENT_STARTED,
        ECONOMIC_EVENT_FINISHED,
    ], lines


def test_a_plan_replaced_mid_execution_ends_once_and_the_next_is_separate() -> None:
    """**Test K**: the old campaign's terminal, then a new id from scratch."""
    first_end = NOW + 4 * QUARTER
    second_end = first_end + timedelta(hours=3)
    first = run_at(start=NOW + QUARTER, end=first_end)
    second = run_at(start=second_end - 2 * QUARTER, end=second_end)
    lines = feed(
        [
            (NOW, (first,), None),
            (
                NOW + QUARTER,
                (first,),
                dispatch(end=first_end, activation_confirmed=True),
            ),
            (
                NOW + 2 * QUARTER,
                (second,),
                dispatch(
                    end=first_end,
                    objective_realized_kwh=1.40,
                    running=False,
                    stop_reason=EXECUTION_STOP_PLAN_REPLACED,
                ),
            ),
            (second_end - 2 * QUARTER, (second,), None),
        ]
    )

    assert [kind for kind, _ in lines] == [
        ECONOMIC_EVENT_PLANNED,
        ECONOMIC_EVENT_STARTED,
        ECONOMIC_EVENT_CANCELLED,
        ECONOMIC_EVENT_PLANNED,
    ], lines
    assert "Plan Replaced" in lines[2][1]
    first_id = plan_id_for(
        PlanIdentity(category=ACTIVITY_CATEGORY_SAFETY_BUY, end_utc=first_end)
    )
    second_id = plan_id_for(
        PlanIdentity(category=ACTIVITY_CATEGORY_SAFETY_BUY, end_utc=second_end)
    )
    assert first_id != second_id
    assert second_id in lines[3][1]


def test_a_mode_change_during_execution_reads_as_a_mode_change() -> None:
    """A user who switched out of Live is told that, not told about a limit."""
    end = NOW + 4 * QUARTER
    run = run_at(start=NOW + QUARTER, end=end)
    lines = feed(
        [
            (NOW, (run,), None),
            (NOW + QUARTER, (run,), dispatch(end=end, activation_confirmed=True)),
            (
                NOW + 2 * QUARTER,
                (run,),
                dispatch(
                    end=end,
                    objective_realized_kwh=0.55,
                    running=False,
                    stop_reason=EXECUTION_STOP_SWITCHED_TO_SHADOW,
                ),
            ),
        ]
    )

    assert lines[-1][0] == ECONOMIC_EVENT_CANCELLED
    assert "Control Mode Changed" in lines[-1][1]


# ===========================================================================
# C. what a line may and may not contain
# ===========================================================================


#: Every message shape this surface can produce, gathered once so the content
#: rules below are asserted against all of them rather than a chosen sample.
def _every_message() -> list[str]:
    """Return one message of every kind the lifecycle can emit."""
    messages: list[str] = []
    for category in ACTIVITY_CATEGORIES:
        direction = (
            ECONOMIC_DIRECTION_DISCHARGE
            if category in (ACTIVITY_CATEGORY_ECONOMIC_SELL,)
            else ECONOMIC_DIRECTION_CHARGE
        )
        messages.extend(m for _, m in _campaign(category, direction=direction))
    end = NOW + 4 * QUARTER
    run = run_at(start=NOW + QUARTER, end=end)
    for reason in (
        EXECUTION_STOP_OWNERSHIP_CONFLICT,
        EXECUTION_STOP_EXECUTION_ERROR,
        EXECUTION_STOP_WINDOW_ENDED,
        EXECUTION_STOP_PLAN_REPLACED,
    ):
        messages.extend(
            m
            for _, m in feed(
                [
                    (NOW, (run,), None),
                    (
                        NOW + QUARTER,
                        (run,),
                        dispatch(end=end, activation_confirmed=True),
                    ),
                    (
                        NOW + 2 * QUARTER,
                        (run,),
                        dispatch(
                            end=end,
                            objective_realized_kwh=0.5,
                            running=False,
                            stop_reason=reason,
                        ),
                    ),
                ]
            )
        )
    # And the two marked forms.
    messages.extend(m for _, m in feed([(NOW, (run,), None)], shadow=True))
    messages.extend(
        m
        for _, m in feed(
            [(NOW, (run_at(start=NOW + QUARTER, end=end, executable=False),), None)]
        )
    )
    return messages


def test_no_activity_message_mentions_power() -> None:
    """**Test L**, and it is structural rather than a filter.

    :class:`RunContent` and :class:`ExecutionView` carry no power at all, so the
    sentence cannot contain one. The string check below is the visible half; the
    field check is the guarantee.
    """
    for message in _every_message():
        assert "kW" not in message.replace("kWh", ""), message

    for field in ("power_kw", "average_power_kw", "peak_power_kw", "initial_power_kw"):
        assert not hasattr(RunContent, field), field
        assert not hasattr(ExecutionView, field), field


def test_no_activity_message_runs_to_more_than_one_clause() -> None:
    """**Test M**: no advisory paragraph, no reserve explanation, no prose.

    A *sentence-ending* full stop is the test -- one followed by a space, or one
    closing the line -- because that is what the beta.30 lines had: two of them,
    followed by "Advisory only: no command is sent for this action." A decimal
    point is not a sentence, so the check cannot simply forbid the character.
    """
    for message in _every_message():
        assert ". " not in message, message
        assert not message.endswith("."), message
        assert "because" not in message, message
        assert "Advisory only" not in message, message
        assert len(message) <= 90, (len(message), message)


def test_every_lifecycle_line_carries_its_plan_id() -> None:
    """**Test N**: on the message and on the event, so neither has to be parsed."""
    end = NOW + 4 * QUARTER
    run = run_at(start=NOW + QUARTER, end=end)
    plan_id = plan_id_for(
        PlanIdentity(category=ACTIVITY_CATEGORY_SAFETY_BUY, end_utc=end)
    )
    state: ActivityState | None = None
    seen = 0
    for now, runs, execution in (
        (NOW, (run,), None),
        (NOW + QUARTER, (run,), dispatch(end=end, activation_confirmed=True)),
        (
            NOW + 2 * QUARTER,
            (run,),
            dispatch(
                end=end,
                objective_realized_kwh=2.22,
                running=False,
                stop_reason=EXECUTION_STOP_TARGET_REACHED,
            ),
        ),
    ):
        entry = next_activity(previous=state, runs=runs, now=now, execution=execution)
        assert entry is not None
        state = entry.state
        seen += 1
        assert entry.plan_id == plan_id
        assert plan_id in entry.message
        payload = logbook_payload(
            entry, domain="alpha_ems_manager", entity_id="sensor.x"
        )
        assert payload["plan_id"] == plan_id
    assert seen == 3


def test_the_plan_id_is_short_stable_and_reproducible() -> None:
    """An id a person cannot say is an id a person will not use.

    Derived from the identity rather than minted, so a reload recovers the same id
    and a reader with a diagnostic can compute it. Six hex characters.
    """
    identity = PlanIdentity(category=ACTIVITY_CATEGORY_SAFETY_BUY, end_utc=CAMPAIGN_END)
    first = plan_id_for(identity)

    assert len(first) == 6
    assert first == plan_id_for(identity)
    # Seconds and microseconds cannot move it: two callers resolving the same
    # quarter-hour boundary must agree.
    assert first == plan_id_for(
        PlanIdentity(
            category=ACTIVITY_CATEGORY_SAFETY_BUY,
            end_utc=CAMPAIGN_END.replace(second=41, microsecond=9),
        )
    )
    # And the category is part of it, or two plans in one window would collide.
    assert first != plan_id_for(
        PlanIdentity(category=ACTIVITY_CATEGORY_ECONOMIC_BUY, end_utc=CAMPAIGN_END)
    )


def test_an_unexecutable_action_is_marked_in_one_word() -> None:
    """The honesty guarantee beta.30 spent a sentence on, kept in one word.

    A plan an actuator cannot perform still gets planned and still deserves to say
    so -- but "Advisory only: no command is sent for this action." on line after
    line is a disclaimer a reader learns to skip, which is worse than none.
    """
    end = NOW + 4 * QUARTER
    lines = feed(
        [(NOW, (run_at(start=NOW + QUARTER, end=end, executable=False),), None)]
    )

    assert len(lines) == 1
    assert lines[0][1].endswith("— Advisory")


def test_shadow_subsumes_the_advisory_marker() -> None:
    """Never both. In Shadow nothing is sent whatever the actuator could do."""
    end = NOW + 4 * QUARTER
    lines = feed(
        [(NOW, (run_at(start=NOW + QUARTER, end=end, executable=False),), None)],
        shadow=True,
    )

    assert lines[0][1].endswith("— Shadow")
    assert "Advisory" not in lines[0][1]


# ===========================================================================
# D. structure
# ===========================================================================


def test_the_planned_message_matches_the_specified_format() -> None:
    """The format, asserted exactly once so a drift is a visible diff."""
    end = datetime(2026, 8, 22, 16, 15, tzinfo=UTC)
    start = datetime(2026, 8, 22, 15, 15, tzinfo=UTC)
    run = run_at(start=start, end=end, energy_kwh=2.22)
    plan_id = plan_id_for(
        PlanIdentity(category=ACTIVITY_CATEGORY_SAFETY_BUY, end_utc=end)
    )

    lines = feed([(start - QUARTER, (run,), None)])

    assert lines == [
        (
            ECONOMIC_EVENT_PLANNED,
            f"Plan ID: {plan_id} — Safety Buy Planned — 15:15-16:15 — 2.22 kWh",
        )
    ]


def test_only_one_entry_is_produced_per_refresh() -> None:
    """Three plans appearing at once produce one line, then the next, then the next.

    A refresh that spoke three times would be a burst, and a burst is what a
    reader experiences as spam whatever its cause.
    """
    end = NOW + 4 * QUARTER
    runs = tuple(
        run_at(start=NOW + QUARTER, end=end + i * timedelta(hours=2), category=category)
        for i, category in enumerate(
            (
                ACTIVITY_CATEGORY_SAFETY_BUY,
                ACTIVITY_CATEGORY_ECONOMIC_BUY,
                ACTIVITY_CATEGORY_MIXED_BUY,
            )
        )
    )

    state: ActivityState | None = None
    for expected in range(3):
        entry = next_activity(previous=state, runs=runs, now=NOW)
        assert entry is not None, expected
        state = entry.state
    assert next_activity(previous=state, runs=runs, now=NOW) is None


def test_a_terminal_event_outranks_a_new_announcement() -> None:
    """An ending that goes unrecorded leaves the previous line standing.

    Which is the beta.30 fault in miniature: a superseded plan whose retraction
    lost a race to a new plan's announcement stayed open in the history for ever.
    """
    first_end = NOW + 2 * QUARTER
    second_end = NOW + 12 * QUARTER
    first = run_at(start=NOW, end=first_end)
    second = run_at(start=NOW + QUARTER, end=second_end)

    state = next_activity(previous=None, runs=(first,), now=NOW)
    assert state is not None
    entry = next_activity(previous=state.state, runs=(second,), now=NOW + QUARTER)

    assert entry is not None
    assert entry.kind == ECONOMIC_EVENT_CANCELLED


def test_the_open_and_closed_sets_are_both_bounded() -> None:
    """An observational surface may not grow without limit.

    Driven with far more plans than the cap, each in its own window, so both
    collections are exercised.
    """
    from custom_components.alpha_ems_manager.const import MAX_ECONOMIC_RUNS_TRACKED

    state: ActivityState | None = None
    for index in range(MAX_ECONOMIC_RUNS_TRACKED * 3):
        end = NOW + timedelta(hours=2 + index)
        run = run_at(start=end - QUARTER, end=end)
        entry = next_activity(previous=state, runs=(run,), now=end - QUARTER)
        if entry is not None:
            state = entry.state

    assert state is not None
    assert len(state.open) <= MAX_ECONOMIC_RUNS_TRACKED
    assert len(state.closed) <= MAX_ECONOMIC_RUNS_TRACKED


def test_a_dispatch_nobody_announced_is_adopted_rather_than_dropped() -> None:
    """A reload mid-campaign, which is the only way to reach this.

    A start that goes unrecorded would leave the later terminal line referring to
    a plan id the history has never seen, so the lifecycle is adopted from the run
    -- and its category is left empty rather than guessed, because Stage B's
    report genuinely does not carry one.
    """
    end = NOW + 4 * QUARTER
    lines = feed(
        [
            (NOW, (), dispatch(end=end, activation_confirmed=True)),
            (
                NOW + QUARTER,
                (),
                dispatch(
                    end=end,
                    objective_realized_kwh=2.22,
                    running=False,
                    stop_reason=EXECUTION_STOP_TARGET_REACHED,
                ),
            ),
        ]
    )

    assert [kind for kind, _ in lines] == [
        ECONOMIC_EVENT_STARTED,
        ECONOMIC_EVENT_FINISHED,
    ], lines
    # The direction still names the action, because that much is knowable.
    assert "Buy Started" in lines[0][1]


def test_the_module_reads_no_clock_of_its_own() -> None:
    """``now`` is a value. Everything here depends on that being true."""
    import inspect

    from custom_components.alpha_ems_manager import activity as module

    source = inspect.getsource(module)
    for forbidden in ("utcnow", "dt_util", "datetime.now", "time.time"):
        assert forbidden not in source, forbidden


def test_the_category_comes_from_the_attribution() -> None:
    """The word a user reads and the figures a reader audits are one measurement.

    ``safety_buy_runs`` is a set of indices and can only say yes or no; the
    attribution says *how much*, which is what separates a Safety Buy from a
    Mixed Buy. Both come from the same reserve-relaxed counterfactual.
    """
    assert (
        category_of(ECONOMIC_ACTION_SAFETY_BUY, (2.0, 0.0))
        == ACTIVITY_CATEGORY_SAFETY_BUY
    )
    assert (
        category_of(ECONOMIC_ACTION_CHARGE, (1.0, 1.0)) == ACTIVITY_CATEGORY_MIXED_BUY
    )
    assert (
        category_of(ECONOMIC_ACTION_CHARGE, (0.0, 3.0))
        == ACTIVITY_CATEGORY_ECONOMIC_BUY
    )
    # No attribution at all means the counterfactual declined to buy anything,
    # which is exactly what "nothing was compulsory" means.
    assert category_of(ECONOMIC_ACTION_CHARGE, None) == ACTIVITY_CATEGORY_ECONOMIC_BUY
    assert category_of(ECONOMIC_ACTION_EXPORT, None) == ACTIVITY_CATEGORY_ECONOMIC_SELL


def test_the_identity_is_what_decides_and_the_predicate_only_documents_it() -> None:
    """``materially_changed`` answers nothing the identity does not.

    Stated as a test because it is the shape of the whole design: a change big
    enough to matter changes the identity and therefore ends one lifecycle
    through the ordinary paths, and a change too small to matter changes nothing.
    There is no third case for a predicate to arbitrate.
    """
    end = NOW + 4 * QUARTER
    run = run_at(start=NOW + QUARTER, end=end)
    entry = next_activity(previous=None, runs=(run,), now=NOW)
    assert entry is not None
    lifecycle = entry.state.open[0]

    assert not materially_changed(lifecycle, run)
    assert materially_changed(
        lifecycle, run_at(start=NOW + QUARTER, end=end + timedelta(hours=2))
    )
    assert materially_changed(
        lifecycle,
        run_at(start=NOW + QUARTER, end=end, category=ACTIVITY_CATEGORY_ECONOMIC_BUY),
    )
    assert materially_changed(
        lifecycle,
        run_at(
            start=NOW + QUARTER,
            end=end,
            energy_kwh=2.22 + 2 * ECONOMIC_DEADBAND_ENERGY_KWH,
        ),
    )


# ===========================================================================
# E. the entity vocabulary a person reads
# ===========================================================================


def _translations(language: str) -> dict:
    """Return one shipped translation file."""
    import json

    path = (
        pathlib.Path("custom_components/alpha_ems_manager/translations")
        / f"{language}.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_control_state_keys_did_not_change() -> None:
    """**Test O's first half, and the answer to "is this breaking?" is no.**

    An automation matching ``executed`` still matches ``executed``. The five values
    beta.30 published are all still published and all still mean what they meant --
    the change is a *display* layer, which is how Home Assistant renders an
    ``ENUM`` sensor and how ``select.control_mode`` has worked since Phase 4.

    ``error`` is added, and additive is not breaking: no existing value is removed
    or redefined, so nothing built against beta.30 stops matching. What it fixes is
    real -- a failed write used to publish whatever eligibility had computed
    *before* the write was attempted, so a reader could not tell a refresh that
    sent nothing from one whose command failed.
    """
    assert CONTROL_STATE_OPTIONS == (
        "off",
        "inhibited",
        "eligible",
        "idle",
        "executed",
        "error",
    )


def test_every_control_state_has_a_professional_display_label() -> None:
    """**Test O's second half.** Capitalised, in both shipped languages.

    And no label is the raw key: a value that renders as ``eligible`` in a history
    view is the thing this fixes.
    """
    for language in ("en", "nl"):
        states = _translations(language)["entity"]["sensor"]["control_state"]["state"]

        assert set(states) == set(CONTROL_STATE_OPTIONS), language
        for key, label in states.items():
            assert label, (language, key)
            assert label[0].isupper(), (language, key, label)
            assert label != key, (language, key)


def test_the_other_two_enum_sensors_are_labelled_too() -> None:
    """Because they appear in the same history view, beside the Control State.

    Leaving ``safety_buy`` and ``curtail`` rendering as lowercase identifiers next
    to a labelled Control State would look like an oversight, and would be one.
    """
    for language in ("en", "nl"):
        sensors = _translations(language)["entity"]["sensor"]

        for key, options in (
            ("battery_recommendation", BATTERY_ACTION_OPTIONS),
            ("economic_action", ECONOMIC_ACTION_OPTIONS),
        ):
            states = sensors[key]["state"]
            assert set(states) == set(options), (language, key)
            for label in states.values():
                assert label and label[0].isupper(), (language, key, label)


def test_no_lifecycle_state_machine_was_bolted_onto_the_control_state() -> None:
    """**The half of the request that was declined, and why.**

    The professional vocabulary asked for was Idle / Planned / Starting /
    Executing / Updating / Completed / Canceled / Inhibited / Error. Four of those
    have no distinct runtime meaning on *this* entity, and the request itself said
    to use only the ones that do and not to invent state churn:

    * **Starting** -- there is no refresh between deciding to write and the write
      landing. It either succeeded (``executed``) or it failed (``error``).
    * **Updating** -- a setpoint correction happens on the sixty-second tick, so
      the entity would flip Executing/Updating every minute of a campaign. That is
      churn, and it would make the recorded history less readable rather than more.
    * **Completed** and **Canceled** -- these are *plan* terminals, and they are
      now emitted on the Activity lifecycle where a plan id ties them to what they
      terminated. Publishing them here too would give two entities their own
      version of the same campaign's ending.

    So the five existing values keep their meanings and gain labels, ``error``
    fills the one genuine gap, and the lifecycle lives in exactly one place.
    """
    labels = _translations("en")["entity"]["sensor"]["control_state"]["state"]

    assert set(labels.values()) == {
        "Off",
        "Inhibited",
        "Planned",
        "Idle",
        "Executing",
        "Error",
    }
    for absent in ("Starting", "Updating", "Completed", "Canceled"):
        assert absent not in labels.values(), absent


def test_a_failed_write_is_the_one_state_that_was_missing() -> None:
    """The error state is set where the failure is recorded, not at the call sites.

    So a future error path cannot forget it. And the execution *barrier* is
    explicitly not a failure: a non-writing release refusing before the first
    service call is the expected outcome, and reporting it as an error would cry
    wolf on every refresh of every release that does not execute.
    """
    import inspect

    from custom_components.alpha_ems_manager import coordinator as module

    source = inspect.getsource(module._mark_execution_error)

    assert "CONTROL_STATE_ERROR" in source
    assert "failed" in inspect.signature(module._mark_execution_error).parameters
    whole = inspect.getsource(module)
    assert 'execution_unavailable", failed=False' in whole


def test_the_activity_event_name_is_the_integration_rather_than_one_surface() -> None:
    """It was "Economic plan", and it stopped being accurate when this surface
    began reporting real dispatches: "Economic plan - Grid charge started" reads as
    though the plan started rather than the battery.

    Fixed rather than taken from the entity's friendly name, which is renameable --
    a logbook filter built on that would silently stop matching. No entity id
    changes and no state changes; a filter someone built on the old string is the
    whole cost.
    """
    from custom_components.alpha_ems_manager.activity import ACTIVITY_NAME

    assert ACTIVITY_NAME == "Alpha EMS"
    assert ACTIVITY_NAME[0].isupper()
    # And the lifecycle is carried by the message, not by the name.
    end = NOW + 4 * QUARTER
    entry = next_activity(
        previous=None, runs=(run_at(start=NOW + QUARTER, end=end),), now=NOW
    )
    assert entry is not None
    payload = logbook_payload(entry, domain="alpha_ems_manager", entity_id="sensor.x")
    assert payload["name"] == ACTIVITY_NAME
    assert "Planned" in payload["message"]


def test_a_superseded_campaign_quotes_the_target_it_announced() -> None:
    """**The pair has to come from one plan, and it nearly did not.**

    A plan is replaced because the optimizer revised it, so by the refresh that
    withdraws it Stage B's target has *already* moved to the replacement's figure.
    Quoting that beside the old plan's progress would put two plans' numbers in one
    fraction -- which is the same class of fault as beta.16's "0.95 kW, 0.27 kWh",
    two boundaries in one sentence with nothing saying so.

    The denominator is therefore what this plan announced. When **Stage B** ends
    the run instead, its own target is the right one, because that is the number
    the run was tracking.
    """
    end = NOW + 4 * QUARTER
    announced = 2.22
    run = run_at(start=NOW + QUARTER, end=end, energy_kwh=announced)

    state = next_activity(previous=None, runs=(run,), now=NOW).state
    state = next_activity(
        previous=state,
        runs=(run,),
        now=NOW + QUARTER,
        execution=dispatch(end=end, activation_confirmed=True),
    ).state

    # The plan withdraws it, and Stage B's target has already been revised.
    replacement = run_at(
        start=NOW + 6 * QUARTER, end=end + timedelta(hours=2), energy_kwh=1.11
    )
    entry = next_activity(
        previous=state,
        runs=(replacement,),
        now=NOW + 2 * QUARTER,
        execution=dispatch(
            end=end, objective_target_kwh=1.11, objective_realized_kwh=0.90
        ),
    )

    assert entry is not None
    assert entry.kind == ECONOMIC_EVENT_CANCELLED
    assert "0.90 / 2.22 kWh" in entry.message

    # And where Stage B ended the run, its own pair is used.
    stopped = next_activity(
        previous=state,
        runs=(run,),
        now=NOW + 2 * QUARTER,
        execution=dispatch(
            end=end,
            objective_target_kwh=2.22,
            objective_realized_kwh=0.90,
            running=False,
            stop_reason=EXECUTION_STOP_OWNERSHIP_CONFLICT,
        ),
    )
    assert stopped is not None
    assert "0.90 / 2.22 kWh" in stopped.message


def test_a_plan_that_never_started_is_cancelled_without_figures() -> None:
    """Quoting 0.00 against a target invites a hunt for a fault that is not there."""
    end = NOW + 4 * QUARTER
    run = run_at(start=NOW + QUARTER, end=end)
    state = next_activity(previous=None, runs=(run,), now=NOW).state

    entry = next_activity(
        previous=state,
        runs=(),
        now=NOW + QUARTER,
        execution=dispatch(end=end, objective_realized_kwh=0.0),
    )

    assert entry is not None
    assert entry.kind == ECONOMIC_EVENT_CANCELLED
    assert "kWh" not in entry.message
