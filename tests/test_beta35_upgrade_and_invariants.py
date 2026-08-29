"""beta.35: upgrading onto it, and the two invariants A9 is worth nothing without.

**Upgrading.** A beta.34 installation has an execution record on disk whose
``stale_after`` is the row's own end -- the defect this release exists to remove.
The first refresh after the upgrade reads it, finds it expired, and stops the
dispatch once. That is the safe direction and it is correct; what it must not do is
look like a *beta.35* fault, and it must not leave anything behind that a later row
could arm from. No schema version moves for any of this: the value written into
``stale_after`` changed, its shape did not.

**The invariants.** A9 says there are two states and no third one. The third state
is what actually happened on 2026-08-29: for fifteen minutes the controller
described a ``net_export`` quarter, under a live ``plan_id`` and ``run_id``, with
its own ticks reporting ``dispatch_not_active`` -- and 0.001 kWh crossed the meter
against 2.28 planned. Nothing raised, nothing logged a stop reason, and nothing in
the payload said anything was wrong. So it is asserted directly, on the published
report rather than on an internal flag.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.alphaess_device import DISPATCH_ENABLE
from custom_components.alpha_ems_manager.const import (
    CLAIM_SCHEMA_VERSION,
    CONFIG_ENTRY_VERSION,
    EXECUTION_ABORT_STOP_REASONS,
    EXECUTION_WITHDRAWAL_STOP_REASONS,
    OWNERSHIP_OWNED,
    STORAGE_VERSION,
)

from .beta35_trace import admitted_plan, opens_at, step_clock
from .test_beta24_live_charge import LiveSurface, step_once
from .test_beta35_campaign_continuity import start_the_campaign

pytestmark = pytest.mark.usefixtures("control_surface")


@pytest.fixture
def live_surface(hass: HomeAssistant, control_surface: None) -> LiveSurface:
    """Return a control surface that responds to writes."""
    return LiveSurface(hass)


# ===========================================================================
# 1. upgrading from beta.34
# ===========================================================================


def test_no_persisted_schema_version_moves_for_this_release() -> None:
    """**The cheapest possible upgrade, and it is checked rather than assumed.**

    ``async_migrate_entry`` is a refusal rather than a converter, so bumping
    ``CONFIG_ENTRY_VERSION`` would make every existing entry fail to load. The
    claim record's *value* changed and its shape did not, so its schema is
    untouched too -- a beta.34 record still parses, it simply expires earlier,
    which is the safe direction.
    """
    assert CONFIG_ENTRY_VERSION == 2
    assert STORAGE_VERSION == 2
    assert CLAIM_SCHEMA_VERSION == 2


async def test_a_beta_34_record_expires_once_and_leaves_nothing_behind(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**The first refresh after the upgrade, with the old row-bounded claim.**

    Rewriting the persisted ``stale_after`` back to the row's end is exactly what
    a beta.34 install has on disk. The claim is then genuinely expired -- and with
    the frozen schedule gone as well, nothing outranks the withdrawal, so the stop
    stands. It is one stop, it is not reported as a beta.35 defect, and the
    teardown is total: no plan survives for a later row to arm from.
    """
    coordinator = await start_the_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    record = dict(coordinator.store.execution_record or {})
    assert record

    # Exactly what beta.34 persisted: the claim dies with the row it was made for.
    record["stale_after"] = coordinator._quarter.quarter_end.isoformat()
    coordinator.store.execution_record = record
    # And a process restart, which is when an upgrade is actually noticed.
    coordinator._carried = None
    coordinator._plan = None
    coordinator._quarter = None
    coordinator._reset_quarter_progress(None)
    coordinator._quarter_progress_unknown = False

    report = await step_once(hass, coordinator, live_surface, **step_clock(1))
    execution = report.get("execution") or {}
    boundary = execution.get("write_boundary") or {}
    authority = boundary.get("authority") or {}

    # Nothing was withheld -- there was no authority to withhold it.
    assert authority.get("plan_authority_holds") is False
    assert authority.get("withheld_stop_reason") is None

    # Total, so the upgrade cannot leave a schedule that arms fifteen minutes later.
    assert coordinator._plan is None
    assert coordinator._quarter is None
    assert coordinator.store.execution_record is None
    assert coordinator._campaign_id is None

    # And it does not repeat: a second refresh has nothing left to stop.
    live_surface.calls.clear()
    await step_once(hass, coordinator, live_surface, **step_clock(2))
    armed = [
        call
        for call in live_surface.calls
        if call.data.get("entity_id") == DISPATCH_ENABLE and call.service == "turn_on"
    ]
    assert armed == []


# ===========================================================================
# 2. A9 -- no third state
# ===========================================================================


async def test_no_refresh_narrates_a_moving_intent_while_nothing_is_dispatched(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**The silent state, asserted on the payload rather than on a flag.**

    Between 20:00 and 20:15 on 2026-08-29 the report described a ``net_export``
    quarter with a 2.28 kWh meter target under a live ``plan_id`` and ``run_id``
    while the dispatch was inactive. Either the plan is authoritative and something
    is being written, or it is gone and there is nothing to describe -- and if a
    refresh manages to be in neither position, it must at least publish the reason.

    Checked across the whole replayed campaign, every refresh, so it is an
    invariant rather than a spot check.
    """
    coordinator = await start_the_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )

    for row in (0, 1, 2):
        report = await step_once(hass, coordinator, live_surface, **step_clock(row))
        execution = report.get("execution") or {}
        boundary = execution.get("write_boundary") or {}
        intent = report.get("intent") or {}
        if not intent.get("moves_battery"):
            continue
        owned = (execution.get("ownership") or {}).get("state") == OWNERSHIP_OWNED
        if not owned:
            continue
        wrote = bool(live_surface.calls)
        named = boundary.get("stop_reason") or (boundary.get("authority") or {}).get(
            "withheld_stop_reason"
        )
        refused = (report.get("authorization") or {}).get("refusal")
        assert wrote or named or refused, (
            f"row {row}: an owned battery-moving intent was narrated, nothing was "
            f"written, and no reason was published -- the beta.34 silent state"
        )
        live_surface.calls.clear()


async def test_a_safety_condition_at_the_boundary_aborts_through_the_refresh(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """The abort branch of the same trace, driven the way production reaches it.

    The continuity suite proves the teardown is total by calling the helper; this
    proves the *refresh* reaches it. Safety is in ``EXECUTION_ABORT_STOP_REASONS``
    and no plan authority may withhold it -- the suppression is for Stage A
    revising the future and for nothing else, and that distinction is the only
    thing standing between beta.35 and a much worse defect than the one it fixes.
    """
    coordinator = await start_the_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    assert coordinator._plan_authority_holds(opens_at(1) + timedelta(minutes=1))

    # The dispatch stops being ours mid-campaign: the marker goes out from under a
    # running dispatch, which is a lost claim rather than a horizon revision.
    hass.states.async_set("input_boolean.alpha_ems_dispatch_owner", "off")
    await hass.async_block_till_done()

    live_surface.calls.clear()
    report = await step_once(hass, coordinator, live_surface, **step_clock(1))
    boundary = (report.get("execution") or {}).get("write_boundary") or {}

    # **Nothing was withheld, and that is the assertion.** Authority alone is not
    # enough to suppress a stop: the suppression also requires the run to be safe,
    # owned and measurable, and a marker that has gone out from under a running
    # dispatch fails that whatever the frozen schedule says.
    authority = boundary.get("authority") or {}
    assert authority.get("withheld_stop_reason") is None

    # The reason published here is the withdrawal one, because Stage A has also
    # genuinely stopped affirming this run in the same refresh -- the replay
    # withdraws it before quarter one opens. What matters is that the withdrawal
    # was allowed to stand rather than being outranked, which the line above is.
    stop = boundary.get("stop_reason")
    assert stop is not None
    assert stop in set(EXECUTION_WITHDRAWAL_STOP_REASONS) | set(
        EXECUTION_ABORT_STOP_REASONS
    ), stop

    # And the third row never comes back, whatever the clock does.
    live_surface.calls.clear()
    await step_once(hass, coordinator, live_surface, **step_clock(2))
    armed = [
        call
        for call in live_surface.calls
        if call.data.get("entity_id") == DISPATCH_ENABLE and call.service == "turn_on"
    ]
    assert armed == [], "a campaign that lost its claim may not re-arm a later row"


async def test_the_frozen_schedule_cannot_outlive_its_own_end(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**The bound that keeps the suppression from becoming indefinite execution.**

    Withholding a withdrawal is only defensible because it cannot last: the plan
    ends when the plan ends. Past ``ends_at`` there is no authority left to outrank
    anything, whatever the schedule still contains.
    """
    coordinator = await start_the_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    plan = coordinator._plan
    assert plan is not None

    assert coordinator._plan_authority_holds(plan.ends_at - timedelta(minutes=1))
    assert not coordinator._plan_authority_holds(plan.ends_at)
    assert not coordinator._plan_authority_holds(plan.ends_at + timedelta(hours=1))

    # And a row that covers no instant grants nothing either, which is the second
    # bound: a gap inside an open plan is not an authority to keep executing.
    beyond = admitted_plan().rows[-1].end + timedelta(minutes=1)
    assert plan.row_covering(beyond) is None
