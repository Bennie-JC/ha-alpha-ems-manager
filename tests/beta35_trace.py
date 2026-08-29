"""The 2026-08-29 hardware Sell, as a replayable fixture.

**The first real economic export this project has ever executed, and the first
one it destroyed.** Everything here is measured, from two diagnostics downloads
and the physical-decision ring between them. Nothing is invented; where a figure
was not recorded it is absent rather than estimated.

The campaign, admitted at 19:30 from a publication issued 19:30:06.094997+02:00:

===========  ==============  ==================  ===================
row          window (local)  battery ceiling     meter objective
===========  ==============  ==================  ===================
Q1           19:45-20:00     2.50 kWh            2.25 kWh
Q2           20:00-20:15     2.50 kWh            2.28 kWh
Q3           20:15-20:30     0.75 kWh            0.52 kWh
===========  ==============  ==================  ===================

``plan_id`` ``5a4f54a741429531``, ``run_id`` ``b960f9b5e1d9e4cb``,
``campaign_id`` ``c23ecf5eabfbe386``, battery target 5.75 kWh, meter target
5.05 kWh.

What beta.34 did with it
------------------------

**Q1 worked, and proved the hard part.** ~10.05 kW battery discharge, 8.7-8.9 kW
meter export, P1 genuinely negative, Mode 2, ownership ``owned``, dead-man armed.
Measured: 2.211 kWh battery, 1.92 kWh meter.

**Q2 was lost.** At 20:00:05.889489 the refresh adopted the persisted claim --
whose ``stale_after`` was ``quarter.quarter_end``, i.e. 20:00:00.000000 -- read it
as ``stale_plan`` 5.9 s past a deadline it could never have been inside, and reset
the dispatch. The controller then went on *describing* Q2 as ``net_export`` with
its original 2.50 / 2.28 kWh targets under the same ``plan_id`` and ``run_id``,
while the physical ticks reported ``dispatch_not_active``. **≈0.001 kWh crossed
the meter against 2.28 planned.**

**Q3 came back from the dead.** At 20:15 the surviving frozen schedule advanced to
its third row, ``_claim_authority`` minted a fresh claim, and Stage B **re-armed
the inverter**: ~3.0 kW calculated, ~3.1 kW battery, ~2.1 kW meter observed.

And the logbook recorded the whole thing as
``Canceled -- Plan Replaced -- 0.00 / 5.05 kWh``.

What beta.35 must do with it
----------------------------

Uninterrupted Q1 -> Q2 -> Q3, one campaign opened once and closed once, realised
meter export equal to the sum of the three quarters. The abort branch of the same
fixture -- a safety condition injected at the Q1/Q2 boundary instead of a plan
withdrawal -- must stop immediately and **never re-arm Q3**, asserted on the write
log rather than on a flag.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from custom_components.alpha_ems_manager.const import EXECUTION_INTENT_NET_EXPORT
from custom_components.alpha_ems_manager.execution import (
    AdmittedPlan,
    QuarterRow,
    admit,
    admit_plan,
    parse_target,
)

from .forecast_helpers import NORMAL, local

#: The identities the live capture published, kept verbatim.
PLAN_ID = "5a4f54a741429531"
RUN_ID = "b960f9b5e1d9e4cb"
CAMPAIGN_ID = "c23ecf5eabfbe386"

#: The frozen schedule, as three consecutive quarters:
#: ``(battery ceiling kWh, meter objective kWh)``.
ROW_TARGETS: tuple[tuple[float, float], ...] = (
    (2.50, 2.25),
    (2.50, 2.28),
    (0.75, 0.52),
)

#: Where the live campaign sat on the clock: 19:45-20:30 Europe/Amsterdam.
LIVE_START = (19, 45)

#: Where the replay puts it, and why it is not 19:45.
#:
#: **The shape is the trace; the wall-clock is the harness's.** ``owned_live_charge``
#: establishes a *proven* ownership claim anchored at its own quarter, and ownership
#: has to be proven rather than asserted -- a claim re-stamped onto a different hour
#: stops matching the dispatch it names and the replay would then be measuring a
#: refusal instead of a continuation. So the three rows are re-based onto the
#: fixture's clock and everything that matters is unchanged: three consecutive
#: quarters, the measured targets, one campaign identity, and a Stage-A publication
#: that moves the Sell out of the admitted window at the first boundary.
REPLAY_START = (10, 45)

#: Measured Q1 delivery, from ``completed_quarters[0]`` of the 20:00 download.
Q1_BATTERY_KWH = 2.211
Q1_METER_KWH = 1.92

#: The campaign objective the publication announced.
BATTERY_TARGET_KWH = 5.75
METER_TARGET_KWH = 5.05

QUARTER = timedelta(minutes=15)


def opens_at(index: int = 0, *, start: tuple[int, int] = REPLAY_START):
    """Return the local instant row ``index`` opens at."""
    return local(NORMAL, *start) + index * QUARTER


def step_clock(index: int) -> dict[str, int]:
    """Return ``hour``/``minute`` keyword arguments opening row ``index``.

    ``step_once`` takes a wall clock, the rows are defined as instants, and this
    is the single place the two meet -- so re-basing the replay stays a one-line
    change to :data:`REPLAY_START`.
    """
    moment = opens_at(index)
    return {"hour": moment.hour, "minute": moment.minute}


def rows() -> tuple[QuarterRow, ...]:
    """Return the three frozen rows, as ``quarter_schedule_for`` emits them."""
    return tuple(
        QuarterRow(
            start=opens_at(index),
            end=opens_at(index) + QUARTER,
            battery_kwh=battery,
            grid_authorised_kwh=0.0,
            grid_export_target_kwh=meter,
            grid_export_caused_kwh=meter,
            desired_grid_kw=-meter / 0.25,
        )
        for index, (battery, meter) in enumerate(ROW_TARGETS)
    )


def published_target(*, plan_id: str = PLAN_ID, **overrides) -> dict:
    """Return the publication the campaign was admitted from, as Stage A emits one.

    **The whole payload, not the fields a test happens to read.** ``admit_plan``
    keeps the publication it admitted, ``_write_execution_record`` persists it, and
    ``carried_from_record`` rebuilds a run from it after a restart -- so a fixture
    whose target is ``None`` silently disables adoption, which is the very path the
    2026-08-29 boundary failure ran through. A replay that cannot reach it cannot
    reproduce the defect.
    """
    opens = opens_at(0)
    payload = {
        "plan_id": plan_id,
        "revision": 1,
        "intent": EXECUTION_INTENT_NET_EXPORT,
        "purpose": "export",
        "window_start": opens.isoformat(),
        "window_end": (opens + len(ROW_TARGETS) * QUARTER).isoformat(),
        "issued_at": (opens - timedelta(minutes=15)).isoformat(),
        "stale_after": (opens + timedelta(minutes=30)).isoformat(),
        "battery_target_kwh": BATTERY_TARGET_KWH,
        "grid_target_kwh": METER_TARGET_KWH,
        "average_power_kw": 9.0,
        "first_power_kw": 10.0,
        "reserve_floor_kwh": 4.32,
        "campaign_id": CAMPAIGN_ID,
        "quarter_schedule": [row.as_dict() for row in rows()],
    }
    payload.update(overrides)
    return payload


def admitted_plan(
    *,
    admitted_minutes_early: int = 15,
    run_id: str | None = None,
    plan_id: str = PLAN_ID,
) -> AdmittedPlan:
    """Return the frozen schedule exactly as the 19:30 publication produced it.

    **Built by ``admit_plan`` from a real publication**, not assembled field by
    field: the identity rule that makes a boundary sustain possible -- the plan
    adopts the run's ``run_id`` -- lives in that function, and a fixture that
    restated it by hand could agree with a version of production that no longer
    exists.

    Admitted before the first row opens, which is what makes it authoritative for
    the whole span: Stage A's head is ``elapsed + 1``, so no later publication can
    describe a row that has already opened.

    ``run_id`` names the run to adopt the identity of. ``None`` -- the default --
    means no carried run, so the plan is its own identity, which is what production
    does for a campaign Stage A has already moved past.
    """
    target = parse_target(published_target(plan_id=plan_id))
    assert target is not None
    admitted_at = opens_at(0) - timedelta(minutes=admitted_minutes_early)
    run = None
    if run_id is not None:
        run = replace(admit(target, admitted_at), run_id=run_id)
    plan = admit_plan(target, run=run, now=admitted_at)
    assert plan is not None
    return plan


def moved_the_plan_away(coordinator, monkeypatch, *, quarters_after: int = 4) -> None:
    """Publish a Sell that does **not** overlap the admitted window.

    This is the condition that destroyed the live campaign, reproduced exactly:
    at 20:00 Stage A published an export starting at 21:00, which shares no
    interval with the accepted 19:45-20:30 window, so ``affirms`` is false and the
    carried run is withdrawn. A revision of the *future*, and nothing more.
    """
    start = opens_at(len(ROW_TARGETS) + quarters_after)
    target = {
        "plan_id": "moved-elsewhere",
        "revision": 1,
        "intent": EXECUTION_INTENT_NET_EXPORT,
        "purpose": "export",
        "window_start": start.isoformat(),
        "window_end": (start + 2 * QUARTER).isoformat(),
        "issued_at": start.isoformat(),
        "stale_after": (start + timedelta(minutes=30)).isoformat(),
        "battery_target_kwh": 4.0,
        "grid_target_kwh": 3.6,
        "average_power_kw": 8.0,
        "first_power_kw": 10.0,
        "reserve_floor_kwh": 4.32,
        "campaign_id": "a-different-campaign",
        "quarter_schedule": [],
    }
    monkeypatch.setattr(
        type(coordinator), "_execution_targets", lambda self, **kwargs: (target,)
    )


def withdraw_everything(coordinator, monkeypatch) -> None:
    """Publish nothing at all -- the strongest form of the same condition."""
    monkeypatch.setattr(
        type(coordinator), "_execution_targets", lambda self, **kwargs: ()
    )


def zero_objective_plan() -> AdmittedPlan:
    """Return the same campaign with a frozen objective of exactly zero.

    **"This campaign sells nothing" and "nobody published it" are different
    answers**, and the freeze conflated them by reaching for the opening capture
    whenever the live figure was falsy. A schedule of executable rows that command
    no meter export is the case that separates them: the objective is genuinely
    ``0.0``, and a campaign judged against 5.05 kWh it never promised would be
    reported as a total failure.
    """
    flat = [
        {**row.as_dict(), "grid_export_target_kwh": 0.0, "grid_export_caused_kwh": 0.0}
        for row in rows()
    ]
    target = parse_target(published_target(quarter_schedule=flat, grid_target_kwh=0.0))
    assert target is not None
    plan = admit_plan(target, run=None, now=opens_at(0) - timedelta(minutes=15))
    assert plan is not None
    return plan
