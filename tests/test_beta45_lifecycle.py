"""beta.45: the recovery that declared every live campaign a corpse.

**The defect, from the 2026-09-06 capture.** ``_recover_campaign_lifecycle`` runs on
every report refresh -- deliberately, because a never-started instance cannot be
classified at restore time -- and its only questions were "does a mark exist" and
"which marks does it carry". Neither can tell a mark left behind by a previous
process from the mark of the campaign this process is running right now.

So the first report after ``started`` found ``started`` present and ``stopped``
absent, fell through to *"Started and never stopped: the restart is the stop"*, and
published ``failed / quarter_progress_unknown / 0.0`` over a campaign that went on
charging for five more hours. It then latched the instance closed, so the genuine
terminal was swallowed by ``_lifecycle_removed``'s own exactly-once guard and the
campaign finished in silence. The capture shows all of it: ``completed_campaign:
null`` because ``_close_campaign`` never ran, beside ``campaign_realized_kwh: 6.323``
still accumulating under a campaign the log had already buried.

Two further corrections ride with it. The persisted mark's realised figure was
written once at creation and refreshed only by a campaign-scoped stop, so even a
*genuine* restart recovered ``0.0``. And the public ``window_end`` was the high-water
mark of rows already executed rather than the end Stage A planned -- 10:15Z against a
planned 15:00Z on the same capture.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.alpha_ems_manager.const import (
    CAMPAIGN_BOUNDARY_BATTERY,
    EXECUTION_STOP_QUARTER_PROGRESS_UNKNOWN,
    LIFECYCLE_KIND_CREATED,
    LIFECYCLE_KIND_REMOVED,
    LIFECYCLE_KIND_STARTED,
    LIFECYCLE_KIND_STOPPED,
    OUTCOME_FAILED,
    OUTCOME_PARTIAL,
)
from custom_components.alpha_ems_manager.coordinator import AlphaEmsCoordinator

from .test_beta42_lifecycle_events import (  # noqa: F401
    _mark,
    events,
    restarted,
    settle,
)

NOW = datetime(2026, 8, 21, 3, 30, tzinfo=UTC)

#: The live instance from the capture, and the target it announced.
LIVE_INSTANCE = "c21a6c3046961830"
LIVE_CAMPAIGN = "af82a579ac6a803a"
LIVE_TARGET = 15.11


def _running(coordinator, **overrides: Any) -> dict[str, Any]:
    """Give the coordinator a mark for the campaign it is *currently running*.

    The identity is the point: ``_campaign_instance_id`` is what the coordinator
    minted when it opened this campaign, and the mark carries the same id because
    ``_lifecycle_created`` wrote it there.
    """
    mark = _mark(
        instance_id=LIVE_INSTANCE,
        campaign_id=LIVE_CAMPAIGN,
        marks=[LIFECYCLE_KIND_CREATED, LIFECYCLE_KIND_STARTED],
        started_at="2026-08-21T02:15:00+00:00",
        planned_kwh=LIVE_TARGET,
        window_end="2026-08-21T09:00:00+00:00",
        **overrides,
    )
    coordinator.store.campaign_lifecycle = mark
    coordinator._campaign_instance_id = LIVE_INSTANCE
    coordinator._campaign_id = LIVE_CAMPAIGN
    return mark


# ===========================================================================
# G1 -- the live instance is not a corpse
# ===========================================================================


async def test_a_live_campaign_survives_twenty_refreshes_without_a_terminal(
    restarted,  # noqa: F811
    events,  # noqa: F811
    settle,  # noqa: F811
) -> None:
    """**The anchor, at the live shape and the live cadence.**

    Twenty refreshes is five hours of the captured campaign. Before beta.45 the
    *first* one published ``failed`` and latched the instance closed.

    *Mutation: remove the liveness guard, or key it on ``campaign_id``, and this
    fails on the first pass.*
    """
    mark = _running(restarted)

    for index in range(20):
        restarted._recover_campaign_lifecycle(NOW + index * timedelta(minutes=15))
    await settle()

    assert events == [], "a running campaign published nothing"
    assert restarted.store.campaign_lifecycle is mark, "and its mark survived"
    assert not restarted.store.lifecycle_closed(LIVE_INSTANCE), (
        "the instance is not latched, so its real terminal can still fire"
    )
    assert restarted._last_campaign_result is None


async def test_the_guard_reports_that_it_recognised_the_live_instance(
    restarted,  # noqa: F811
) -> None:
    """The return value names the decision, so a trace can show which branch ran."""
    _running(restarted)
    assert restarted._recover_campaign_lifecycle(NOW) == "live"


async def test_a_genuine_restart_mark_is_still_recovered(
    restarted,  # noqa: F811
    events,  # noqa: F811
    settle,  # noqa: F811
) -> None:
    """**The counterweight, and the reason the guard is an identity test.**

    On a real restart the in-memory instance id is ``None`` -- the attribute is
    minted when a campaign opens and this process has opened nothing -- so the mark
    is somebody else's and recovery proceeds exactly as beta.42 designed it.

    *Mutation: make the guard unconditional and this stops recovering anything.*
    """
    restarted.store.campaign_lifecycle = _mark(
        marks=[LIFECYCLE_KIND_CREATED, LIFECYCLE_KIND_STARTED],
        started_at="2026-08-21T02:15:00+00:00",
    )
    assert restarted._campaign_instance_id is None, "the restart witness"

    result = restarted._recover_campaign_lifecycle(NOW)
    await settle()

    assert result == OUTCOME_FAILED
    kinds = [event["kind"] for event in events]
    assert kinds == [LIFECYCLE_KIND_STOPPED, LIFECYCLE_KIND_REMOVED]
    assert events[-1]["completion_reason"] == EXECUTION_STOP_QUARTER_PROGRESS_UNKNOWN


async def test_a_second_attempt_does_not_shield_the_first_attempts_orphan(
    restarted,  # noqa: F811
    events,  # noqa: F811
    settle,  # noqa: F811
) -> None:
    """**The same campaign, attempted twice -- which is why the key is the instance.**

    ``_note_campaign_progress`` says it outright: one economic campaign may be
    attempted more than once in a day, once aborted for a hazard and once afresh,
    and those are two things that happened with two frozen objectives and two
    terminals. So the live attempt and the orphan share a ``campaign_id`` and differ
    only by instance -- and a guard that compared the campaign would call the dead
    attempt live and leave it open for ever.

    The first version of this test gave the orphan a *different* campaign id, which
    let the campaign-keyed guard reach the right answer by accident. The mutation
    survived, so the test was rewritten rather than the mutation weakened.

    *Mutation: compare ``campaign_id``, or invert the guard, and this fails.*
    """
    restarted._campaign_instance_id = "the_second_attempt"
    restarted._campaign_id = LIVE_CAMPAIGN
    restarted.store.campaign_lifecycle = _mark(
        instance_id="the_first_attempt",
        campaign_id=LIVE_CAMPAIGN,
        marks=[LIFECYCLE_KIND_CREATED, LIFECYCLE_KIND_STARTED],
        started_at="2026-08-21T02:15:00+00:00",
    )

    result = restarted._recover_campaign_lifecycle(NOW)
    await settle()

    assert result == OUTCOME_FAILED, "the abandoned attempt closed"
    assert [event["kind"] for event in events][-1] == LIFECYCLE_KIND_REMOVED
    assert events[-1]["campaign_instance_id"] == "the_first_attempt"


# ===========================================================================
# G2 -- the persisted evidence is kept current
# ===========================================================================


async def test_a_restart_mid_campaign_recovers_the_realised_figure(
    restarted,  # noqa: F811
    events,  # noqa: F811
    settle,  # noqa: F811
) -> None:
    """**``0.0`` was structural, not a measurement.**

    ``realized_kwh`` was written once at creation and refreshed only by
    ``_lifecycle_stopped``, which a campaign reaches only through a campaign-scoped
    stop. So a genuine restart three hours in recovered a zero and filed ``failed``,
    when the evidence to file ``partial`` against the real figure existed all along.

    *Mutation: stop refreshing the mark and this reports 0.0 and ``failed``.*
    """
    restarted.store.campaign_lifecycle = _mark(
        marks=[LIFECYCLE_KIND_CREATED, LIFECYCLE_KIND_STARTED, LIFECYCLE_KIND_STOPPED],
        started_at="2026-08-21T02:15:00+00:00",
        stopped_at="2026-08-21T03:00:00+00:00",
        stop_reason="window_ended",
        realized_kwh=6.323,
        frozen_target_kwh=15.11,
        success_tolerance_kwh=0.75,
        measurable=True,
    )

    result = restarted._recover_campaign_lifecycle(NOW)
    await settle()

    assert result == OUTCOME_PARTIAL, "measurable evidence, short of target"
    assert events[-1]["realised_kwh"] == pytest.approx(6.323)
    assert events[-1]["shortfall_kwh"] == pytest.approx(8.787, abs=1e-3)


def _evidence_rig(**overrides: Any):
    """Return a coordinator carrying an open campaign and its mark."""
    coordinator = object.__new__(AlphaEmsCoordinator)
    coordinator._campaign_instance_id = LIVE_INSTANCE
    coordinator._campaign_realized_kwh = 6.323
    coordinator._campaign_quarters_admitted = 13
    coordinator._campaign_measurable = True
    coordinator._campaign_frozen_target_kwh = LIVE_TARGET
    coordinator._campaign_accrued_row = None
    coordinator._quarter = None
    # The tolerance is a fraction of the pack as well as of the promise, so the
    # rig carries the reference installation's capacity rather than a stub number.
    coordinator.config = SimpleNamespace(battery_capacity_kwh=21.6)
    for key, value in overrides.items():
        setattr(coordinator, key, value)
    mark = {"instance_id": LIVE_INSTANCE, "realized_kwh": 0.0, "measurable": True}
    coordinator.store = SimpleNamespace(
        campaign_lifecycle=mark, schedule_save=lambda: None
    )
    return coordinator, mark


def test_refreshing_the_evidence_writes_the_live_figures() -> None:
    """Realised, measurability, the frozen target and its tolerance, together.

    The four are written as a set because a restart landing between them could not
    tell a finished campaign from an interrupted one -- the reason
    ``_lifecycle_stopped`` already writes them together.
    """
    coordinator, mark = _evidence_rig()
    coordinator._refresh_lifecycle_evidence()

    assert mark["realized_kwh"] == pytest.approx(6.323)
    assert mark["measurable"] is True
    assert mark["frozen_target_kwh"] == pytest.approx(LIVE_TARGET)
    assert mark["success_tolerance_kwh"] > 0.0


def test_an_unfrozen_target_writes_no_tolerance() -> None:
    """**Nothing is fabricated where there is no evidence.**

    A campaign that never froze a target has no promise to be judged against, so it
    gets no tolerance rather than a zero one -- a zero tolerance would make the
    success test exact and turn a rounding difference into a failure.
    """
    coordinator, mark = _evidence_rig(_campaign_frozen_target_kwh=None)
    coordinator._refresh_lifecycle_evidence()

    assert mark["frozen_target_kwh"] is None
    assert "success_tolerance_kwh" not in mark


def test_another_instances_mark_is_never_written() -> None:
    """This boot's figures never land in a mark waiting to be recovered.

    Writing them would invent the very evidence the recovery pass exists to read.
    """
    coordinator, mark = _evidence_rig()
    mark["instance_id"] = "somebody_else"
    coordinator._refresh_lifecycle_evidence()

    assert mark["realized_kwh"] == 0.0


def test_accrual_refreshes_the_evidence() -> None:
    """The hook, so the mark moves with the campaign rather than with its stop."""
    coordinator, mark = _evidence_rig(
        _campaign_realized_kwh=0.0, _campaign_quarters_admitted=0
    )
    coordinator._campaign_id = LIVE_CAMPAIGN
    coordinator._quarter_progress_unknown = False
    quarter = SimpleNamespace(
        campaign_id=LIVE_CAMPAIGN, quarter_start=NOW, quarter_end=NOW
    )
    coordinator._accrue_campaign_progress(quarter, 0.56)

    assert mark["realized_kwh"] == pytest.approx(0.56)
    assert coordinator._campaign_quarters_admitted == 1


# ===========================================================================
# G3 -- the published end is the planned end
# ===========================================================================


def _window_rig(planned: datetime | None, observed: datetime | None):
    coordinator = object.__new__(AlphaEmsCoordinator)
    coordinator._campaign_planned_end_utc = planned
    coordinator._campaign_end_utc = observed
    return coordinator


PLANNED_END = datetime(2026, 9, 6, 15, 0, tzinfo=UTC)
OBSERVED_END = datetime(2026, 9, 6, 10, 15, tzinfo=UTC)


def test_the_public_window_end_is_the_planned_end() -> None:
    """**10:15Z against a planned 15:00Z, on the capture.**

    ``_campaign_end_utc`` is a high-water mark of rows already executed, so
    publishing it told a reader a thirty-three row campaign ended at the quarter in
    flight -- and it is the instant ``_dangling_creation_reason`` compares ``now``
    against.

    *Mutation: prefer the observed end and this fails.*
    """
    assert _window_rig(PLANNED_END, OBSERVED_END)._campaign_window_end_utc() == (
        PLANNED_END
    )


def test_the_observed_end_is_used_only_when_no_end_was_planned() -> None:
    """A fallback, not a preference: some campaigns open before the plan arrives."""
    assert _window_rig(None, OBSERVED_END)._campaign_window_end_utc() == OBSERVED_END
    assert _window_rig(None, None)._campaign_window_end_utc() is None


def test_the_open_campaign_block_publishes_both_ends() -> None:
    """``campaign_end`` keeps its meaning; the planned end gets its own key.

    The observed end still bounds the orphan grace, which is a statement about how
    far execution actually got -- so it is published beside the planned one rather
    than replaced by it.
    """
    coordinator = object.__new__(AlphaEmsCoordinator)
    coordinator._campaign_id = LIVE_CAMPAIGN
    coordinator._campaign_instance_id = LIVE_INSTANCE
    coordinator._campaign_end_utc = OBSERVED_END
    coordinator._campaign_planned_end_utc = PLANNED_END
    coordinator._campaign_boundary = CAMPAIGN_BOUNDARY_BATTERY
    coordinator._campaign_started_at = NOW
    coordinator._campaign_frozen_target_kwh = LIVE_TARGET
    coordinator._campaign_opening_target_kwh = LIVE_TARGET
    coordinator._campaign_realized_kwh = 6.323
    coordinator._campaign_quarters_admitted = 13
    coordinator._campaign_measurable = True
    coordinator._campaign_accrued_row = None
    coordinator._campaign_run_id = "c8297d1335c2a178"
    coordinator._campaign_objective_rows = 33
    coordinator._quarter = None

    block = coordinator._open_campaign_block()

    assert block["campaign_end"] == OBSERVED_END.isoformat()
    assert block["planned_end"] == PLANNED_END.isoformat()
