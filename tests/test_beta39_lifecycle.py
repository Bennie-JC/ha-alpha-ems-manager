"""beta.39 Gate 1: the lifecycle says what the battery is doing.

**The 2026-09-02 Sell, and the one word it never said.**

Run ``0492b715ccf76ce0`` delivered 1.701 of 1.75 kWh at the pack and 1.437 of
1.49 kWh at the meter across the 20:45-21:00 row, at 7.4 kW, under a proven claim.
``control.state`` read ``executing`` throughout. ``execution.lifecycle.state``
walked::

    admitted -> starting -> stopping

``executing`` appeared nowhere, and neither did ``stopped``.

Not a control fault -- a **publish-ordering** one, and it had four parts:

1. ``_lifecycle_state_from`` has exactly one call site, inside the pure
   ``_build_control_report``, which runs one call frame *before* the write boundary.
   So ``ownership_state`` is the **pre-arm** reading: the 20:45 payload shows
   ``ownership.state: "none"`` beside ``power.executed: true, applied_kw: 6.9``.
2. ``arming`` was tested *ahead* of ``OWNERSHIP_OWNED``, so a refresh that armed
   published ``starting`` even where ownership already proved a run in flight.
3. Together those mean ``executing`` needed a **second quarter refresh inside the
   same run**, whose pre-write ownership already read ``owned``. A single-row
   campaign has no such refresh, so the state was structurally unreachable.
4. The sixty-second tick never projected at all, so through fifteen ticks at
   7.4 kW the field was frozen -- and ``stopped``, noted after the report was
   built, was overwritten by the next refresh's projection before anybody saw it.

What beta.39 changes, and what it deliberately does not
-------------------------------------------------------

**The criterion is the one that was already there, and only its timing moves.** A
run is ``executing`` if and only if ``ownership_of(evidence) == OWNED``: the vendor
register reports ``dispatch_active``, our persisted claim matches this run, and the
owner marker is on. ``ownership_of`` returns ``NONE`` the instant activity is false,
so the answer cannot precede execution.

It is emphatically **not** "the arm write landed". The live probe dates the register
going active at 20:45:49 -- 44.7 seconds after the claim -- so on the arming refresh
``starting`` is the truthful answer and the first *observation* that proves OWNED is
what records ``executing``. Nothing in this release manufactures vendor state from a
command we just sent, and the test below that pins the arming refresh as ``starting``
is the one that says so.

Three mechanisms, no second state machine:

* the ``OWNED`` test moves above the ``arming`` test in the same projection;
* the sixty-second tick projects through that same function once it has proven an
  owned, active dispatch;
* the write boundary re-publishes the block it already built, so a transition it
  settled is not a refresh late -- which also fixes ``open_campaign.started``,
  false in the payload on the very refresh the campaign began.

And every transition is appended to a bounded trail, because two of the three
cadences that advance the lifecycle publish no report at all: a single state field
structurally cannot answer *"did it ever reach executing?"*, which is the question
the incident actually raised.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.const import (
    BOOLEAN_EXECUTION_OWNER,
    LIFECYCLE_ADMITTED,
    LIFECYCLE_CLEANUP_COMPLETE,
    LIFECYCLE_EXECUTING,
    LIFECYCLE_IDLE,
    LIFECYCLE_STARTING,
    LIFECYCLE_STATES,
    LIFECYCLE_STOPPED,
    LIFECYCLE_STOPPING,
    LIFECYCLE_TERMINAL_NOTES,
    LIFECYCLE_TRAIL_LIMIT,
    LIFECYCLE_UPDATING,
    OWNERSHIP_DEGRADED,
    OWNERSHIP_FOREIGN,
    OWNERSHIP_NONE,
    OWNERSHIP_OWNED,
    OWNERSHIP_UNPROVEN,
)

from .beta38_trace import (
    BOTH_INTENTS,
    campaign_of,
    lifecycle_of,
    moved_elsewhere,
    opens_at,
    publish,
    step_clock,
)
from .test_beta24_live_charge import LiveSurface, step_once
from .test_beta38_opened_row_authority import admitted_before_open, open_the_row


@pytest.fixture
def live_surface(hass: HomeAssistant, control_surface: None) -> LiveSurface:
    """Return a control surface that responds to writes."""
    return LiveSurface(hass)


def trail_of(coordinator) -> list[str]:
    """Return the lifecycle states this session has passed through, in order."""
    return [entry["state"] for entry in coordinator._lifecycle_trail]


def published_trail(report) -> list[str]:
    """Return the same trail as the payload states it."""
    return [entry["state"] for entry in lifecycle_of(report).get("transitions") or []]


async def owned_and_running(hass, coordinator, live_surface, monkeypatch, *, intent):
    """Open the row, arm it, and tick once so the register is provably active.

    The live sequence, in the order the hardware produced it: the row opens and the
    arm is sent, and only a later observation can prove the dispatch is ours and
    running. Returns the arming report so a caller can assert on it.
    """
    from .beta36_trace import tick_at

    arming = await open_the_row(
        hass, coordinator, live_surface, monkeypatch, intent=intent
    )
    assert coordinator.store.execution_record is not None, "nothing was armed"
    await tick_at(hass, coordinator, live_surface, opens_at(0) + _minutes(1))
    return arming


def _minutes(count: int):
    """Return ``count`` minutes."""
    from datetime import timedelta

    return timedelta(minutes=count)


# ===========================================================================
# 1. the criterion, and what it refuses to claim
# ===========================================================================


def test_executing_requires_confirmed_ownership_and_nothing_less() -> None:
    """**The authoritative criterion, swept over the whole input space.**

    There is exactly one route to ``executing`` and it is
    ``ownership_state == OWNERSHIP_OWNED``: the vendor register reports
    ``dispatch_active``, the persisted claim matches this run, and the owner marker
    is on. beta.38 had three routes -- ``holding`` and ``sustaining`` as well -- and
    both are computed with ``owned`` conjoined, so they reached nothing ownership
    did not and left the predicate answering ``executing`` for
    ``holding=True, ownership=none`` when asked directly. They are gone.

    So: no combination in which ownership is anything but ``owned`` may produce
    ``executing``, and ``starting`` means exactly "arming, and not yet provably
    running".

    *Mutation: make ``arming`` reach ``executing``, or restore the disjunction, and
    this fails.*
    """
    from datetime import UTC, datetime
    from itertools import product
    from unittest.mock import Mock

    from custom_components.alpha_ems_manager.coordinator import AlphaEmsCoordinator

    now = datetime(2026, 9, 2, 20, 45, tzinfo=UTC)
    faux = Mock(spec=AlphaEmsCoordinator)
    faux._plan = None
    faux._carried = None
    faux._admission_abandoned = lambda plan: False

    states = (
        OWNERSHIP_NONE,
        OWNERSHIP_OWNED,
        OWNERSHIP_DEGRADED,
        OWNERSHIP_FOREIGN,
        OWNERSHIP_UNPROVEN,
    )
    reasons = (None, "safety", "deadman_not_refreshed", "stage_a_hold")
    executing = starting = 0
    for ownership, reason, flags in (
        (o, r, f)
        for o in states
        for r in reasons
        for f in product((False, True), repeat=3)
    ):
        resetting, releasing, arming = flags
        state = AlphaEmsCoordinator._lifecycle_state_from(
            faux,
            ownership_state=ownership,
            stop_reason=reason,
            resetting=resetting,
            releasing=releasing,
            arming=arming,
            now=now,
        )
        if state == LIFECYCLE_EXECUTING:
            executing += 1
            assert ownership == OWNERSHIP_OWNED, (ownership, reason, flags)
        if state == LIFECYCLE_STARTING:
            starting += 1
            # ``starting`` is now exactly "arming, and not yet provably running".
            assert arming and ownership != OWNERSHIP_OWNED, (ownership, flags)

    # The witnesses: both answers were actually reached.
    assert executing > 0
    assert starting > 0


def test_confirmed_execution_outranks_a_start_in_progress() -> None:
    """**The reorder, isolated.** Owned *and* arming reads ``executing``.

    A refresh that re-arms a run already confirmed running is not starting it. With
    ``arming`` tested first this published ``starting`` for the whole of every
    multi-quarter run's second and subsequent refreshes.

    *Mutation: put the ``arming`` branch back in front and this fails.*
    """
    from datetime import UTC, datetime
    from unittest.mock import Mock

    from custom_components.alpha_ems_manager.coordinator import AlphaEmsCoordinator

    faux = Mock(spec=AlphaEmsCoordinator)
    faux._plan = None
    faux._carried = None
    faux._admission_abandoned = lambda plan: False

    def project(**kwargs):
        return AlphaEmsCoordinator._lifecycle_state_from(
            faux,
            stop_reason=None,
            resetting=False,
            releasing=False,
            now=datetime(2026, 9, 2, 20, 45, tzinfo=UTC),
            **kwargs,
        )

    assert project(ownership_state=OWNERSHIP_OWNED, arming=True) == LIFECYCLE_EXECUTING
    assert project(ownership_state=OWNERSHIP_NONE, arming=True) == LIFECYCLE_STARTING
    assert project(ownership_state=OWNERSHIP_OWNED, arming=False) == LIFECYCLE_EXECUTING


def test_a_hazard_still_outranks_confirmed_execution() -> None:
    """The reorder moved one pair and nothing else.

    A lost marker, a foreign claim, an unprovable dispatch and an expired dead-man
    are all things a reader must see *instead of* ``executing``, and they are all
    tested ahead of it. This pins that the beta.38 order above the swap survived.
    """
    from datetime import UTC, datetime
    from unittest.mock import Mock

    from custom_components.alpha_ems_manager.const import (
        EXECUTION_STOP_TIMER_NOT_REFRESHED,
        LIFECYCLE_DEADMAN_EXPIRED,
        LIFECYCLE_DEGRADED,
        LIFECYCLE_FOREIGN,
        LIFECYCLE_UNPROVEN,
    )
    from custom_components.alpha_ems_manager.coordinator import AlphaEmsCoordinator

    faux = Mock(spec=AlphaEmsCoordinator)
    faux._plan = None
    faux._carried = None
    faux._admission_abandoned = lambda plan: False

    def project(ownership, reason=None):
        return AlphaEmsCoordinator._lifecycle_state_from(
            faux,
            ownership_state=ownership,
            stop_reason=reason,
            resetting=False,
            releasing=False,
            arming=True,
            now=datetime(2026, 9, 2, 20, 45, tzinfo=UTC),
        )

    assert project(OWNERSHIP_DEGRADED) == LIFECYCLE_DEGRADED
    assert project(OWNERSHIP_FOREIGN) == LIFECYCLE_FOREIGN
    assert project(OWNERSHIP_UNPROVEN) == LIFECYCLE_UNPROVEN
    assert (
        project(OWNERSHIP_OWNED, EXECUTION_STOP_TIMER_NOT_REFRESHED)
        == LIFECYCLE_DEADMAN_EXPIRED
    )
    assert project(OWNERSHIP_OWNED, "safety") == LIFECYCLE_STOPPING


def test_a_dispatch_we_cannot_prove_is_ours_is_not_executing() -> None:
    """**The claim is part of the criterion, not decoration beside it.**

    An active dispatch with our marker on but no matching persisted claim is
    ``unproven``: it might be ours and it is never touched. The lifecycle has to
    say so rather than ``executing``, because "the battery is running under our
    authority" and "something is running and we cannot prove why" are the two
    facts an operator most needs told apart.

    Asserted against ``ownership_of`` itself as well as against the projection, so
    the composition cannot be broken from either end.

    *Mutation: drop the ``record_matches`` test in ``ownership_of`` and this
    fails.*
    """
    from datetime import UTC, datetime
    from unittest.mock import Mock

    from custom_components.alpha_ems_manager.const import LIFECYCLE_UNPROVEN
    from custom_components.alpha_ems_manager.coordinator import AlphaEmsCoordinator
    from custom_components.alpha_ems_manager.execution import (
        OwnershipEvidence,
        ownership_of,
    )

    evidence = OwnershipEvidence(
        dispatch_active=True, marker_on=True, record=None, readback_compatible=True
    )
    state = ownership_of(evidence)
    assert state == OWNERSHIP_UNPROVEN, state

    faux = Mock(spec=AlphaEmsCoordinator)
    faux._plan = None
    faux._carried = None
    faux._admission_abandoned = lambda plan: False
    projected = AlphaEmsCoordinator._lifecycle_state_from(
        faux,
        ownership_state=state,
        stop_reason=None,
        resetting=False,
        releasing=False,
        arming=True,
        now=datetime(2026, 9, 2, 20, 45, tzinfo=UTC),
    )
    assert projected == LIFECYCLE_UNPROVEN, projected


def test_the_projection_can_never_return_a_terminal_note() -> None:
    """``stopped`` and ``cleanup_complete`` are notes about a completed transition.

    **This is what makes them bounded**, and it is the whole of the mechanism: the
    projection cannot produce either, so the next ordinary refresh reads ``idle``
    and the trail keeps the fact. There is no latch to get stuck.
    """
    from datetime import UTC, datetime
    from itertools import product
    from unittest.mock import Mock

    from custom_components.alpha_ems_manager.coordinator import AlphaEmsCoordinator

    faux = Mock(spec=AlphaEmsCoordinator)
    faux._plan = None
    faux._carried = None
    faux._admission_abandoned = lambda plan: False

    for ownership, reason, flags in (
        (o, r, f)
        for o in (
            OWNERSHIP_NONE,
            OWNERSHIP_OWNED,
            OWNERSHIP_DEGRADED,
            OWNERSHIP_FOREIGN,
            OWNERSHIP_UNPROVEN,
        )
        for r in (None, "safety", "deadman_not_refreshed", "target_reached")
        for f in product((False, True), repeat=3)
    ):
        resetting, releasing, arming = flags
        state = AlphaEmsCoordinator._lifecycle_state_from(
            faux,
            ownership_state=ownership,
            stop_reason=reason,
            resetting=resetting,
            releasing=releasing,
            arming=arming,
            now=datetime(2026, 9, 2, 20, 45, tzinfo=UTC),
        )
        assert state not in LIFECYCLE_TERMINAL_NOTES, (ownership, reason, flags)
        assert state in LIFECYCLE_STATES, state


def test_the_vocabulary_is_checked_at_the_call_site() -> None:
    """A typo publishes nothing a reader can interpret, so it fails loudly instead.

    ``_note_lifecycle`` took a bare string with nothing to compare it against.
    """
    from datetime import UTC, datetime
    from unittest.mock import Mock

    from custom_components.alpha_ems_manager.coordinator import AlphaEmsCoordinator

    faux = Mock(spec=AlphaEmsCoordinator)
    faux._lifecycle = LIFECYCLE_IDLE

    with pytest.raises(ValueError, match="unknown lifecycle state"):
        AlphaEmsCoordinator._note_lifecycle(
            faux, "exectuing", datetime(2026, 9, 2, tzinfo=UTC)
        )


def test_the_one_state_that_stays_unused_says_why() -> None:
    """``updating`` is deliberately never published, and the reason is recorded.

    A setpoint correction is not a state of the run: the run is executing before
    it, during it and after it. Publishing ``updating`` would split "is the battery
    running?" across two words -- the exact defect this field exists to prevent --
    and ``tick.reason`` already says *which* correction happened.

    The other two dead constants beta.39 found are now written by production code,
    so this is the only member left, and it is left on purpose rather than by
    omission.
    """
    import pathlib

    package = pathlib.Path("custom_components/alpha_ems_manager")
    sources = "".join(
        path.read_text(encoding="utf-8")
        for path in package.glob("*.py")
        if path.name != "const.py"
    )

    assert "LIFECYCLE_UPDATING" not in sources
    assert LIFECYCLE_UPDATING in LIFECYCLE_STATES
    reason = (package / "const.py").read_text(encoding="utf-8")
    assert "Deliberately unused" in reason


# ===========================================================================
# 2. the live failure shape, replayed in both directions
# ===========================================================================


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_the_arming_refresh_is_starting_and_says_so_truthfully(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """**The refusal, and it is as load-bearing as the fix.**

    A write that landed is not evidence that a dispatch is running. The live probe
    dates the vendor register going active at 20:45:49, 44.7 seconds after the
    claim was written, so on the refresh that arms, ``starting`` is the honest
    answer and ``executing`` would be a claim about the plant made from our own
    command.

    *Mutation: promote ``starting`` to ``executing`` at the write boundary and this
    fails.*
    """
    coordinator = await admitted_before_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    assert lifecycle_of(coordinator.control_report or {}).get("state") == (
        LIFECYCLE_ADMITTED
    )

    arming = await open_the_row(
        hass, coordinator, live_surface, monkeypatch, intent=intent
    )

    assert coordinator.store.execution_record is not None, "the witness: it armed"
    assert lifecycle_of(arming).get("state") == LIFECYCLE_STARTING, arming
    ownership = ((arming.get("execution") or {}).get("ownership") or {}).get("state")
    assert ownership != OWNERSHIP_OWNED, "the pre-write reading, as in the capture"


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_the_first_confirmed_observation_publishes_executing(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """**The fix, on the cadence that could see it.**

    The sixty-second tick has already proven three things before it reaches the
    projection: the register reports active, ``ownership_of`` answers ``owned`` on
    a snapshot read *this* tick, and a quarter or run covers the instant. That is
    the definition of ``executing``, and until beta.39 no cadence but the quarter
    refresh could record it.

    *Mutation: delete the tick projection and this fails in both directions.*
    """
    from custom_components.alpha_ems_manager.alphaess_adapter import read_snapshot

    from .beta36_trace import tick_at

    coordinator = await admitted_before_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    await open_the_row(hass, coordinator, live_surface, monkeypatch, intent=intent)
    assert coordinator._lifecycle == LIFECYCLE_STARTING

    await tick_at(hass, coordinator, live_surface, opens_at(0) + _minutes(1))

    assert coordinator._lifecycle == LIFECYCLE_EXECUTING
    # And the criterion it was reached by, asked directly: the plant, not the
    # command. ``ownership_of`` returns ``none`` the instant activity is false, so
    # this cannot be true before execution really began.
    assert (
        coordinator._ownership_now(read_snapshot(hass), opens_at(0) + _minutes(1))
        == OWNERSHIP_OWNED
    )


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_the_lifecycle_holds_executing_through_the_whole_row(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """Fifteen minutes of ticks, and a setpoint correction must not regress it.

    The live row ran at 7.4 kW for fifteen minutes with the field frozen at
    ``starting``. A correction inside a run is not a state of the run, which is why
    ``updating`` stays unused.

    *Mutation: publish ``starting`` on a re-arm, or ``updating`` on a correction,
    and this fails.*
    """
    from .beta36_trace import tick_at

    coordinator = await admitted_before_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    await open_the_row(hass, coordinator, live_surface, monkeypatch, intent=intent)

    seen = set()
    for minute in range(1, 15):
        await tick_at(hass, coordinator, live_surface, opens_at(0) + _minutes(minute))
        seen.add(coordinator._lifecycle)

    assert seen == {LIFECYCLE_EXECUTING}, seen


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_a_refresh_inside_a_confirmed_run_publishes_executing(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """A quarter refresh inside a live run reads ``executing``, not ``starting``.

    **And a note on what this cannot test.** The refresh where a later frozen row
    opens *sustains* rather than re-arms -- ``sustaining`` is checked before the
    arm branch and the run identity has not changed -- so ``arming`` and
    ``OWNERSHIP_OWNED`` are never both true here. The one reachable state where
    they are is a new run arming under a dispatch that is still live, which needs
    a second campaign the replay does not construct.

    So the projection order is proven directly instead, over its whole input
    space, in ``test_confirmed_execution_outranks_a_start_in_progress`` and
    ``test_executing_requires_confirmed_ownership_and_nothing_less``. What this
    covers is the wiring: a real refresh, both directions, on the published
    payload.

    *Mutation: delete the ownership branch of the projection and this fails in
    both directions.*
    """
    from .beta36_trace import tick_at

    coordinator = await admitted_before_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    await open_the_row(hass, coordinator, live_surface, monkeypatch, intent=intent)
    await tick_at(hass, coordinator, live_surface, opens_at(0) + _minutes(1))

    publish(coordinator, monkeypatch, (moved_elsewhere(intent),))
    report = await step_once(hass, coordinator, live_surface, **step_clock(1))

    # The witnesses: this refresh owned the dispatch and wrote to it.
    ownership = ((report.get("execution") or {}).get("ownership") or {}).get("state")
    assert ownership == OWNERSHIP_OWNED, report
    sequence = ((report.get("execution") or {}).get("write_boundary") or {}).get(
        "sequence"
    )
    assert sequence == "sustain", sequence
    assert lifecycle_of(report).get("state") == LIFECYCLE_EXECUTING, report


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_the_observed_bad_sequence_is_now_impossible(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """**``admitted -> starting -> stopping`` with a confirmed run in between.**

    The exact published sequence of 2026-09-02, asserted absent. A confirmed owned
    physical interval occurred between the start and the stop, so ``executing`` has
    to appear between them -- in the trail if not in the state, because the state
    can only ever show the latest answer and the run began and ended between two
    publications.

    *Mutation: delete either the reorder or the tick projection and this fails.*
    """
    from itertools import pairwise

    from .beta36_trace import tick_at

    coordinator = await admitted_before_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    await open_the_row(hass, coordinator, live_surface, monkeypatch, intent=intent)
    for minute in range(1, 15):
        await tick_at(hass, coordinator, live_surface, opens_at(0) + _minutes(minute))

    trail = trail_of(coordinator)

    assert LIFECYCLE_EXECUTING in trail, trail
    # The bad sequence, as a contiguous pair. ``starting`` may never be followed
    # directly by a stop once a run has been confirmed.
    pairs = list(pairwise(trail))
    assert (LIFECYCLE_STARTING, LIFECYCLE_STOPPING) not in pairs, trail
    assert (LIFECYCLE_STARTING, LIFECYCLE_STOPPED) not in pairs, trail


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_a_tick_that_cannot_prove_ownership_publishes_no_execution(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """**The tick projection sits behind the tick's own ownership guard.**

    The marker goes while the dispatch keeps running -- the shape a helper deleted
    or turned off by hand produces. The tick must not carry the lifecycle forward
    from what it believed a minute ago, and it must not project ``executing`` from
    the mere fact that a tick happened.

    *Mutation: move the projection above the ownership guard, or hard-code the
    ownership it projects from, and this fails.*
    """
    from .beta36_trace import tick_at

    coordinator = await admitted_before_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    await open_the_row(hass, coordinator, live_surface, monkeypatch, intent=intent)
    await tick_at(hass, coordinator, live_surface, opens_at(0) + _minutes(1))
    assert coordinator._lifecycle == LIFECYCLE_EXECUTING, "the witness: it was running"

    # The marker is what proves the dispatch is ours, and it has gone.
    hass.states.async_set(BOOLEAN_EXECUTION_OWNER, "off")
    await hass.async_block_till_done()
    await tick_at(hass, coordinator, live_surface, opens_at(0) + _minutes(2))

    from custom_components.alpha_ems_manager.const import (
        LIFECYCLE_DEGRADED,
        LIFECYCLE_FOREIGN,
    )

    assert coordinator._lifecycle in (LIFECYCLE_DEGRADED, LIFECYCLE_FOREIGN), (
        coordinator._lifecycle
    )
    assert coordinator._lifecycle in trail_of(coordinator)


# ===========================================================================
# 3. the terminal, and the sequence it publishes
# ===========================================================================


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_the_stop_and_its_cleanup_are_two_published_transitions(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """``stopped`` then ``cleanup_complete``, and ``cleanup_complete`` had no writer.

    ``_async_stop_dispatch`` already made the distinction -- it withholds the
    cleanup on an unverified stop precisely because the two are not the same event
    -- and then published one word for both. The constant existed with zero
    references, which is the same dead-constant shape beta.38 fixed elsewhere.
    """
    from custom_components.alpha_ems_manager.alphaess_adapter import read_snapshot
    from custom_components.alpha_ems_manager.const import (
        EXECUTION_STOP_QUARTER_PROGRESS_UNKNOWN,
        STOP_SCOPE_ABORT,
    )

    from .beta36_trace import tick_at

    coordinator = await admitted_before_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    await open_the_row(hass, coordinator, live_surface, monkeypatch, intent=intent)
    await tick_at(hass, coordinator, live_surface, opens_at(0) + _minutes(1))
    assert coordinator._lifecycle == LIFECYCLE_EXECUTING

    moment = opens_at(0) + _minutes(2)
    live_surface.at(moment)
    await coordinator._async_stop_dispatch(
        moment,
        read_snapshot(hass),
        EXECUTION_STOP_QUARTER_PROGRESS_UNKNOWN,
        scope=STOP_SCOPE_ABORT,
    )
    await hass.async_block_till_done()

    trail = trail_of(coordinator)
    assert trail[-2:] == [LIFECYCLE_STOPPED, LIFECYCLE_CLEANUP_COMPLETE], trail
    assert coordinator.store.execution_record is None, "the claim is gone"
    assert hass.states.get(BOOLEAN_EXECUTION_OWNER).state == "off"


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_cleanup_complete_is_not_a_sticky_between_campaign_state(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """**Observable, then bounded.** The next ordinary refresh reads ``idle``.

    There is no latch and no timer: ``_lifecycle_state_from`` cannot return either
    terminal note, so an ordinary refresh with nothing owned and nothing admitted
    projects ``idle`` and the trail keeps the fact. A ``cleanup_complete`` that
    persisted until the next admission would be a terminal state describing an idle
    plant, which is the class of defect this whole field exists to prevent.
    """
    from custom_components.alpha_ems_manager.alphaess_adapter import read_snapshot
    from custom_components.alpha_ems_manager.const import (
        EXECUTION_STOP_QUARTER_PROGRESS_UNKNOWN,
        STOP_SCOPE_ABORT,
    )

    from .beta36_trace import tick_at
    from .beta38_trace import publish_nothing

    coordinator = await admitted_before_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    await open_the_row(hass, coordinator, live_surface, monkeypatch, intent=intent)
    await tick_at(hass, coordinator, live_surface, opens_at(0) + _minutes(1))

    moment = opens_at(0) + _minutes(2)
    live_surface.at(moment)
    await coordinator._async_stop_dispatch(
        moment,
        read_snapshot(hass),
        EXECUTION_STOP_QUARTER_PROGRESS_UNKNOWN,
        scope=STOP_SCOPE_ABORT,
    )
    await hass.async_block_till_done()
    assert coordinator._lifecycle == LIFECYCLE_CLEANUP_COMPLETE

    publish_nothing(coordinator, monkeypatch)
    report = await step_once(hass, coordinator, live_surface, **step_clock(3))

    assert lifecycle_of(report).get("state") == LIFECYCLE_IDLE, report
    # Bounded, not forgotten: the fact is still inspectable.
    assert LIFECYCLE_CLEANUP_COMPLETE in published_trail(report)
    assert lifecycle_of(report).get("previous_state") == LIFECYCLE_CLEANUP_COMPLETE


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_the_refresh_that_resets_publishes_its_terminal(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """**The publish-ordering fix, on the terminal it was losing.**

    A reset is decided while the report is built and performed a call frame later,
    so ``stopped`` and ``cleanup_complete`` were both recorded *after* the payload
    describing them existed -- and the next refresh's projection overwrote the
    state before anybody could read it. The 2026-09-02 capture published
    ``stopping`` and nothing after it.

    Driven through a safety hazard, because that is the path that resets on the
    quarter cadence rather than on the tick.

    *Mutation: drop the lifecycle half of the post-write patch, or collapse the
    cleanup back into the stop, and this fails.*
    """
    from custom_components.alpha_ems_manager import coordinator as module
    from custom_components.alpha_ems_manager.safety import SafetyVerdict

    from .beta36_trace import tick_at

    coordinator = await admitted_before_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    await open_the_row(hass, coordinator, live_surface, monkeypatch, intent=intent)
    await tick_at(hass, coordinator, live_surface, opens_at(0) + _minutes(1))
    assert coordinator._lifecycle == LIFECYCLE_EXECUTING

    # A hazard the next refresh cannot suppress. **Both judges are patched**,
    # because an admitted ``net_export`` is judged by ``_export_verdict`` and
    # everything else by the module-level ``evaluate`` -- for a Sell the meter
    # *is* the objective, so the absorbing-capacity refusal that governs every
    # other discharge has the opposite answer and a separate path. Patching one
    # of them leaves the other direction hazard-free and the parametrisation
    # decorative, which is the trap beta.38 recorded here.
    unsafe = SafetyVerdict(False, "battery_power_stale", ())
    monkeypatch.setattr(module, "evaluate", lambda intent, context: unsafe)
    monkeypatch.setattr(
        type(coordinator),
        "_export_verdict",
        lambda self, intent, context, now: unsafe,
    )
    report = await step_once(hass, coordinator, live_surface, **step_clock(0))

    state = lifecycle_of(report).get("state")
    assert state in (LIFECYCLE_STOPPED, LIFECYCLE_CLEANUP_COMPLETE), report
    trail = published_trail(report)
    assert LIFECYCLE_STOPPED in trail, trail
    assert LIFECYCLE_CLEANUP_COMPLETE in trail, trail
    assert coordinator.store.execution_record is None, "the claim is gone"


# ===========================================================================
# 4. the trail, which is what makes any of it readable
# ===========================================================================


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_the_payload_carries_every_transition_not_only_the_latest(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """**The observability fix, on the payload a user downloads.**

    Two of the three cadences that advance the lifecycle publish no control report,
    so a run that started and finished between two publications left no trace of
    having executed at all. One field can only show the latest answer; the question
    a reader has is whether it ever got there.

    *Mutation: drop ``transitions`` from the block and this fails.*
    """
    from .beta36_trace import tick_at

    coordinator = await admitted_before_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    await open_the_row(hass, coordinator, live_surface, monkeypatch, intent=intent)
    await tick_at(hass, coordinator, live_surface, opens_at(0) + _minutes(1))
    publish(coordinator, monkeypatch, (moved_elsewhere(intent),))
    report = await step_once(hass, coordinator, live_surface, **step_clock(1))

    trail = published_trail(report)
    assert LIFECYCLE_ADMITTED in trail, trail
    assert LIFECYCLE_STARTING in trail, trail
    assert LIFECYCLE_EXECUTING in trail, trail
    # Ordered, and each entry stamped, so a reader can date the run from the trail
    # alone rather than correlating three blocks by timestamp.
    entries = lifecycle_of(report).get("transitions")
    assert [entry["at"] for entry in entries] == sorted(
        entry["at"] for entry in entries
    )


async def test_the_trail_is_bounded(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """A ring, so a long session cannot grow the payload without limit.

    **The bound is asserted against a literal as well as against the constant**,
    because a test that only compares the trail's length to
    ``LIFECYCLE_TRAIL_LIMIT`` is satisfied by any limit at all -- including one
    large enough to be no limit. Two hundred transitions is a day of ordinary
    campaign activity; a payload carrying all of them would be the defect.
    """
    from datetime import UTC, datetime

    from custom_components.alpha_ems_manager.const import EXECUTION_INTENT_NET_EXPORT

    coordinator = await admitted_before_open(
        hass,
        config_data,
        frank,
        live_surface,
        monkeypatch,
        intent=EXECUTION_INTENT_NET_EXPORT,
    )
    base = datetime(2026, 9, 2, 20, 45, tzinfo=UTC)
    for step in range(200):
        coordinator._note_lifecycle(
            LIFECYCLE_EXECUTING if step % 2 else LIFECYCLE_IDLE,
            base + _minutes(step),
        )

    assert len(coordinator._lifecycle_trail) == LIFECYCLE_TRAIL_LIMIT
    assert len(coordinator._lifecycle_trail) <= 32, "the trail is not a log"


# ===========================================================================
# 5. the second victim of the same ordering
# ===========================================================================


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_the_payload_cannot_report_an_unstarted_campaign_that_started(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """**beta.38 fixed the freeze and the payload still lied.**

    The 2026-09-01 capture published ``open_campaign.started: false`` and
    ``frozen_target_kwh: null`` beside a ``completed_campaign`` whose ``started_at``
    was that very refresh. beta.38 moved the freeze to the instant the activation
    write lands -- correctly -- and asserted it on coordinator state *after* the
    refresh, which is why the test passed and the payload did not.

    Asserted here on the published block, which is the only place it was ever
    wrong.

    *Mutation: drop the ``open_campaign`` half of the post-write patch and this
    fails.*
    """
    coordinator = await admitted_before_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    arming = await open_the_row(
        hass, coordinator, live_surface, monkeypatch, intent=intent
    )

    assert coordinator._campaign_started_at is not None, "the witness: it started"
    campaign = campaign_of(arming)
    assert campaign.get("started") is True, campaign
    assert campaign.get("frozen_target_kwh") is not None, campaign


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_the_post_write_patch_publishes_and_decides_nothing(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """It re-renders two blocks. **It must not be able to do anything else.**

    Asserted structurally as well as behaviourally: the helper may not name a send,
    a stop, a claim or a teardown, and calling it twice changes neither the payload
    nor the plant.
    """
    import inspect

    from custom_components.alpha_ems_manager.coordinator import AlphaEmsCoordinator

    coordinator = await admitted_before_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    report = await open_the_row(
        hass, coordinator, live_surface, monkeypatch, intent=intent
    )

    source = inspect.getsource(AlphaEmsCoordinator._settle_execution_payload)
    for forbidden in (
        "_async_send_locked",
        "_async_stop_dispatch",
        "_abandon_execution",
        "_claim_authority",
        "_note_campaign_started",
        "read_snapshot",
    ):
        assert forbidden not in source, forbidden

    before = (
        dict(lifecycle_of(report)),
        dict(campaign_of(report)),
        coordinator._lifecycle,
        len(live_surface.calls),
    )
    coordinator._settle_execution_payload(report)
    coordinator._settle_execution_payload(report)

    assert (
        dict(lifecycle_of(report)),
        dict(campaign_of(report)),
        coordinator._lifecycle,
        len(live_surface.calls),
    ) == before


# ===========================================================================
# 6. the beta.38 invariants, still standing
# ===========================================================================


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_the_observability_change_moved_no_physical_decision(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """**Observability-neutral, measured on the wire.**

    The replay's writes -- which entities, in which order, with which values -- are
    the physical behaviour of the release. A lifecycle change that moved any of them
    would not be a lifecycle change.
    """
    from .beta36_trace import tick_at

    coordinator = await admitted_before_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    live_surface.calls.clear()
    await open_the_row(hass, coordinator, live_surface, monkeypatch, intent=intent)
    armed = [
        (call.data["entity_id"], call.data.get("value")) for call in live_surface.calls
    ]

    live_surface.calls.clear()
    await tick_at(hass, coordinator, live_surface, opens_at(0) + _minutes(1))
    ticked = [
        (call.data["entity_id"], call.data.get("value")) for call in live_surface.calls
    ]

    # The witness: the replay actually wrote to the plant.
    assert armed, "nothing was written on the arming refresh"
    # And the tick, which is where the projection was added, wrote what a tick
    # writes -- a setpoint or a dead-man re-arm, never a claim or a stop.
    for entity_id, _value in ticked:
        assert "dispatch" in entity_id or "power" in entity_id, entity_id
    assert coordinator._lifecycle == LIFECYCLE_EXECUTING


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_an_opened_row_still_holds_its_authority(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """beta.38's release, re-asserted against the reordered projection.

    The opened frozen row keeps execution authority through a Stage-A publication
    that cannot describe it, and no false withdrawal terminates it. If the
    lifecycle change had touched the carry state machine, this is what would break.
    """
    from .beta38_trace import carried_of
    from .test_beta38_opened_row_authority import authority_of

    coordinator = await admitted_before_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    report = await open_the_row(
        hass, coordinator, live_surface, monkeypatch, intent=intent
    )

    assert carried_of(report).get("ended_reason") is None, report
    assert authority_of(report).get("plan_authority_holds") is True, report
    assert coordinator._carried is not None
    assert coordinator._plan is not None
