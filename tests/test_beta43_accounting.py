"""beta.43: a row's measurement has to survive its own boundary.

**The defect this file exists for is one statement of ordering.** On the physical
tick ``_async_correct_setpoint`` advanced the executing slot to the successor row and
then let ``_accrue_quarter_progress`` rebase the accumulators onto it -- zeroing the
totals of the row that had just ended -- *before* ``_async_end_row`` was reached. The
capture/restore pair inside that method was written to protect those totals across
the physical stop, which happens two statements later than the loss.

So the recorded outcome of a row depended on something with nothing to do with what
the plant did: a row with a successor recorded ``0.0``, and a row that ended with
nothing after it recorded the truth, because that path returns early from
``_accrue_quarter_progress`` and never resets.

Measured on the reference installation, 2026-09-05. The 20:15-20:30 export row filed
``realized_grid_kwh: 0.0`` with a 100 % shortfall, while the physical decision ring
held ``grid_realized_kwh: 0.494`` for that same row twenty-three seconds before it
ended. All nine completed rows of that capture split exactly along "has a successor",
across two campaigns and both directions.

Every test names a **published witness** before asserting the fix, so none can pass on
a branch it never reached.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.const import (
    CAMPAIGN_BOUNDARY_BATTERY,
    EXECUTION_STOP_CAMPAIGN_COMPLETE,
    QUARTER_END_TARGET_REACHED,
    SHORTFALL_TARGET_REACHED,
    STOP_SCOPE_CAMPAIGN,
)

from .beta36_trace import (
    EXECUTABLE_ROWS,
    ROW_BATTERY_KWH,
    drive_quarter,
    opens_at,
    tick_at,
)
from .test_beta36_lifecycle import (  # noqa: F401
    live_surface,
    start_the_charge_campaign,
)

pytestmark = pytest.mark.usefixtures("control_surface")


def _recorded(coordinator, index: int) -> dict:
    """Return the completed-row record for row ``index``, or an empty mapping."""
    wanted = opens_at(index).isoformat()
    for row in coordinator._completed_quarters:
        if row.get("quarter_start") == wanted:
            return row
    return {}


async def test_a_row_with_a_successor_keeps_its_own_measurement(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface,  # noqa: F811
    monkeypatch,
) -> None:
    """**The anchor.** Row 0 is followed by row 1, and still records what it moved.

    This is the shape that failed live: an ordinary mid-campaign row, ended by the
    physical tick that opens its successor. Nothing about it is exceptional, which is
    why every mid-campaign row of the 2026-09-05 capture recorded zero.

    *Mutations: move the capture back below ``_refresh_executing_quarter``, or drop
    the ``measured`` argument on the way into ``_async_end_row``, and this fails.*
    """
    coordinator = await start_the_charge_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    await drive_quarter(hass, coordinator, live_surface, 0, ticks=14)

    delivered = coordinator._quarter_battery_kwh
    assert delivered > 0.0, "the witness: row 0 moved energy before its boundary"

    # The tick that crosses into row 1. This is the statement that used to lose it.
    await tick_at(hass, coordinator, live_surface, opens_at(1))

    assert coordinator._quarter is not None, "the witness: a successor row is open"
    assert coordinator._quarter.quarter_start == opens_at(1)

    record = _recorded(coordinator, 0)
    assert record, "row 0 was recorded"
    assert record["realized_battery_kwh"] == pytest.approx(delivered, abs=1e-3), (
        "and it recorded what it moved, not what its successor had moved"
    )
    assert record["objective_battery_kwh"] > 0.0
    assert record["shortfall_percent"] != 100.0


async def test_the_campaign_total_is_the_sum_of_its_rows(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface,  # noqa: F811
    monkeypatch,
) -> None:
    """Three mid-campaign rows, and the accumulator holds all three.

    ``quarters_admitted`` counted accruals of ``0.0`` for every row the wipe reached,
    so the counter looked healthy while the total was empty -- which is how the live
    3.62 kWh campaign reported an empty result beside five recorded rows.

    **Row 1 is the second witness, and it is the aliasing one.** Its allowance is
    0.28 kWh against the 0.56 kWh of the rows either side, so a row measured against
    the *wrong* envelope reports a different number here: judged at its own it is
    capped at 0.28 while the pack really took more, and the remainder is absorbed
    production rather than progress against a promise the row never made.
    """
    coordinator = await start_the_charge_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    moved: list[float] = []
    for index in (0, 1, 2):
        await drive_quarter(hass, coordinator, live_surface, index, ticks=14)
        moved.append(coordinator._quarter_battery_kwh)
        await tick_at(hass, coordinator, live_surface, opens_at(index + 1))

    assert all(value > 0.0 for value in moved), "the witness: every row moved energy"
    assert coordinator._campaign_quarters_admitted == 3
    recorded = [_recorded(coordinator, index) for index in (0, 1, 2)]
    assert all(row for row in recorded), "and every row is in the history"

    allowance = ROW_BATTERY_KWH[1]
    assert moved[1] > allowance, "the witness: row 1 took more than it promised"
    assert recorded[1]["objective_battery_kwh"] == pytest.approx(allowance, abs=1e-3), (
        "row 1 is judged at its own allowance, not a neighbour's"
    )
    assert recorded[1]["absorbed_extra_kwh"] == pytest.approx(
        moved[1] - allowance, abs=1e-3
    ), "and the remainder is absorbed production, which sums to the whole exactly"

    assert coordinator._campaign_realized_kwh == pytest.approx(
        sum(row["objective_battery_kwh"] for row in recorded), abs=3e-3
    ), "the campaign total and its own rows agree"


async def test_a_serve_load_gap_does_not_break_the_accumulation(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface,  # noqa: F811
    monkeypatch,
) -> None:
    """Row 3 is the gap. The row before it is still judged at its own allowance.

    A gap row is what beta.35 lost every row after, and its allowance is zero -- so
    it is also where the objective aliasing corrected here would have zeroed the row
    *before* it, by measuring that row against the gap's envelope.
    """
    coordinator = await start_the_charge_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    assert 3 not in EXECUTABLE_ROWS, "the witness: row 3 really is the gap"

    # **Captured per row, and the sum alone would not do.** The fixture's
    # allowances are 0.56, 0.28, 0.56, so judging each row against its *successor*
    # produces two equal and opposite errors that cancel exactly in the total. A
    # test that only added them up would pass against the defect it exists for.
    increments: list[float] = []
    for index in (0, 1, 2, 3):
        before = coordinator._campaign_realized_kwh
        await drive_quarter(hass, coordinator, live_surface, index, ticks=14)
        await tick_at(hass, coordinator, live_surface, opens_at(index + 1))
        increments.append(coordinator._campaign_realized_kwh - before)

    before_gap = _recorded(coordinator, 2)
    assert before_gap, "the row before the gap was recorded"
    assert before_gap["objective_battery_kwh"] > 0.0, (
        "and it is judged against its own allowance, not the gap's"
    )
    assert coordinator._campaign_id is not None, "one campaign, throughout"
    assert coordinator._campaign_quarters_admitted >= 3

    # **The accumulator is pinned too, not just the history.** They are advanced by
    # different helpers -- the record by ``_objective_kwh_for``, the accrual by
    # ``_row_objective_kwh`` -- and a test that checked only the record would say
    # nothing about the figure the campaign is actually judged on.
    for index in (0, 1, 2):
        recorded = _recorded(coordinator, index)["objective_battery_kwh"]
        assert increments[index] == pytest.approx(recorded, abs=3e-3), (
            f"row {index} accrued exactly what it recorded, at its own allowance"
        )
    assert increments[1] == pytest.approx(ROW_BATTERY_KWH[1], abs=3e-3), (
        "and row 1 is capped at its own 0.28 kWh, not at a neighbour's 0.56"
    )


async def test_the_ending_row_is_accrued_before_the_terminal_is_filed(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface,  # noqa: F811
    monkeypatch,
) -> None:
    """The close reads a campaign that still has an identity and a counted row.

    ``_close_campaign`` runs inside ``_async_stop_dispatch``. Until beta.43 that
    method nulled ``self._quarter`` and called ``_reset_quarter_progress(None)``
    first, which cleared ``_campaign_accrued_row`` -- the exactly-once latch -- and
    made the open-quarter term the terminal promises to include structurally zero.

    *Mutations: move the two reset lines back above the campaign branch, and either
    the latch assertion or the total fails.*
    """
    coordinator = await start_the_charge_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    await drive_quarter(hass, coordinator, live_surface, 0, ticks=6)
    row = coordinator._quarter
    assert row is not None
    delivered = coordinator._quarter_battery_kwh
    assert delivered > 0.0, "the witness: the closing row moved energy"

    await coordinator._async_end_quarter(
        opens_at(0) + timedelta(minutes=7),
        coordinator._pending_snapshot,
        QUARTER_END_TARGET_REACHED,
        SHORTFALL_TARGET_REACHED,
        stop_reason=EXECUTION_STOP_CAMPAIGN_COMPLETE,
        scope=STOP_SCOPE_CAMPAIGN,
    )
    terminal = coordinator._closed_campaign or {}
    assert terminal, "a started campaign files a terminal"
    assert terminal["objective_realized_kwh"] == pytest.approx(delivered, abs=1e-3)
    assert terminal["rows_completed"] == terminal["quarters_admitted"], (
        "the closing row is counted even though its record is written after this"
    )
    assert terminal["objective_boundary"] == CAMPAIGN_BOUNDARY_BATTERY


async def test_the_terminal_publishes_the_rows_it_was_summed_from(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface,  # noqa: F811
    monkeypatch,
) -> None:
    """A terminal that disagrees with its own rows is visible from one payload.

    The live capture needed the physical decision ring to catch it; the terminal
    itself published two counters that were the same local under two names, which
    read as corroboration and was not.
    """
    coordinator = await start_the_charge_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    for index in (0, 1):
        await drive_quarter(hass, coordinator, live_surface, index, ticks=14)
        await tick_at(hass, coordinator, live_surface, opens_at(index + 1))
    await drive_quarter(hass, coordinator, live_surface, 2, ticks=6)

    await coordinator._async_end_quarter(
        opens_at(2) + timedelta(minutes=7),
        coordinator._pending_snapshot,
        QUARTER_END_TARGET_REACHED,
        SHORTFALL_TARGET_REACHED,
        stop_reason=EXECUTION_STOP_CAMPAIGN_COMPLETE,
        scope=STOP_SCOPE_CAMPAIGN,
    )
    terminal = coordinator._closed_campaign or {}
    rows = terminal.get("objective_rows_realised")
    assert isinstance(rows, list) and rows, "the rows are published"
    assert all("quarter_start" in row for row in rows)
    banked = sum(float(row["objective_kwh"] or 0.0) for row in rows)
    assert banked > 0.0, "the witness: the published rows carry energy"
    assert terminal["objective_realized_kwh"] >= banked - 1e-3, (
        "the total is not less than the rows it was summed from"
    )


async def test_the_two_row_counters_are_genuinely_independent(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface,  # noqa: F811
    monkeypatch,
) -> None:
    """``rows_completed`` and ``quarters_admitted`` answer different questions.

    They agree in healthy operation, which is what made publishing one number under
    two names look like corroboration. The live capture is the shape where they must
    not: three completed rows carried the campaign id while only two accruals had
    happened, and both counters said 2.

    Reproduced here by recording a row for the campaign that the accumulator never
    saw -- exactly what the boundary wipe produced.

    *Mutation: publish the accrual count under both names and this fails.*
    """
    coordinator = await start_the_charge_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    await drive_quarter(hass, coordinator, live_surface, 0, ticks=14)
    await tick_at(hass, coordinator, live_surface, opens_at(1))
    await drive_quarter(hass, coordinator, live_surface, 1, ticks=6)

    accrued = coordinator._campaign_quarters_admitted
    assert accrued >= 1, "the witness: something has been accrued"

    # A row that reached the history and never reached the accumulator.
    orphan = dict(coordinator._completed_quarters[-1])
    orphan["quarter_start"] = opens_at(9).isoformat()
    coordinator._completed_quarters.append(orphan)

    await coordinator._async_end_quarter(
        opens_at(1) + timedelta(minutes=7),
        coordinator._pending_snapshot,
        QUARTER_END_TARGET_REACHED,
        SHORTFALL_TARGET_REACHED,
        stop_reason=EXECUTION_STOP_CAMPAIGN_COMPLETE,
        scope=STOP_SCOPE_CAMPAIGN,
    )
    terminal = coordinator._closed_campaign or {}
    assert terminal["rows_completed"] > terminal["quarters_admitted"], (
        "a row the accumulator never saw is visible in the counters, not hidden"
    )
