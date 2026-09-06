"""beta.48: how much energy the progress reset actually discards.

Code inspection says `_accrue_quarter_progress` throws away the whole sample
interval in which the ``(claim_id, quarter_start)`` key changes: the reset nulls
``_quarter_sampled_at``, and the very next line returns when ``previous is None``.
The cursor is not among the eight fields ``_capture_quarter_progress`` preserves,
and ``_async_end_row`` resets on every row handover.

That is a **mechanism**, not a magnitude. Inspection cannot say how many seconds any
real arm lost, and the difference matters: the rough live gap it might explain was
0.63 kWh against 6.10 kWh physically metered, over a window whose endpoints were
never aligned. So the arithmetic is pinned here, exactly, with fixed timestamps and a
constant export power, and the prose elsewhere follows these numbers rather than the
other way round.

**Nothing here changes the accumulator.** beta.48 measures; the repair, if the live
audit says one is warranted, is a separate decision with its own economics.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.alpha_ems_manager.const import (
    EXECUTION_INTENT_NET_EXPORT,
    MAX_METER_AUDITS_REPORTED,
)
from custom_components.alpha_ems_manager.coordinator import AlphaEmsCoordinator
from custom_components.alpha_ems_manager.execution import CarriedQuarter

#: A row boundary, and the tick phase that drifted away from it.
ROW_OPENS = datetime(2026, 9, 6, 19, 0, tzinfo=UTC)
TICK_OFFSET = timedelta(seconds=30)
TICK = timedelta(seconds=60)

#: Constant export, chosen so every figure below is exact.
EXPORT_KW = 8.0
EXPORT_W = EXPORT_KW * 1000.0


def kwh(seconds: float) -> float:
    """Return the energy a constant :data:`EXPORT_KW` moves in ``seconds``."""
    return EXPORT_KW * seconds / 3600.0


def quarter_at(start: datetime) -> CarriedQuarter:
    """Return an export row opening at ``start``."""
    return CarriedQuarter(
        quarter_start=start,
        quarter_end=start + timedelta(minutes=15),
        intent=EXECUTION_INTENT_NET_EXPORT,
        battery_target_kwh=2.26,
        grid_authorised_kwh=0.0,
        grid_export_target_kwh=2.26,
        initial_desired_grid_kw=EXPORT_KW,
        run_id="run-1",
        plan_id="plan-1",
        revision=1,
        admitted_at=start - timedelta(minutes=15),
    )


class _Rig:
    """A coordinator whose ``__init__`` never ran, measuring one export row."""

    def __init__(self) -> None:
        self.c = object.__new__(AlphaEmsCoordinator)
        self.c._quarter = None
        self.c._quarter_key = None
        self.c._quarter_claim = None
        self.c._quarter_sampled_at = None
        self.c._quarter_battery_kwh = 0.0
        self.c._quarter_grid_import_kwh = 0.0
        self.c._quarter_grid_export_kwh = 0.0
        self.c._quarter_peak_kw = 0.0
        self.c._quarter_power_sum = 0.0
        self.c._quarter_power_samples = 0
        self.c._quarter_pv_helped = False
        self.c._quarter_target_reached_at = None
        self.c._quarter_clamps = set()
        self.c._quarter_hold_failures = 0
        self.c._campaign_accrued_row = None
        self.c.store = SimpleNamespace(execution_record=None)
        self.c._quarter_counter_from = None
        self.c._quarter_counter_to = None
        self.c._quarter_counter_at_cursor = None
        self.c._meter_audits = deque(maxlen=MAX_METER_AUDITS_REPORTED)
        # No cumulative counter by default: the audit must work honestly without
        # one, and saying so is the point of `not_configured`.
        self.c.config = SimpleNamespace(
            grid_export_energy_entity=None, grid_power_entity="sensor.p1"
        )
        self.counter_kwh: float | None = None
        self.c.read_grid_export_counter_kwh = lambda: self.counter_kwh
        # Export is flowing from the instant the test says it is, and not before.
        self.exporting_from: datetime | None = None
        self.now: datetime = ROW_OPENS
        self.c.read_flows = self._flows
        self.c._budget_surplus_kw = lambda: 0.0

    def _flows(self):
        live = self.exporting_from is not None and self.now >= self.exporting_from
        return SimpleNamespace(
            grid_export_w=EXPORT_W if live else 0.0,
            grid_import_w=0.0,
            battery_discharge_w=EXPORT_W if live else 0.0,
            battery_charge_w=0.0,
        )

    def open_row(self, start: datetime) -> None:
        """Advance the executing row, as ``_refresh_executing_quarter`` does."""
        self.c._quarter = quarter_at(start)

    def claim(self, claim_id: str | None) -> None:
        """Write or clear the ownership claim."""
        self.c.store.execution_record = (
            None if claim_id is None else {"claim_id": claim_id}
        )

    def tick(self, moment: datetime) -> None:
        """Run one physical tick, which is the only accrual cadence."""
        self.now = moment
        self.c._accrue_quarter_progress(moment)

    @property
    def measured(self) -> float:
        return self.c._quarter_grid_export_kwh


# =====================================================================
# A -- a new claim arriving mid-row
# =====================================================================


def test_a_new_claim_mid_row_loses_exactly_the_interval_it_lands_in() -> None:
    """**A. The live arm shape, to the second.**

    Row opens at :00. The tick at :30 sees a new ``row_start`` and resets. Export
    begins at :45, when the vendor register goes active. The tick at 1:30 sees a new
    ``claim`` and resets *again*. The first interval that accrues anything is 2:30,
    and it covers 1:30-2:30.

    So the loss is **45 s, not a full tick**: the discarded interval carried export
    only for its last 45 seconds. That distinction is the point of measuring rather
    than asserting -- a naive reading of the mechanism would have said 60 s.
    """
    rig = _Rig()
    rig.open_row(ROW_OPENS)
    rig.exporting_from = ROW_OPENS + timedelta(seconds=45)

    rig.tick(ROW_OPENS + TICK_OFFSET)  # row changed -> reset, nothing accrued
    rig.claim("arm-1")
    rig.tick(ROW_OPENS + TICK_OFFSET + TICK)  # claim changed -> reset again
    rig.tick(ROW_OPENS + TICK_OFFSET + 2 * TICK)  # first real accrual

    physical = kwh(105.0)  # 12:00:45 -> 12:02:30
    assert rig.measured == pytest.approx(kwh(60.0), abs=1e-9)
    assert physical - rig.measured == pytest.approx(kwh(45.0), abs=1e-9)
    # Stated in the units the audit will publish.
    assert kwh(45.0) == pytest.approx(0.1, abs=1e-9)


# =====================================================================
# B -- a row boundary under one continuous claim
# =====================================================================


def test_a_row_boundary_under_one_claim_loses_one_interval_split_across_two_rows() -> (
    None
):
    """**B. One tick lost, and it belongs to two rows at once.**

    Export is already flowing and the claim never changes. The tick after the
    boundary sees a new ``row_start``, resets, and accrues nothing -- so the closing
    row never receives its last 30 s and the opening row never receives its first 30 s.

    Asserted on **both** rows, because a single total would hide which one was short.
    """
    rig = _Rig()
    rig.open_row(ROW_OPENS)
    rig.claim("arm-1")
    rig.exporting_from = ROW_OPENS - timedelta(minutes=5)

    # Settle the cursor, then accrue one clean interval inside the first row.
    rig.tick(ROW_OPENS - TICK + TICK_OFFSET)
    rig.tick(ROW_OPENS - TICK + TICK_OFFSET + TICK)  # ends at :30 into the row

    first_row_total = rig.measured
    boundary = ROW_OPENS + timedelta(minutes=15)

    # The next tick crosses the boundary at :15:00 and finds a new row.
    rig.open_row(boundary)
    rig.tick(boundary + TICK_OFFSET)

    assert rig.measured == pytest.approx(0.0, abs=1e-9), (
        "the opening row must start empty, and it must not inherit"
    )

    # The closing row stopped measuring at :14:30, so it is short by 30 s.
    closing_measured_until = ROW_OPENS + timedelta(minutes=14, seconds=30)
    lost_before = (boundary - closing_measured_until).total_seconds()
    # The opening row does not measure until :15:30, so it is short by 30 s.
    lost_after = (boundary + TICK_OFFSET - boundary).total_seconds()

    assert lost_before == pytest.approx(30.0)
    assert lost_after == pytest.approx(30.0)
    assert kwh(lost_before + lost_after) == pytest.approx(kwh(60.0), abs=1e-9)
    assert kwh(60.0) == pytest.approx(0.1333, abs=1e-4)
    assert first_row_total > 0.0


# =====================================================================
# C -- additive, or subsumed?
# =====================================================================


def test_two_changes_on_one_tick_lose_one_interval_not_two() -> None:
    """**C1. The reset condition is a single ``or``, so one reset fires.**

    When the row advances and the claim lands between the same two ticks, the loss is
    one interval -- not two. Pinned so the magnitude in the audit cannot be doubled
    by assuming every change costs its own tick.
    """
    rig = _Rig()
    rig.exporting_from = ROW_OPENS - timedelta(minutes=5)
    rig.open_row(ROW_OPENS)
    rig.claim("arm-1")

    rig.tick(ROW_OPENS + TICK_OFFSET)  # both changed at once -> one reset
    rig.tick(ROW_OPENS + TICK_OFFSET + TICK)  # accrues a full clean interval

    assert rig.measured == pytest.approx(kwh(60.0), abs=1e-9)


def test_two_changes_on_different_ticks_are_additive() -> None:
    """**C2. The live shape, and the reason it costs twice.**

    The row advances at the boundary; the claim cannot land until the refresh has
    finished its solve, which is a tick later. Two resets, two discarded intervals.

    This is the only case in which the loss exceeds one tick, and it is the case that
    happens on every real arm -- which is why the arm-scoped figure is the one the
    audit reports.
    """
    rig = _Rig()
    rig.exporting_from = ROW_OPENS - timedelta(minutes=5)
    rig.open_row(ROW_OPENS)

    rig.tick(ROW_OPENS + TICK_OFFSET)  # row changed -> reset one
    rig.claim("arm-1")
    rig.tick(ROW_OPENS + TICK_OFFSET + TICK)  # claim changed -> reset two
    rig.tick(ROW_OPENS + TICK_OFFSET + 2 * TICK)  # first accrual

    assert rig.measured == pytest.approx(kwh(60.0), abs=1e-9)
    # Two intervals elapsed before anything was measured, against one in C1.
    assert kwh(120.0) == pytest.approx(0.2667, abs=1e-4)


def test_an_ordinary_tick_inside_a_settled_row_loses_nothing() -> None:
    """The control: with no key change, every second is accounted for."""
    rig = _Rig()
    rig.exporting_from = ROW_OPENS - timedelta(minutes=5)
    rig.open_row(ROW_OPENS)
    rig.claim("arm-1")

    rig.tick(ROW_OPENS + TICK_OFFSET)
    for step in range(1, 6):
        rig.tick(ROW_OPENS + TICK_OFFSET + step * TICK)

    assert rig.measured == pytest.approx(kwh(5 * 60.0), abs=1e-9)


# =====================================================================
# D -- the reconciliation, and its refusals
# =====================================================================


def _measured_row(rig: _Rig, *, counter: float | None = None) -> None:
    """Measure one clean minute of a settled row, optionally with a counter."""
    rig.exporting_from = ROW_OPENS - timedelta(minutes=5)
    rig.open_row(ROW_OPENS)
    rig.claim("arm-1")
    if counter is not None:
        rig.counter_kwh = counter
    rig.tick(ROW_OPENS + TICK_OFFSET)
    if counter is not None:
        # The meter advances by exactly what flowed over the integrated interval.
        rig.counter_kwh = counter + kwh(60.0)
    rig.tick(ROW_OPENS + TICK_OFFSET + TICK)


def test_a_row_reconciles_exactly_when_the_counter_agrees() -> None:
    """**The whole point: a physical delta and an accounted figure, compared.**

    The counter is the only figure here that does not come from the instantaneous
    grid sensor, so it is the only one that could ever contradict it.
    """
    rig = _Rig()
    rig.c.config.grid_export_energy_entity = "sensor.p1_export_total"
    _measured_row(rig, counter=1234.0)
    rig.c._file_meter_audit(rig.c._quarter)

    audit = rig.c._meter_audits[-1]
    assert audit["counter_status"] == "ok"
    assert audit["physical_export_kwh"] == pytest.approx(kwh(60.0), abs=1e-4)
    assert audit["attributed_export_kwh"] == pytest.approx(kwh(60.0), abs=1e-4)
    assert audit["unexplained_kwh"] == pytest.approx(0.0, abs=1e-4)
    assert audit["status"] == "exact"


def test_without_a_counter_the_verdict_can_never_be_exact() -> None:
    """**The refusal that keeps the audit honest.**

    Path A and Path B agreeing says our arithmetic is self-consistent. It says
    nothing about physical energy, because both integrate the same sensor. So with
    no counter configured there is no physical figure and no exact verdict -- and
    the fields stay null rather than becoming a zero delta.
    """
    rig = _Rig()
    _measured_row(rig)
    rig.c._file_meter_audit(rig.c._quarter)

    audit = rig.c._meter_audits[-1]
    assert audit["counter_status"] == "not_configured"
    assert audit["physical_export_kwh"] is None
    assert audit["unexplained_kwh"] is None
    assert audit["status"] == "uncertain"
    assert audit["attributed_export_kwh"] > 0.0


def test_a_decreasing_counter_is_rejected_and_never_wrapped() -> None:
    """A reset and a rollover are indistinguishable from one reading."""
    rig = _Rig()
    rig.c.config.grid_export_energy_entity = "sensor.p1_export_total"
    rig.exporting_from = ROW_OPENS - timedelta(minutes=5)
    rig.open_row(ROW_OPENS)
    rig.claim("arm-1")
    rig.counter_kwh = 1234.0
    rig.tick(ROW_OPENS + TICK_OFFSET)
    rig.counter_kwh = 3.0  # the meter was replaced, or rolled over
    rig.tick(ROW_OPENS + TICK_OFFSET + TICK)
    rig.c._file_meter_audit(rig.c._quarter)

    audit = rig.c._meter_audits[-1]
    assert audit["counter_status"] == "reset_detected"
    assert audit["physical_export_kwh"] is None
    assert audit["status"] == "uncertain"


def test_an_unreadable_counter_is_uncertain_not_zero() -> None:
    """Configured but unreadable is a different statement from absent, and from 0."""
    rig = _Rig()
    rig.c.config.grid_export_energy_entity = "sensor.p1_export_total"
    _measured_row(rig)  # counter stays None throughout
    rig.c._file_meter_audit(rig.c._quarter)

    audit = rig.c._meter_audits[-1]
    assert audit["counter_status"] == "unavailable"
    assert audit["physical_export_kwh"] is None
    assert audit["unexplained_kwh"] is None


def test_the_audit_publishes_the_gap_the_reset_created() -> None:
    """**The defect, as a published number rather than a claim.**

    The row is fifteen minutes; measurement covered one. The difference is stated,
    in seconds, and is not folded into the reconciliation residual -- converting it
    would need a rate for an interval nobody sampled.
    """
    rig = _Rig()
    rig.c.config.grid_export_energy_entity = "sensor.p1_export_total"
    _measured_row(rig, counter=10.0)
    rig.c._file_meter_audit(rig.c._quarter)

    audit = rig.c._meter_audits[-1]
    assert audit["measured_seconds"] == pytest.approx(60.0)
    assert audit["unmeasured_seconds"] == pytest.approx(840.0)
    assert audit["sampled_from"] is not None
    # The residual is about the meter, not about the gap.
    assert audit["unexplained_kwh"] == pytest.approx(0.0, abs=1e-4)


def test_a_charge_row_files_no_export_reconciliation() -> None:
    """Import is an attribution estimate, not a metered channel. Not audited here."""
    rig = _Rig()
    charge = quarter_at(ROW_OPENS)
    charge = CarriedQuarter(
        quarter_start=charge.quarter_start,
        quarter_end=charge.quarter_end,
        intent="grid_charge",
        battery_target_kwh=1.0,
        grid_authorised_kwh=1.0,
        grid_export_target_kwh=0.0,
        initial_desired_grid_kw=4.0,
        run_id="run-1",
        plan_id="plan-1",
        revision=1,
        admitted_at=charge.admitted_at,
    )
    rig.c._file_meter_audit(charge)

    assert list(rig.c._meter_audits) == []


def test_no_decision_path_reads_a_beta48_audit_figure() -> None:
    """**Structural.** The audit informs a person, never a decision."""
    import pathlib

    import custom_components.alpha_ems_manager.coordinator as module

    names = (
        "_meter_audits",
        "physical_export_kwh",
        "unexplained_kwh",
        "unmeasured_seconds",
        "_quarter_counter_from",
    )
    root = pathlib.Path(module.__file__).parent
    for path in root.glob("*.py"):
        if path.name == "coordinator.py":
            continue
        source = path.read_text(encoding="utf-8")
        for name in names:
            assert name not in source, (path.name, name)


def test_a_row_that_was_never_measured_refuses_a_window_rather_than_zeroing_it() -> (
    None
):
    """**A row nobody integrated has no measured window, and zero is not one.**

    Reachable whenever a row opens and closes without two consecutive accepted
    ticks -- a short row at a stop, or a restart. ``measured_seconds: 0.0`` would
    say the row was watched and nothing happened; ``null`` says it was not watched.
    """
    rig = _Rig()
    rig.c.config.grid_export_energy_entity = "sensor.p1_export_total"
    rig.open_row(ROW_OPENS)
    rig.claim("arm-1")
    rig.exporting_from = ROW_OPENS - timedelta(minutes=5)
    rig.tick(ROW_OPENS + TICK_OFFSET)  # the only tick: reset, nothing accrued

    rig.c._file_meter_audit(rig.c._quarter)
    audit = rig.c._meter_audits[-1]

    assert audit["sampled_from"] is None
    assert audit["sampled_to"] is None
    assert audit["measured_seconds"] is None
    assert audit["unmeasured_seconds"] is None
    assert audit["status"] == "uncertain"


def test_a_material_discrepancy_is_never_reported_as_exact() -> None:
    """**The verdict that matters: the meter and the ledger disagreeing.**

    The counter advanced by more than the accounting attributed -- ambient export
    during the row, an unowned discharge, or a genuine drift. Whatever the cause,
    ``exact`` would be a lie, and a tolerance wide enough to swallow it would make
    the whole reconciliation worthless.

    The residual is published rather than explained away: naming the cause is a live
    question, not something the code may assume.
    """
    rig = _Rig()
    rig.c.config.grid_export_energy_entity = "sensor.p1_export_total"
    rig.exporting_from = ROW_OPENS - timedelta(minutes=5)
    rig.open_row(ROW_OPENS)
    rig.claim("arm-1")
    rig.counter_kwh = 500.0
    rig.tick(ROW_OPENS + TICK_OFFSET)
    # The meter moved by twice what the dispatch accounted for.
    rig.counter_kwh = 500.0 + 2 * kwh(60.0)
    rig.tick(ROW_OPENS + TICK_OFFSET + TICK)

    rig.c._file_meter_audit(rig.c._quarter)
    audit = rig.c._meter_audits[-1]

    assert audit["counter_status"] == "ok"
    assert audit["physical_export_kwh"] == pytest.approx(2 * kwh(60.0), abs=1e-4)
    assert audit["attributed_export_kwh"] == pytest.approx(kwh(60.0), abs=1e-4)
    assert audit["unexplained_kwh"] == pytest.approx(kwh(60.0), abs=1e-4)
    assert audit["status"] != "exact"
    assert audit["status"] == "explained"
