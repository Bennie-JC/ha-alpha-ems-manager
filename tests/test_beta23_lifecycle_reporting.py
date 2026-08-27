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
    CONTROL_MODE_SHADOW,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_NET_EXPORT,
    EXECUTION_INTENT_SERVE_LOAD,
    EXECUTION_STATE_ARMED,
    EXECUTION_STATE_IDLE,
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
    # And the reset gate is not.
    assert sum(row.count("reset_required=owned") for row in code) == 7


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
    """Freshness is checked before affirmation, so this arm wins where both apply."""
    stale = charge_publication(
        WINDOW_START, stale_after=(WINDOW_START + timedelta(minutes=5)).isoformat()
    )
    admitted = carry_forward(None, [stale], ADMITTED)
    assert admitted.carried is not None

    later = WINDOW_START + timedelta(minutes=30)
    outcome = carry_forward(admitted.carried, [stale], later)
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


def view(**overrides) -> object:
    """Return an execution view for a run that has stopped, Shadow by default."""
    params = {
        "target_kwh": TARGET_KWH,
        "delivered_kwh": REALIZED_KWH,
        "intent": EXECUTION_INTENT_GRID_CHARGE,
        "stop_reason": EXECUTION_STOP_STAGE_A_HOLD,
        "executed": False,
    }
    params.update(overrides)
    return activity_module.ExecutionView(**params)


def line(**overrides) -> str:
    """Return the sentence a stopped run produces."""
    return activity_module._stopped_message(view(**overrides))


def test_the_incident_now_reads_as_a_sentence_about_a_battery() -> None:
    """**The observed line was "Shadow run finished: plan ended." and nothing else.**

    Three facts in one clause: what it was doing, why it stopped, how far it got.
    The disclaimer stays -- while the barrier stands, a Shadow line that reads like
    a Live one is the one thing this release cannot publish.
    """
    # beta.24 renamed the withdrawal outcome: "cancelled" is what a withdrawn plan
    # is, and "plan ended" said nothing about which of six endings had happened.
    assert line() == "Charge cancelled - 1.76 / 8.06 kWh, no command sent"


def test_a_live_line_drops_the_disclaimer_and_nothing_else() -> None:
    """Same three facts. The difference between the modes is one clause."""
    assert line(executed=True) == "Charge cancelled - 1.76 / 8.06 kWh"


def test_the_reasons_a_reader_must_tell_apart_read_differently() -> None:
    """Withdrawal, the window, the target, replacement, freshness, the budget."""
    sentences = {
        reason: line(stop_reason=reason, executed=True) for reason in REACHABLE
    }

    assert len(set(sentences.values())) == len(REACHABLE)
    assert sentences[EXECUTION_STOP_STAGE_A_HOLD] == (
        "Charge cancelled - 1.76 / 8.06 kWh"
    )
    assert sentences[EXECUTION_STOP_WINDOW_ENDED] == (
        "Charge stopped - window ended - 1.76 / 8.06 kWh"
    )
    # Short of the target here, so both figures are quoted. The one-figure form is
    # asserted on its own below.
    assert sentences[EXECUTION_STOP_TARGET_REACHED] == (
        "Charge complete - 1.76 / 8.06 kWh"
    )
    assert sentences[EXECUTION_STOP_PLAN_REPLACED] == (
        "Charge stopped - plan replaced - 1.76 / 8.06 kWh"
    )
    assert sentences[EXECUTION_STOP_STALE_PLAN] == (
        "Charge stopped - plan expired - 1.76 / 8.06 kWh"
    )
    assert sentences[EXECUTION_STOP_GRID_CEILING] == (
        "Charge stopped - grid limit - 1.76 / 8.06 kWh"
    )


def test_a_reached_target_quotes_one_figure() -> None:
    """Inside the completion tolerance the two figures are the same number, and
    printing both invites a reader to hunt for a difference that is not there.

    Outside it they are genuinely different and both are quoted: a run that stopped
    at 1.76 of 8.06 did not complete, whatever branch reported it.
    """
    met = line(
        stop_reason=EXECUTION_STOP_TARGET_REACHED, delivered_kwh=8.02, executed=True
    )
    short = line(stop_reason=EXECUTION_STOP_TARGET_REACHED, executed=True)

    assert met == "Charge complete - 8.06 kWh"
    assert met.count("/") == 0
    assert short.count("/") == 1


def test_the_subject_follows_the_intent() -> None:
    """A discharge that stops is not a charge that stops."""
    assert line(intent=EXECUTION_INTENT_SERVE_LOAD, executed=True).startswith(
        "Discharge "
    )
    assert line(intent=EXECUTION_INTENT_NET_EXPORT, executed=True).startswith("Export ")
    # An intent the wording layer does not know is described, not guessed at.
    assert line(intent="", executed=True).startswith("Plan ")


def test_no_reason_reaches_a_person_as_an_identifier() -> None:
    """**Every** declared constant, not only the reachable ones.

    The old line interpolated ``stop_reason`` raw, so a Live grid-budget stop read
    "Dispatch stopped: grid_energy_ceiling." A constant that acquires a branch later
    must not acquire that sentence with it.
    """
    for reason in EXECUTION_STOP_REASONS:
        rendered = line(stop_reason=reason, executed=True)

        # No snake_case, which is the actual shape of an identifier leak.
        assert "_" not in rendered, rendered
        # And no multi-word constant appears verbatim. Single-word constants are
        # excluded on purpose: ``safety`` is an ordinary English word, and
        # "stopped for safety" is a sentence rather than a leaked symbol.
        if "_" in reason:
            assert reason not in rendered, rendered
        # Nor may internal vocabulary arrive spelled out.
        lowered = rendered.lower()
        for term in ("stage a", "stage b", "carry", "affirm", "shadow run", "dispatch"):
            assert term not in lowered, rendered


def test_every_declared_reason_has_wording_of_its_own() -> None:
    """No constant may fall through to the generic phrase."""
    phrases = activity_module._STOP_PHRASES

    assert set(EXECUTION_STOP_REASONS) <= set(phrases)
    assert len(set(phrases.values())) == len(phrases)
    assert all(phrase and phrase == phrase.lower() for phrase in phrases.values())


def test_the_line_stays_one_short_clause() -> None:
    """No paragraph, no second sentence, nothing about what happens next.

    The line must not read as though it explains the Economic Action beside it --
    that implied causation is exactly what sent this investigation down the wrong
    path.
    """
    for reason in EXECUTION_STOP_REASONS:
        for executed in (True, False):
            rendered = line(stop_reason=reason, executed=executed)

            assert not rendered.endswith("."), rendered
            # The longest legitimate form is 73 characters, and the bound is set
            # just above it so a new phrase cannot quietly grow into a paragraph.
            assert len(rendered) <= 76, rendered
            assert "because" not in rendered.lower(), rendered
            assert "\n" not in rendered, rendered


def test_an_unknown_constant_still_produces_a_sentence() -> None:
    """The fallback stays reachable in principle and unreachable in practice."""
    assert line(stop_reason="a_reason_from_the_future", executed=True) == (
        "Charge stopped - plan ended - 1.76 / 8.06 kWh"
    )


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
        assert reason not in line(stop_reason=reason, executed=True)

    # And the fallback no longer describes eight different endings identically.
    rendered = {line(stop_reason=r, executed=True) for r in REACHABLE}
    assert len(rendered) == len(REACHABLE)


def test_a_known_reason_falling_back_to_the_generic_phrase_is_caught() -> None:
    """Only an unknown constant may reach the fallback."""
    generic = line(stop_reason="a_reason_from_the_future", executed=True)

    # beta.24 gave withdrawal its own word, so no reachable reason shares the
    # fallback phrase any more -- there is no longer an exception to make.
    assert activity_module._STOP_PHRASES[EXECUTION_STOP_STAGE_A_HOLD] == "cancelled"
    for reason in REACHABLE:
        assert line(stop_reason=reason, executed=True) != generic, reason


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
    assert activity_module._STOP_PHRASES[EXECUTION_STOP_SWITCHED_TO_SHADOW] == (
        "switched to shadow"
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
