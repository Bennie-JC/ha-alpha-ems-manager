"""Carrying one accepted run across a rolling horizon -- gates A13, A14 and A15.

**These are the gates that would have caught the blocker.** Strict activation is
correct: on this hardware arming delivers energy immediately, so a command may not
exist before the window opens. But every refresh rebuilds the economic horizon from
the *next* interval boundary, so a freshly published target always opens fifteen
minutes from now -- and a controller that evaluates the publication can therefore
only ever reach ``prepared``. Ten consecutive real refreshes measured exactly that:
``gap_to_window_start`` was ``+15 min`` every single time.

The target whose window opens is the one accepted a refresh earlier. Carrying it is
execution continuity, and these tests pin the four things that makes true and the
four bounds that keep it short.

Every assertion here is on instants, intents and identities. Nothing in this file
mentions a price, and nothing asks which of two targets is better -- that would be
Stage B forming an economic opinion, which is the one thing it may never do.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.alpha_ems_manager.alphaess_device import (
    CHARGE_FAMILY as _CHARGE_FAMILY,
)
from custom_components.alpha_ems_manager.const import (
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_HOLD,
    EXECUTION_INTENT_NET_EXPORT,
    EXECUTION_STATE_ARMED,
    EXECUTION_STATE_PREPARED,
    EXECUTION_STATE_STOPPING,
    EXECUTION_STOP_STAGE_A_HOLD,
    EXECUTION_STOP_STALE_PLAN,
    EXECUTION_STOP_WINDOW_ENDED,
)
from custom_components.alpha_ems_manager.execution import (
    OwnershipEvidence,
    admit,
    affirms,
    carry_forward,
    control_intent_for,
    decide,
    mint_run_id,
    parse_target,
)

from .test_stage_b_controller import (
    CLOSES,
    DISPATCH_START,
    OPENS,
    matching_record,
    progress_of,
    raw_target,
)

# The horizon begins at the next boundary, so a refresh at 10:30 publishes a run
# that opens at 10:45. That single fact is the whole reason this module exists.
BEFORE = OPENS - timedelta(minutes=15)


def published(start: datetime, end: datetime = CLOSES, **overrides) -> dict:
    """Return a publication opening at ``start``, identified the way Stage A does.

    ``plan_id`` is derived from the window, because that is what Stage A actually
    does -- ``sha256(intent | window_start)``. Faking a stable id here would hide
    the churn these tests exist to survive.
    """
    payload = raw_target(
        window_start=start.isoformat(),
        window_end=end.isoformat(),
        plan_id=f"pub-{start.isoformat()}",
        issued_at=(start - timedelta(minutes=15)).isoformat(),
        stale_after=(start + timedelta(hours=8)).isoformat(),
    )
    payload.update(overrides)
    return payload


def evidence_for(run_id: str) -> OwnershipEvidence:
    """Return evidence that establishes ownership of ``run_id``."""
    return OwnershipEvidence(
        dispatch_active=True,
        marker_on=True,
        record=matching_record(run_id=run_id),
        dispatch_start=DISPATCH_START,
        run_id=run_id,
    )


def decision_for(carry, now, *, mode_executes=False, evidence=None, delivered=0.0):
    """Return the decision for one refresh of a carried run."""
    return decide(
        mode_executes=mode_executes,
        mode_off=False,
        targets=(),
        now=now,
        evidence=evidence or OwnershipEvidence(dispatch_active=False, marker_on=False),
        progress=progress_of(delivered),
        current_energy_kwh=9.0,
        remaining_expected_pv_kwh=4.0,
        carried=carry.carried,
        carry_ended=carry.ended,
        ended_run=carry.ended_run,
        running_run_id=None if evidence is None else evidence.run_id,
    )


# ===========================================================================
# A13. the measured failure, reproduced and then fixed
# ===========================================================================


def test_a_published_target_can_never_open_its_own_window() -> None:
    """**The blocker, stated as a test before it is fixed.**

    This is what ten real refreshes measured. Reading the publication, the gap to
    the window start is one interval at *every* refresh, so strict activation --
    which is correct -- can never be satisfied. The bug was not the activation
    rule; it was evaluating a target that is always in the future.
    """
    for minute in range(0, 60, 15):
        now = OPENS + timedelta(minutes=minute)
        latest = parse_target(published(now + timedelta(minutes=15)))
        assert latest is not None
        assert not latest.activatable_at(now)
        gap = (latest.window_start - now).total_seconds() / 60.0
        assert gap == 15.0


def test_the_admitted_run_survives_the_publication_that_replaced_it() -> None:
    """**A13, and the whole of the fix.**

    09:00 admits a run opening at 09:15. At 09:15 Stage A publishes a *different*
    target opening at 09:30 -- a new ``plan_id``, because the id is derived from the
    window. The admitted run is not erased by it: it is affirmed, its own window is
    now open, and it becomes actionable.
    """
    nine = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
    quarter_past = nine + timedelta(minutes=15)
    half_past = nine + timedelta(minutes=30)
    closes = nine + timedelta(hours=3)

    first = carry_forward(None, [published(quarter_past, closes)], nine)
    assert first.admitted
    assert first.carried is not None
    run_id = first.carried.run_id

    # At 09:00 the window is fifteen minutes away: everything is computed, and
    # nothing may be sent.
    early = decision_for(first, nine)
    assert early.state == EXECUTION_STATE_PREPARED
    assert early.request_kw == 0.0

    # 09:15. Stage A now publishes a run opening at 09:30, under a new id.
    second = carry_forward(first.carried, [published(half_past, closes)], quarter_past)
    assert second.affirmed
    assert second.carried is not None
    # The identity held, and the publication's did not.
    assert second.carried.run_id == run_id
    assert second.carried.window_start == quarter_past
    assert (
        published(half_past, closes)["plan_id"]
        != published(quarter_past, closes)["plan_id"]
    )

    # And this is the state beta.19 could not reach from any input at all.
    late = decision_for(second, quarter_past)
    assert late.state == EXECUTION_STATE_ARMED
    assert late.request_kw > 0.0
    assert second.carried.actionable_at(quarter_past)


def test_the_run_identity_holds_while_the_publication_identity_churns() -> None:
    """Across a whole campaign, and the two are asserted separately.

    The identity Stage B executes against must survive the horizon rolling; the
    identity Stage A publishes must be seen to churn, or this proves nothing.
    """
    now = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
    closes = now + timedelta(hours=4)
    carry = carry_forward(None, [published(now + timedelta(minutes=15), closes)], now)
    assert carry.carried is not None
    run_id = carry.carried.run_id
    plan_ids = set()

    for step in range(1, 12):
        moment = now + timedelta(minutes=15 * step)
        payload = published(moment + timedelta(minutes=15), closes)
        plan_ids.add(payload["plan_id"])
        carry = carry_forward(carry.carried, [payload], moment)
        assert carry.carried is not None, step
        assert carry.carried.run_id == run_id, step
        # The accepted window is never moved by a later publication. That is
        # exactly what makes activation reachable.
        assert carry.carried.window_start == now + timedelta(minutes=15)

    assert len(plan_ids) == 11


def test_an_affirmed_run_is_never_reported_as_replaced() -> None:
    """The defect keying continuity on ``plan_id`` would have left behind.

    ``decide`` stops an owned run whose identity no longer matches the one being
    executed. Against the publication identity that fired **every fifteen minutes**
    for the whole of any owned campaign -- stopping and resetting a run nothing had
    replaced. It was unreachable only because ownership was.
    """
    now = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
    closes = now + timedelta(hours=4)
    carry = carry_forward(None, [published(now + timedelta(minutes=15), closes)], now)
    assert carry.carried is not None
    evidence = evidence_for(carry.carried.run_id)

    for step in range(1, 8):
        moment = now + timedelta(minutes=15 * step)
        carry = carry_forward(
            carry.carried, [published(moment + timedelta(minutes=15), closes)], moment
        )
        decision = decision_for(
            carry, moment, mode_executes=True, evidence=evidence, delivered=0.4 * step
        )
        assert decision.stop_reason is None, (step, decision.stop_reason)
        assert not decision.reset_required, step


def test_a_carried_run_yields_a_charge_intent_only_once_its_window_opens() -> None:
    """The seam, driven from the carried run rather than from a publication."""
    now = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
    opens = now + timedelta(minutes=15)
    carry = carry_forward(None, [published(opens, now + timedelta(hours=3))], now)

    before = control_intent_for(
        decision_for(carry, now),
        floor_soc_percent=20.0,
        ceiling_soc_percent=100.0,
        horizon_minutes=20,
        target_day=now.date(),
        start_index=36,
        built_at=now,
    )
    assert before is None

    carry = carry_forward(
        carry.carried, [published(opens + timedelta(minutes=15))], opens
    )
    after = control_intent_for(
        decision_for(carry, opens),
        floor_soc_percent=20.0,
        ceiling_soc_percent=100.0,
        horizon_minutes=20,
        target_day=opens.date(),
        start_index=37,
        built_at=opens,
    )
    assert after is not None
    assert after.action == "charge"
    assert after.average_power_kw > 0.0


# ===========================================================================
# A14. withdrawal and supersession
# ===========================================================================


def test_rolling_movement_affirms_and_a_move_elsewhere_does_not() -> None:
    """The whole discrimination, in one place, on instants alone.

    Rolling movement always overlaps -- the new start is one interval later, deep
    inside the accepted window. A campaign Stage A has genuinely moved to tonight
    starts after the accepted window ends, and does not.
    """
    run = admit(parse_target(published(OPENS)), BEFORE)

    rolled = parse_target(published(OPENS + timedelta(minutes=15)))
    assert affirms(run, rolled)

    tonight = parse_target(
        published(CLOSES + timedelta(hours=4), CLOSES + timedelta(hours=6))
    )
    assert not affirms(run, tonight)


def test_a_non_overlapping_publication_of_the_same_intent_withdraws_the_run() -> None:
    """Same intent, different campaign. It is not the run that was accepted."""
    run = admit(parse_target(published(OPENS)), BEFORE)
    tonight = published(CLOSES + timedelta(hours=4), CLOSES + timedelta(hours=6))

    outcome = carry_forward(run, [tonight], OPENS)

    assert outcome.carried is None
    assert outcome.ended == EXECUTION_STOP_STAGE_A_HOLD
    assert outcome.ended_run is run


def test_the_intent_vanishing_entirely_withdraws_the_run() -> None:
    """Absence and movement are the same signal, because the contract carries one.

    Stage A publishes no tombstone: a withdrawn run and a rolled-forward run are
    both simply not in the next publication. That is why overlap is described as an
    inference and never as a cancellation signal.
    """
    run = admit(parse_target(published(OPENS)), BEFORE)

    for payload in ([], [published(OPENS, intent=EXECUTION_INTENT_HOLD)]):
        outcome = carry_forward(run, payload, OPENS)
        assert outcome.carried is None
        assert outcome.ended == EXECUTION_STOP_STAGE_A_HOLD


def test_an_opposite_direction_publication_does_not_become_a_carried_charge() -> None:
    """Supersession never mutates a charge into a discharge.

    Stage B executes one direction. An export publication cannot affirm a charge
    run and cannot be admitted as one, so there is no input that turns a carried
    charge into a discharge.
    """
    run = admit(parse_target(published(OPENS)), BEFORE)
    export = published(
        OPENS + timedelta(minutes=15), intent=EXECUTION_INTENT_NET_EXPORT
    )

    outcome = carry_forward(run, [export], OPENS)
    assert outcome.carried is None

    fresh = carry_forward(None, [export], OPENS)
    assert fresh.carried is None
    assert not fresh.admitted


def test_a_withdrawal_resets_an_owned_run_and_never_a_foreign_one() -> None:
    """Ending is not enough; an owned dispatch has to be stopped."""
    run = admit(parse_target(published(OPENS)), BEFORE)
    outcome = carry_forward(run, [], OPENS)

    owned = decision_for(
        outcome, OPENS, mode_executes=True, evidence=evidence_for(run.run_id)
    )
    assert owned.state == EXECUTION_STATE_STOPPING
    assert owned.stop_reason == EXECUTION_STOP_STAGE_A_HOLD
    assert owned.reset_required

    foreign = decision_for(
        outcome,
        OPENS,
        mode_executes=True,
        evidence=OwnershipEvidence(dispatch_active=True, marker_on=False),
    )
    assert not foreign.reset_required

    unproven = decision_for(
        outcome,
        OPENS,
        mode_executes=True,
        evidence=OwnershipEvidence(dispatch_active=True, marker_on=True),
    )
    assert not unproven.reset_required


def test_two_runs_are_never_carried_at_once() -> None:
    """R5, structurally: ending and admitting cannot happen in one refresh.

    The old run ends on the refresh that fails to affirm it, and the next one is
    admitted no earlier than the refresh after -- so a reset always lands before a
    new claim. It costs one interval of latency on a genuine supersession, and
    admission only ever needs to happen one refresh before a window opens.
    """
    run = admit(parse_target(published(OPENS)), BEFORE)
    tonight = published(CLOSES + timedelta(hours=4), CLOSES + timedelta(hours=6))

    ending = carry_forward(run, [tonight], OPENS)
    assert ending.carried is None
    assert ending.ended is not None
    assert not ending.admitted


def test_the_four_bounds_that_keep_a_carried_run_short() -> None:
    """A carried plan may survive refreshes. It may not become an indefinite one."""
    run = admit(parse_target(published(OPENS)), BEFORE)

    # 1. its own window end.
    ended = carry_forward(run, [published(OPENS)], CLOSES)
    assert ended.carried is None
    assert ended.ended == EXECUTION_STOP_WINDOW_ENDED

    # 2. freshness, re-anchored on each affirmation but never removed.
    brief = admit(
        parse_target(
            published(OPENS, stale_after=(OPENS + timedelta(minutes=5)).isoformat())
        ),
        BEFORE,
    )
    stale = carry_forward(brief, [published(OPENS)], OPENS + timedelta(minutes=10))
    assert stale.carried is None
    assert stale.ended == EXECUTION_STOP_STALE_PLAN

    # 3. withdrawal, within a single refresh. 4. the grid ceiling, which the
    # budget gate owns and which is asserted where it is enforced.
    assert carry_forward(run, [], OPENS).carried is None


def test_affirmation_re_anchors_freshness_rather_than_extending_the_window() -> None:
    """The two are different clocks and conflating them was the earlier bug.

    An affirming publication is Stage A restating the intent, so the freshness
    deadline moves. The *window* does not: moving it would let a carried run outlive
    the economics that chose it.
    """
    run = admit(parse_target(published(OPENS)), BEFORE)
    later = OPENS + timedelta(minutes=30)

    outcome = carry_forward(run, [published(later + timedelta(minutes=15))], later)

    assert outcome.carried is not None
    assert outcome.carried.stale_after > run.stale_after
    assert outcome.carried.window_start == run.window_start
    assert outcome.carried.window_end == run.window_end
    assert outcome.carried.affirmed_at == later
    assert outcome.carried.admitted_at == run.admitted_at


def test_a_revision_tracks_a_material_change_and_ignores_the_horizon() -> None:
    """``window_start`` advancing every refresh is the horizon, not news.

    beta.19 read exactly that as novelty. A revision means Stage A moved an
    executable figure.
    """
    run = admit(parse_target(published(OPENS)), BEFORE)
    later = OPENS + timedelta(minutes=15)

    same = carry_forward(run, [published(later)], later)
    assert same.carried is not None
    assert same.carried.revision == run.revision

    moved = carry_forward(run, [published(later, battery_target_kwh=18.4)], later)
    assert moved.carried is not None
    assert moved.carried.revision == run.revision + 1
    # And the accepted figures are not overwritten: progress is measured against
    # them, and a publication's remaining target shrinks as the horizon eats the
    # run, so adopting it would count delivered energy twice.
    assert moved.carried.target.battery_target_kwh == run.target.battery_target_kwh


# ===========================================================================
# A15. a restart discards the run and keeps the claim
# ===========================================================================


def test_the_run_identity_is_deterministic_and_distinct_from_a_publication() -> None:
    """Reproducible in a test, and not confusable with Stage A's id."""
    first = mint_run_id(EXECUTION_INTENT_GRID_CHARGE, OPENS, BEFORE)
    again = mint_run_id(EXECUTION_INTENT_GRID_CHARGE, OPENS, BEFORE)
    other = mint_run_id(
        EXECUTION_INTENT_GRID_CHARGE, OPENS, BEFORE + timedelta(hours=1)
    )

    assert first == again
    assert first != other
    assert len(first) == 16
    assert first != published(OPENS)["plan_id"]


def test_the_carried_record_carries_only_what_execution_needs() -> None:
    """No mutable convenience copies: a second source of truth is a second bug."""
    run = admit(parse_target(published(OPENS)), BEFORE)
    payload = run.as_dict()

    assert payload["run_id"] == run.run_id
    assert payload["window_start"] == OPENS.isoformat()
    assert payload["battery_target_kwh"] == 11.94
    assert payload["expected_grid_to_battery_kwh"] == 3.0
    # The publication that admitted it, for tracing only.
    assert payload["plan_id"] == published(OPENS)["plan_id"]
    # Nothing that would let a reader mistake this for a live figure.
    assert "request_kw" not in payload
    assert "realized_kwh" not in payload


# ===========================================================================
# A2. everything that is not an actionable charge yields no command
# ===========================================================================


def intent_at(carry, now, **overrides):
    """Return the Stage-B intent for a refresh, or ``None``."""
    fields = {
        "floor_soc_percent": 20.0,
        "ceiling_soc_percent": 100.0,
        "horizon_minutes": 20,
        "target_day": now.date(),
        "start_index": 40,
        "built_at": now,
    }
    fields.update(overrides)
    return control_intent_for(decision_for(carry, now, **{}), **fields)


def test_no_intent_stage_b_does_not_execute_produces_a_command() -> None:
    """**A2.** Every one of them yields ``None``, never the opposite direction.

    ``None`` is what leaves the reserve-guard path exactly as it was: Stage B
    declines to be the command source rather than substituting something. There is
    no branch here that can produce a discharge, so an unsupported intent cannot
    become one by accident.
    """
    for intent in (
        EXECUTION_INTENT_NET_EXPORT,
        EXECUTION_INTENT_HOLD,
        "serve_load",
    ):
        carry = carry_forward(None, [published(OPENS, intent=intent)], BEFORE)
        assert carry.carried is None, intent
        assert intent_at(carry, OPENS) is None, intent


def test_a_blocked_or_inhibited_refresh_produces_no_command() -> None:
    """A foreign or unproven dispatch is never touched, and never commanded over."""
    carry = carry_forward(None, [published(OPENS)], BEFORE)
    run = carry.carried
    assert run is not None
    carry = carry_forward(run, [published(OPENS + timedelta(minutes=15))], OPENS)

    for evidence in (
        OwnershipEvidence(dispatch_active=True, marker_on=False),
        OwnershipEvidence(dispatch_active=True, marker_on=True),
    ):
        decision = decision_for(carry, OPENS, mode_executes=True, evidence=evidence)
        assert not decision.wants_command
        assert not decision.reset_required
        assert (
            control_intent_for(
                decision,
                floor_soc_percent=20.0,
                ceiling_soc_percent=100.0,
                horizon_minutes=20,
                target_day=OPENS.date(),
                start_index=43,
                built_at=OPENS,
            )
            is None
        )


def test_a_charge_with_no_establishable_ceiling_is_refused_not_substituted() -> None:
    """**A6's refusal half.** A fabricated bound is a silent wrong instruction.

    The discharge floor must be unreachable from the charge path -- not clamped,
    not reused with a different argument. Reusing it is what would have written
    about 21 % as a charge cutoff while the pack sat at 61 %.
    """
    from custom_components.alpha_ems_manager.alphaess_device import (
        build_command,
        plan_commands,
    )

    carry = carry_forward(None, [published(OPENS)], BEFORE)
    carry = carry_forward(
        carry.carried, [published(OPENS + timedelta(minutes=15))], OPENS
    )
    decision = decision_for(carry, OPENS)

    blind = control_intent_for(
        decision,
        floor_soc_percent=20.0,
        ceiling_soc_percent=None,
        horizon_minutes=20,
        target_day=OPENS.date(),
        start_index=43,
        built_at=OPENS,
    )
    assert blind is not None, "the refusal belongs at the device boundary"
    command = build_command(blind)
    steps = plan_commands(command)
    # Nothing that moves energy: no activation, and no floor-derived cutoff.
    assert not steps or all(
        step.entity_id != _CHARGE_FAMILY.activate for step in steps
    ), [step.entity_id for step in steps]
