"""Why a carried run stopped, answerable from one snapshot taken any time after.

**The incident these tests are built from.** On 2026-08-25 a carried ``grid_charge``
run for **8.06 kWh**, admitted 12:50 with an admitted window of **13:00-16:30**
local, ended at the 15:00 refresh with ninety minutes of window still to run and
6.30 kWh outstanding. The Activity log said, in full: *"Shadow run finished: plan
ended."* The Economic Action became ``export`` on the same refresh, which invites
the reading that a newer opposite plan killed a running charge.

It did not. Stage A withdrew the campaign because the pack had filled 3.43 kWh from
production, headroom became binding, and the remaining 6.30 kWh no longer fitted.
The lifecycle did exactly what it should. **Three observability defects, stacked,
made that impossible to see:**

* ``decide()`` discarded the reason unless the run was *owned* -- and no mode in
  this release can reach ownership, so the reason was structurally always absent;
* ``carried.ended_reason`` is truthful for exactly one refresh, and the ending
  refresh was not the one captured;
* the wording layer was a single ``or "plan ended"`` fallback, so the fallback was
  the only sentence the log could ever produce.

Nothing here changes the lifecycle. Every assertion is about what gets *reported*,
and the ownership used throughout is ``none`` -- the mode the release actually runs
in, and the arm the previous stop-reason tests never exercised.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager import activity as activity_module
from custom_components.alpha_ems_manager import execution as execution_module
from custom_components.alpha_ems_manager.const import (
    ACTIVITY_CATEGORY_SAFETY_BUY,
    CONTROL_MODE_SHADOW,
    ECONOMIC_DIRECTION_CHARGE,
    ECONOMIC_EVENT_CANCELLED,
    ECONOMIC_EVENT_ERROR,
    ECONOMIC_EVENT_FINISHED,
    ECONOMIC_EVENT_PLANNED,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_NET_EXPORT,
    EXECUTION_STATE_ARMED,
    EXECUTION_STATE_IDLE,
    EXECUTION_STOP_EXECUTION_ERROR,
    EXECUTION_STOP_GRID_CEILING,
    EXECUTION_STOP_PLAN_REPLACED,
    EXECUTION_STOP_REASONS,
    EXECUTION_STOP_STAGE_A_HOLD,
    EXECUTION_STOP_STALE_PLAN,
    EXECUTION_STOP_SWITCHED_OFF,
    EXECUTION_STOP_SWITCHED_TO_SHADOW,
    EXECUTION_STOP_TARGET_REACHED,
    EXECUTION_STOP_WINDOW_ENDED,
    OWNERSHIP_NONE,
    OWNERSHIP_OWNED,
)
from custom_components.alpha_ems_manager.execution import (
    OwnershipEvidence,
    carry_forward,
    decide,
    withdrawal_basis,
)

from .test_control_modes import set_mode
from .test_stage_b_carry_forward import decision_for, published
from .test_stage_b_controller import owned_evidence, progress_of, raw_target

# The coordinator tests in section F drive the real Stage B report, which reads
# the control surface for its ownership evidence.
pytestmark = pytest.mark.usefixtures("control_surface")

# ---------------------------------------------------------------------------
# The incident's own clock. Local times are Europe/Amsterdam, UTC+2 in August,
# and the constants are UTC so nothing here depends on a zone database.
# ---------------------------------------------------------------------------

#: 12:50 local -- the refresh that admitted the run.
ADMITTED = datetime(2026, 8, 25, 10, 50, tzinfo=UTC)
#: 13:00 local -- the admitted window opens and the run arms.
WINDOW_START = datetime(2026, 8, 25, 11, 0, tzinfo=UTC)
#: 16:30 local -- the admitted window end. Never moved, by contract.
WINDOW_END = datetime(2026, 8, 25, 14, 30, tzinfo=UTC)
#: 15:00 local -- the refresh with no affirming publication.
WITHDRAWN = datetime(2026, 8, 25, 13, 0, tzinfo=UTC)

#: The run's own figures, from the two snapshots that bracket it.
TARGET_KWH = 8.06
REALIZED_KWH = 1.76
REMAINING_KWH = 6.30


def charge_publication(start: datetime, **overrides) -> dict:
    """Return the incident's charge publication, opening at ``start``.

    ``window_end`` is held at 16:30 throughout: the horizon rolls its *front*, so
    a campaign still ahead of the front keeps a stable end. That is what made the
    run affirmable for two hours.
    """
    return published(start, WINDOW_END, battery_target_kwh=TARGET_KWH, **overrides)


def export_publication() -> dict:
    """Return the ``export`` recommendation that appeared as the charge vanished.

    Both were consequences of the same cause -- a pack filling from production --
    and the log's silence about the real reason is what made them look causal.
    """
    return raw_target(
        intent=EXECUTION_INTENT_NET_EXPORT,
        purpose="export",
        plan_id="export-make-headroom",
        window_start=WITHDRAWN.isoformat(),
        window_end=(WITHDRAWN + timedelta(hours=2)).isoformat(),
        issued_at=WITHDRAWN.isoformat(),
        stale_after=(WITHDRAWN + timedelta(hours=8)).isoformat(),
    )


def replay_the_incident() -> list[tuple[datetime, object]]:
    """Replay 12:50 through 15:30 and return every refresh's carry outcome.

    The affirming refreshes publish a window that has rolled forward, which is the
    normal case and the one the admitted window must survive.
    """
    schedule: list[tuple[datetime, list[dict]]] = [
        (ADMITTED, [charge_publication(WINDOW_START)]),
        (WINDOW_START, [charge_publication(WINDOW_START + timedelta(minutes=15))]),
        *[
            (
                WINDOW_START + timedelta(minutes=offset),
                [charge_publication(WINDOW_START + timedelta(minutes=offset + 15))],
            )
            for offset in (15, 30, 45, 60, 75, 90)
        ],
        # 15:00 -- no grid_charge for today at all, and an export recommendation.
        (WITHDRAWN, [export_publication()]),
        (WITHDRAWN + timedelta(minutes=15), [export_publication()]),
        (WITHDRAWN + timedelta(minutes=30), [export_publication()]),
    ]
    carried = None
    trace: list[tuple[datetime, object]] = []
    for now, targets in schedule:
        outcome = carry_forward(carried, targets, now)
        carried = outcome.carried
        trace.append((now, outcome))
    return trace


# ===========================================================================
# A. the incident, replayed
# ===========================================================================


def test_the_run_survives_two_hours_of_rolling_publications() -> None:
    """One identity, seven refreshes, and the admitted window never moves.

    Stated first because everything after it is about the *end* of this run, and a
    run that had already died of something else would prove nothing about the end.
    """
    trace = replay_the_incident()
    live = [outcome for _, outcome in trace[:8]]

    assert all(outcome.carried is not None for outcome in live)
    assert len({outcome.carried.run_id for outcome in live}) == 1
    assert all(outcome.carried.window_end == WINDOW_END for outcome in live)
    assert all(outcome.carried.window_start == WINDOW_START for outcome in live)
    assert all(outcome.ended is None for outcome in live)


def test_the_withdrawal_ends_the_run_for_the_reason_stage_a_gave() -> None:
    """**The incident.** The reason existed all along; it was thrown away."""
    trace = replay_the_incident()
    now, outcome = trace[8]

    assert now == WITHDRAWN
    assert outcome.carried is None
    assert outcome.ended == EXECUTION_STOP_STAGE_A_HOLD
    assert outcome.ended_run is not None
    assert outcome.ended_run.intent == EXECUTION_INTENT_GRID_CHARGE
    assert outcome.ended_run.target.battery_target_kwh == pytest.approx(TARGET_KWH)
    # Ninety minutes of admitted window were still to run, which is what made the
    # bare "plan ended" so hard to accept at face value.
    assert now < WINDOW_END
    assert (WINDOW_END - now) == timedelta(minutes=90)


def test_shadow_reports_the_reason_and_still_touches_nothing() -> None:
    """The correction, in one decision: a reason, and no authority.

    ``ownership`` is ``none`` because Shadow never acquires the marker, and this is
    the arm every previous stop-reason test skipped by constructing an owned
    decision.
    """
    trace = replay_the_incident()
    decision = decision_for(trace[8][1], WITHDRAWN, delivered=REALIZED_KWH)

    assert decision.stop_reason == EXECUTION_STOP_STAGE_A_HOLD
    assert decision.ownership == OWNERSHIP_NONE
    assert decision.reset_required is False
    assert decision.state == EXECUTION_STATE_IDLE
    assert decision.request_kw == 0.0
    assert decision.wants_command is False


def test_the_export_recommendation_neither_affirms_nor_supersedes() -> None:
    """It is not an executable intent, so it was never a candidate at all.

    If ``export`` could supersede, the reason would have been ``plan_replaced`` and
    the two events really would have been cause and effect.
    """
    trace = replay_the_incident()

    assert trace[8][1].ended != EXECUTION_STOP_PLAN_REPLACED
    assert trace[8][1].affirmed is False
    # And it admits nothing on the refreshes after, so no export run appears.
    assert all(outcome.carried is None for _, outcome in trace[9:])
    assert all(outcome.ended is None for _, outcome in trace[9:])


def test_the_withdrawal_is_not_the_window_running_out() -> None:
    """Two reasons, two instants, and the difference is the whole diagnosis."""
    trace = replay_the_incident()
    assert trace[8][1].ended == EXECUTION_STOP_STAGE_A_HOLD

    # The same run, affirmed to the end, stops for the other reason entirely.
    # The walk runs past 16:30 local, because a walk that stops short would prove
    # only that nothing had happened yet.
    carried = None
    for offset in range(0, 240, 15):
        now = ADMITTED + timedelta(minutes=offset)
        outcome = carry_forward(
            carried, [charge_publication(max(now, WINDOW_START))], now
        )
        carried = outcome.carried
        if outcome.ended is not None:
            assert now >= WINDOW_END
            assert outcome.ended == EXECUTION_STOP_WINDOW_ENDED
            return
    pytest.fail("the admitted window never ended")


def test_the_reason_is_a_report_and_not_an_instruction() -> None:
    """Nothing downstream may branch on it. Read structurally, once.

    ``reset_required`` is the field that stops a dispatch, and it keeps its
    ownership gate. This is the guarantee that ungating the *reason* cannot make a
    write reachable.
    """
    with open(execution_module.__file__, encoding="utf-8") as handle:
        text = handle.read()

    # Comment lines are excluded deliberately: the field's own documentation
    # quotes the old gate in order to explain it, and a check that cannot tell
    # prose from code would be satisfied by deleting the explanation.
    code = [row.strip() for row in text.splitlines() if not row.strip().startswith("#")]

    # The old gate is gone from every branch.
    assert any("stop_reason=" in row for row in code)
    assert not [row for row in code if "if owned else None" in row]
    # And the reset gate is not. **Eight since beta.32**, not seven: the export
    # branch added to ``demand_for`` gives ``battery_ceiling`` its own stop, and like
    # every other stop it resets only what it owns. The number is asserted rather
    # than bounded because a *new unguarded* reset is what this catches, and a new
    # guarded one should have to declare itself here.
    assert sum(row.count("reset_required=owned") for row in code) == 8


# ===========================================================================
# B. every reachable reason, in the mode the release runs in
# ===========================================================================

#: The eight reasons ``decide()`` actually assigns. The other five in
#: :data:`EXECUTION_STOP_REASONS` are declared vocabulary that no branch reaches.
REACHABLE = (
    EXECUTION_STOP_STAGE_A_HOLD,
    EXECUTION_STOP_WINDOW_ENDED,
    EXECUTION_STOP_TARGET_REACHED,
    EXECUTION_STOP_STALE_PLAN,
    EXECUTION_STOP_SWITCHED_OFF,
    EXECUTION_STOP_SWITCHED_TO_SHADOW,
    EXECUTION_STOP_GRID_CEILING,
    EXECUTION_STOP_PLAN_REPLACED,
)


def shadow_decision(**overrides) -> object:
    """Return a Shadow decision -- ownership ``none``, nothing owned, nothing sent."""
    params = {
        "mode_executes": False,
        "mode_off": False,
        "targets": (),
        "now": WINDOW_START + timedelta(minutes=30),
        "evidence": OwnershipEvidence(dispatch_active=False, marker_on=False),
        "progress": progress_of(0.0),
        "current_energy_kwh": 9.0,
        "remaining_expected_pv_kwh": 4.0,
    }
    params.update(overrides)
    return decide(**params)


def test_shadow_can_report_a_withdrawal() -> None:
    """The incident's reason, reached through the carry machine."""
    trace = replay_the_incident()
    decision = decision_for(trace[8][1], WITHDRAWN)

    assert decision.stop_reason == EXECUTION_STOP_STAGE_A_HOLD
    assert decision.reset_required is False


def test_shadow_can_report_the_window_ending() -> None:
    """Distinct from withdrawal, and this is the pair that was indistinguishable."""
    carried = None
    for offset in range(0, 240, 15):
        now = ADMITTED + timedelta(minutes=offset)
        outcome = carry_forward(
            carried, [charge_publication(max(now, WINDOW_START))], now
        )
        carried = outcome.carried
        if outcome.ended is not None:
            decision = decision_for(outcome, now)
            assert decision.stop_reason == EXECUTION_STOP_WINDOW_ENDED
            assert decision.reset_required is False
            return
    pytest.fail("the admitted window never ended")


def test_shadow_can_report_a_stale_plan() -> None:
    """A run Stage A has stopped publishing at all is stale, and Shadow says so.

    **The silence is the point, and beta.35 made it the condition.** This was
    written with the expired publication still in hand, and passed because
    freshness was judged *before* that publication was read -- an ordering that,
    on live hardware, declared a run stale in the same refresh that was
    re-anchoring it. The deadline detects Stage A having gone quiet, so it is
    asserted where Stage A is quiet; the reason, the absence of a reset and the
    Shadow reporting of all three are unchanged.
    """
    brief = charge_publication(
        WINDOW_START, stale_after=(WINDOW_START + timedelta(minutes=5)).isoformat()
    )
    admitted = carry_forward(None, [brief], ADMITTED)
    assert admitted.carried is not None

    later = WINDOW_START + timedelta(minutes=30)
    outcome = carry_forward(admitted.carried, [], later)
    decision = decision_for(outcome, later)

    assert outcome.ended == EXECUTION_STOP_STALE_PLAN
    assert decision.stop_reason == EXECUTION_STOP_STALE_PLAN
    assert decision.reset_required is False


def test_shadow_can_report_the_target_being_reached() -> None:
    """The happy ending, and it was as silent as the others."""
    decision = shadow_decision(
        targets=[charge_publication(WINDOW_START)],
        progress=progress_of(TARGET_KWH + 0.1),
    )

    assert decision.stop_reason == EXECUTION_STOP_TARGET_REACHED
    assert decision.reset_required is False


def test_shadow_can_report_the_grid_allowance_being_spent() -> None:
    """A configured budget stop, which a reader has no other way to detect."""
    decision = shadow_decision(
        targets=[charge_publication(WINDOW_START)],
        grid_charged_kwh=5.0,
        configured_budget_kwh=5.0,
    )

    assert decision.stop_reason == EXECUTION_STOP_GRID_CEILING
    assert decision.reset_required is False


def test_shadow_reports_a_mode_change_as_the_reason_it_is() -> None:
    """Switching off is a stop, and worth a line saying so rather than nothing."""
    decision = shadow_decision(
        mode_off=True, targets=[charge_publication(WINDOW_START)]
    )

    assert decision.stop_reason == EXECUTION_STOP_SWITCHED_OFF
    assert decision.reset_required is False


def test_nothing_carried_and_nothing_owned_reports_nothing() -> None:
    """**The silence that must survive the ungating.**

    An idle controller with no target, nothing carried and no record of a run never
    stopped anything, so it has no reason to give. Reporting one here would be the
    mirror image of the bug: a sentence about an event that did not happen.
    """
    decision = shadow_decision(targets=())

    assert decision.state == EXECUTION_STATE_IDLE
    assert decision.stop_reason is None
    assert decision.reset_required is False


def test_an_ordinary_armed_refresh_reports_nothing_either() -> None:
    """Mid-run is not a stop."""
    decision = shadow_decision(targets=[charge_publication(WINDOW_START)])

    assert decision.state == EXECUTION_STATE_ARMED
    assert decision.stop_reason is None


# ===========================================================================
# C. the withdrawal basis -- diagnostics vocabulary, and nothing more
# ===========================================================================


def test_the_basis_names_what_was_missing() -> None:
    """Including the intent, because "withdrawn" alone leaves out from what."""
    assert (
        withdrawal_basis(EXECUTION_STOP_STAGE_A_HOLD, EXECUTION_INTENT_GRID_CHARGE)
        == "no_affirming_grid_charge_publication"
    )


def test_the_basis_distinguishes_the_four_ways_a_carried_run_can_end() -> None:
    """Absence, the window, freshness and replacement each read differently."""
    bases = {
        withdrawal_basis(reason, EXECUTION_INTENT_GRID_CHARGE)
        for reason in (
            EXECUTION_STOP_STAGE_A_HOLD,
            EXECUTION_STOP_WINDOW_ENDED,
            EXECUTION_STOP_STALE_PLAN,
            EXECUTION_STOP_PLAN_REPLACED,
        )
    }

    assert len(bases) == 4
    assert None not in bases


def test_the_basis_is_absent_where_it_would_be_an_invention() -> None:
    """A mode change is not an observation about a publication."""
    assert withdrawal_basis(EXECUTION_STOP_SWITCHED_OFF, "grid_charge") is None
    assert withdrawal_basis(EXECUTION_STOP_TARGET_REACHED, "grid_charge") is None
    assert withdrawal_basis(None, "grid_charge") is None


def test_the_basis_changes_no_decision() -> None:
    """It is derived from the reason and reaches nothing. Same inputs, same run."""
    trace = replay_the_incident()
    before = decision_for(trace[8][1], WITHDRAWN)
    basis = withdrawal_basis(before.stop_reason, EXECUTION_INTENT_GRID_CHARGE)
    after = decision_for(trace[8][1], WITHDRAWN)

    assert basis is not None
    assert (before.state, before.stop_reason, before.reset_required) == (
        after.state,
        after.stop_reason,
        after.reset_required,
    )


# ===========================================================================
# D. the wording, which is what a person actually reads
# ===========================================================================
#
# **Rewritten for beta.31, and the claims are unchanged.** These cases used to
# drive ``activity._stopped_message`` -- one sentence with the reason interpolated
# into it. A run ending is now one of three lifecycle *terminals*, so the same
# four claims are made against the real ``next_activity`` instead:
#
#   * every declared stop reason has wording of its own;
#   * no reason reaches a person as an identifier;
#   * the line stays one short clause;
#   * the endings a reader must tell apart read differently.
#
# What has genuinely changed is the shape of the answer. beta.30 said "Charge
# stopped - grid limit - 1.76 / 8.06 kWh" for six different endings and "Charge
# complete" for the seventh, so *whether it worked* had to be read out of an
# adjective. beta.31 answers that first, in the event kind: a success, a
# cancellation and an error are three kinds, and the reason is detail beneath it.


LIFECYCLE_END = datetime(2026, 3, 14, 16, 30, tzinfo=UTC)
LIFECYCLE_START = LIFECYCLE_END - timedelta(hours=2)


def _lifecycle_run() -> activity_module.PlannedRun:
    """Return the incident's charge campaign as Activity now sees it."""
    return activity_module.PlannedRun(
        identity=activity_module.RunIdentity(
            direction=ECONOMIC_DIRECTION_CHARGE, start_utc=LIFECYCLE_START
        ),
        content=activity_module.RunContent(
            category=ACTIVITY_CATEGORY_SAFETY_BUY,
            energy_kwh=TARGET_KWH,
            end_utc=LIFECYCLE_END,
            window="14:30-16:30",
        ),
    )


def _dispatch(**overrides) -> activity_module.ExecutionView:
    """Return Stage B's view of the incident's run, Live by default."""
    params = {
        "identity": activity_module.RunIdentity(
            direction=ECONOMIC_DIRECTION_CHARGE, start_utc=LIFECYCLE_START
        ),
        "end_utc": LIFECYCLE_END,
        "objective_target_kwh": TARGET_KWH,
        "objective_realized_kwh": REALIZED_KWH,
        "intent": EXECUTION_INTENT_GRID_CHARGE,
        "run_id": "incident",
        "executed": True,
        "running": True,
    }
    params.update(overrides)
    return activity_module.ExecutionView(**params)


def terminal(reason: str, **overrides):
    """Return the ``(kind, message)`` a run ending for ``reason`` produces.

    A whole campaign is driven -- planned, started, ended -- because a terminal is
    only reachable from a lifecycle that started, which is itself one of the
    guarantees: nothing can report a battery stopping that never reported it
    starting.
    """
    run = _lifecycle_run()
    state = None
    for now, execution in (
        (LIFECYCLE_START - timedelta(minutes=10), None),
        (LIFECYCLE_START, _dispatch(activation_confirmed=True, **overrides)),
        (
            LIFECYCLE_START + timedelta(minutes=30),
            _dispatch(running=False, stop_reason=reason, **overrides),
        ),
    ):
        entry = activity_module.next_activity(
            previous=state, runs=(run,), now=now, execution=execution
        )
        assert entry is not None, (reason, now)
        state = entry.state
    return entry.kind, entry.message


def test_the_incident_now_reads_as_a_sentence_about_a_battery() -> None:
    """**The observed line was "Shadow run finished: plan ended." and nothing else.**

    It now names the plan, says the ending was a cancellation rather than a
    failure, gives the reason in words, and quotes how far the run got.
    """
    kind, message = terminal(EXECUTION_STOP_STAGE_A_HOLD)

    assert kind == ECONOMIC_EVENT_CANCELLED
    # beta.34: relabelled. A Stage-A withdrawal is one plan replacing another,
    # and "No Longer Economically Valid" read as a verdict on the plan's worth.
    assert "Plan Superseded" in message
    assert "1.76 / 8.06 kWh" in message


def test_a_success_and_a_failure_are_different_kinds_not_different_adjectives() -> None:
    """The distinction a reader needs first, answered before any wording.

    beta.30 put all three outcomes in one kind and left the difference to a word
    inside the sentence, so a history view could not filter or count them.
    """
    assert terminal(EXECUTION_STOP_TARGET_REACHED)[0] == ECONOMIC_EVENT_FINISHED
    assert terminal(EXECUTION_STOP_EXECUTION_ERROR)[0] == ECONOMIC_EVENT_ERROR
    assert terminal(EXECUTION_STOP_STAGE_A_HOLD)[0] == ECONOMIC_EVENT_CANCELLED


def test_the_reasons_a_reader_must_tell_apart_read_differently() -> None:
    """Withdrawal, the window, the target, replacement, freshness, the budget.

    ``switched_off`` and ``switched_to_shadow`` deliberately share one phrase:
    both are the user changing the mode, and a reader does not need to know which
    of the two positions the switch landed in -- that is in diagnostics.
    """
    rendered = {reason: terminal(reason)[1] for reason in REACHABLE}
    modes = (EXECUTION_STOP_SWITCHED_OFF, EXECUTION_STOP_SWITCHED_TO_SHADOW)

    distinct = {m for r, m in rendered.items() if r not in modes}
    assert len(distinct) == len(REACHABLE) - len(modes)
    assert rendered[modes[0]] == rendered[modes[1]]
    assert "Window Expired" in rendered[EXECUTION_STOP_WINDOW_ENDED]
    assert "Plan Replaced" in rendered[EXECUTION_STOP_PLAN_REPLACED]
    assert "Plan Expired" in rendered[EXECUTION_STOP_STALE_PLAN]
    assert "Grid Limit Reached" in rendered[EXECUTION_STOP_GRID_CEILING]
    assert "Control Mode Changed" in rendered[EXECUTION_STOP_SWITCHED_OFF]


def test_a_reached_target_quotes_the_pair_and_says_when_they_agree() -> None:
    """A run-level ``target_reached`` reads the same whatever the pair says.

    **The tolerance has left this module, and its absence is the assertion.**
    Through beta.31 Activity held ``TARGET_TOLERANCE_KWH`` and decided from it
    whether to print "Target Reached" -- a presentation layer ruling on whether
    0.014 kWh of residue was a success, which is 0.56 of one actuator step and
    therefore not a question a renderer can answer. Since beta.32 the outcome
    class is computed where the energy was measured, and this path renders Stage
    B's own ``target_reached`` verdict without second-guessing it.

    Both figures are still printed. The format is one line and one shape.
    """
    met = terminal(EXECUTION_STOP_TARGET_REACHED, objective_realized_kwh=8.02)[1]
    short = terminal(EXECUTION_STOP_TARGET_REACHED)[1]

    assert "Success — Target Reached — 8.02 / 8.06 kWh" in met
    # The same phrase on a pair that disagrees, because Stage B said the target was
    # reached and this layer no longer holds a tolerance to disagree with it.
    assert "Success — Target Reached — 1.76 / 8.06 kWh" in short
    assert not hasattr(activity_module, "TARGET_TOLERANCE_KWH"), (
        "the completion tolerance must not return to the Activity surface"
    )


def test_no_reason_reaches_a_person_as_an_identifier() -> None:
    """**Every** declared constant, not only the reachable ones.

    The old line interpolated ``stop_reason`` raw, so a Live grid-budget stop read
    "Dispatch stopped: grid_energy_ceiling." A constant that acquires a branch
    later must not acquire that sentence with it.
    """
    for reason in EXECUTION_STOP_REASONS:
        _, rendered = terminal(reason)

        # No snake_case, which is the actual shape of an identifier leak.
        assert "_" not in rendered, rendered
        if "_" in reason:
            assert reason not in rendered, rendered
        lowered = rendered.lower()
        for term in ("stage a", "stage b", "carry", "affirm", "shadow run", "dispatch"):
            assert term not in lowered, rendered


def test_every_declared_reason_has_wording_of_its_own() -> None:
    """No constant may fall through to the generic phrase.

    The two tables partition the reasons: a cancellation is the optimizer or the
    world changing its mind, an error is something that needs looking at, and
    ``target_reached`` is neither because it is the success. Nothing may be in both
    tables, and nothing may be in neither.
    """
    cancels = activity_module._CANCEL_REASONS
    errors = activity_module._ERROR_REASONS
    declared = set(EXECUTION_STOP_REASONS)

    assert not set(cancels) & set(errors)
    assert declared == set(cancels) | set(errors) | {EXECUTION_STOP_TARGET_REACHED}
    for phrase in (*cancels.values(), *errors.values()):
        assert phrase and phrase == phrase.title(), phrase


def test_the_line_stays_one_short_clause() -> None:
    """No paragraph, no second sentence, nothing about what happens next."""
    for reason in EXECUTION_STOP_REASONS:
        _, rendered = terminal(reason)

        assert not rendered.endswith("."), rendered
        assert ". " not in rendered, rendered
        assert len(rendered) <= 90, (len(rendered), rendered)
        assert "because" not in rendered.lower(), rendered
        assert "\n" not in rendered, rendered


def test_an_unknown_constant_still_produces_a_line() -> None:
    """The fallback stays reachable in principle and unreachable in practice.

    A reason from the future is a cancellation rather than an error, which is the
    safe direction: calling an unknown ending a failure would raise an alarm the
    code has no evidence for.
    """
    kind, message = terminal("a_reason_from_the_future")

    assert kind == ECONOMIC_EVENT_CANCELLED
    assert "Plan Replaced" in message


def test_shadow_reaches_no_terminal_at_all() -> None:
    """Because it reaches no start, and a terminal without a start is a claim.

    beta.30 emitted ``would_stop`` here, which meant a Shadow history looked
    exactly as busy as a Live one and every line needed a disclaimer to stay
    honest. The plan's own retraction is what ends a Shadow lifecycle.
    """
    run = _lifecycle_run()
    entry = activity_module.next_activity(
        previous=None,
        runs=(run,),
        now=LIFECYCLE_START,
        execution=_dispatch(
            executed=False,
            activation_confirmed=True,
            running=False,
            stop_reason=EXECUTION_STOP_TARGET_REACHED,
        ),
        shadow=True,
    )

    assert entry is not None
    assert entry.kind == ECONOMIC_EVENT_PLANNED
    assert entry.message.endswith("— Shadow")


# ===========================================================================
# E. mutations -- each defect, restored, must fail
# ===========================================================================


def old_wording(stop_reason: str | None) -> str:
    """Return the beta.22 reason clause, reproduced exactly as it was written.

    One line, and it was wrong in both directions: ``None`` in Shadow reached the
    fallback, and a populated reason in Live reached the sentence as a raw symbol.
    """
    return stop_reason or "plan ended"


def test_restoring_the_ownership_gate_is_caught() -> None:
    """The mutation is ``stop_reason=reason if owned else None``.

    Reproduced by reading the decision's own ownership rather than by editing the
    module: in Shadow ``owned`` is false, so the mutated field is ``None`` for the
    incident and the honest one is not.
    """
    trace = replay_the_incident()
    decision = decision_for(trace[8][1], WITHDRAWN)
    owned = decision.ownership == OWNERSHIP_OWNED
    mutated = decision.stop_reason if owned else None

    assert owned is False
    assert mutated is None
    assert decision.stop_reason == EXECUTION_STOP_STAGE_A_HOLD


def test_reverting_the_wording_to_the_bare_fallback_is_caught() -> None:
    """The mutation is ``execution.stop_reason or "plan ended"``.

    Two ways it was wrong, and both are reproduced: in Shadow it printed the
    fallback for every reason, and in Live it printed the raw identifier.
    """
    for reason in REACHABLE:
        # Shadow could not populate the field at all, so it printed the fallback.
        assert old_wording(None) == "plan ended"
        # Live populated it, and printed the constant.
        assert old_wording(reason) == reason
        assert reason not in terminal(reason)[1]

    # And the fallback no longer describes eight different endings identically:
    # the two mode reasons share a phrase deliberately, and the rest are distinct.
    modes = {EXECUTION_STOP_SWITCHED_OFF, EXECUTION_STOP_SWITCHED_TO_SHADOW}
    rendered = {terminal(r)[1] for r in REACHABLE if r not in modes}
    assert len(rendered) == len(REACHABLE) - len(modes)


def test_a_known_reason_falling_back_to_the_generic_phrase_is_caught() -> None:
    """Only an unknown constant may reach the fallback.

    ``plan_replaced`` is the exception and it is not one: its own phrase *is* the
    fallback phrase, because a replacement is exactly what an unrecognised ending
    is assumed to be.
    """
    generic = terminal("a_reason_from_the_future")[1]

    assert activity_module._CANCEL_REASONS[EXECUTION_STOP_STAGE_A_HOLD] == (
        # beta.34. The phrase changed and the rule it proves did not: this reason
        # has a phrase of its own and never falls through to the generic one.
        "Plan Superseded"
    )
    for reason in REACHABLE:
        if reason == EXECUTION_STOP_PLAN_REPLACED:
            continue
        assert terminal(reason)[1] != generic, reason


def test_treating_the_export_recommendation_as_a_supersession_is_caught() -> None:
    """The mutation is dropping the intent filter from the carry candidates.

    With it dropped the export publication becomes a candidate and the charge run
    reads as replaced -- which is the false story the silent log told.
    """
    trace = replay_the_incident()
    outcome = trace[8][1]

    assert outcome.ended == EXECUTION_STOP_STAGE_A_HOLD
    assert outcome.ended != EXECUTION_STOP_PLAN_REPLACED
    # The export publication cannot admit a run of its own either, so no
    # replacement exists to be mistaken for one.
    assert carry_forward(None, [export_publication()], WITHDRAWN).carried is None


def test_a_reset_becoming_reachable_in_shadow_is_caught() -> None:
    """Across every reachable reason, not only the incident's.

    ``reset_required`` is the only field that stops a dispatch. If ungating the
    reason had loosened it anywhere, one of these would be true.
    """
    trace = replay_the_incident()
    decisions = [
        decision_for(trace[8][1], WITHDRAWN),
        shadow_decision(targets=[charge_publication(WINDOW_START)]),
        shadow_decision(
            targets=[charge_publication(WINDOW_START)],
            progress=progress_of(TARGET_KWH + 0.1),
        ),
        shadow_decision(mode_off=True, targets=[charge_publication(WINDOW_START)]),
        shadow_decision(
            targets=[charge_publication(WINDOW_START)],
            grid_charged_kwh=5.0,
            configured_budget_kwh=5.0,
        ),
    ]

    assert all(decision.reset_required is False for decision in decisions)
    assert all(decision.ownership == OWNERSHIP_NONE for decision in decisions)


def test_the_owned_reset_path_is_unchanged_by_the_ungating() -> None:
    """An owned withdrawal still stops the dispatch, and still says why.

    The ungating may only ever *add* a reason. This is the other half of that
    claim: where a reason was reported before it is reported still, and the reset
    that accompanied it is untouched.
    """
    decision = decide(
        mode_executes=True,
        mode_off=False,
        targets=(),
        now=WINDOW_START + timedelta(minutes=30),
        evidence=owned_evidence(),
        progress=progress_of(0.0),
        current_energy_kwh=9.0,
        remaining_expected_pv_kwh=4.0,
    )

    assert decision.stop_reason == EXECUTION_STOP_STAGE_A_HOLD
    assert decision.reset_required is True


def test_switching_out_of_live_still_reports_from_the_shadow_side() -> None:
    """Ownership ``none`` and a mode that no longer executes still stopped something."""
    decision = shadow_decision(
        targets=[charge_publication(WINDOW_START)],
        evidence=OwnershipEvidence(dispatch_active=False, marker_on=False),
    )

    assert decision.state == EXECUTION_STATE_ARMED
    assert EXECUTION_STOP_SWITCHED_TO_SHADOW in EXECUTION_STOP_REASONS
    assert activity_module._CANCEL_REASONS[EXECUTION_STOP_SWITCHED_TO_SHADOW] == (
        "Control Mode Changed"
    )


# ===========================================================================
# F. the record that survives later refreshes
# ===========================================================================


async def stage_b_coordinator(hass: HomeAssistant, entry: MockConfigEntry) -> object:
    """Return a loaded coordinator, with Shadow selected and nothing carried."""
    coordinator = entry.runtime_data
    await set_mode(hass, CONTROL_MODE_SHADOW)
    coordinator._carried = None
    coordinator._last_ended = None
    coordinator.execution_targets = ()
    return coordinator


def report_at(coordinator: object, now: datetime, targets: list[dict]) -> dict:
    """Drive one Stage B refresh and return its report.

    ``plan`` and ``snapshot`` are absent, which is a real Shadow condition rather
    than a convenience: without a device snapshot no ownership evidence exists, so
    the controller runs on the arm the release actually uses.
    """
    coordinator.execution_targets = tuple(targets)
    return coordinator._stage_b_report(
        plan=None, snapshot=None, now=now, mode=CONTROL_MODE_SHADOW
    )


def replay_into(coordinator: object) -> list[dict]:
    """Replay the incident through the coordinator and return every report."""
    schedule: list[tuple[datetime, list[dict]]] = [
        (ADMITTED, [charge_publication(WINDOW_START)]),
        (WINDOW_START, [charge_publication(WINDOW_START + timedelta(minutes=15))]),
        *[
            (
                WINDOW_START + timedelta(minutes=offset),
                [charge_publication(WINDOW_START + timedelta(minutes=offset + 15))],
            )
            for offset in (15, 30, 45, 60, 75, 90)
        ],
        (WITHDRAWN, [export_publication()]),
        (WITHDRAWN + timedelta(minutes=15), [export_publication()]),
        (WITHDRAWN + timedelta(minutes=30), [export_publication()]),
    ]
    return [report_at(coordinator, now, targets) for now, targets in schedule]


async def test_the_reason_survives_the_refreshes_after_the_one_that_ended_it(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """**The defect that made the incident a two-snapshot reconstruction.**

    ``ended_reason`` is truthful for exactly one refresh, and the 15:00 refresh was
    not the one captured. By 15:43 the payload carried nothing about the stop at
    all. ``last_ended`` is the answer to a question asked later.
    """
    coordinator = await stage_b_coordinator(hass, setup_integration)
    reports = replay_into(coordinator)

    ending = reports[8]["carried"]
    assert ending["ended_reason"] == EXECUTION_STOP_STAGE_A_HOLD

    # The two refreshes after it -- the ones a real download is taken on.
    #
    # **A carried run is no longer proof that the charge did not end.** Since
    # beta.27.1 ``carry_forward`` admits ``net_export`` as well, so these refreshes
    # legitimately carry the *export* run that follows -- which is the whole point
    # of that fix. What this test is about is that the ended reason survives the
    # refreshes after the one that ended it, so it asserts that, and asserts that no
    # **charge** was re-admitted.
    for report in reports[9:]:
        carried = report["carried"]
        assert carried["ended_reason"] is None
        run = carried["run"]
        if run is not None:
            assert run["intent"] == EXECUTION_INTENT_NET_EXPORT, run["intent"]
        assert carried["last_ended"] is not None
        assert carried["last_ended"]["reason"] == EXECUTION_STOP_STAGE_A_HOLD


async def test_the_record_carries_what_the_investigation_needed(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Every field that had to be reconstructed by hand from two snapshots."""
    coordinator = await stage_b_coordinator(hass, setup_integration)
    reports = replay_into(coordinator)
    record = reports[-1]["carried"]["last_ended"]

    assert record["intent"] == EXECUTION_INTENT_GRID_CHARGE
    assert record["reason"] == EXECUTION_STOP_STAGE_A_HOLD
    assert record["withdrawal_basis"] == "no_affirming_grid_charge_publication"
    assert record["battery_target_kwh"] == pytest.approx(TARGET_KWH)
    assert record["window_start"] == WINDOW_START.isoformat()
    assert record["window_end"] == WINDOW_END.isoformat()
    assert record["ended_at"] == WITHDRAWN.isoformat()
    assert record["run_id"] == reports[7]["carried"]["run"]["run_id"]
    assert record["plan_id"]
    # The shortfall is stated rather than left to be subtracted, and the two agree.
    assert record["remaining_battery_kwh"] == pytest.approx(
        record["battery_target_kwh"] - record["battery_realized_kwh"]
    )


async def test_the_record_is_written_only_when_a_run_actually_ends(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Not on an affirmation, not on a rolling publication, not while prepared.

    A record that updated every refresh would describe the latest refresh rather
    than the last lifecycle event, which is the same defect wearing a new field
    name.
    """
    coordinator = await stage_b_coordinator(hass, setup_integration)
    reports = replay_into(coordinator)

    # Nine refreshes of admitting, arming and affirming, and none of them wrote.
    assert all(report["carried"]["last_ended"] is None for report in reports[:8])
    assert reports[8]["carried"]["last_ended"] is not None
    # And once written it is not rewritten by the quiet refreshes that follow.
    written = [report["carried"]["last_ended"] for report in reports[8:]]
    assert all(record == written[0] for record in written)


async def test_the_reason_survives_an_unrelated_publication_being_actionable(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """**The harsher case, and the one that exposed a second dropped verdict.**

    ``execution_targets`` carries every run the plan contains, ``net_export``
    included, so on the refresh the charge was withdrawn the export recommendation
    was selectable. The state machine then took a different branch and dropped the
    carry machine's verdict -- the ownership gate's defect wearing new clothes.

    The reason must survive that, and it must survive it without becoming a claim
    about the export: the reset stays refused and the ownership stays ``none``.
    """
    coordinator = await stage_b_coordinator(hass, setup_integration)
    reports = replay_into(coordinator)
    ending = reports[8]

    assert ending["carried"]["run"] is None
    assert ending["result"]["stop_reason"] == EXECUTION_STOP_STAGE_A_HOLD
    assert ending["result"]["reset_required"] is False
    assert ending["ownership"]["state"] == OWNERSHIP_NONE
    # The figures for the run that ended come from the record, because the target
    # block legitimately describes whatever is selectable now.
    record = ending["carried"]["last_ended"]
    assert record["battery_target_kwh"] == pytest.approx(TARGET_KWH)
    assert record["intent"] == EXECUTION_INTENT_GRID_CHARGE


async def test_a_restart_forgets_it_rather_than_restating_it(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Session-local by design, and the stored document must not carry it.

    A retained claim that outlived the session it was observed in would be exactly
    the kind of stale fact this project keeps refusing to publish.
    """
    coordinator = await stage_b_coordinator(hass, setup_integration)
    replay_into(coordinator)

    assert coordinator._last_ended is not None
    stored = coordinator.store.to_dict()
    assert "last_ended" not in repr(stored)


async def test_nothing_is_recorded_on_a_day_with_no_run_at_all(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The field is absent rather than empty, so its presence means something."""
    coordinator = await stage_b_coordinator(hass, setup_integration)

    for offset in (0, 15, 30):
        report = report_at(coordinator, WINDOW_START + timedelta(minutes=offset), [])
        assert report["carried"]["last_ended"] is None
        assert report["result"]["stop_reason"] is None


# ===========================================================================
# G. the second dropped verdict, and the shell that stops it
# ===========================================================================


def ending_decision(targets: list[dict], **overrides) -> object:
    """Return the decision for the refresh a carried run was withdrawn on."""
    trace = replay_the_incident()
    outcome = trace[8][1]
    params = {
        "mode_executes": False,
        "mode_off": False,
        "targets": targets,
        "now": WITHDRAWN,
        "evidence": OwnershipEvidence(dispatch_active=False, marker_on=False),
        "progress": progress_of(REALIZED_KWH),
        "current_energy_kwh": 18.04,
        "remaining_expected_pv_kwh": 1.0,
        "carried": outcome.carried,
        "carry_ended": outcome.ended,
        "ended_run": outcome.ended_run,
    }
    params.update(overrides)
    return decide(**params)


def test_an_actionable_export_no_longer_swallows_the_withdrawal() -> None:
    """**The second dropped verdict, and it is the incident's own shape.**

    ``execution_targets`` carries every run the plan contains, so the export
    recommendation was selectable on the very refresh the charge was withdrawn. The
    state machine reaches the withdrawal reason only through the branch it takes
    when *nothing* is selectable, so it took another branch and the verdict went
    the same way the ownership gate sent it.
    """
    decision = decide(
        mode_executes=False,
        mode_off=False,
        targets=[export_publication()],
        now=WITHDRAWN,
        evidence=OwnershipEvidence(dispatch_active=False, marker_on=False),
        progress=progress_of(REALIZED_KWH),
        carried=None,
        carry_ended=EXECUTION_STOP_STAGE_A_HOLD,
        ended_run=replay_the_incident()[8][1].ended_run,
    )

    assert decision.stop_reason == EXECUTION_STOP_STAGE_A_HOLD
    assert decision.reset_required is False
    assert decision.ownership == OWNERSHIP_NONE


def test_removing_the_shell_loses_the_reason_again() -> None:
    """The mutation is calling the state machine directly.

    ``_decide`` is the machine and ``decide`` is the shell that fills the verdict
    in. With the export selectable the machine names no reason at all, which is
    precisely the observed silence.
    """
    ended = replay_the_incident()[8][1]
    arguments = {
        "mode_executes": False,
        "mode_off": False,
        "targets": [export_publication()],
        "now": WITHDRAWN,
        "evidence": OwnershipEvidence(dispatch_active=False, marker_on=False),
        "progress": progress_of(REALIZED_KWH),
        "carried": None,
        "carry_ended": EXECUTION_STOP_STAGE_A_HOLD,
        "ended_run": ended.ended_run,
    }

    assert execution_module._decide(**arguments).stop_reason is None
    assert decide(**arguments).stop_reason == EXECUTION_STOP_STAGE_A_HOLD


def test_the_shell_does_not_overwrite_a_reason_the_machine_named() -> None:
    """A reason the machine reached describes the target it actually selected.

    Trading one true statement for another buys nothing, and the withdrawal is not
    lost by leaving it alone: the coordinator records it from the carry outcome
    before this function is reached.
    """
    decision = decide(
        mode_executes=False,
        mode_off=True,
        targets=[export_publication()],
        now=WITHDRAWN,
        evidence=OwnershipEvidence(dispatch_active=False, marker_on=False),
        progress=progress_of(REALIZED_KWH),
        carried=None,
        carry_ended=EXECUTION_STOP_STAGE_A_HOLD,
        ended_run=replay_the_incident()[8][1].ended_run,
    )

    assert decision.stop_reason == EXECUTION_STOP_SWITCHED_OFF


def test_the_shell_adds_a_reason_and_moves_nothing_else() -> None:
    """Every other field is whatever the machine decided. Read field by field."""
    ended = replay_the_incident()[8][1]
    arguments = {
        "mode_executes": False,
        "mode_off": False,
        "targets": [export_publication()],
        "now": WITHDRAWN,
        "evidence": OwnershipEvidence(dispatch_active=False, marker_on=False),
        "progress": progress_of(REALIZED_KWH),
        "carried": None,
        "carry_ended": EXECUTION_STOP_STAGE_A_HOLD,
        "ended_run": ended.ended_run,
    }
    machine = execution_module._decide(**arguments)
    shelled = decide(**arguments)

    assert shelled.stop_reason != machine.stop_reason
    assert shelled.state == machine.state
    assert shelled.ownership == machine.ownership
    assert shelled.reset_required == machine.reset_required
    assert shelled.request_kw == machine.request_kw
    assert shelled.target == machine.target
    assert shelled.demand == machine.demand
    assert shelled.progress == machine.progress
    assert shelled.clear_stale_marker == machine.clear_stale_marker
    assert shelled.notes == machine.notes


def test_the_shell_invents_nothing_when_no_run_ended() -> None:
    """No carry verdict, no added reason -- on every refresh of a quiet day."""
    for offset in (0, 15, 30, 45):
        decision = decide(
            mode_executes=False,
            mode_off=False,
            targets=[],
            now=WINDOW_START + timedelta(minutes=offset),
            evidence=OwnershipEvidence(dispatch_active=False, marker_on=False),
            progress=progress_of(0.0),
            carried=None,
            carry_ended=None,
        )

        assert decision.stop_reason is None
        assert decision.reset_required is False


async def test_reading_only_the_single_refresh_field_is_caught(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The mutation is dropping the record and reading ``ended_reason`` alone.

    That is exactly what a reader had on 25 August: the 15:43 download carried
    ``null`` there, and the reason had to be reconstructed from a second snapshot
    and the event ring. Reproduced rather than described -- the field really is
    ``null`` two refreshes later, and the record really is not.
    """
    coordinator = await stage_b_coordinator(hass, setup_integration)
    reports = replay_into(coordinator)

    mutated = [report["carried"]["ended_reason"] for report in reports[9:]]
    honest = [report["carried"]["last_ended"] for report in reports[9:]]

    assert mutated == [None, None]
    assert all(record is not None for record in honest)
    assert {record["reason"] for record in honest} == {EXECUTION_STOP_STAGE_A_HOLD}


def test_the_direct_path_reports_its_own_reason_without_the_shell() -> None:
    """**The withdrawal arm's own guard, and it needs one.**

    The shell fills in the carry machine's verdict, so on the carried path the arm's
    ungating is belt and braces. On the *direct* path there is no carry verdict to
    fill in: the caller holds a publication id, the arm identifies the ended run
    from it, and the reason exists only because the arm itself no longer discards
    it. Without this test that ungating would be behaviourally unguarded.
    """
    target = charge_publication(WINDOW_START)
    decision = decide(
        mode_executes=False,
        mode_off=False,
        targets=[target],
        now=WINDOW_END + timedelta(minutes=15),
        evidence=OwnershipEvidence(dispatch_active=False, marker_on=False),
        progress=progress_of(REALIZED_KWH),
        running_run_id=target["plan_id"],
        carried=None,
        carry_ended=None,
    )

    assert decision.ownership == OWNERSHIP_NONE
    assert decision.stop_reason == EXECUTION_STOP_WINDOW_ENDED
    assert decision.reset_required is False
    assert decision.target is not None
    assert decision.target.battery_target_kwh == pytest.approx(TARGET_KWH)
