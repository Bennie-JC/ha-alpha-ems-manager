"""beta.46: an arm is measured against what the arm promised.

Three defects, all of them in the observability layer and none of them anywhere near
a decision.

**The objective was the wrong quantity.** ``_observe_arm`` captured
``_objective_kwh_for(self._quarter)``, which is the *realised* objective of the row in
flight. An arm opens the instant the claim is written, so nothing has been realised
yet: the figure was structurally ``0.0`` on every arm that has ever run. The
2026-09-06 charge filed ``objective_kwh: 0.0`` for an eight-hour arm belonging to a
campaign that promised 15.11 kWh and moved 13.72.

**So the forgone figure could not exist.** It is derived from the objective, and
``0.0 * anything`` is ``0.0`` -- which the guard then withheld as ``None``. Correct by
accident, for the wrong reason, and it would have published a wrong number the moment
the objective was fixed: prorating a whole multi-row arm over a single quarter charges
the arm's entire promise to its first fifteen minutes.

**And delivery compared a dataclass to a string.** ``self._coherence`` is a
``ControlCoherence``; ``COHERENCE_OK`` is ``"ok"``. ``not in (None, COHERENCE_OK)`` was
therefore true on every tick that had a coherence at all, which is every tick after the
first of a run. Delivery was evaluated once per arm -- before the setpoint had reached
the pack -- and every later sample was discarded as ``sources_incoherent``.

Nothing here relaxes an attribution rule. Ambient production still never counts as
dispatch-caused delivery, on either boundary, and ``null`` is still never zero.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.alpha_ems_manager.const import (
    ARM_EVIDENCE_INCOHERENT,
    ARM_EVIDENCE_INCOMPLETE,
    CAMPAIGN_BOUNDARY_BATTERY,
    CAMPAIGN_BOUNDARY_METER,
    COHERENCE_HOLDING,
    COHERENCE_OK,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_NET_EXPORT,
    MAX_ARM_MEASUREMENTS_REPORTED,
    OWNERSHIP_OWNED,
)
from custom_components.alpha_ems_manager.coordinator import AlphaEmsCoordinator
from custom_components.alpha_ems_manager.energy_balance import ControlCoherence
from custom_components.alpha_ems_manager.execution import AdmittedPlan, QuarterRow

#: The live charge, to the row. Its first quarter opened here.
BASE = datetime(2026, 9, 6, 6, 45, tzinfo=UTC)
QUARTER = timedelta(minutes=15)

#: The frozen schedule of the 2026-09-06 charge, first eight rows, as published.
LIVE_CHARGE_ROWS = (0.28, 0.28, 0.28, 0.56, 0.56, 0.56, 0.56, 0.56)


def coherence(state: str = COHERENCE_OK) -> ControlCoherence:
    """Return a real coherence verdict, of the type the live tick passes."""
    return ControlCoherence(
        state=state,
        bad_since=None,
        bad_ticks=0,
        grace_seconds=180.0,
        action="none",
        last_coherent_tick=None,
    )


def row(
    index: int,
    *,
    battery: float = 0.0,
    export: float = 0.0,
    executable: bool = True,
) -> QuarterRow:
    """Return one solved quarter, ``index`` quarters after :data:`BASE`."""
    start = BASE + index * QUARTER
    return QuarterRow(
        start=start,
        end=start + QUARTER,
        battery_kwh=battery,
        grid_authorised_kwh=battery,
        grid_export_target_kwh=export,
        grid_export_caused_kwh=export,
        desired_grid_kw=0.0,
        not_executable=None if executable else "below_controllable_objective",
    )


def plan(intent: str, rows: tuple[QuarterRow, ...]) -> AdmittedPlan:
    """Return a frozen schedule of ``rows``, admitted before the first of them."""
    return AdmittedPlan(
        plan_id="d5e422a6bec8ad18",
        revision=1,
        run_id="c8297d1335c2a178",
        intent=intent,
        purpose="charge" if intent == EXECUTION_INTENT_GRID_CHARGE else "export",
        admitted_at=BASE - QUARTER,
        rows=rows,
    )


class _Rig:
    """A coordinator whose ``__init__`` never ran, holding one frozen schedule."""

    def __init__(self, intent: str, rows: tuple[QuarterRow, ...]):
        self.c = object.__new__(AlphaEmsCoordinator)
        self.c._arm_open = None
        self.c._arm_measurements = deque(maxlen=MAX_ARM_MEASUREMENTS_REPORTED)
        self.c._arm_saw_dispatch = False
        self.c._coherence = coherence()
        # beta.47: with no write timing recorded the three decomposition figures
        # stay null, which leaves every beta.46 assertion below unchanged.
        self.c._write_timing = None
        self.c._arm_observe_unsub = None
        self.c._arm_observe_left = 0
        self.c._plan = plan(intent, rows)
        self.c._quarter = None
        self.c.store = SimpleNamespace(execution_record=None)
        self.surplus: float | None = 0.0
        self.flows = SimpleNamespace(grid_export_w=0.0, battery_charge_w=0.0)
        self.c._dispatch_state_changed_at = lambda: None
        self.c._ownership_now = lambda snapshot, now: OWNERSHIP_OWNED
        self.c._budget_surplus_kw = lambda: self.surplus
        self.c.read_flows = lambda: self.flows
        self.c._executing_intent = lambda: intent

    def claim(self, claim_id: str, *, at_row: int) -> None:
        """Write a claim, as the arm opening on ``at_row`` would."""
        start = BASE + at_row * QUARTER
        self.c._quarter = SimpleNamespace(
            quarter_start=start, quarter_end=start + QUARTER
        )
        self.c.store.execution_record = {
            "claim_id": claim_id,
            "run_id": "c8297d1335c2a178",
            "written_at": start.isoformat(),
        }

    def release(self) -> None:
        """Clear the record, as a stop at a non-executable row does."""
        self.c.store.execution_record = None
        self.c._quarter = None

    def tick(
        self, *, row_index: int, offset_s: float = 30.0, active: bool = True
    ) -> None:
        """Observe one physical tick, ``offset_s`` into ``row_index``."""
        now = BASE + row_index * QUARTER + timedelta(seconds=offset_s)
        self.c._observe_arm(SimpleNamespace(dispatch_active=active), now)

    @property
    def arm(self) -> dict:
        return self.c._arm_open

    @property
    def filed(self) -> list[dict]:
        return list(self.c._arm_measurements)


# =====================================================================
# A -- the charge arm, and the live regression
# =====================================================================


def test_a_multi_row_charge_arm_measures_the_whole_arm() -> None:
    """**A. The arm's objective is every executable row of its own stretch.**

    Not one row, not the campaign, and not what has been realised so far.

    *Mutation: sum only the row covering now and this collapses to 0.28.*
    """
    rows = tuple(row(i, battery=value) for i, value in enumerate(LIVE_CHARGE_ROWS))
    rig = _Rig(EXECUTION_INTENT_GRID_CHARGE, rows)
    rig.claim("7761d6e1fc8167f9", at_row=0)
    rig.tick(row_index=0)

    assert rig.arm["objective_kwh"] == pytest.approx(sum(LIVE_CHARGE_ROWS), abs=1e-3)
    assert rig.arm["objective_boundary"] == CAMPAIGN_BOUNDARY_BATTERY
    assert rig.arm["row_count"] == len(LIVE_CHARGE_ROWS)


def test_the_live_charge_arm_no_longer_files_a_zero_objective() -> None:
    """**H. The 2026-09-06 arm, reproduced through the finalised measurement.**

    The claim was written at the row boundary and the first observing tick came
    43.4 s later, exactly as the capture records. Before beta.46 the objective was
    sampled from the realised accumulator at that instant, so it was ``0.0`` --
    which is what the live measurement published for an arm that moved 13.72 kWh.

    *Mutation: restore ``_objective_kwh_for(self._quarter)`` and this fails.*
    """
    rows = tuple(row(i, battery=value) for i, value in enumerate(LIVE_CHARGE_ROWS))
    rig = _Rig(EXECUTION_INTENT_GRID_CHARGE, rows)
    rig.claim("7761d6e1fc8167f9", at_row=0)
    rig.tick(row_index=0, offset_s=43.4)
    rig.release()
    rig.tick(row_index=1)

    filed = rig.filed[0]
    assert filed["claim_id"] == "7761d6e1fc8167f9"
    assert filed["objective_kwh"] != 0.0, (
        "an arm with a real objective must never finalise as zero"
    )
    assert filed["objective_kwh"] == pytest.approx(sum(LIVE_CHARGE_ROWS), abs=1e-3)
    assert filed["objective_boundary"] == CAMPAIGN_BOUNDARY_BATTERY
    assert filed["closed_at"] is not None


def test_the_forgone_objective_is_prorated_over_the_arm_not_one_quarter() -> None:
    """**A, second half. The denominator is the arm's own planned span.**

    A multi-row arm prorated over a single quarter would charge its whole promise to
    its first fifteen minutes: here that would be 3.64 kWh lost to a sixty-second
    activation delay, on an arm that plans 3.64 kWh over two hours.

    *Mutation: divide by ``QUARTER_SECONDS`` and this is off by the row count.*
    """
    rows = tuple(row(i, battery=value) for i, value in enumerate(LIVE_CHARGE_ROWS))
    rig = _Rig(EXECUTION_INTENT_GRID_CHARGE, rows)
    rig.claim("7761d6e1fc8167f9", at_row=0)
    rig.surplus = 0.0
    rig.flows = SimpleNamespace(grid_export_w=0.0, battery_charge_w=2000.0)
    rig.tick(row_index=0, offset_s=60.0)

    total = sum(LIVE_CHARGE_ROWS)
    span = len(LIVE_CHARGE_ROWS) * 900.0
    assert rig.arm["planned_span_s"] == pytest.approx(span)
    assert rig.arm["delivery_latency_s"] == pytest.approx(60.0, abs=0.1)
    assert rig.arm["objective_forgone_to_activation_kwh"] == pytest.approx(
        round(total * 60.0 / span, 3), abs=1e-3
    )
    assert rig.arm["objective_forgone_to_activation_kwh"] < total / 4


def test_an_arm_with_no_frozen_schedule_withholds_the_objective() -> None:
    """A figure that cannot be derived stays ``null``, and never becomes zero.

    *Mutation: default the objective to ``0.0`` and this fails.*
    """
    rig = _Rig(EXECUTION_INTENT_GRID_CHARGE, (row(0, battery=0.5),))
    rig.c._plan = None
    rig.claim("orphan", at_row=0)
    rig.tick(row_index=0)

    assert rig.arm["objective_kwh"] is None
    assert rig.arm["objective_forgone_to_activation_kwh"] is None


# =====================================================================
# B, C -- the export arm, at the meter boundary
# =====================================================================


def test_a_single_row_export_arm_keeps_the_meter_objective() -> None:
    """**B. Tonight's 19:45 arm: one row, 2.26 kWh, measured at the meter.**

    The battery figure on the same row is a ceiling, not the promise, and reading it
    instead would publish 2.5 for an arm that owes 2.26.

    *Mutation: use ``battery_kwh`` for an export and this reads 2.5.*
    """
    rig = _Rig(
        EXECUTION_INTENT_NET_EXPORT,
        (row(0, battery=2.5, export=2.26),),
    )
    rig.claim("evening", at_row=0)
    rig.tick(row_index=0)

    assert rig.arm["objective_kwh"] == pytest.approx(2.26)
    assert rig.arm["objective_boundary"] == CAMPAIGN_BOUNDARY_METER
    assert rig.arm["row_count"] == 1


def test_a_contiguous_export_arm_sums_its_executable_rows() -> None:
    """**C. Several executable rows in a row are one arm, and one objective.**

    *Mutation: stop the forward walk at the covering row and this reads 2.26.*
    """
    rig = _Rig(
        EXECUTION_INTENT_NET_EXPORT,
        (
            row(0, battery=2.5, export=2.26),
            row(1, battery=1.75, export=1.53),
            row(2, battery=0.5, export=0.25),
        ),
    )
    rig.claim("evening", at_row=0)
    rig.tick(row_index=0)

    assert rig.arm["objective_kwh"] == pytest.approx(4.04, abs=1e-3)
    assert rig.arm["objective_boundary"] == CAMPAIGN_BOUNDARY_METER
    assert rig.arm["row_count"] == 3


# =====================================================================
# D -- the gap, which is what makes an arm an arm
# =====================================================================


def test_a_non_executable_gap_bounds_the_arm_on_both_sides() -> None:
    """**D. Objectives do not bleed across a gap, in either direction.**

    A non-executable row stops the dispatch, so the stretch each side of it is its
    own arm with its own promise. The published rule for the *plan* has said so
    since beta.44; this is the same rule on the measurement.

    *Mutation: ignore ``executable`` in either walk and both arms read 5.32.*
    """
    rig = _Rig(
        EXECUTION_INTENT_NET_EXPORT,
        (
            row(0, battery=2.5, export=2.26),
            row(1, battery=1.75, export=1.53),
            row(2, battery=0.25, export=0.03, executable=False),
            row(3, battery=1.0, export=0.75),
            row(4, battery=1.0, export=0.75),
        ),
    )

    rig.claim("first", at_row=0)
    rig.tick(row_index=0)
    assert rig.arm["objective_kwh"] == pytest.approx(3.79, abs=1e-3)

    # The gap stops the dispatch and clears the record; the next executable row
    # claims the marker again.
    rig.release()
    rig.tick(row_index=2)
    rig.claim("second", at_row=3)
    rig.tick(row_index=3)

    assert rig.arm["claim_id"] == "second"
    assert rig.arm["objective_kwh"] == pytest.approx(1.5, abs=1e-3)
    assert rig.arm["row_count"] == 2

    first = rig.filed[0]
    assert first["claim_id"] == "first"
    assert first["objective_kwh"] == pytest.approx(3.79, abs=1e-3)


def test_a_claim_retaken_mid_stretch_does_not_inherit_earlier_rows() -> None:
    """A restart re-claims where it stands, and owes only what is left.

    Every row here is executable, so the backward walk would run to the start of the
    schedule were it not bounded by the arm's own first row.

    *Mutation: drop the ``since`` bound and this reads the whole schedule.*
    """
    rig = _Rig(
        EXECUTION_INTENT_GRID_CHARGE,
        tuple(row(i, battery=1.0) for i in range(5)),
    )
    rig.claim("after_restart", at_row=3)
    rig.tick(row_index=3)

    assert rig.arm["objective_kwh"] == pytest.approx(2.0, abs=1e-3)
    assert rig.arm["row_count"] == 2


def test_a_stale_claim_never_carries_an_objective_across_a_gap() -> None:
    """The backward walk stops at a gap, and a gap row derives nothing at all.

    Stage B stops the dispatch at a non-executable row and the record is cleared, so
    a claim should not survive one. This is the belt-and-braces half: if one ever
    did, the measurement must still not sum rows either side of the gap into a single
    arm's promise, and it must not re-derive an objective from a row no arm can run.

    *Mutation: drop ``executable`` from the backward walk, or let a non-executable
    row derive a span, and the two arms merge into one 3.06 kWh promise.*
    """
    rig = _Rig(
        EXECUTION_INTENT_NET_EXPORT,
        (
            row(0, battery=2.5, export=2.26),
            row(1, battery=0.25, export=0.03, executable=False),
            row(2, battery=1.0, export=0.75),
            row(3, battery=1.0, export=0.75),
        ),
    )
    rig.claim("stale", at_row=0)
    rig.tick(row_index=0)
    assert rig.arm["objective_kwh"] == pytest.approx(2.26, abs=1e-3)
    assert rig.arm["row_count"] == 1

    # A tick landing on the gap itself derives nothing, and leaves the proven
    # figure standing rather than replacing it with a stretch no arm ran.
    rig.tick(row_index=1)
    assert rig.arm["objective_kwh"] == pytest.approx(2.26, abs=1e-3)
    assert rig.arm["row_count"] == 1

    # And a tick past the gap measures the stretch it is in, never both.
    rig.tick(row_index=2)
    assert rig.arm["objective_kwh"] == pytest.approx(1.5, abs=1e-3)
    assert rig.arm["row_count"] == 2


# =====================================================================
# E, F, G -- delivery evidence, unrelaxed
# =====================================================================


def test_a_coherent_attributable_sample_is_no_longer_discarded() -> None:
    """**F. The defect that made an eight-hour charge report no delivery at all.**

    ``self._coherence`` is a ``ControlCoherence`` and ``COHERENCE_OK`` is a string,
    so the old membership test rejected every tick that carried a verdict -- which is
    every tick after the first of a run.

    *Mutation: compare the object instead of its state and this fails.*
    """
    rig = _Rig(EXECUTION_INTENT_GRID_CHARGE, (row(0, battery=2.5), row(1, battery=2.5)))
    rig.claim("live", at_row=0)

    # Tick one: coherent, but nothing above the surplus has moved yet.
    rig.surplus = 1.0
    rig.flows = SimpleNamespace(grid_export_w=0.0, battery_charge_w=1000.0)
    rig.tick(row_index=0, offset_s=43.4)
    assert rig.arm["delivery_latency_s"] is None

    # Tick two: the live shape from the capture -- 1.27 kW into the pack against a
    # 0.45 kW production surplus, which is 0.82 kW of grid-caused charge.
    rig.flows = SimpleNamespace(grid_export_w=0.0, battery_charge_w=1270.0)
    rig.surplus = 0.45
    rig.tick(row_index=0, offset_s=103.4)

    assert rig.arm["delivery_latency_s"] == pytest.approx(103.4, abs=0.1)
    assert rig.arm["delivery_evidence"] is None


def test_a_delay_longer_than_the_arm_forgoes_the_arm_and_no_more() -> None:
    """An arm cannot lose more than it ever planned to deliver.

    The claim is written at the row boundary and nothing attributable moves until
    the row is already over, so the raw delay exceeds the whole planned span.

    *Mutation: drop the clamp and this forgoes 122 % of the promise.*
    """
    rig = _Rig(EXECUTION_INTENT_NET_EXPORT, (row(0, battery=2.5, export=2.26),))
    rig.claim("late", at_row=0)
    rig.surplus = 0.0
    rig.tick(row_index=0, offset_s=30.0)
    assert rig.arm["planned_span_s"] == pytest.approx(900.0)

    # Past the end of the only row, so no fresh span is derivable and the proven
    # one stands.
    rig.flows = SimpleNamespace(grid_export_w=3000.0, battery_charge_w=0.0)
    rig.tick(row_index=1, offset_s=200.0)

    assert rig.arm["delivery_latency_s"] == pytest.approx(1100.0, abs=0.1)
    assert rig.arm["objective_forgone_to_activation_kwh"] == pytest.approx(2.26)


def test_the_published_reason_describes_the_latest_observation() -> None:
    """Evidence is a statement about this tick, not the first thing that went wrong.

    Under ``setdefault`` the first reason wore the arm for its whole life, which is
    how a healthy eight-hour charge finalised ``sources_incoherent``.

    *Mutation: restore ``setdefault`` and the arm still reports the stale reason.*
    """
    rig = _Rig(EXECUTION_INTENT_GRID_CHARGE, (row(0, battery=2.5),))
    rig.claim("drifting", at_row=0)
    rig.surplus = 5.0
    rig.flows = SimpleNamespace(grid_export_w=0.0, battery_charge_w=1000.0)
    rig.tick(row_index=0, offset_s=30.0)
    assert rig.arm["delivery_evidence"] == ARM_EVIDENCE_INCOMPLETE

    rig.c._coherence = coherence(COHERENCE_HOLDING)
    rig.tick(row_index=0, offset_s=90.0)

    assert rig.arm["delivery_evidence"] == ARM_EVIDENCE_INCOHERENT, (
        "the sources are blind now, and that is what the arm must say"
    )
    assert rig.arm["delivery_latency_s"] is None


def test_an_incoherent_tick_still_refuses_a_delivery_figure() -> None:
    """**E. Nothing here relaxes the coherence standard.**

    The pack may genuinely be moving, and that is published beside the refusal --
    never instead of it.

    *Mutation: drop the coherence gate and this attributes a figure.*
    """
    rig = _Rig(EXECUTION_INTENT_GRID_CHARGE, (row(0, battery=2.5),))
    rig.c._coherence = coherence(COHERENCE_HOLDING)
    rig.claim("blind", at_row=0)
    rig.surplus = 0.0
    rig.flows = SimpleNamespace(grid_export_w=0.0, battery_charge_w=3000.0)
    rig.tick(row_index=0)

    assert rig.arm["delivery_latency_s"] is None
    assert rig.arm["delivery_evidence"] == ARM_EVIDENCE_INCOHERENT
    assert rig.arm["objective_forgone_to_activation_kwh"] is None


def test_a_recovered_source_clears_a_hiccup_instead_of_latching_it() -> None:
    """The published reason is why *this* observation attributed nothing.

    Under ``setdefault`` one bad tick wore the label for the life of the arm, which
    is how the live measurement finalised ``sources_incoherent`` while the sources
    were fine.

    *Mutation: restore ``setdefault`` and the stale reason survives.*
    """
    rig = _Rig(EXECUTION_INTENT_GRID_CHARGE, (row(0, battery=2.5),))
    rig.c._coherence = coherence(COHERENCE_HOLDING)
    rig.claim("recovering", at_row=0)
    rig.tick(row_index=0, offset_s=30.0)
    assert rig.arm["delivery_evidence"] == ARM_EVIDENCE_INCOHERENT

    rig.c._coherence = coherence(COHERENCE_OK)
    rig.surplus = 5.0
    rig.flows = SimpleNamespace(grid_export_w=0.0, battery_charge_w=1000.0)
    rig.tick(row_index=0, offset_s=90.0)

    assert rig.arm["delivery_evidence"] == ARM_EVIDENCE_INCOMPLETE, (
        "readable sources with nothing above the surplus is not an evidence failure"
    )
    assert rig.arm["delivery_latency_s"] is None


def test_ambient_absorption_moves_the_battery_clock_and_nothing_else() -> None:
    """**G. Production charging the pack is not the dispatch delivering.**

    *Mutation: time delivery off raw battery charge and this fails.*
    """
    rig = _Rig(EXECUTION_INTENT_GRID_CHARGE, (row(0, battery=2.5),))
    rig.claim("absorbing", at_row=0)
    rig.surplus = 3.0
    rig.flows = SimpleNamespace(grid_export_w=0.0, battery_charge_w=3000.0)
    rig.tick(row_index=0, offset_s=45.0)

    assert rig.arm["battery_delivery_latency_s"] == pytest.approx(45.0, abs=0.1)
    assert rig.arm["delivery_latency_s"] is None
    assert rig.arm["objective_forgone_to_activation_kwh"] is None


def test_ambient_export_is_not_credited_to_an_export_arm() -> None:
    """The meter side of the same rule, on the arm the live plan runs tonight.

    *Mutation: compare raw export against the deadband and this fires.*
    """
    rig = _Rig(EXECUTION_INTENT_NET_EXPORT, (row(0, battery=2.5, export=2.26),))
    rig.claim("evening", at_row=0)
    rig.surplus = 2.0
    rig.flows = SimpleNamespace(grid_export_w=2000.0, battery_charge_w=0.0)
    rig.tick(row_index=0)
    assert rig.arm["delivery_latency_s"] is None

    rig.flows = SimpleNamespace(grid_export_w=3000.0, battery_charge_w=0.0)
    rig.tick(row_index=0, offset_s=90.0)

    assert rig.arm["delivery_latency_s"] == pytest.approx(90.0, abs=0.1)
    assert rig.arm["objective_forgone_to_activation_kwh"] == pytest.approx(
        round(2.26 * 90.0 / 900.0, 3), abs=1e-3
    )


# =====================================================================
# The fence: still diagnostics, still session-local
# =====================================================================


def test_nothing_decides_anything_from_an_arm_measurement() -> None:
    """The ring is written in one place and read in one place.

    beta.46 widened what an arm measurement *says*; it must not have widened who
    listens. ``_arm_measurements`` is appended by ``_close_arm`` and read by the
    diagnostics block, and by nothing else in the component.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    package = root / "custom_components" / "alpha_ems_manager"
    readers: list[str] = []
    for path in sorted(package.glob("*.py")):
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if "_arm_measurements" in line or "_arm_open" in line:
                readers.append(f"{path.name}:{number}")

    assert all(entry.startswith("coordinator.py:") for entry in readers), (
        f"arm measurement state leaked out of the coordinator: {readers}"
    )
