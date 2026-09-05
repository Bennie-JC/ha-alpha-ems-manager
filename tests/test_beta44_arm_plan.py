"""beta.44: one economic campaign can ask for several physical arms.

The DP prices a campaign as one uninterrupted run and charges one fee for it. Stage B
may arm, stop and re-arm several times inside that same campaign: a ``serve_load`` gap
between two exports, or a PV-only ``hold`` inside a charge, each force a stop and a
fresh marker claim. On the 2026-09-05 horizon that was **eleven arms against two
direction changes** — and no published figure said so.

This file pins the counting. It decides nothing: every figure is derived from targets
already published and a trajectory already final, and the release's hard gate is that
no planner digest moves.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.alpha_ems_manager.const import (
    CAMPAIGN_BOUNDARY_BATTERY,
    CAMPAIGN_BOUNDARY_METER,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_NET_EXPORT,
    EXECUTION_INTENT_SERVE_LOAD,
    MAX_ARM_PLAN_ENTRIES_PUBLISHED,
    QUARTER_NOT_EXECUTABLE_BELOW_CONTROLLABLE,
    QUARTER_NOT_EXECUTABLE_INTENT,
    REFUSED_RUN_VALUE_BASIS,
)
from custom_components.alpha_ems_manager.coordinator import AlphaEmsCoordinator

START = datetime(2026, 9, 5, 20, 30, tzinfo=UTC)


def _row(offset: int, *, kwh: float = 0.5, refusal: str | None = None) -> dict:
    start = START + timedelta(minutes=15 * offset)
    return {
        "start": start.isoformat(),
        "end": (start + timedelta(minutes=15)).isoformat(),
        "battery_kwh": kwh,
        "grid_export_target_kwh": kwh,
        "not_executable": refusal,
    }


def _target(
    plan_id: str,
    rows: list[dict],
    *,
    intent: str = EXECUTION_INTENT_NET_EXPORT,
    campaign: str = "c1",
) -> dict:
    return {
        "plan_id": plan_id,
        "campaign_id": campaign,
        "intent": intent,
        "purpose": "export" if intent == EXECUTION_INTENT_NET_EXPORT else "charge",
        "quarter_schedule": rows,
    }


def _run(start_index: int, count: int) -> SimpleNamespace:
    return SimpleNamespace(start_index=start_index, end_index=start_index + count - 1)


def _interval(index: int, marginal: float) -> SimpleNamespace:
    return SimpleNamespace(index=index, marginal_cost_eur=marginal)


def _outcome(intervals, *, campaigns=(), runs=(), direction_changes=0):
    desired = SimpleNamespace(
        intervals=intervals,
        campaigns=campaigns,
        runs=runs,
        direction_changes=direction_changes,
    )
    return SimpleNamespace(desired=desired)


def _holder() -> AlphaEmsCoordinator:
    """Return a coordinator whose ``__init__`` never ran.

    These helpers read their arguments and nothing else -- no hass, no store, no
    config -- so binding them to a bare instance exercises the real methods without
    standing up an integration. A stub with the same shape would be a double that
    drifts.
    """
    return object.__new__(AlphaEmsCoordinator)


def _plan(targets, runs_by_plan, outcome):
    return _holder()._arm_plan_block(outcome, targets, runs_by_plan)


def _stretches(rows: list[dict]) -> list[tuple[int, int]]:
    return _holder()._armable_stretches({"quarter_schedule": rows})


# ===========================================================================
# 1. the arm boundary
# ===========================================================================


def test_a_continuation_of_executable_rows_is_one_arm() -> None:
    """An ordinary quarter boundary claims no marker and mints no run id.

    The slot advances, the sustain path re-arms the dead-man, and the dispatch never
    stops. Counting these would make ``arm_count`` a row count.
    """
    assert _stretches([_row(0), _row(1), _row(2)]) == [(0, 2)]


def test_a_non_executable_gap_creates_a_second_arm() -> None:
    """**The anchor.** A gap forces a stop, and the next row claims again.

    ``executing_quarter`` returns ``None`` for a non-executable row, the tick reads
    that as ``stop``, and the row-scope teardown clears the carried run — so the
    next executable row mints a fresh ``run_id`` and runs the full two-stage arm.

    *Mutation: treat a non-executable row as continuation and this collapses to one.*
    """
    rows = [_row(0), _row(1, refusal=QUARTER_NOT_EXECUTABLE_INTENT), _row(2)]
    assert _stretches(rows) == [(0, 0), (2, 2)]


def test_a_leading_and_trailing_gap_do_not_invent_arms() -> None:
    """Only stretches that contain something armable are arms."""
    rows = [
        _row(0, refusal=QUARTER_NOT_EXECUTABLE_INTENT),
        _row(1),
        _row(2, refusal=QUARTER_NOT_EXECUTABLE_BELOW_CONTROLLABLE),
    ]
    assert _stretches(rows) == [(1, 1)]


def test_a_wholly_unarmable_target_has_no_arm() -> None:
    """This is the refused run, and it is counted as refused rather than as an arm."""
    rows = [
        _row(i, refusal=QUARTER_NOT_EXECUTABLE_BELOW_CONTROLLABLE) for i in range(3)
    ]
    assert _stretches(rows) == []


# ===========================================================================
# 2. the plan block
# ===========================================================================


def test_a_serve_load_gap_inside_one_campaign_costs_two_arms() -> None:
    """The live export shape: two exports, one gap, one campaign.

    ``serve_load`` is published as its own target so the campaign identity survives
    the gap, and Stage B can never arm it. It is neither an arm nor a refusal.
    """
    targets = [
        _target("p1", [_row(0)]),
        _target(
            "gap",
            [_row(1, refusal=QUARTER_NOT_EXECUTABLE_INTENT)],
            intent=EXECUTION_INTENT_SERVE_LOAD,
        ),
        _target("p2", [_row(2)]),
    ]
    runs = {"p1": _run(0, 1), "gap": _run(1, 1), "p2": _run(2, 1)}
    intervals = [_interval(i, -0.10) for i in range(3)]

    plan = _plan(targets, runs, _outcome(intervals, direction_changes=1))

    assert plan["arm_count"] == 2
    assert plan["runs_published"] == 2, "the gap is not a run Stage B could arm"
    assert plan["runs_refused_nothing_armable"] == 0
    assert plan["direction_changes"] == 1, (
        "one campaign, one fee, two arms -- the gap this release measures"
    )


def test_a_hold_gap_inside_a_charge_campaign_costs_two_arms() -> None:
    """The charge side has the identical defect, made by PV rather than by load.

    ``runs_from`` flushes on ``HOLD``, so a PV-only quarter inside a charge campaign
    mints a second run, a second target and a second arm — and ``run_start`` stays
    false for it, so the trade hurdle is not charged either.
    """
    targets = [
        _target("c1", [_row(0)], intent=EXECUTION_INTENT_GRID_CHARGE),
        _target(
            "hold",
            [_row(1, refusal=QUARTER_NOT_EXECUTABLE_INTENT)],
            intent=EXECUTION_INTENT_SERVE_LOAD,
        ),
        _target("c2", [_row(2)], intent=EXECUTION_INTENT_GRID_CHARGE),
    ]
    runs = {"c1": _run(0, 1), "hold": _run(1, 1), "c2": _run(2, 1)}
    plan = _plan(targets, runs, _outcome([_interval(i, -0.05) for i in range(3)]))

    assert plan["arm_count"] == 2
    assert plan["arms"][0]["objective_boundary"] == CAMPAIGN_BOUNDARY_BATTERY


def test_arm_count_is_independent_of_direction_changes() -> None:
    """They answer different questions and must not be derivable from each other."""
    targets = [
        _target(
            "p1", [_row(0), _row(1, refusal=QUARTER_NOT_EXECUTABLE_INTENT), _row(2)]
        )
    ]
    plan = _plan(
        targets,
        {"p1": _run(0, 3)},
        _outcome([_interval(i, -0.10) for i in range(3)], direction_changes=1),
    )

    assert plan["arm_count"] == 2
    assert plan["direction_changes"] == 1
    assert plan["arm_count"] != plan["direction_changes"]


def test_a_refused_run_reports_its_energy_and_its_advantage() -> None:
    """A run Stage B can never arm, and what the plan thought it was worth.

    *Mutation: count the refused run as an arm, or price it on ``cost_eur`` instead
    of the marginal advantage, and this fails.*
    """
    rows = [
        _row(0, kwh=0.04, refusal=QUARTER_NOT_EXECUTABLE_BELOW_CONTROLLABLE),
        _row(1, kwh=0.05, refusal=QUARTER_NOT_EXECUTABLE_BELOW_CONTROLLABLE),
    ]
    plan = _plan(
        [_target("tiny", rows)],
        {"tiny": _run(0, 2)},
        _outcome([_interval(0, -0.03), _interval(1, -0.05)]),
    )

    assert plan["arm_count"] == 0
    assert plan["runs_refused_nothing_armable"] == 1
    assert plan["energy_planned_on_refused_runs_kwh"] == pytest.approx(0.09)
    assert plan["value_planned_on_refused_runs_eur"] == pytest.approx(0.08), (
        "the advantage over leaving the battery alone, negated"
    )
    assert plan["refused_run_value_basis"] == REFUSED_RUN_VALUE_BASIS


def test_the_refused_value_basis_excludes_ambient_energy() -> None:
    """**Why the marginal figure, and not the cash figure.**

    ``marginal_cost_eur`` is ``cost_eur - idle_cost_eur``. Ambient production and
    unavoidable household import sit inside the idle counterfactual on *both* sides
    of that difference, so neither can be reported as dispatch-caused value. A run
    whose whole flow would have happened anyway is worth zero here, and that is the
    property the basis token promises.
    """
    rows = [_row(0, refusal=QUARTER_NOT_EXECUTABLE_BELOW_CONTROLLABLE)]
    plan = _plan(
        [_target("ambient", rows)],
        {"ambient": _run(0, 1)},
        _outcome([_interval(0, 0.0)]),
    )

    assert plan["runs_refused_nothing_armable"] == 1
    assert plan["value_planned_on_refused_runs_eur"] == pytest.approx(0.0), (
        "a run that changes nothing against the idle walk is worth nothing"
    )


def test_each_published_arm_carries_its_own_objective_and_value() -> None:
    """Per-arm figures, at the boundary the arm is paid at."""
    targets = [
        _target(
            "p1",
            [
                _row(0, kwh=0.8),
                _row(1, refusal=QUARTER_NOT_EXECUTABLE_INTENT),
                _row(2, kwh=0.3),
            ],
        )
    ]
    plan = _plan(
        targets,
        {"p1": _run(10, 3)},
        _outcome([_interval(10, -0.4), _interval(11, 0.0), _interval(12, -0.1)]),
    )

    first, second = plan["arms"]
    assert first["arm_index"] == 1 and second["arm_index"] == 2
    assert first["objective_kwh"] == pytest.approx(0.8)
    assert second["objective_kwh"] == pytest.approx(0.3)
    assert first["marginal_value_eur"] == pytest.approx(0.4)
    assert second["marginal_value_eur"] == pytest.approx(0.1)
    assert first["objective_boundary"] == CAMPAIGN_BOUNDARY_METER
    assert first["starts_at"] == START.isoformat()


def test_the_published_arm_list_is_bounded() -> None:
    """An entity attribute, not a log."""
    rows: list[dict] = []
    for index in range(MAX_ARM_PLAN_ENTRIES_PUBLISHED * 2):
        rows.append(_row(index * 2))
        rows.append(_row(index * 2 + 1, refusal=QUARTER_NOT_EXECUTABLE_INTENT))
    plan = _plan(
        [_target("many", rows)],
        {"many": _run(0, len(rows))},
        _outcome([_interval(i, -0.01) for i in range(len(rows))]),
    )

    assert plan["arm_count"] == MAX_ARM_PLAN_ENTRIES_PUBLISHED * 2
    assert len(plan["arms"]) == MAX_ARM_PLAN_ENTRIES_PUBLISHED
    assert plan["arms_truncated"] == MAX_ARM_PLAN_ENTRIES_PUBLISHED
