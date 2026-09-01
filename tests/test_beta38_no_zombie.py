"""beta.38 Gate 2: nothing terminal owns moving hardware.

**The invariant, and it is structural rather than enumerated.**

The 2026-09-01 capture reported ``lifecycle.state: "idle"`` beside a 10 kW export.
That was not a wrong state -- it was a dead field: ``_note_lifecycle`` had no callers
anywhere in the package, so the value never left its initial ``idle`` and the other
eleven members of the vocabulary were unreachable. The question this release has to
answer -- *can the lifecycle say terminal while the EMS owns a battery that is
moving?* -- was being answered by a constant.

Wiring the field is only half of it. The other half is that the answer must be
**impossible to get wrong**, which is what ``_lifecycle_state_from`` buys:
``ownership_of`` returns ``none`` if and only if the dispatch is inactive, and the
projection returns ``idle`` only when ownership is ``none``. So ``idle`` implies an
inactive dispatch by construction, not by a list of cases somebody remembered.

Beside it, the two cadences are checked separately, because they fail differently.
The quarter cadence decides stops; the sixty-second cadence used to return before it
had asked whether anything of ours was running.
"""

from __future__ import annotations

from datetime import UTC

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.const import (
    BOOLEAN_EXECUTION_OWNER,
    EXECUTION_ABORT_STOP_REASONS,
    EXECUTION_STOP_QUARTER_PROGRESS_UNKNOWN,
    LIFECYCLE_EXECUTING,
    LIFECYCLE_IDLE,
    LIFECYCLE_STOPPED,
    LIFECYCLE_STOPPING,
    OWNERSHIP_DEGRADED,
    OWNERSHIP_FOREIGN,
    OWNERSHIP_NONE,
    OWNERSHIP_OWNED,
    OWNERSHIP_UNPROVEN,
    TICK_STOPPED_ORPHAN_DISPATCH,
)

from .beta38_trace import (
    BOTH_INTENTS,
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


# ===========================================================================
# 1. the invariant, proven structurally rather than case by case
# ===========================================================================


def test_idle_is_unreachable_while_anything_is_owned() -> None:
    """**M, and it holds by construction rather than by enumeration.**

    ``_lifecycle_state_from`` is a pure projection over booleans the write boundary
    has already settled, so the property can be checked over its whole input space
    instead of over the handful of scenarios a suite happens to build.

    Two facts compose into the invariant:

    * ``ownership_of`` answers ``none`` **if and only if** ``dispatch_active`` is
      false -- every other combination returns ``degraded``, ``foreign``,
      ``unproven`` or ``owned``;
    * this projection returns ``idle`` only when ownership is ``none``.

    Therefore ``lifecycle == idle`` implies the dispatch is not running. A zombie
    would need a fourth state to hide in, and there is not one.

    *Mutation: let any owned branch fall through to ``idle`` and this fails.*
    """
    from datetime import datetime
    from itertools import product
    from unittest.mock import Mock

    from custom_components.alpha_ems_manager.coordinator import AlphaEmsCoordinator

    now = datetime(2026, 9, 1, 20, 30, tzinfo=UTC)
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
    flags = list(product((False, True), repeat=5))

    seen = set()
    for ownership, reason, (resetting, releasing, arming, sustaining, holding) in (
        (o, r, f) for o in states for r in reasons for f in flags
    ):
        state = AlphaEmsCoordinator._lifecycle_state_from(
            faux,
            ownership_state=ownership,
            stop_reason=reason,
            resetting=resetting,
            releasing=releasing,
            arming=arming,
            sustaining=sustaining,
            holding=holding,
            now=now,
        )
        seen.add(state)
        if state == LIFECYCLE_IDLE:
            assert ownership == OWNERSHIP_NONE, (ownership, reason, state)

    # The witness: the sweep genuinely reached more than one answer, and reached
    # ``idle`` at all -- otherwise the implication above would hold vacuously.
    assert LIFECYCLE_IDLE in seen
    assert len(seen) >= 5, seen


def test_ownership_none_means_the_dispatch_is_not_running() -> None:
    """The other half of the composition, asserted against ``ownership_of`` itself.

    Without this the invariant above would rest on a reading of a function in
    another module, and a change there would break it silently.
    """
    from custom_components.alpha_ems_manager.execution import (
        OwnershipEvidence,
        ownership_of,
    )

    for marker_on in (False, True):
        evidence = OwnershipEvidence(
            dispatch_active=False, marker_on=marker_on, record=None
        )
        assert ownership_of(evidence) == OWNERSHIP_NONE

    # And an active dispatch is never ``none``, whatever else is true.
    active = OwnershipEvidence(dispatch_active=True, marker_on=False, record=None)
    assert ownership_of(active) != OWNERSHIP_NONE


# ===========================================================================
# 2. the sixty-second cadence
# ===========================================================================


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_the_tick_stops_an_owned_dispatch_with_no_authority(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """**F5: the cadence that could have stopped it, doing so.**

    Through beta.37 the tick asked "is there anything to execute?" *before* it asked
    "is anything of ours running?", so an owned dispatch whose authority had gone was
    published as ``no_admitted_quarter`` and left alone -- until the economic cadence
    reset it fifteen minutes later, or the vendor dead-man twenty minutes after the
    arm. Neither is cleanup.

    *Mutation: restore the ``no_quarter`` return ahead of the activity check and
    this fails.*
    """
    from .beta36_trace import tick_at

    coordinator = await admitted_before_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    await open_the_row(hass, coordinator, live_surface, monkeypatch, intent=intent)
    await step_once(hass, coordinator, live_surface, **step_clock(0))
    assert coordinator.store.execution_record is not None, "the witness: owned"

    # The authority disappears without the hardware noticing -- the shape a plan
    # ending between refreshes, or a torn-down schedule, actually produces.
    coordinator._plan = None
    coordinator._quarter = None
    coordinator._carried = None
    live_surface.calls.clear()

    await tick_at(hass, coordinator, live_surface, opens_at(0) + _one_minute())

    assert coordinator._tick_outcome is not None
    assert coordinator._tick_outcome.reason == TICK_STOPPED_ORPHAN_DISPATCH
    assert coordinator._tick_outcome.wrote is True
    assert coordinator.store.execution_record is None, "the claim is gone"
    assert hass.states.get(BOOLEAN_EXECUTION_OWNER).state == "off", "marker released"
    # And it is an abort reason, so it can never be withheld by the opened-row
    # authority the rest of this release adds.
    assert EXECUTION_STOP_QUARTER_PROGRESS_UNKNOWN in EXECUTION_ABORT_STOP_REASONS


def _one_minute():
    from datetime import timedelta

    return timedelta(minutes=1)


# ===========================================================================
# 3. the quarter cadence, and the states it publishes
# ===========================================================================


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_the_lifecycle_walks_from_admitted_to_executing(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """The field moves, and it moves through the states the vocabulary already had.

    A hold reports ``executing`` on purpose: beta.36 settled that a hold is a state
    of a run that is still going, and ``hold_reason`` -- published in the same block
    -- is the discriminator. Inventing a twelfth member would split one question
    across two fields, which is the defect this field exists to prevent.
    """
    coordinator = await admitted_before_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    assert lifecycle_of(coordinator.control_report or {}).get("state") == "admitted"

    publish(coordinator, monkeypatch, (moved_elsewhere(intent),))
    seen = []
    for index in (0, 0, 1):
        report = await step_once(hass, coordinator, live_surface, **step_clock(index))
        seen.append(lifecycle_of(report).get("state"))

    assert LIFECYCLE_EXECUTING in seen, seen
    assert LIFECYCLE_IDLE not in seen, seen


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_every_refresh_that_owns_the_battery_says_so(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """**The no-zombie invariant, walked over a whole replay rather than argued.**

    On every refresh of the replay: if the EMS owns the dispatch, the published
    lifecycle is not ``idle``. Checked against the payload a user would download, so
    it covers the wiring as well as the projection.
    """
    coordinator = await admitted_before_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    publish(coordinator, monkeypatch, (moved_elsewhere(intent),))

    owned_refreshes = 0
    for index in (0, 0, 1, 1, 2):
        report = await step_once(hass, coordinator, live_surface, **step_clock(index))
        ownership = ((report.get("execution") or {}).get("ownership") or {}).get(
            "state"
        )
        state = lifecycle_of(report).get("state")
        if ownership == OWNERSHIP_OWNED:
            owned_refreshes += 1
            assert state != LIFECYCLE_IDLE, (index, state, ownership)
            assert state in (
                LIFECYCLE_EXECUTING,
                LIFECYCLE_STOPPING,
                "starting",
            ), state

    assert owned_refreshes > 0, "the witness: the replay must own a dispatch"


# ===========================================================================
# 4. restart, which is a stop and is meant to be
# ===========================================================================


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_a_restart_mid_run_produces_a_verified_stop(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """**A8: a restart stops the run, and that is the design rather than a gap.**

    The frozen schedule and the quarter's measured progress are deliberately not
    persisted -- an envelope restored without the progress that gives it meaning is
    worse than no envelope. So a restart cannot know how much of the row is already
    delivered, and continuing would execute against an unknown remainder.

    beta.38 does **not** add a "started, therefore immune" flag for exactly this
    reason: it would survive while the schedule it refers to would not. What this
    pins is that the stop is *positively performed* -- the enable off, the cleanup,
    the marker released -- rather than left to the dead-man.

    Restart survival of an in-progress campaign is recorded as deferred work; it
    needs the frozen schedule, the row identity, the campaign target and the realised
    progress all persisted, and that is a schema change beta.38 does not make.
    """
    from .test_beta27_restart import simulate_restart

    coordinator = await admitted_before_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    await open_the_row(hass, coordinator, live_surface, monkeypatch, intent=intent)
    await step_once(hass, coordinator, live_surface, **step_clock(0))
    assert coordinator.store.execution_record is not None, "the witness: owned"

    simulate_restart(coordinator)
    assert coordinator._plan is None, "the frozen schedule does not survive"
    assert coordinator._carried is None
    live_surface.calls.clear()

    await step_once(hass, coordinator, live_surface, **step_clock(1))

    written = [call.data["entity_id"] for call in live_surface.calls]
    assert BOOLEAN_EXECUTION_OWNER in written, written
    assert hass.states.get(BOOLEAN_EXECUTION_OWNER).state == "off"
    assert coordinator.store.execution_record is None
    assert coordinator._lifecycle in (LIFECYCLE_STOPPED, LIFECYCLE_IDLE), (
        coordinator._lifecycle
    )
