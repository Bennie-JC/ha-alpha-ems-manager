"""The 2026-09-01 hardware Sell, and its Buy mirror, as one replayable fixture.

**One refresh, and it is the refresh a row opens on.**

On 2026-09-01 at 20:30:05 a frozen two-row ``net_export`` run -- ``plan_id``
``6b16ce3f451b7aa0``, ``run_id`` ``d1c6f2e778b0469f``, window 20:30-21:00, rows of
2.28 and 2.25 kWh at the meter against a 4.53 kWh campaign objective -- was recorded
as **ended**::

    stop_reason        stage_a_hold
    withdrawal_basis   no_affirming_net_export_publication
    battery_realized   0.173 kWh
    remaining_battery  4.827 kWh

The 20:30 Stage-A solve had moved the export to tomorrow evening, so no publication
overlapped the accepted window and ``affirms`` was false for every one of them. The
*same refresh* then armed 9.7 kW. A terminal was filed against a run that was about
to start.

Why it reached that state, in two independent halves
----------------------------------------------------

**``carry_forward`` withdrew by absence and could not see that the row was open.**
Its terminal ``return`` is unguarded -- the default when every earlier clause falls
through -- and its parameters carry no plan, no quarter and no progress. Stage A's
horizon head is ``elapsed + 1``, so a publication issued once a row has opened
*structurally cannot describe it*; requiring an affirmation is requiring the
impossible, and for the **final row of every run** it is impossible by construction.
Withdrawal-by-absence was therefore the ordinary state of every run's last quarter.

**The suppression that existed could not prove itself on that refresh.**
``_plan_authority_holds`` required ``recorded == authority`` -- a persisted arm
claim -- and the claim is written *by* an arm, at the write boundary, after the stop
is decided in the same refresh. The download says so exactly: ``record_present:
false``, ``record_matches: false``, ``plan_authority_holds: false``. A reset was
avoided only because ``ownership_of`` answers ``none`` while the dispatch is still
inactive; with the marker already on, the campaign would have been torn down.

What this fixture reproduces
----------------------------

The shape that matters is **admitted a refresh early, then opened**, because that is
the only way to reach a real ``CarriedRun`` with no claim behind it yet:

======  =====================  ==================================================
step    refresh                what production does
======  =====================  ==================================================
1       one quarter early      Stage A publishes the run; ``carry_forward``
                               admits it and ``carry_plan_verbose`` freezes the
                               schedule. Nothing physical. No claim.
2       the row opens          Stage A publishes **only a future run of the same
                               intent**. Nothing affirms the open row. beta.37
                               ended it here.
======  =====================  ==================================================

Both economic directions are built from one shape, because the defect is in the
lifecycle and the lifecycle is shared -- but the *objectives* are not, and the tests
must respect that:

* ``net_export`` -- the campaign objective is the **meter** export; battery
  discharge is a ceiling.
* ``grid_charge`` -- the campaign objective is **battery charge** energy; grid
  import authorisation is a ceiling, and free production still pays toward the
  objective.

The future run published in step 2 is deliberately of the *same intent*: "Stage A
still wants to do this later" must not be mistaken for "Stage A has replaced what is
running now".
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from custom_components.alpha_ems_manager.const import (
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_NET_EXPORT,
)
from custom_components.alpha_ems_manager.execution import QuarterRow

from .forecast_helpers import NORMAL, local

QUARTER = timedelta(minutes=15)

#: Where the replay's first frozen row opens, in local wall-clock terms. The live
#: incident was at 20:30; the suite's fixtures are seeded around the middle of the
#: day, so the replay is re-based and every instant derives from here.
REPLAY_START = (10, 45)

# --- the Sell, from the capture -------------------------------------------
SELL_PLAN_ID = "b38-sell-plan"
SELL_CAMPAIGN_ID = "b38-sell-campaign"
#: ``(battery ceiling kWh, meter objective kWh)`` per row, as published.
SELL_ROWS: tuple[tuple[float, float], ...] = ((2.5, 2.28), (2.5, 2.25))
SELL_BATTERY_TARGET_KWH = 5.0
#: The frozen campaign objective the terminal is judged against: 2.28 + 2.25.
SELL_METER_TARGET_KWH = 4.53

# --- the Buy mirror --------------------------------------------------------
BUY_PLAN_ID = "b38-buy-plan"
BUY_CAMPAIGN_ID = "b38-buy-campaign"
#: ``(battery objective kWh, grid authorisation kWh)`` per row. The authorisation is
#: deliberately **smaller** than the objective: production is expected to pay the
#: difference, and a charge that treated the ceiling as the target would stop short.
BUY_ROWS: tuple[tuple[float, float], ...] = ((0.56, 0.30), (0.56, 0.30))
BUY_BATTERY_TARGET_KWH = 1.12


def opens_at(index: int = 0):
    """Return the local instant row ``index`` opens at. Negative indices are before."""
    return local(NORMAL, *REPLAY_START) + index * QUARTER


def step_clock(index: int) -> dict[str, int]:
    """Return ``hour``/``minute`` keywords for the refresh that opens row ``index``."""
    moment = opens_at(index)
    return {"hour": moment.hour, "minute": moment.minute}


def rows_for(intent: str) -> tuple[QuarterRow, ...]:
    """Return the frozen rows for one intent, as ``quarter_schedule_for`` emits them.

    The asymmetry is the point. An export row carries its objective in
    ``grid_export_target_kwh`` and its ceiling in ``battery_kwh``; a charge row
    carries its objective in ``battery_kwh`` and its ceiling in
    ``grid_authorised_kwh``. Building both from one helper keeps the two shapes
    beside each other where the difference is legible.
    """
    if intent == EXECUTION_INTENT_NET_EXPORT:
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
            for index, (battery, meter) in enumerate(SELL_ROWS)
        )
    return tuple(
        QuarterRow(
            start=opens_at(index),
            end=opens_at(index) + QUARTER,
            battery_kwh=battery,
            grid_authorised_kwh=authorised,
            grid_export_target_kwh=0.0,
            grid_export_caused_kwh=0.0,
            desired_grid_kw=authorised / 0.25,
        )
        for index, (battery, authorised) in enumerate(BUY_ROWS)
    )


def target_for(intent: str, **overrides: Any) -> dict[str, Any]:
    """Return the publication the run is admitted from, whole.

    **The whole payload, not the fields a test happens to read.** ``admit_plan``
    keeps the publication it admitted, ``_write_execution_record`` persists it, and
    ``carried_from_record`` rebuilds a run from it after a restart -- so a fixture
    with a hollow target silently disables adoption, which is one of the paths under
    test here.
    """
    sell = intent == EXECUTION_INTENT_NET_EXPORT
    opens = opens_at(0)
    rows = rows_for(intent)
    payload: dict[str, Any] = {
        "plan_id": SELL_PLAN_ID if sell else BUY_PLAN_ID,
        "revision": 1,
        "intent": intent,
        "purpose": "export" if sell else "charge",
        "window_start": opens.isoformat(),
        "window_end": (opens + len(rows) * QUARTER).isoformat(),
        # Issued one interval before it opens, which is what Stage A does: the
        # horizon head is ``elapsed + 1``.
        "issued_at": (opens - QUARTER).isoformat(),
        "stale_after": (opens + timedelta(minutes=30)).isoformat(),
        "battery_target_kwh": (
            SELL_BATTERY_TARGET_KWH if sell else BUY_BATTERY_TARGET_KWH
        ),
        "grid_target_kwh": SELL_METER_TARGET_KWH if sell else None,
        "average_power_kw": 9.0 if sell else 2.24,
        "first_power_kw": 10.0 if sell else 2.24,
        "reserve_floor_kwh": 4.32,
        "campaign_id": SELL_CAMPAIGN_ID if sell else BUY_CAMPAIGN_ID,
        "quarter_schedule": [row.as_dict() for row in rows],
    }
    payload.update(overrides)
    return payload


def moved_elsewhere(intent: str) -> dict[str, Any]:
    """Return a publication of the **same intent** that starts well after the window.

    **The case the release turns on, and the one a looser test would miss.** Stage A
    has not abandoned the idea -- it still wants to charge, or still wants to sell --
    it has simply moved the work. ``affirms`` is purely temporal, so a run beginning
    four quarters past the accepted window overlaps nothing and affirms nothing, and
    beta.37 read that as a withdrawal of the row already executing.

    "Stage A wants this later" and "Stage A has replaced what is running" are
    different statements, and only the second could ever end a run.
    """
    start = opens_at(len(rows_for(intent)) + 4)
    sell = intent == EXECUTION_INTENT_NET_EXPORT
    return {
        "plan_id": "b38-moved-elsewhere",
        "revision": 1,
        "intent": intent,
        "purpose": "export" if sell else "charge",
        "window_start": start.isoformat(),
        "window_end": (start + 2 * QUARTER).isoformat(),
        "issued_at": start.isoformat(),
        "stale_after": (start + timedelta(minutes=30)).isoformat(),
        "battery_target_kwh": 4.0,
        "grid_target_kwh": 3.6 if sell else None,
        "average_power_kw": 8.0,
        "first_power_kw": 8.0,
        "reserve_floor_kwh": 4.32,
        "campaign_id": "b38-a-different-campaign",
        "quarter_schedule": [],
    }


def shrunk_but_overlapping(intent: str) -> dict[str, Any]:
    """Return an **affirming** publication that wants much less than was frozen.

    Same intent, and a window that starts inside the accepted one, so ``affirms``
    is true and the run is re-affirmed rather than withdrawn. What it carries is a
    quarter of the energy.

    **The accepted figures must not move.** Progress and the per-row grid ceiling
    are both measured against them, so adopting the fresh number would rebase both
    -- a run that had already delivered 2.28 kWh against a 4.53 kWh objective would
    suddenly be judged against 1.13 and read as finished. A change large enough to
    abandon a run is a supersession, decided by direction, not by magnitude.
    """
    payload = target_for(intent)
    sell = intent == EXECUTION_INTENT_NET_EXPORT
    payload.update(
        {
            "plan_id": "b38-shrunk",
            "revision": 2,
            "window_start": opens_at(1).isoformat(),
            "battery_target_kwh": 0.25,
            "grid_target_kwh": 0.2 if sell else None,
            "average_power_kw": 1.0,
            "first_power_kw": 1.0,
        }
    )
    return payload


def publish(coordinator, monkeypatch, targets: tuple[dict[str, Any], ...]) -> None:
    """Make Stage A publish exactly ``targets`` from the next refresh onward."""
    monkeypatch.setattr(
        type(coordinator),
        "_execution_targets",
        lambda self, **kwargs: tuple(targets),
    )


def publish_nothing(coordinator, monkeypatch) -> None:
    """Publish nothing at all -- the strongest form of the same condition."""
    publish(coordinator, monkeypatch, ())


def carried_of(report: dict[str, Any]) -> dict[str, Any]:
    """Return the ``carried`` block of a control report."""
    return ((report.get("execution") or {}).get("carried")) or {}


def boundary_of(report: dict[str, Any]) -> dict[str, Any]:
    """Return the ``write_boundary`` block of a control report."""
    return ((report.get("execution") or {}).get("write_boundary")) or {}


def authority_of(report: dict[str, Any]) -> dict[str, Any]:
    """Return the write boundary's ``authority`` block."""
    return boundary_of(report).get("authority") or {}


def campaign_of(report: dict[str, Any]) -> dict[str, Any]:
    """Return the ``open_campaign`` block of a control report."""
    return ((report.get("execution") or {}).get("open_campaign")) or {}


def lifecycle_of(report: dict[str, Any]) -> dict[str, Any]:
    """Return the ``lifecycle`` block of a control report."""
    return ((report.get("execution") or {}).get("lifecycle")) or {}


BOTH_INTENTS = (EXECUTION_INTENT_NET_EXPORT, EXECUTION_INTENT_GRID_CHARGE)
