"""beta.35: a multi-quarter campaign survives its own boundaries, or dies once.

**Gate 1 of the release, and the reason it exists is a hardware trace.** The
2026-08-29 Sell executed its first quarter perfectly and then, at the very
boundary the design was built around, reset itself -- and then kept walking. See
``beta35_trace`` for the measured sequence.

Three defects had to line up, and beta.34 supplied all three:

1. the ownership claim was persisted with ``stale_after = quarter.quarter_end``,
   so the refresh that fires *at* that boundary adopts a claim already expired;
2. adoption set ``_quarter_progress_unknown`` unconditionally, which forces a
   reset with no stop reason at all;
3. the sustain compared against ``self._carried`` alone, which is ``None`` on
   every ordinary quarter-authority refresh, so continuation was unreachable.

And a fourth, which turned a bad stop into a worse one: the teardown was partial.
``self._plan`` survived, so the terminated campaign's schedule advanced a row and
re-armed the inverter fifteen minutes later.

The tests below drive **real refreshes** through ``step_once``. That is the whole
point: beta.32's multi-quarter tests called ``_async_end_row`` directly and never
ran a refresh at a boundary against a publication that fails to affirm, which is
exactly the path production takes and exactly why this shipped.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.alphaess_device import (
    DISPATCH_DEADMAN_MINUTES,
    DISPATCH_DURATION,
    DISPATCH_ENABLE,
)
from custom_components.alpha_ems_manager.const import (
    EXECUTION_ABORT_IS_TOTAL,
    EXECUTION_ABORT_STOP_REASONS,
    EXECUTION_INTENT_NET_EXPORT,
    EXECUTION_STOP_CAMPAIGN_COMPLETE,
    EXECUTION_STOP_SAFETY,
    EXECUTION_STOP_STAGE_A_HOLD,
    EXECUTION_STOP_STALE_PLAN,
    EXECUTION_STOP_WINDOW_ENDED,
    EXECUTION_WITHDRAWAL_STOP_REASONS,
    OWNERSHIP_OWNED,
)

from .beta35_trace import (
    BATTERY_TARGET_KWH,
    CAMPAIGN_ID,
    METER_TARGET_KWH,
    Q1_BATTERY_KWH,
    Q1_METER_KWH,
    admitted_plan,
    moved_the_plan_away,
    opens_at,
    step_clock,
    zero_objective_plan,
)
from .forecast_helpers import NORMAL, local
from .test_beta24_live_charge import (
    LiveSurface,
    charge_now_price,
    live_coordinator,
    owned_live_charge,
    step_once,
)

pytestmark = pytest.mark.usefixtures("control_surface")

QUARTER = timedelta(minutes=15)


@pytest.fixture
def live_surface(hass: HomeAssistant, control_surface: None) -> LiveSurface:
    """Return a control surface that responds to writes."""
    return LiveSurface(hass)


def one_admissible_target(**overrides) -> dict:
    """Return a published export target, shaped as Stage A publishes one."""
    opens = local(NORMAL, 10, 45)
    payload = {
        "plan_id": "5a4f54a741429531",
        "revision": 1,
        "intent": EXECUTION_INTENT_NET_EXPORT,
        "purpose": "export",
        "window_start": opens.isoformat(),
        "window_end": (opens + 3 * QUARTER).isoformat(),
        "issued_at": (opens - timedelta(minutes=15)).isoformat(),
        "stale_after": (opens + timedelta(minutes=30)).isoformat(),
        "battery_target_kwh": BATTERY_TARGET_KWH,
        "grid_target_kwh": METER_TARGET_KWH,
        "average_power_kw": 9.0,
        "first_power_kw": 10.0,
        "reserve_floor_kwh": 4.32,
        "campaign_id": CAMPAIGN_ID,
        "quarter_schedule": [row.as_dict() for row in admitted_plan().rows],
    }
    payload.update(overrides)
    return payload


def _target_at(opens, *, stale_minutes: int):
    """Return a parsed target opening at ``opens`` with a bounded deadline."""
    from custom_components.alpha_ems_manager.execution import parse_target

    target = parse_target(
        one_admissible_target(
            window_start=opens.isoformat(),
            window_end=(opens + 3 * QUARTER).isoformat(),
            issued_at=opens.isoformat(),
            stale_after=(opens + timedelta(minutes=stale_minutes)).isoformat(),
        )
    )
    assert target is not None
    return target


async def start_the_campaign(hass, config_data, frank, live_surface, monkeypatch):
    """Drive the live trace through quarter one, and return the coordinator.

    **Quarter one is armed by production, not asserted by the test.** The frozen
    schedule is installed on a clean Live coordinator, the first row is derived
    from it, and two real refreshes then do what the hardware did on 2026-08-29:
    arm the export, prove ownership from the device's own readback, and sustain.
    Nothing here writes an ownership claim by hand -- a hand-written claim is the
    one thing that cannot reproduce this defect, because ownership has to be
    *proven* against the running dispatch and a synthetic record proves nothing.

    Quarter one's measured delivery is then accrued as
    ``_record_completed_quarter`` would have, and Stage A moves the Sell out of the
    admitted window -- the condition that destroyed the live campaign.
    """
    from .forecast_helpers import history_before, seed
    from .frank_capture import synthetic_day
    from .test_economic_published import allow_trading

    coordinator = await live_coordinator(hass, config_data)
    seed(coordinator, history_before(NORMAL))
    frank.publish(today=synthetic_day(NORMAL, price_at=charge_now_price), tomorrow=None)
    allow_trading(coordinator, allow_grid_charging=True, allow_battery_export=True)

    # **Stage A has already moved on before the first row opens.** That is the
    # live condition, and putting it here rather than after quarter one keeps the
    # replay faithful to production in the one way that matters: with no carried
    # run to claim under, the arm claims under the admitted plan, so the record
    # and the plan name the same run. ``admit_plan`` guarantees exactly that
    # identity in production -- see
    # ``test_an_admitted_plan_adopts_the_identity_of_the_run_it_came_from``.
    moved_the_plan_away(coordinator, monkeypatch)
    plan = admitted_plan()
    coordinator._plan = plan
    coordinator._carried = None
    coordinator._quarter = plan.executing_quarter(opens_at(0) + timedelta(minutes=1))
    assert coordinator._quarter is not None

    # Arm, then sustain -- ownership is only provable once the readback lands.
    armed = await step_once(hass, coordinator, live_surface, **step_clock(0))
    assert (armed.get("execution") or {}).get("write_boundary", {}).get(
        "sequence"
    ) == "arm"
    sustained = await step_once(hass, coordinator, live_surface, **step_clock(0))
    execution = sustained.get("execution") or {}
    assert (execution.get("ownership") or {}).get("state") == OWNERSHIP_OWNED, execution
    assert coordinator._campaign_id == CAMPAIGN_ID
    assert coordinator._campaign_started_at is not None

    # The arm and the schedule agree about which run owns execution -- the
    # invariant a sustain across the boundary depends on.
    record = coordinator.store.execution_record or {}
    assert record.get("run_id") == plan.run_id

    # Quarter one delivered what the hardware delivered.
    coordinator._accrue_campaign_progress(coordinator._quarter, Q1_METER_KWH)
    live_surface.calls.clear()
    return coordinator


# ===========================================================================
# 1. the vocabularies -- withdrawal is not abort
# ===========================================================================


def test_the_two_stop_vocabularies_do_not_overlap() -> None:
    """**The distinction the whole fix rests on, pinned as a partition.**

    A reason that appeared in both sets could be withheld *and* would abort, and
    which happened would depend on evaluation order -- which is precisely the kind
    of accident this release exists to remove.
    """
    withdrawal = set(EXECUTION_WITHDRAWAL_STOP_REASONS)
    abort = set(EXECUTION_ABORT_STOP_REASONS)

    assert not withdrawal & abort
    assert EXECUTION_STOP_STALE_PLAN in withdrawal
    assert EXECUTION_STOP_STAGE_A_HOLD in withdrawal
    assert EXECUTION_STOP_SAFETY in abort
    # The natural terminal belongs to neither: it is how a campaign *finishes*.
    assert EXECUTION_STOP_WINDOW_ENDED not in withdrawal
    assert EXECUTION_STOP_WINDOW_ENDED not in abort


# ===========================================================================
# 2. the claim outlives the boundary it was made for
# ===========================================================================


async def test_the_claim_is_bounded_by_the_plan_not_by_the_row(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**The one line that cost a hardware Sell.**

    beta.34 persisted ``stale_after = quarter.quarter_end``. The refresh that opens
    the *next* row fires a few seconds after that instant, adopts the record, and
    reads it as stale -- always, by construction, with no jitter involved. The
    claim must instead last as long as the authority that made it.

    *Mutation: restore ``stale_after=quarter.quarter_end`` and this fails.*
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)
    plan = admitted_plan()
    coordinator._plan = plan
    coordinator._quarter = plan.executing_quarter(opens_at(0) + timedelta(minutes=1))
    assert coordinator._quarter is not None

    claim = coordinator._claim_authority(None)
    assert claim is not None

    # The instant the next refresh actually fires at: a few seconds past the row's
    # end, which is where the live one landed.
    next_refresh = opens_at(1) + timedelta(seconds=6)
    assert claim.stale_after > next_refresh, (
        "a claim that expires at its own row's end is stale before anything reads it"
    )
    # And still bounded -- it may not outlive the schedule that authorised it.
    assert claim.stale_after == plan.ends_at


# ===========================================================================
# 3. Q1 -> Q2 -> Q3 through real refreshes
# ===========================================================================


async def test_the_campaign_survives_both_boundaries(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**The whole of Gate 1, on the measured trace.**

    Stage A stops carrying the run at the first boundary -- it publishes a Sell at
    21:00, which shares no interval with the admitted 19:45-20:30 window, so
    ``affirms`` is false. That is a revision of the future and nothing more, and it
    must not touch a row that is already frozen and already running.

    *Mutation: delete the withdrawal suppression, or narrow ``sustaining`` back to
    the carried run, and this fails.*
    """
    coordinator = await start_the_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    admitted = coordinator._plan
    assert admitted is not None

    report = await step_once(hass, coordinator, live_surface, **step_clock(1))

    # **beta.38: the withdrawal no longer arises at all.** Through beta.37 it was
    # raised by ``carry_forward`` and then outranked here, which left a terminal
    # filed against a running run. ``carry_forward`` now keeps a run whose frozen
    # row is open, so there is nothing to outrank -- and the run is never nominally
    # ended. Both facts are asserted, because "nothing was withheld" alone would
    # also be satisfied by the suppression silently disappearing.
    execution = report.get("execution") or {}
    authority = (execution.get("write_boundary") or {}).get("authority") or {}
    carried_block = execution.get("carried") or {}
    assert authority.get("plan_authority_holds") is True
    assert authority.get("withheld_stop_reason") is None
    assert carried_block.get("ended_reason") is None, carried_block
    assert carried_block.get("last_ended") is None, "no terminal for a running row"
    assert EXECUTION_STOP_STAGE_A_HOLD in EXECUTION_WITHDRAWAL_STOP_REASONS

    # **A continuation, named as one.** "not a reset" would also be satisfied by
    # doing nothing at all, which is the state quarter two actually spent fifteen
    # minutes in: described, authorised by nobody, and moving 0.001 kWh.
    assert (execution.get("write_boundary") or {}).get("sequence") == "sustain"
    assert coordinator._plan is not None
    assert coordinator._plan.plan_id == admitted.plan_id, (
        "the same frozen schedule, not a re-derived one"
    )
    assert coordinator._campaign_id == CAMPAIGN_ID
    assert coordinator._campaign_started_at is not None

    # The second frozen row is now the executing quarter.
    assert coordinator._quarter is not None
    assert coordinator._quarter.quarter_start == opens_at(1)
    assert coordinator._quarter.grid_export_target_kwh == pytest.approx(2.28)

    # Quarter one's measured export is still there.
    assert coordinator._campaign_realized_kwh == pytest.approx(Q1_METER_KWH)

    # And the third row follows the second.
    await step_once(hass, coordinator, live_surface, **step_clock(2))
    assert coordinator._campaign_id == CAMPAIGN_ID
    assert coordinator._quarter is not None
    assert coordinator._quarter.quarter_start == opens_at(2)
    assert coordinator._campaign_realized_kwh == pytest.approx(Q1_METER_KWH)


async def test_the_frozen_objective_is_non_null_and_comes_from_the_schedule(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**The freeze can no longer depend on a publication that cannot describe it.**

    ``execution_targets`` is this refresh's solve, whose head is ``elapsed + 1``. A
    campaign whose remaining rows are behind that head appears in none of it, so
    every read returned ``None`` -- and a sale that had already moved 1.92 kWh was
    published with ``frozen_target_kwh: null`` and closed as *target unavailable*.

    The frozen schedule has no such problem, and it is where the objective now
    comes from.

    *Mutation: drop the plan source from ``_campaign_objective_kwh`` and this
    fails.*
    """
    coordinator = await start_the_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )

    frozen = coordinator._campaign_frozen_target_kwh
    assert frozen is not None, "a started campaign must know what it promised"
    assert frozen == pytest.approx(METER_TARGET_KWH, abs=0.01)

    # And it survives the boundary that used to erase it.
    await step_once(hass, coordinator, live_surface, **step_clock(1))
    assert coordinator._campaign_frozen_target_kwh == pytest.approx(frozen)


async def test_ownership_and_the_deadman_survive_the_boundary(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """A continuation is a sustain, not a fresh arm.

    Stopping and re-arming at every boundary would return the pack to rest, re-run
    the whole authorisation sequence and re-anchor the vendor timer three times
    inside one sale -- each with its own chance of an unverified write. The dispatch
    stays enabled and the dead-man is re-armed in place.
    """
    coordinator = await start_the_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )

    await step_once(hass, coordinator, live_surface, **step_clock(1))

    assert hass.states.get(DISPATCH_ENABLE).state == "on"
    # **Arm, claim and sustain agree about which run owns execution.** That they
    # could not was the beta.34 defect: the arm accepted the admitted plan and the
    # sustain compared against the carried run alone.
    record = coordinator.store.execution_record
    assert record is not None
    assert record["run_id"] == coordinator._authority_run_id()
    # **The dead-man was re-armed, which is the only reason a run continues.**
    # The power helper is rewritten only when the quantised setpoint has actually
    # moved -- writing a helper a value it already holds buys nothing -- so the
    # duration is what proves the sustain ran rather than being skipped.
    durations = [
        call.data["value"]
        for call in live_surface.calls
        if call.data.get("entity_id") == DISPATCH_DURATION
    ]
    assert durations, "the boundary must re-arm the vendor dead-man"
    assert durations[-1] in DISPATCH_DEADMAN_MINUTES


# ===========================================================================
# 4. a genuine abort is total, and never re-arms
# ===========================================================================


async def test_a_safety_abort_stops_at_once_and_q3_never_rearms(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**The other half of A9, and it is asserted on the write log.**

    Safety is in ``EXECUTION_ABORT_STOP_REASONS`` and may never be withheld. When
    it fires, the teardown is total: the record, the row, the schedule, the carried
    run and the campaign all go, one terminal is filed, and the identity is
    remembered so the surviving rows cannot come back -- which is exactly what they
    did on 2026-08-29 fifteen minutes after the campaign had been declared over.

    *Mutation: leave ``self._plan`` alive on abort, or remove the re-arm guard, and
    the final assertion fails.*
    """
    coordinator = await start_the_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )

    coordinator._abandon_execution(opens_at(1), EXECUTION_STOP_SAFETY)

    # Total: nothing of the authority survives.
    assert coordinator._plan is None
    assert coordinator._carried is None
    assert coordinator._quarter is None
    assert coordinator.store.execution_record is None
    assert coordinator._campaign_id is None

    # Exactly one terminal, and it reports the energy that actually moved.
    terminal = coordinator._closed_campaign
    assert terminal is not None
    assert terminal["campaign_id"] == CAMPAIGN_ID
    assert terminal["objective_realized_kwh"] == pytest.approx(Q1_METER_KWH)
    assert terminal["objective_target_kwh"] is not None

    # And the third row cannot resurrect it, however the clock advances.
    live_surface.calls.clear()
    await step_once(hass, coordinator, live_surface, **step_clock(2))
    armed = [
        call
        for call in live_surface.calls
        if call.data.get("entity_id") == DISPATCH_ENABLE and call.service == "turn_on"
    ]
    assert armed == [], "an abandoned schedule must never arm the inverter again"
    assert coordinator._campaign_id is None


async def test_an_aborted_attempt_is_replaced_by_a_new_instance_not_reopened(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**Realised history is immutable, and beta.36 says of what.**

    beta.35 wrote this rule at the level of the campaign *identity*, and that is what
    destroyed two Live campaigns. ``campaign_identity`` is a digest of the campaign's
    end, so every republication of one live campaign is byte-identical -- and a single
    hazard abort therefore barred that campaign from ever admitting a plan again for
    the rest of the session. The 2026-08-31 capture shows the consequence directly:
    an affirmed carried run accumulating energy beside ``admitted_plan: null``, for
    every refresh until the process restarted.

    The rule that actually holds is about the **attempt**. A hazard abort ends one
    physical attempt; a genuinely new admission afterwards is a second attempt, with
    its own frozen objective, its own realised total and its own terminal. What may
    never happen is the *closed* attempt coming back: its instance identity is dead,
    its terminal is final, and its measured energy is never touched again.

    *Mutation: let ``_note_campaign_progress`` reuse the closed instance's identity,
    or carry its realised total forward, and this fails.*
    """
    coordinator = await start_the_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    first_instance = coordinator._campaign_instance_id
    first_opened = coordinator._campaign_opened_at
    assert first_instance is not None
    assert first_opened is not None

    coordinator._abandon_execution(opens_at(1), EXECUTION_STOP_SAFETY)
    first_terminal = dict(coordinator._closed_campaign or {})
    assert first_terminal
    assert first_terminal["campaign_instance_id"] == first_instance
    realised = first_terminal["objective_realized_kwh"]
    assert realised > 0.0, "the aborted attempt measured something"

    # Put the schedule back and drive a refresh at the third row's window: this is
    # Stage A republishing a campaign its horizon still contains.
    coordinator._plan = admitted_plan()
    coordinator._quarter = coordinator._plan.executing_quarter(
        opens_at(2) + timedelta(minutes=1)
    )
    coordinator._note_campaign_progress(opens_at(2) + timedelta(minutes=1), None)

    assert coordinator._campaign_id == CAMPAIGN_ID, "a new attempt may execute"
    assert coordinator._campaign_instance_id is not None
    assert coordinator._campaign_instance_id != first_instance, (
        "a second attempt is a second instance, never the closed one reopened"
    )
    assert coordinator._campaign_opened_at != first_opened
    assert coordinator._campaign_realized_kwh == 0.0, (
        "the new attempt starts at zero and the closed one keeps its measurement"
    )
    assert coordinator._closed_campaign == first_terminal, (
        "one terminal per instance, and the closed one is immutable"
    )
    assert first_terminal["objective_realized_kwh"] == realised


async def test_a_completed_campaign_may_not_open_another_instance(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """The other half of the asymmetry, and the reason it is not simply "allow it".

    An abort is a failed attempt and deserves another. A campaign that reached its
    objective, or ran out of schedule, is **finished** -- and Stage A goes on
    publishing it for as long as its horizon contains it. Without this, a completed
    campaign would open a fresh instance on the very next refresh and loop for ever,
    each iteration buying the same energy again.

    *Mutation: latch the final set on any reason, or on none, and this passes while
    the test above fails -- which is why both exist.*
    """
    coordinator = await start_the_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    coordinator._close_campaign(opens_at(1), EXECUTION_STOP_CAMPAIGN_COMPLETE)
    first_terminal = dict(coordinator._closed_campaign or {})
    assert first_terminal
    assert coordinator._campaign_id is None

    coordinator._plan = admitted_plan()
    coordinator._quarter = coordinator._plan.executing_quarter(
        opens_at(2) + timedelta(minutes=1)
    )
    coordinator._note_campaign_progress(opens_at(2) + timedelta(minutes=1), None)

    assert coordinator._campaign_id is None, "a finished campaign does not run again"
    assert coordinator._campaign_instance_id is None
    assert coordinator._closed_campaign == first_terminal, "one terminal, exactly"


async def test_a_truly_stale_execution_still_fails_closed(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """Suppression requires authority, and authority requires an open row.

    With the schedule gone there is nothing to outrank the withdrawal, and the stop
    stands. This is the guard that keeps the fix from being "ignore bad news".
    """
    coordinator = await start_the_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    coordinator._plan = None
    coordinator._quarter = None
    assert coordinator._plan_authority_holds(opens_at(1)) is False

    report = await step_once(hass, coordinator, live_surface, **step_clock(1))
    execution = report.get("execution") or {}
    boundary = execution.get("write_boundary") or {}
    authority = boundary.get("authority") or {}

    # Nothing was withheld, because nothing outranked the stop.
    assert authority.get("plan_authority_holds") is False
    assert authority.get("withheld_stop_reason") is None
    # And the teardown was total: an abandoned execution leaves no authority behind
    # for a later row to find, which is the whole of A9.
    assert coordinator._campaign_id is None
    assert coordinator._plan is None
    assert coordinator.store.execution_record is None


# ===========================================================================
# 5. the two structural invariants the rest of this rests on
# ===========================================================================


def test_an_admitted_plan_adopts_the_identity_of_the_run_it_came_from() -> None:
    """**Arm, claim and sustain can only agree if the plan and the run do.**

    ``_authority_run_id`` returns the carried run when there is one and the
    admitted plan otherwise, so a plan whose ``run_id`` differed from the run its
    quarter one was claimed under would silently turn every boundary into a fresh
    arm: the record would name one identity and the surviving authority another.

    ``admit_plan`` makes that unrepresentable by adopting the run's identity, and
    this pins it -- the assumption is load-bearing and it is one line deep.
    """
    from custom_components.alpha_ems_manager.execution import (
        admit,
        admit_plan,
        parse_target,
    )

    target = parse_target(one_admissible_target())
    assert target is not None
    run = admit(target, target.window_start)
    plan = admit_plan(target, run=run, now=target.window_start)
    assert plan is not None
    assert plan.run_id == run.run_id

    # And with no run to adopt from, the plan is its own identity -- never
    # ``None``, which would leave an arm with nothing to claim under.
    orphan = admit_plan(target, run=None, now=target.window_start)
    assert orphan is not None
    assert orphan.run_id == target.plan_id


def test_an_affirming_publication_is_read_before_the_deadline_is_judged() -> None:
    """R6g: this refresh's own evidence must not arrive after the verdict.

    The deadline detects Stage A having **gone quiet**. A publication in hand that
    re-affirms the run is direct proof it has not -- so testing staleness first
    decided the question on the older of two facts, and killed runs the very same
    refresh was rescuing. On the reference installation the two are seconds apart:
    the claim's deadline and the publication's ``stale_after`` differed by 0.2 s.

    *Mutation: put the staleness test back above the affirmation and this fails.*
    """
    from custom_components.alpha_ems_manager.execution import admit, carry_forward

    opens = local(NORMAL, 10, 45)
    carried = admit(_target_at(opens, stale_minutes=30), opens)
    # Six seconds past its deadline, and an affirmation in the same refresh.
    now = carried.stale_after + timedelta(seconds=6)
    # A rolling publication: the window has advanced to the head, which is the
    # ordinary case and the one an affirmation exists for.
    fresh = one_admissible_target(
        window_start=now.isoformat(),
        stale_after=(now + timedelta(minutes=30)).isoformat(),
    )

    sells = frozenset({EXECUTION_INTENT_NET_EXPORT})
    outcome = carry_forward(carried, [fresh], now, executable_intents=sells)

    assert outcome.ended is None, "an affirmed run is not a stale one"
    assert outcome.affirmed is True
    assert outcome.carried is not None
    assert outcome.carried.run_id == carried.run_id, "the same run, re-anchored"
    assert outcome.carried.stale_after > now

    # And with nothing affirming it, the very same run is still stale on the very
    # same deadline. Reordering rescues a run that has evidence; it forgives none.
    silent = carry_forward(carried, [], now, executable_intents=sells)
    assert silent.ended == EXECUTION_STOP_STALE_PLAN


async def test_a_campaign_that_sells_nothing_freezes_at_zero_and_not_at_a_fallback(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**``0.0`` is an objective. ``None`` is the absence of one.**

    ``_campaign_objective_kwh`` goes to some trouble to keep them apart -- a
    campaign made entirely of gaps sums to zero and says so, while an unplaceable
    one returns ``None`` -- and the freeze then wrote ``live or opening``, which
    threw that distinction away for every legitimate zero. The campaign would be
    judged against a figure it had never promised.

    The freeze is written the careful way for this reason, though today it is a
    latch rather than a fix: the block above it already assigns the opening capture
    from the same reading, so ``live or opening`` happens to agree in every
    reachable state. This test pins the *property* -- a campaign that promises
    nothing is judged against nothing -- which is what has to stay true however
    those two blocks are later rearranged.
    """
    coordinator = await start_the_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    # It opened against the real schedule, so the fallback holds a real figure --
    # which is exactly what makes the two rules distinguishable here.
    assert coordinator._campaign_opening_target_kwh == pytest.approx(
        METER_TARGET_KWH, abs=0.01
    )

    # The same campaign, commanding no meter export at all.
    coordinator._plan = zero_objective_plan()
    assert coordinator._plan.campaign_id == CAMPAIGN_ID
    assert coordinator._campaign_objective_kwh(CAMPAIGN_ID) == pytest.approx(0.0)

    # Re-freeze it the way an activation does.
    coordinator._campaign_started_at = None
    coordinator._campaign_frozen_target_kwh = None
    coordinator._activation_confirmed = True
    coordinator._note_campaign_progress(opens_at(1) + timedelta(minutes=1), None)

    assert coordinator._campaign_started_at is not None
    assert coordinator._campaign_frozen_target_kwh == pytest.approx(0.0)


async def test_a_discharge_reports_the_energy_it_actually_moved(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**Every export campaign this project ever ran reported exactly zero.**

    ``max(0.0, stored - opening)`` is a *charge's* arithmetic and it was applied to
    every run. On a discharge the pack falls, the difference is negative, and the
    clamp returned zero -- which is what published ``5.75 / 0.00 / 5.75`` for a
    sale that had physically moved 2.211 kWh out of the battery. The second basis,
    a power accumulator clamped at ``max(0.0, power)``, discarded the same energy
    independently, so both bases agreed on nothing.

    Read at the run level here, against a pack that visibly fell.

    *Mutation: restore ``soc_delta = max(0.0, stored - opening)`` and this fails.*
    """
    coordinator = await start_the_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    assert coordinator._run_is_discharge(), "the replayed campaign is a sale"

    plan = coordinator._plan
    assert plan is not None
    run_id = plan.run_id

    # The pack as it stood when the run opened, and 2.211 kWh later -- the figure
    # the hardware actually delivered in quarter one.
    coordinator._execution_run = run_id
    coordinator._execution_window_start_kwh = 12.0
    fell = _plan_with_stored(coordinator, 12.0 - Q1_BATTERY_KWH)

    progress = coordinator._execution_progress(run_id, fell)
    assert progress.soc_delta_kwh == pytest.approx(Q1_BATTERY_KWH, abs=1e-6)

    # And a charge is unchanged: the sign follows the run, it is not inverted.
    coordinator._quarter = None
    coordinator._plan = None
    coordinator._carried = None
    assert coordinator._run_is_discharge() is False
    coordinator._execution_run = None
    coordinator._execution_window_start_kwh = None
    coordinator._execution_progress(run_id, _plan_with_stored(coordinator, 12.0))
    rose = coordinator._execution_progress(run_id, _plan_with_stored(coordinator, 14.5))
    assert rose.soc_delta_kwh == pytest.approx(2.5, abs=1e-6)


def _plan_with_stored(coordinator, energy_kwh: float):
    """Return the coordinator's battery plan restated at a given stored energy."""
    from dataclasses import replace

    plan = (coordinator.data or {}).get("battery_plan")
    assert plan is not None and plan.state is not None
    return replace(plan, state=replace(plan.state, energy_kwh=energy_kwh))


async def test_the_abort_clears_every_field_the_vocabulary_names(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**A9, checked against the list rather than against a reading of the code.**

    ``EXECUTION_ABORT_IS_TOTAL`` states what an abort must leave behind: nothing.
    A constant nobody reads is decoration, so it is read here -- every name in it
    is resolved to the state it stands for and asserted empty after one call to the
    central helper.

    The value of stating it as a list is what happens next time. beta.34's teardown
    was a hand-written subset that had been correct when written and had silently
    stopped being exhaustive; the field it was missing -- the admitted plan -- is
    the one that re-armed the inverter after the campaign had been declared over.
    Adding state to this machine now means either clearing it here or editing this
    list, and both are visible in a diff.
    """
    coordinator = await start_the_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    # Everything the vocabulary names is populated before the abort, or the
    # assertions below would pass against a machine that never held any of it.
    reads = {
        "execution_record": lambda: coordinator.store.execution_record,
        "sustained_deadline": lambda: coordinator._sustained_deadline,
        "sustained_run_id": lambda: coordinator._sustained_run_id,
        "quarter": lambda: coordinator._quarter,
        "quarter_progress": lambda: coordinator._quarter_grid_export_kwh or None,
        "quarter_progress_unknown": lambda: coordinator._quarter_progress_unknown,
        "carried_run": lambda: coordinator._carried,
        "admitted_plan": lambda: coordinator._plan,
        "campaign": lambda: coordinator._campaign_id,
    }
    assert set(reads) == set(EXECUTION_ABORT_IS_TOTAL), (
        "every name in the vocabulary must resolve to a state this test can read"
    )
    populated = {name for name, read in reads.items() if read()}
    assert {"execution_record", "quarter", "admitted_plan", "campaign"} <= populated

    coordinator._abandon_execution(opens_at(1), EXECUTION_STOP_SAFETY)

    left = {name: read() for name, read in reads.items() if read()}
    assert left == {}, left
