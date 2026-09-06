"""beta.47: look sooner, and say what the latency was made of.

Two defects of measurement, neither of them a defect of dispatch.

**We were not watching.** ``_observe_arm`` ran only inside the sixty-second physical
tick, whose phase is whatever instant setup happened to finish. So the delay between
the vendor register going active and this component noticing was uniform on [0, 60):
the three 2026-09-06 export arms measured 37.1 s, 42.5 s and 45.2 s of pure waiting.
That delay was then charged to ``delivery_latency_s`` -- which is prorated into a
published economic figure -- so an arm that began delivering at once could report
having forgone twice what it really did.

**And the forty seconds before activation were one undivided number.** The capture
showed ``solve_ms`` of 32.4-35.2 s against ``activation_latency_s`` of 38.6-41.5 s,
which says most of it is our own Stage A solve rather than the vendor. Saying so is
not the same as measuring it, so activation is now decomposed into three terms that
must add up, and the middle one -- our own write path -- is measured rather than
assumed, because an identity with an estimated term is not a reconciliation.

**beta.47 makes no dispatch faster.** The battery starts when it always did. What
changes is when we look, and what we can say about what we saw.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.alpha_ems_manager.const import (
    ARM_EVIDENCE_INCOHERENT,
    ARM_EVIDENCE_STALE_REGISTER,
    COHERENCE_HOLDING,
    COHERENCE_OK,
    EXECUTION_INTENT_NET_EXPORT,
    MAX_ARM_MEASUREMENTS_REPORTED,
    OWNERSHIP_OWNED,
    POST_ARM_OBSERVE_MAX_PASSES,
)
from custom_components.alpha_ems_manager.coordinator import AlphaEmsCoordinator
from custom_components.alpha_ems_manager.energy_balance import ControlCoherence
from custom_components.alpha_ems_manager.execution import AdmittedPlan, QuarterRow

#: The 21:00 export arm, to the row.
BASE = datetime(2026, 9, 6, 19, 0, tzinfo=UTC)
QUARTER = timedelta(minutes=15)

#: The live 21:00 arm, as captured: claim at :05.25, register at :43.8.
LIVE_CLAIM = BASE
LIVE_SOLVE_S = 32.4
LIVE_REGISTER_S = 38.6


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


def row(index: int, *, export: float = 2.26) -> QuarterRow:
    """Return one solved export quarter, ``index`` quarters after :data:`BASE`."""
    start = BASE + index * QUARTER
    return QuarterRow(
        start=start,
        end=start + QUARTER,
        battery_kwh=0.0,
        grid_authorised_kwh=0.0,
        grid_export_target_kwh=export,
        grid_export_caused_kwh=export,
        desired_grid_kw=export * 4.0,
    )


class _Rig:
    """A coordinator whose ``__init__`` never ran, holding one frozen schedule."""

    def __init__(self, rows: tuple[QuarterRow, ...] | None = None):
        rows = rows or (row(0), row(1))
        self.c = object.__new__(AlphaEmsCoordinator)
        self.c._arm_open = None
        self.c._arm_measurements = deque(maxlen=MAX_ARM_MEASUREMENTS_REPORTED)
        self.c._arm_saw_dispatch = False
        self.c._coherence = coherence()
        self.c._write_timing = None
        self.c._arm_observe_unsub = None
        self.c._arm_observe_left = 0
        self.c._plan = AdmittedPlan(
            plan_id="d5e422a6bec8ad18",
            revision=1,
            run_id="c8297d1335c2a178",
            intent=EXECUTION_INTENT_NET_EXPORT,
            purpose="export",
            admitted_at=BASE - QUARTER,
            rows=rows,
        )
        self.c._quarter = None
        self.c.hass = SimpleNamespace()
        self.c.store = SimpleNamespace(execution_record=None)
        self.register_changed: datetime | None = None
        self.surplus: float | None = 0.0
        self.flows = SimpleNamespace(grid_export_w=0.0, battery_charge_w=0.0)
        self.c._dispatch_state_changed_at = lambda: self.register_changed
        self.c._ownership_now = lambda snapshot, now: OWNERSHIP_OWNED
        self.c._budget_surplus_kw = lambda: self.surplus
        self.c.read_flows = lambda: self.flows
        self.c._executing_intent = lambda: EXECUTION_INTENT_NET_EXPORT

    def claim(self, claim_id: str, *, at_row: int = 0) -> None:
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

    def time_write(self, claim_id: str, *, started_s: float, duration_s: float) -> None:
        """Record a write boundary, as ``_note_write_timing`` would."""
        started = LIVE_CLAIM + timedelta(seconds=started_s)
        self.c._write_timing = {
            "claim_id": claim_id,
            "dispatch_write_started_at": started.isoformat(),
            "dispatch_enable_written_at": (
                started + timedelta(seconds=duration_s)
            ).isoformat(),
        }

    def tick(self, *, at_s: float, active: bool = True) -> None:
        """Observe once, ``at_s`` seconds after the claim."""
        self.c._observe_arm(
            SimpleNamespace(dispatch_active=active),
            LIVE_CLAIM + timedelta(seconds=at_s),
        )

    @property
    def arm(self) -> dict:
        return self.c._arm_open

    @property
    def filed(self) -> list[dict]:
        return list(self.c._arm_measurements)


def _armed(rig: _Rig, claim: str = "a1") -> None:
    """Arm the live 21:00 shape: claim, write, register, first observation."""
    rig.claim(claim)
    rig.time_write(claim, started_s=LIVE_SOLVE_S, duration_s=0.05)
    rig.register_changed = LIVE_CLAIM + timedelta(seconds=LIVE_REGISTER_S)


# =====================================================================
# A -- the three-term identity
# =====================================================================


def test_activation_latency_decomposes_into_exactly_three_measured_terms() -> None:
    """**The reconciliation, and all three terms of it.**

    The live 21:00 arm: claim at T, solve returns at T+32.4, the seven-step sequence
    takes 50 ms, the register moves at T+38.6. Every term is measured; none is
    inferred from the others by subtraction.

    *Mutation: reconcile with two terms and the residual swallows the write path.*
    """
    rig = _Rig()
    _armed(rig)
    rig.tick(at_s=LIVE_REGISTER_S + 0.2)

    arm = rig.arm
    assert arm["activation_latency_s"] == pytest.approx(LIVE_REGISTER_S, abs=1e-3)
    assert arm["claim_to_write_latency_s"] == pytest.approx(LIVE_SOLVE_S, abs=1e-3)
    assert arm["dispatch_write_duration_s"] == pytest.approx(0.05, abs=1e-3)
    assert arm["enable_to_register_latency_s"] == pytest.approx(
        LIVE_REGISTER_S - LIVE_SOLVE_S - 0.05, abs=1e-3
    )

    total = (
        arm["claim_to_write_latency_s"]
        + arm["dispatch_write_duration_s"]
        + arm["enable_to_register_latency_s"]
    )
    assert total == pytest.approx(arm["activation_latency_s"], abs=0.2)

    # And the two-term identity must NOT reconcile, or the test proves nothing.
    partial = arm["claim_to_write_latency_s"] + arm["enable_to_register_latency_s"]
    assert partial != pytest.approx(arm["activation_latency_s"], abs=1e-3)


def test_the_solve_is_most_of_the_activation_latency() -> None:
    """**The finding the release exists to publish, stated as a ratio.**

    84-86 % on the live arms. Not an assertion about a good number -- an assertion
    that the decomposition attributes the time to the stage that actually spent it.
    """
    rig = _Rig()
    _armed(rig)
    rig.tick(at_s=LIVE_REGISTER_S + 0.2)

    share = rig.arm["claim_to_write_latency_s"] / rig.arm["activation_latency_s"]
    assert 0.80 <= share <= 0.90


def test_the_write_duration_is_signed_so_a_reversed_ordering_shows() -> None:
    """**An impossible ordering is published, not disguised.**

    In production the enable cannot precede the sequence that wrote it, so this is
    only reachable if a clock moved. Taking the absolute value would turn that into a
    plausible small positive and hide it; the signed difference says something is
    wrong. A diagnostic that cannot report an impossibility is not a diagnostic.

    *Mutation: wrap the subtraction in abs() and this fails.*
    """
    rig = _Rig()
    _armed(rig)
    rig.tick(at_s=LIVE_REGISTER_S + 0.2)
    assert rig.arm["dispatch_write_duration_s"] == pytest.approx(0.05, abs=1e-3)

    reversed_rig = _Rig()
    reversed_rig.claim("a1")
    reversed_rig.time_write("a1", started_s=LIVE_SOLVE_S, duration_s=-0.4)
    reversed_rig.register_changed = LIVE_CLAIM + timedelta(seconds=LIVE_REGISTER_S)
    reversed_rig.tick(at_s=LIVE_REGISTER_S + 0.2)

    assert reversed_rig.arm["dispatch_write_duration_s"] == pytest.approx(
        -0.4, abs=1e-3
    )


def test_the_register_latency_is_the_external_share_and_only_that() -> None:
    """**A part of the whole, and strictly smaller whenever a solve preceded it.**

    ``<=`` alone is satisfied by measuring the term from the claim, which would
    charge our own solve to the vendor -- the exact misattribution this release
    exists to undo. So the value is asserted, and the strictness with it.

    *Mutation: anchor it on claim_written_at and this fails on both counts.*
    """
    rig = _Rig()
    _armed(rig)
    rig.tick(at_s=LIVE_REGISTER_S + 0.2)

    external = rig.arm["enable_to_register_latency_s"]
    assert external == pytest.approx(LIVE_REGISTER_S - LIVE_SOLVE_S - 0.05, abs=1e-3)
    assert external < rig.arm["activation_latency_s"]
    assert external < rig.arm["claim_to_write_latency_s"]


# =====================================================================
# B -- refusing rather than guessing
# =====================================================================


def test_an_unmatched_write_leaves_every_new_figure_null() -> None:
    """**No write timing, no decomposition.** Null, and never zero."""
    rig = _Rig()
    rig.claim("a1")
    rig.register_changed = LIVE_CLAIM + timedelta(seconds=LIVE_REGISTER_S)
    rig.tick(at_s=LIVE_REGISTER_S + 0.2)

    for key in (
        "dispatch_write_started_at",
        "dispatch_enable_written_at",
        "claim_to_write_latency_s",
        "dispatch_write_duration_s",
        "enable_to_register_latency_s",
    ):
        assert rig.arm[key] is None, key


def test_a_second_arm_never_inherits_the_first_arms_timing() -> None:
    """The timing is keyed on the claim, so it cannot outlive its own arm."""
    rig = _Rig()
    _armed(rig, "a1")
    rig.tick(at_s=LIVE_REGISTER_S + 0.2)
    assert rig.arm["claim_to_write_latency_s"] is not None

    # A new claim on the next row, with no write recorded for it.
    rig.claim("a2", at_row=1)
    rig.tick(at_s=900.0 + 30.0)

    assert rig.arm["claim_id"] == "a2"
    assert rig.arm["claim_to_write_latency_s"] is None
    assert rig.arm["dispatch_write_duration_s"] is None


def test_a_register_that_predates_the_enable_is_refused() -> None:
    """A register already active proves nothing about the write that just ran."""
    rig = _Rig()
    rig.claim("a1")
    rig.time_write("a1", started_s=LIVE_SOLVE_S, duration_s=0.05)
    # Moved *before* our activation write.
    rig.register_changed = LIVE_CLAIM + timedelta(seconds=1.0)
    rig.tick(at_s=LIVE_REGISTER_S)

    assert rig.arm["enable_to_register_latency_s"] is None


def test_a_register_that_predates_the_claim_is_still_stale() -> None:
    """beta.44's refusal is untouched: it still governs activation itself."""
    rig = _Rig()
    rig.claim("a1")
    rig.time_write("a1", started_s=LIVE_SOLVE_S, duration_s=0.05)
    rig.register_changed = LIVE_CLAIM - timedelta(seconds=5.0)
    rig.tick(at_s=LIVE_REGISTER_S)

    assert rig.arm["activation_latency_s"] is None
    assert rig.arm["evidence"] == ARM_EVIDENCE_STALE_REGISTER
    assert rig.arm["enable_to_register_latency_s"] is None


def test_a_dispatch_already_running_at_the_claim_is_still_refused() -> None:
    """The transition requirement is unchanged by observing more often."""
    rig = _Rig()
    # Observed running before any claim existed, which is what sets the flag.
    rig.tick(at_s=1.0, active=True)
    assert rig.c._arm_saw_dispatch is True

    _armed(rig)
    rig.tick(at_s=LIVE_REGISTER_S + 0.2, active=True)

    assert rig.arm["activation_latency_s"] is None
    assert rig.arm["enable_to_register_latency_s"] is None


# =====================================================================
# C -- observing more often changes nothing about what is recorded
# =====================================================================


def test_observing_many_times_records_one_set_of_figures() -> None:
    """**Idempotence, which is what makes a second observer safe.**

    Every field is guarded ``is None`` before assignment, so the event, the sweep and
    the tick can all fire and the arm still carries one measurement.

    *Mutation: drop a guard and the last observation overwrites the first.*
    """
    rig = _Rig()
    _armed(rig)
    rig.tick(at_s=LIVE_REGISTER_S + 0.2)
    first = dict(rig.arm)

    for offset in (1.0, 5.0, 20.0, 60.0):
        rig.tick(at_s=LIVE_REGISTER_S + 0.2 + offset)

    for key in (
        "activation_latency_s",
        "observation_latency_s",
        "claim_to_write_latency_s",
        "dispatch_write_duration_s",
        "enable_to_register_latency_s",
    ):
        assert rig.arm[key] == first[key], key


def test_delivery_is_still_timed_from_the_claim_not_from_the_write() -> None:
    """**The metric definitions do not move.**

    Looking sooner makes ``delivery_latency_s`` smaller because the delay was real,
    not because the origin changed. It is still measured from the claim.

    *Mutation: measure it from dispatch_enable_written_at and this fails.*
    """
    rig = _Rig()
    _armed(rig)
    rig.flows = SimpleNamespace(grid_export_w=8000.0, battery_charge_w=0.0)
    rig.tick(at_s=LIVE_REGISTER_S + 0.4)

    assert rig.arm["delivery_latency_s"] == pytest.approx(
        LIVE_REGISTER_S + 0.4, abs=1e-3
    )


def test_observation_latency_is_still_timed_from_the_claim() -> None:
    """**The second origin that must not move, asserted where it can move.**

    beta.44 separated two clocks on purpose: activation is the vendor register, and
    observation is our own -- and observation *deliberately includes our cadence*,
    because that is the figure that showed 91.7 s against a 37.3 s activation.

    The beta.44 suite pins the value, but its rig records no write timing, so
    re-anchoring observation on the enable is invisible there: the fallback returns
    the claim and the number is unchanged. It is only distinguishable once a real
    write has been timed, which is what this rig does.

    *Mutation: measure it from dispatch_enable_written_at and this fails.*
    """
    rig = _Rig()
    _armed(rig)
    rig.tick(at_s=LIVE_REGISTER_S + 0.2)

    assert rig.arm["observation_latency_s"] == pytest.approx(
        LIVE_REGISTER_S + 0.2, abs=1e-3
    )
    # And it is strictly larger than the write-anchored figure the mutation returns,
    # so the assertion above cannot pass by coincidence.
    assert rig.arm["observation_latency_s"] > rig.arm["claim_to_write_latency_s"]


def test_incoherent_sources_still_refuse_to_attribute_delivery() -> None:
    """No evidence bar is lowered by observing more often."""
    rig = _Rig()
    _armed(rig)
    rig.c._coherence = coherence(COHERENCE_HOLDING)
    rig.flows = SimpleNamespace(grid_export_w=8000.0, battery_charge_w=0.0)
    rig.tick(at_s=LIVE_REGISTER_S + 0.4)

    assert rig.arm["delivery_latency_s"] is None
    assert rig.arm["delivery_evidence"] == ARM_EVIDENCE_INCOHERENT


def test_ambient_export_is_still_never_credited_to_the_arm() -> None:
    """Production the site was exporting anyway is not the dispatch starting."""
    rig = _Rig()
    _armed(rig)
    rig.surplus = 8.0
    rig.flows = SimpleNamespace(grid_export_w=8000.0, battery_charge_w=0.0)
    rig.tick(at_s=LIVE_REGISTER_S + 0.4)

    assert rig.arm["delivery_latency_s"] is None


# =====================================================================
# D -- the bounded sweep
# =====================================================================


def test_the_sweep_is_bounded_and_stops_on_delivery(monkeypatch) -> None:
    """**A sweep, not a cadence.** It ends on the first attribution.

    Patched through ``monkeypatch`` so the module is restored: a leaked
    ``read_snapshot`` poisons every later test in the session, which is exactly what
    it did the first time this was written.
    """
    import custom_components.alpha_ems_manager.coordinator as module

    rig = _Rig()
    _armed(rig)
    calls: list[float] = []
    rig.c.hass = SimpleNamespace()
    rig.c._arm_observe_left = POST_ARM_OBSERVE_MAX_PASSES

    def fake_later(hass, delay, action):
        calls.append(delay)
        return lambda: None

    monkeypatch.setattr(module, "async_call_later", fake_later)
    monkeypatch.setattr(
        module, "read_snapshot", lambda hass: SimpleNamespace(dispatch_active=True)
    )

    rig.tick(at_s=LIVE_REGISTER_S + 0.2)
    # Nothing delivered yet: the pass reschedules.
    rig.c._handle_arm_observation("a1", None)
    assert rig.c._arm_observe_left == POST_ARM_OBSERVE_MAX_PASSES - 1
    assert len(calls) == 1

    # Delivery lands: the sweep stops for good.
    rig.flows = SimpleNamespace(grid_export_w=8000.0, battery_charge_w=0.0)
    rig.c._handle_arm_observation("a1", None)
    assert rig.c._arm_observe_left == 0
    assert len(calls) == 1


def test_the_sweep_exhausts_its_bound_and_then_stops(monkeypatch) -> None:
    """**Bounded, and the bound is what keeps it from being a cadence.**

    Twelve passes at ten seconds covers the gap between an arm landing and the next
    tick. After that the ordinary tick is the floor again -- an unbounded sweep would
    be a second cadence observing a whole run.

    *Mutation: remove the bound and this never stops rescheduling.*
    """
    import custom_components.alpha_ems_manager.coordinator as module

    rig = _Rig()
    _armed(rig)
    scheduled: list[float] = []
    rig.c.hass = SimpleNamespace()
    monkeypatch.setattr(
        module,
        "async_call_later",
        lambda hass, delay, action: scheduled.append(delay) or (lambda: None),
    )
    monkeypatch.setattr(
        module, "read_snapshot", lambda hass: SimpleNamespace(dispatch_active=True)
    )
    rig.tick(at_s=LIVE_REGISTER_S + 0.2)
    rig.c._arm_observe_left = POST_ARM_OBSERVE_MAX_PASSES

    # Nothing is ever delivered, so only the bound can end this.
    for _ in range(POST_ARM_OBSERVE_MAX_PASSES + 5):
        rig.c._handle_arm_observation("a1", None)

    assert rig.c._arm_observe_left == 0
    assert len(scheduled) == POST_ARM_OBSERVE_MAX_PASSES - 1


def test_scheduling_a_new_arm_cancels_the_previous_sweep(monkeypatch) -> None:
    """**At most one handle exists at any instant.**

    Two arms in one session must not leave two sweeps running against each other.

    *Mutation: drop the cancel from _schedule_arm_observation and this fails.*
    """
    import custom_components.alpha_ems_manager.coordinator as module

    rig = _Rig()
    rig.c.hass = SimpleNamespace()
    cancelled: list[str] = []
    monkeypatch.setattr(
        module,
        "async_call_later",
        lambda hass, delay, action: lambda: cancelled.append("first"),
    )

    rig.c._schedule_arm_observation("a1")
    assert cancelled == []

    rig.c._schedule_arm_observation("a2")

    assert cancelled == ["first"]


def test_the_register_subscription_does_nothing_with_no_arm_open(
    monkeypatch,
) -> None:
    """**The cheap guard, and it is a correctness guard too.**

    The subscription is live for the whole session; almost always there is no arm.
    It must not read a snapshot, and must not reach the measurement, when there is
    nothing to measure.

    *Mutation: drop the guard and an idle register change observes.*
    """
    import custom_components.alpha_ems_manager.coordinator as module

    rig = _Rig()
    rig.c.hass = SimpleNamespace()
    rig.c._arm_open = None
    reads: list[bool] = []
    observed: list[bool] = []
    monkeypatch.setattr(
        module, "read_snapshot", lambda hass: reads.append(True) or SimpleNamespace()
    )
    rig.c._observe_arm = lambda snapshot, now: observed.append(True)

    rig.c._handle_dispatch_register(SimpleNamespace())

    assert reads == []
    assert observed == []


def test_the_sweep_stops_when_the_claim_changes() -> None:
    """A pass belonging to a finished arm does nothing and reschedules nothing."""
    rig = _Rig()
    _armed(rig)
    rig.tick(at_s=LIVE_REGISTER_S + 0.2)
    rig.c._arm_observe_left = POST_ARM_OBSERVE_MAX_PASSES

    rig.c._handle_arm_observation("a-different-claim", None)

    assert rig.c._arm_observe_left == 0
    assert rig.c._arm_observe_unsub is None


def test_a_closing_arm_cancels_its_own_sweep() -> None:
    """The handle belongs to one arm and dies with it."""
    rig = _Rig()
    _armed(rig)
    rig.tick(at_s=LIVE_REGISTER_S + 0.2)
    cancelled: list[bool] = []
    rig.c._arm_observe_unsub = lambda: cancelled.append(True)
    rig.c._arm_observe_left = POST_ARM_OBSERVE_MAX_PASSES

    rig.c._close_arm(rig.arm, LIVE_CLAIM + timedelta(seconds=900.0))

    assert cancelled == [True]
    assert rig.c._arm_observe_unsub is None
    assert rig.c._arm_observe_left == 0


def test_only_a_sequence_ending_in_the_activation_is_timed(monkeypatch) -> None:
    """**A sustain is not an arm.**

    The dead-man re-arm, a power correction and a stop all reach the same send site.
    None of them activates anything, so none of them may leave a write timing behind
    for the next arm to inherit.

    *Mutation: time every sequence and a sustain fabricates an arm decomposition.*
    """
    import custom_components.alpha_ems_manager.coordinator as module
    from custom_components.alpha_ems_manager.alphaess_device import (
        DISPATCH_ENABLE,
        DISPATCH_POWER,
    )

    monkeypatch.setattr(
        module, "async_call_later", lambda hass, delay, action: lambda: None
    )
    rig = _Rig()
    rig.claim("a1")
    finished = LIVE_CLAIM + timedelta(seconds=LIVE_SOLVE_S + 0.05)

    # A sustain: the last step is the setpoint, not the activation.
    rig.c._note_write_timing(
        LIVE_CLAIM, finished, (SimpleNamespace(entity_id=DISPATCH_POWER),)
    )
    assert rig.c._write_timing is None

    # Nothing sent at all.
    rig.c._note_write_timing(LIVE_CLAIM, finished, ())
    assert rig.c._write_timing is None

    # **A stop, which writes the enable FIRST and then cleans up.** The enable
    # appears in the sequence, so "contains an activation" would time a teardown as
    # an arm. Only "ends in the activation" is the edge that started a dispatch.
    rig.c._note_write_timing(
        LIVE_CLAIM,
        finished,
        (
            SimpleNamespace(entity_id=DISPATCH_ENABLE),
            SimpleNamespace(entity_id=DISPATCH_POWER),
        ),
    )
    assert rig.c._write_timing is None

    # A real arm: the activation is last.
    rig.c._note_write_timing(
        LIVE_CLAIM,
        finished,
        (
            SimpleNamespace(entity_id=DISPATCH_POWER),
            SimpleNamespace(entity_id=DISPATCH_ENABLE),
        ),
    )
    assert rig.c._write_timing is not None
    assert rig.c._write_timing["claim_id"] == "a1"


# =====================================================================
# E -- the fence
# =====================================================================


def test_no_decision_path_reads_a_beta47_metric() -> None:
    """**Structural, as beta.46 did it.** Instrumentation stays instrumentation."""
    import pathlib

    import custom_components.alpha_ems_manager.coordinator as module

    names = (
        "claim_to_write_latency_s",
        "dispatch_write_duration_s",
        "enable_to_register_latency_s",
        "_write_timing",
        "_arm_observe_unsub",
    )
    root = pathlib.Path(module.__file__).parent
    for path in root.glob("*.py"):
        if path.name == "coordinator.py":
            continue
        source = path.read_text(encoding="utf-8")
        for name in names:
            assert name not in source, (path.name, name)
