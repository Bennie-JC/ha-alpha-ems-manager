"""beta.38 Gate 2: the opened-row authority shields exactly one thing.

**What beta.38 added is a suppression, and a suppression is only as safe as the set
it cannot reach.** ``carry_forward`` now keeps a run whose row has opened rather than
withdrawing it for want of an affirming publication -- and the whole value of that
change depends on every *other* way a run can end still reaching it.

Three families, and the guard's relationship with each is different in kind:

* **Withdrawal** -- ``stage_a_hold`` and its siblings. This is the one family the
  guard suppresses, and suppressing it is the release.
* **Abort** -- safety, a stalled dead-man, a lost owner marker, the user switching
  off. The guard sits inside ``carry_forward``, which these never consult; they
  converge on ``_abandon_execution`` regardless of what the carry state machine
  thinks. Argued structurally in ``test_the_authority_suppresses_absence_and_nothing
  _else``; replayed here **against an actually-owned, actually-open row**, because a
  set-theoretic proof about constants would survive the wiring being wrong.
* **Completion** -- ``window_ended``, ``target_reached``, ``campaign_objective_
  reached``. The guard is placed *after* the window bound in the priority order, so
  it cannot outlive the work it protects. Pinned here on the pure function, where
  the ordering is visible.

Both economic directions throughout: an abort must reach a Sell and a Buy alike, and
the two arrive at the teardown holding different objectives.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.alphaess_device import (
    BOOLEAN_EXECUTION_OWNER,
    DISPATCH_ENABLE,
)
from custom_components.alpha_ems_manager.const import (
    CONTROL_MODE_OFF,
    EXECUTION_COMPLETION_STOP_REASONS,
    EXECUTION_STOP_MARKER_LOST,
    EXECUTION_STOP_SAFETY,
    EXECUTION_STOP_TIMER_NOT_REFRESHED,
    EXECUTION_STOP_WINDOW_ENDED,
    EXECUTION_WITHDRAWAL_STOP_REASONS,
)

from .beta38_trace import (
    BOTH_INTENTS,
    authority_of,
    carried_of,
    opens_at,
    step_clock,
    target_for,
)
from .test_beta24_live_charge import LiveSurface, step_once
from .test_beta38_opened_row_authority import admitted_before_open, open_the_row


@pytest.fixture
def live_surface(hass: HomeAssistant, control_surface: None) -> LiveSurface:
    """Return a control surface that responds to writes."""
    return LiveSurface(hass)


# ===========================================================================
# the harness
# ===========================================================================


async def owned_and_open(
    hass, config_data, frank, live_surface, monkeypatch, *, intent
):
    """Return a coordinator **owning** a dispatch whose frozen row is open.

    The state the guard is active in, and therefore the only state in which "does
    the guard shield this?" is a real question. Reached the long way -- admitted,
    opened, armed -- because a hand-placed claim would not exercise the wiring that
    connects the guard to the teardown.
    """
    coordinator = await admitted_before_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    await open_the_row(hass, coordinator, live_surface, monkeypatch, intent=intent)
    report = await step_once(hass, coordinator, live_surface, **step_clock(0))

    # The witnesses. Without all four the assertions below could pass against a
    # coordinator that was never executing anything.
    assert coordinator.store.execution_record is not None, "nothing was armed"
    assert coordinator._carried is not None, "no run is carried"
    assert coordinator._plan is not None, "no frozen schedule"
    assert authority_of(report).get("plan_authority_holds") is True, report
    live_surface.calls.clear()
    return coordinator


def assert_verified_stop(hass, coordinator, report, *, reason: str) -> None:
    """Assert the run was positively stopped, and stopped for ``reason``.

    Deliberately not ``assert_full_charge_reset``: that helper names ``ACTION_CHARGE``
    and a Sell tears down a discharge. What both directions share is the part that
    matters -- an authorised write naming this reason, the enable off, the marker
    released and the claim dropped.
    """
    boundary = (report.get("execution") or {}).get("write_boundary") or {}
    assert boundary.get("stop_reason") == reason, boundary
    assert (report.get("authorization") or {}).get("authorized") is True
    assert hass.states.get(DISPATCH_ENABLE).state == "off"
    assert hass.states.get(BOOLEAN_EXECUTION_OWNER).state == "off"
    assert coordinator.store.execution_record is None, "the claim outlived the stop"
    # And the guard's own inputs are gone with it, so no later refresh can revive
    # authority for a run that has ended.
    assert coordinator._plan is None
    assert coordinator._carried is None


# ===========================================================================
# 1. the aborts, replayed against an open row
# ===========================================================================


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_a_safety_hazard_still_stops_an_opened_row(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """The hazard family is not negotiable, and an open row does not negotiate.

    *Mutation: add ``safety`` to the withheld set and this fails.*
    """
    from custom_components.alpha_ems_manager import coordinator as module
    from custom_components.alpha_ems_manager.safety import SafetyVerdict

    coordinator = await owned_and_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    unsafe = SafetyVerdict(False, "battery_power_stale", ())
    monkeypatch.setattr(module, "evaluate", lambda intent, context: unsafe)
    # **An admitted ``net_export`` is judged by ``_export_verdict``, not by
    # ``evaluate``** -- for a Sell the meter *is* the objective, so the
    # absorbing-capacity refusal that governs every other discharge has the
    # opposite answer and a separate path. Patching only ``evaluate`` would have
    # left the Sell hazard-free and the parametrisation decorative.
    monkeypatch.setattr(
        type(coordinator),
        "_export_verdict",
        lambda self, intent, context, now: unsafe,
    )
    report = await step_once(hass, coordinator, live_surface, **step_clock(0))

    assert_verified_stop(hass, coordinator, report, reason=EXECUTION_STOP_SAFETY)


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_a_stalled_deadman_still_stops_an_opened_row(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """The measured unknown. An open row makes it more urgent, not less."""
    coordinator = await owned_and_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    monkeypatch.setattr(
        type(coordinator), "_deadman_is_stale", lambda self, snapshot, run_id: True
    )
    report = await step_once(hass, coordinator, live_surface, **step_clock(0))

    assert_verified_stop(
        hass, coordinator, report, reason=EXECUTION_STOP_TIMER_NOT_REFRESHED
    )


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_a_lost_owner_marker_still_stops_an_opened_row(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """Somebody else cleared the marker while the row was open.

    Ownership is the premise of every command the EMS sends. Losing it mid-row is
    the case where continuing would be worst, so it is the case where a suppression
    reaching too far would be worst.
    """
    coordinator = await owned_and_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    hass.states.async_set(BOOLEAN_EXECUTION_OWNER, "off")
    report = await step_once(hass, coordinator, live_surface, **step_clock(0))

    boundary = (report.get("execution") or {}).get("write_boundary") or {}
    assert boundary.get("stop_reason") == EXECUTION_STOP_MARKER_LOST, boundary
    assert hass.states.get(DISPATCH_ENABLE).state == "off"
    assert coordinator.store.execution_record is None
    assert coordinator._plan is None
    assert coordinator._carried is None


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_the_user_switching_off_still_stops_an_opened_row(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """The user's switch outranks the plan, whatever the plan has begun.

    An authority that could outlast ``Off`` would be a control system the operator
    cannot stop, which is a different and much worse defect than the one beta.38
    fixes.
    """
    coordinator = await owned_and_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    coordinator.set_control_mode(CONTROL_MODE_OFF)
    report = await step_once(hass, coordinator, live_surface, **step_clock(0))

    # **Off publishes a different report, and that is the point of reading it in
    # its own shape rather than the executing one.** ``Off`` does not build a
    # Stage-B block at all -- there is no execution to narrate -- so the teardown
    # appears at the top level as ``off_reset``. An assertion written against the
    # executing shape would have found an empty dict and could only have been made
    # to pass by weakening it.
    assert report.get("state") == "off", report.get("state")
    boundary = report.get("write_boundary") or {}
    assert boundary.get("source") == "off_reset", boundary
    assert hass.states.get(DISPATCH_ENABLE).state == "off"
    assert hass.states.get(BOOLEAN_EXECUTION_OWNER).state == "off"
    assert coordinator.store.execution_record is None
    assert coordinator._plan is None
    assert coordinator._carried is None


# ===========================================================================
# 2. completion, which the guard is placed behind
# ===========================================================================


@pytest.mark.parametrize("intent", BOTH_INTENTS)
def test_the_open_row_guard_cannot_outlive_its_own_window(intent: str) -> None:
    """``window_ended`` is decided **before** the guard is consulted, deliberately.

    The guard's whole justification is that Stage A's horizon head is ``elapsed + 1``
    and therefore cannot describe a row already running. That argument expires the
    moment the window does -- past ``window_end`` the horizon *can* describe the
    period, absence of a publication means what it usually means, and a run kept
    alive past its own window would be exactly the zombie beta.38 exists to prevent.

    So the guard is placed after the window bound in ``carry_forward``'s priority
    order, and this asserts the ordering rather than trusting it: ``row_open=True``
    at a moment past ``window_end`` still ends the run.

    *Mutation: move the ``row_open`` return above the window test and this fails.*
    """
    from datetime import timedelta

    from custom_components.alpha_ems_manager.execution import (
        admit,
        carry_forward,
        parse_target,
    )

    parsed = parse_target(target_for(intent))
    assert parsed is not None
    carried = admit(parsed, opens_at(-1))

    past_the_end = carried.window_end + timedelta(minutes=1)
    outcome = carry_forward(
        carried,
        (),
        past_the_end,
        executable_intents=frozenset({intent}),
        row_open=True,
    )

    assert outcome.carried is None, "an open row outlived its own window"
    assert outcome.ended == EXECUTION_STOP_WINDOW_ENDED
    assert outcome.ended_run is carried


def test_the_guard_can_only_ever_withhold_a_withdrawal() -> None:
    """The completion family is disjoint from what the guard returns.

    ``carry_forward``'s opened-row branch returns ``CarryOutcome(carried=carried)``
    with no ``ended`` at all -- it does not *choose* a reason, it declines to file
    one. The only reason it declines to file is the withdrawal-by-absence at the end
    of the function, so no completion or abort reason can be reached by it.

    Stated as a set fact next to the replays above, because the replays prove the
    wiring and this proves there is nothing else for the wiring to reach.
    """
    assert not (
        set(EXECUTION_COMPLETION_STOP_REASONS) & set(EXECUTION_WITHDRAWAL_STOP_REASONS)
    )
    assert EXECUTION_STOP_WINDOW_ENDED in EXECUTION_COMPLETION_STOP_REASONS


# ===========================================================================
# 2b. the layer beta.38 made hard to reach, kept covered anyway
# ===========================================================================


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_the_authority_read_falls_back_to_the_frozen_schedule(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """The arm, the claim and the sustain all read one authority, and it has two
    sources.

    beta.35 widened ``_authority_run_id`` from the carried run to *the carried run
    or the admitted plan*, because beta.34 had taught the arm that a frozen
    schedule is an authority in its own right and left the sustain comparing
    against the run alone. A dispatch was armed by one rule and refused
    continuation by another.

    **beta.38 closed the route that replay reached it by, and the layer is kept
    covered rather than retired.** F1 keeps the carried run alive across the
    boundary that used to drop it, so the continuity replay no longer produces a
    quarter-authority refresh and the mutation that deletes the fallback stopped
    failing anything. The fallback did not stop mattering -- ``_claim_authority``
    still prefers the same two sources in the same order, and the arm still uses
    it. Asserted directly, which is the honest reading of a guard whose scenario a
    later release made unreachable.
    """
    coordinator = await owned_and_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    plan = coordinator._plan
    carried = coordinator._carried
    assert plan is not None and carried is not None

    # The run when there is one...
    assert coordinator._authority_run_id() == carried.run_id
    # ...and the frozen schedule when there is not.
    coordinator._carried = None
    assert coordinator._authority_run_id() == plan.run_id
    assert coordinator._authority_run_id() is not None
    # The same two sources in the same order, so an arm and its sustain cannot
    # disagree about who is executing. ``_claim_authority`` wraps the plan in a
    # ``_PlanAuthority`` rather than returning it bare -- the identity is what has
    # to agree, not the object.
    assert coordinator._claim_authority(carried) is carried
    claim = coordinator._claim_authority(None)
    assert claim is not None
    assert claim.run_id == plan.run_id


# ===========================================================================
# 3. what "finish the started plan" does not mean
# ===========================================================================


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_a_shortfall_in_one_row_is_not_made_up_in_the_next(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """**A6.** Keeping a run alive is not permission to catch up.

    beta.38 stops an opened row being cancelled; it does not change what the row is
    for. A quarter that under-delivered stays under-delivered, and the next row
    executes the figure it was frozen with -- not that figure plus the shortfall.
    Catch-up would move energy the planner never priced, in a quarter whose prices
    it was not chosen against.
    """
    coordinator = await owned_and_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    plan = coordinator._plan
    assert plan is not None
    frozen_second = plan.rows[1]

    # Cross the boundary into the second row.
    for index in (1, 1):
        await step_once(hass, coordinator, live_surface, **step_clock(index))

    after = coordinator._plan
    assert after is not None, "the run did not survive the boundary"
    row = after.row_covering(opens_at(1))
    assert row is not None
    assert row.start == frozen_second.start
    assert row.battery_kwh == pytest.approx(frozen_second.battery_kwh)
    assert row.grid_export_target_kwh == frozen_second.grid_export_target_kwh
    # And the run is still the one that opened, so this is the frozen schedule's
    # second row rather than a fresh admission that happens to look similar.
    assert carried_of(coordinator.control_report or {}).get("ended_reason") is None
