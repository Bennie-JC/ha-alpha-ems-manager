"""The 2026-08-30 and 2026-08-31 Live charges, as one replayable fixture.

**Two hardware incidents, one defect, and neither of them an economic one.** Both
days the optimiser was right, the plant was working, the claim was intact and the
dead-man was advancing. Both days a multi-quarter charge campaign was destroyed by
its own execution layer, hours before its window ended.

2026-08-30 -- a quarter *succeeding*
------------------------------------

Campaign ``da0394e47aa7abd8`` (charge, 16.11 kWh over 33 rows) admitted at 06:15Z.
At **06:59:26Z** a row reached its own battery objective, which is a success, and
beta.35 routed that success through the atomic abort helper: the campaign identity
was latched into ``_abandoned_campaigns``, so ``_refresh_executing_quarter`` nulled
``self._plan`` on **every refresh for the next five and a half hours**. The charge
ran on the degraded run-level fallback with no admitted quarter, no campaign, no
per-row grid ceiling and no completed-quarter record. At **12:45:06Z** a
``stage_a_hold`` arrived; ``_plan_authority_holds`` returns ``False`` whenever
``self._plan is None``, so beta.35's own withdrawal suppression was unreachable and
the run was reset with **9.889 of 16.11 kWh unrealised**.

The terminal read ``0.27 / 16.74 kWh``, ``quarters_admitted: 2`` against three
rows, ``reason: quarter_target_reached``, ``reason_vocabulary: run_stop``. Activity
rendered ``Finished -- Partial`` for a campaign that had lost 61 % of its objective.

2026-08-31 -- a quarter *resting*
---------------------------------

Campaign ``1be3a9699b41dab1`` (charge, 10.89 kWh, ``campaign_end 15:00Z``) ran
three rows 08:15-09:00Z and was terminated at **09:00:06Z** with ``reason:
safety`` -- at the refresh where the *fourth* row opened, which the terminal's own
``window_end 09:15Z`` proves. Nothing was unsafe. Production was covering the
house and the row's grid budget was 70-96 % spent, so ``decide_charge`` clamped the
authorised rate to zero -- a correct clamp -- and:

* ``quarter_intent_for`` returns ``None`` for ``power_kw <= 0``;
* the reserve-guard fallback is suppressed exactly then, because Stage B holds the
  run;
* ``evaluate(None, ...)`` is unsafe by construction with ``no_plan``;
* ``unsafe_while_owned`` promoted it to ``EXECUTION_STOP_SAFETY``, the one member
  of the abort family that may never be suppressed.

Then the same latch, and a zombie loop on top of it: ``carry_forward`` had no
abandoned check, so the run layer minted a fresh run from a target still naming the
dead campaign every fifteen minutes while the plan layer destroyed that run's plan
on the same refresh.

What this fixture reproduces
----------------------------

One campaign, **two admitted plans**, seven rows and a ``serve_load`` gap:

======  ==============  ===========  =========  ==================================
row     window (local)  battery kWh  plan       what it proves
======  ==============  ===========  =========  ==================================
0       10:45-11:00     0.56         A          falls short, expires, hands over
1       11:00-11:15     0.28         A          **reaches target mid-campaign**
2       11:15-11:30     0.56         A          the row after a completion
3       11:30-11:45     --           A          ``serve_load`` gap **inside** a plan
4       11:45-12:00     0.56         A          the row after the gap, last of plan A
5       12:00-12:15     --           --         the gap **between** two runs
6       12:15-12:30     0.56         B          first row of plan B, same campaign
7       12:30-12:45     0.56         B          the genuine final row
======  ==============  ===========  =========  ==================================

Two gaps, and they are different failures. Row 3 is a non-executable interval inside
one admitted schedule: beta.35's ``_async_end_row(stop=True)`` read "no row covers
this instant" as "the plan is over" and lost every row after it (**D2b**). Row 5 is
the space between two published runs of one campaign, which is the shape the
2026-08-30 campaign actually had -- and the campaign has to stay open across it,
because ``_campaign_objective_kwh`` has a whole fallback branch for a campaign
spanning several plans and nothing had ever tested it (**R3**).

**Energy comes from measured flows, never from poking accumulators.** A coherent
charging site -- PV 0 W, house 2.0 kW, battery -1.4 kW, grid 3.4 kW import -- makes
``_accrue_quarter_progress`` integrate 1.4 kW x 60 s = **0.02333 kWh per tick**, so
every figure below is arithmetic rather than assertion: row 1's 0.28 kWh objective
is met on tick 12, strictly inside the row, with five executable rows still ahead;
rows of 0.56 kWh cannot be met in fifteen minutes at 1.4 kW (0.35 kWh) and
therefore expire.

The 2026-09-01 hardware measurement
-----------------------------------

The one physical unknown this release rested on, measured on the reference inverter
with the helpers driven by hand while the integration watched:

==========================  ==========
Dispatch                    ON
Mode                        State of Charge Control (2)
Power command               0.0 kW
Cutoff                      100 %
Duration                    25 min
Battery SoC                 ~75 %
**Battery power**           **exactly 0 W**
PV production               ~2.8 kW
House load                  ~1.5 kW
**Resulting grid export**   **~1.3 kW**
==========================  ==========

**Mode 2 at 0 kW is a total hold.** It suppresses battery *charging* as well as
discharging, so the 1.3 kW of free production went to the meter instead of the pack.
It does not merely withhold commanded grid charging.

Two consequences, and they point in opposite directions:

* for a row whose **objective is already met**, this is exactly right -- it is the
  only command on this surface that cannot overshoot a frozen objective, and
  ``test_a_satisfied_row_is_held_by_a_total_stop`` pins it;
* for an **unfinished** row it would be indefensible, and chasing why the controller
  ever wanted 0 kW there found the actual defect: the grid authorisation was being
  applied twice, once as a term added to the production surplus and again as a bare
  clamp on the battery. With the budget spent the second application pulled the
  battery to zero however much free production was available. ``decide_charge``'s own
  ``desired_grid_kw`` said **-1.240** for that state -- the code predicted the
  measured export before anybody measured it. See ``decide_charge`` and
  ``test_beta36_charge_domains.py``.

**The tick is driven, not simulated.** ``step_once`` drives refreshes only, and the
completion path fires on the sixty-second cadence -- which is why beta.35's
lifecycle tests, all built on ``step_once``, could not see any of this.
``_async_physical_tick`` is the production entry point from
``_handle_safety_sample``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.alpha_ems_manager import economic
from custom_components.alpha_ems_manager.const import EXECUTION_INTENT_GRID_CHARGE
from custom_components.alpha_ems_manager.execution import (
    AdmittedPlan,
    QuarterRow,
    admit,
    admit_plan,
    parse_target,
)

from .conftest import BATTERY_POWER, GRID_POWER, HOUSE_LOAD, PV_POWER, set_sensor
from .forecast_helpers import NORMAL, local

QUARTER = timedelta(minutes=15)
TICK = timedelta(minutes=1)

#: Where the replay puts the campaign. The shape is the trace; the wall clock is
#: the harness's, for the reason ``beta35_trace`` states: ``owned_live_charge``
#: establishes a *proven* claim anchored at its own quarter, and a claim re-stamped
#: onto a different hour stops matching the dispatch it names -- so the replay would
#: measure a refusal instead of a continuation.
REPLAY_START = (10, 45)

#: The two plans, as ``(plan_id, first row, row count)``. **Both are published on
#: every refresh**, which is what production does: ``_execution_targets`` returns a
#: tuple, one entry per run, and a campaign split by a ``serve_load`` interval is
#: exactly why that is plural.
PLAN_A = ("b36-plan-a", 0, 5)
PLAN_B = ("b36-plan-b", 6, 2)

#: The ``serve_load`` interval **inside** plan A: published, never armable.
GAP_ROW = 3

#: The interval between the two runs, which belongs to no plan at all.
RUN_GAP_ROW = 5

#: The rows any plan admits, in order. Row 5 is absent by construction.
EXECUTABLE_ROWS: tuple[int, ...] = (0, 1, 2, 4, 6, 7)

#: Every row's battery objective in kWh, both gaps included as ``0.0``.
ROW_BATTERY_KWH: tuple[float, ...] = (0.56, 0.28, 0.56, 0.0, 0.56, 0.0, 0.56, 0.56)

#: The import ceiling each row carries. Generous on purpose: this fixture is about
#: the lifecycle, and a ceiling that bound would introduce a second explanation for
#: every shortfall. ``S10`` collapses it deliberately, and only there.
ROW_GRID_KWH = 2.0

#: The measured charging site, in watts. Coherent by construction --
#: ``pv == load + battery + export`` with no export -- so ``_update_coherence``
#: accepts it and the accrual is never skipped as incoherent.
SITE_PV_W = 0.0
SITE_LOAD_W = 2000.0
SITE_BATTERY_W = -1400.0
SITE_GRID_W = 3400.0

#: What one sixty-second tick therefore accrues, in kWh at the battery.
TICK_BATTERY_KWH = 1.4 * 60.0 / 3600.0

#: The campaign identity, derived rather than asserted -- see :func:`campaign_id`.
CAMPAIGN_DIRECTION = "charge"


def opens_at(index: int) -> datetime:
    """Return the local instant row ``index`` opens at."""
    return local(NORMAL, *REPLAY_START) + index * QUARTER


def campaign_end() -> datetime:
    """Return the end of the last row: the campaign's own frozen scope.

    ``campaign_identity`` is a digest of *this instant*, which is what makes the id
    byte-identical across every republication of one live campaign -- and what makes
    it safe for beta.36 to freeze the planned end at campaign open.
    """
    return opens_at(len(ROW_BATTERY_KWH))


def campaign_id() -> str:
    """Return the campaign identity, from production.

    **Never a hand-written string.** A fixture that invented an id would prove that
    the coordinator copies a field, not that it agrees with the optimiser about what
    a campaign *is* -- and the whole 2026-08-30 failure turns on that agreement.
    """
    return economic.campaign_identity(
        CAMPAIGN_DIRECTION, dt_util.as_utc(campaign_end())
    )


CAMPAIGN_ID = campaign_id()


def step_clock(index: int, *, minutes: int = 0) -> dict[str, int]:
    """Return ``hour``/``minute`` arguments for ``step_once`` inside row ``index``."""
    moment = opens_at(index) + timedelta(minutes=minutes)
    return {"hour": moment.hour, "minute": moment.minute}


def rows(first: int, count: int) -> tuple[QuarterRow, ...]:
    """Return ``count`` frozen rows from ``first``, as ``quarter_schedule_for`` does.

    The gap row is emitted with ``not_executable`` set, exactly as Stage A publishes
    a ``serve_load`` interval: visible in the schedule, never armable. A fixture that
    simply omitted it would test a shorter plan, not a plan with a hole in it.
    """
    built: list[QuarterRow] = []
    for index in range(first, first + count):
        battery = ROW_BATTERY_KWH[index]
        built.append(
            QuarterRow(
                start=opens_at(index),
                end=opens_at(index) + QUARTER,
                battery_kwh=battery,
                grid_authorised_kwh=0.0 if index == GAP_ROW else ROW_GRID_KWH,
                grid_export_target_kwh=0.0,
                grid_export_caused_kwh=0.0,
                desired_grid_kw=0.0 if index == GAP_ROW else battery / 0.25,
                not_executable="serve_load" if index == GAP_ROW else None,
            )
        )
    return tuple(built)


def published_target(
    which: tuple[str, int, int] = PLAN_A,
    *,
    issued_at: datetime | None = None,
    **overrides: Any,
) -> dict:
    """Return the publication one plan is admitted from, whole.

    **The whole payload, not the fields a test happens to read.** ``admit_plan``
    keeps the publication it admitted, ``_write_execution_record`` persists it, and
    ``carried_from_record`` rebuilds a run from it after a restart -- so a fixture
    whose target omits a field silently disables adoption, which is the path the
    2026-08-29 boundary failure ran through and the one beta.35 had to learn twice.
    """
    plan_id, first, count = which
    schedule = rows(first, count)
    opens = opens_at(first)
    closes = opens_at(first + count)
    objective = sum(row.battery_kwh for row in schedule if row.executable)
    payload = {
        "plan_id": plan_id,
        "revision": 1,
        "intent": EXECUTION_INTENT_GRID_CHARGE,
        "purpose": "charge",
        "window_start": opens.isoformat(),
        "window_end": closes.isoformat(),
        "issued_at": (opens - QUARTER if issued_at is None else issued_at).isoformat(),
        "stale_after": (closes + QUARTER).isoformat(),
        "battery_target_kwh": objective,
        "grid_target_kwh": None,
        "average_power_kw": 2.24,
        "first_power_kw": 2.24,
        "reserve_floor_kwh": 4.32,
        "campaign_id": CAMPAIGN_ID,
        # **The field beta.36 gives its first consumer.** Declared in beta.32,
        # published ever since, and read by nothing -- so the campaign's scope was
        # only ever the high-water mark of rows *observed*, which is why the
        # 2026-08-30 terminal reported a window that ended at the row it died on.
        "campaign_end": campaign_end().isoformat(),
        "quarter_schedule": [row.as_dict() for row in schedule],
    }
    payload.update(overrides)
    return payload


def admitted_plan(
    which: tuple[str, int, int] = PLAN_A, *, run_id: str | None = None
) -> AdmittedPlan:
    """Return one frozen schedule, built by production from a real publication.

    Not assembled field by field: the identity rule a boundary sustain depends on --
    the plan adopts the run's ``run_id`` -- lives in ``admit_plan``, and a fixture
    that restated it by hand could agree with a version of production that no longer
    exists.
    """
    target = parse_target(published_target(which))
    assert target is not None
    admitted_at = opens_at(which[1]) - QUARTER
    run = None
    if run_id is not None:
        run = replace(admit(target, admitted_at), run_id=run_id)
    plan = admit_plan(target, run=run, now=admitted_at)
    assert plan is not None
    assert plan.campaign_id == CAMPAIGN_ID
    assert plan.campaign_end == campaign_end()
    return plan


def publishing(coordinator, monkeypatch) -> None:
    """Publish the plan that covers ``issued_at``, freshly issued, every refresh.

    **The publication has to roll with the clock.** ``carry_plan`` refuses a
    candidate whose first row opened more than ``PLAN_ADMISSION_LOOKBACK_SECONDS``
    ago, so a static payload stops being admissible partway through the replay and
    the fixture would then be measuring a lookback refusal. The hook is installed on
    ``_execution_targets``, which already receives ``issued_at=now``.

    Both plans carry the same ``campaign_id`` and the same ``campaign_end``, which is
    what makes this one campaign spanning two runs rather than two campaigns -- the
    shape that turns fifteen minutes of loss into five and a half hours. Both are
    emitted every refresh, so ``still_planned`` in ``_note_campaign_progress`` can see
    the continuation while the run gap at row 5 has nothing to execute.
    """

    def targets(self, *, issued_at: datetime, **_kwargs):
        return (
            published_target(PLAN_A, issued_at=issued_at),
            published_target(PLAN_B, issued_at=issued_at),
        )

    monkeypatch.setattr(type(coordinator), "_execution_targets", targets)


def charging_site(
    hass: HomeAssistant,
    *,
    battery_w: float = SITE_BATTERY_W,
    pv_w: float = SITE_PV_W,
    house_w: float = SITE_LOAD_W,
) -> None:
    """Point the live flows at a coherent site importing to charge the battery.

    Coherent in the balance layer's own terms -- ``pv + import == load + charge`` --
    because an incoherent snapshot makes the tick skip accrual entirely, and a
    fixture whose energy never moves proves nothing about a campaign that ends when
    energy stops moving.
    """
    charge_w = abs(battery_w)
    grid_w = house_w + charge_w - pv_w
    set_sensor(hass, PV_POWER, pv_w, "W", "power")
    set_sensor(hass, HOUSE_LOAD, house_w, "W", "power")
    set_sensor(hass, BATTERY_POWER, battery_w, "W", "power")
    set_sensor(hass, GRID_POWER, grid_w, "W", "power")


async def tick_at(hass, coordinator, live_surface, moment: datetime) -> None:
    """Drive one production sixty-second tick at ``moment``."""
    live_surface.at(moment)
    await coordinator._async_physical_tick(moment)
    await hass.async_block_till_done()


async def drive_quarter(
    hass,
    coordinator,
    live_surface,
    index: int,
    *,
    ticks: int = 14,
    first_tick: int = 1,
) -> list[str | None]:
    """Tick through row ``index`` and return the reason each tick recorded.

    Fourteen ticks by default rather than fifteen: the row's own boundary belongs to
    the next row, and a tick placed on it would be measuring the handover instead of
    the row.
    """
    reasons: list[str | None] = []
    for minute in range(first_tick, first_tick + ticks):
        await tick_at(hass, coordinator, live_surface, opens_at(index) + minute * TICK)
        reasons.append(coordinator._last_tick_reason)
    return reasons
