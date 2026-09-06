"""beta.44: two clocks, kept apart, and no fabricated attribution.

The 2026-09-05 capture is why this file exists. One arm: the claim was written at
22:15:05.2, the vendor register's own ``last_changed`` was 22:15:42 — **37.3 s** — and
the first tick that *saw* it was 22:16:36.9, giving **91.7 s**. A single figure would
have reported the vendor as two and a half times slower than it is, and a later release
would have priced an arm cycle against our own sixty-second cadence.

The second hazard is attribution. Ambient production can already be charging the pack
before a forced grid charge activates, so battery movement alone never proves the
dispatch started. Nothing here guesses: a figure it cannot support is ``null``, and
``null`` is never zero.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.alpha_ems_manager.const import (
    ARM_EVIDENCE_INCOHERENT,
    ARM_EVIDENCE_INCOMPLETE,
    ARM_EVIDENCE_NO_TRANSITION,
    ARM_EVIDENCE_STALE_REGISTER,
    ARM_EVIDENCE_UNATTRIBUTABLE,
    COHERENCE_HOLDING,
    COHERENCE_OK,
    DISPATCH_POWER_DEADBAND_KW,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_NET_EXPORT,
    MAX_ARM_MEASUREMENTS_REPORTED,
    OWNERSHIP_NONE,
    OWNERSHIP_OWNED,
)
from custom_components.alpha_ems_manager.coordinator import AlphaEmsCoordinator
from custom_components.alpha_ems_manager.energy_balance import ControlCoherence


def coherence(state: str) -> ControlCoherence:
    """Return a real coherence verdict.

    **A string was passed here until beta.46, and that is why the delivery gate's
    object-versus-string comparison survived three releases.** ``_observe_arm`` is
    handed a ``ControlCoherence`` by the live tick and nothing else, so a rig that
    hands it a bare state name is testing a type production never produces.
    """
    return ControlCoherence(
        state=state,
        bad_since=None,
        bad_ticks=0,
        grace_seconds=180.0,
        action="none",
        last_coherent_tick=None,
    )


#: The live arm, to the millisecond.
CLAIM = datetime(2026, 9, 5, 22, 15, 5, 238786, tzinfo=UTC)
REGISTER_ACTIVE = datetime(2026, 9, 5, 22, 15, 42, 509762, tzinfo=UTC)
FIRST_SEEN = datetime(2026, 9, 5, 22, 16, 36, 925484, tzinfo=UTC)


class _Rig:
    """A coordinator whose ``__init__`` never ran, with the reads stubbed."""

    def __init__(self, *, intent=EXECUTION_INTENT_NET_EXPORT, register=REGISTER_ACTIVE):
        self.c = object.__new__(AlphaEmsCoordinator)
        self.c._arm_open = None
        self.c._arm_measurements = __import__("collections").deque(
            maxlen=MAX_ARM_MEASUREMENTS_REPORTED
        )
        self.c._arm_saw_dispatch = False
        self.c._coherence = coherence(COHERENCE_OK)
        self.c._plan = None
        self.c._quarter = None
        self.c.store = SimpleNamespace(execution_record=None)
        self.register = register
        self.ownership = OWNERSHIP_OWNED
        self.surplus: float | None = 0.0
        self.flows = SimpleNamespace(grid_export_w=0.0, battery_charge_w=0.0)
        self.c._dispatch_state_changed_at = lambda: self.register
        self.c._ownership_now = lambda snapshot, now: self.ownership
        self.c._budget_surplus_kw = lambda: self.surplus
        self.c.read_flows = lambda: self.flows
        self.c._executing_intent = lambda: intent

    def claim(self, claim_id="9604ccf198f2b5a5", written=CLAIM):
        self.c.store.execution_record = {
            "claim_id": claim_id,
            "run_id": "a16660350cf0d994",
            "written_at": written.isoformat(),
        }

    def tick(self, now, *, active=True):
        self.c._observe_arm(SimpleNamespace(dispatch_active=active), now)

    @property
    def arm(self):
        return self.c._arm_open


def test_the_two_clocks_are_measured_separately() -> None:
    """**The anchor, at the live magnitudes.**

    *Mutation: derive activation from the first observing tick and this collapses
    to one number, 2.5x the truth.*
    """
    rig = _Rig()
    rig.claim()
    rig.tick(FIRST_SEEN)

    assert rig.arm["activation_latency_s"] == pytest.approx(37.3, abs=0.1)
    assert rig.arm["observation_latency_s"] == pytest.approx(91.7, abs=0.1)
    assert rig.arm["activation_latency_s"] < rig.arm["observation_latency_s"], (
        "the vendor is faster than we are, and merging them would hide that"
    )


def test_a_dispatch_already_running_at_the_claim_proves_nothing() -> None:
    """Pre-existing register state is refused rather than timed.

    A dispatch that was already active when we claimed says nothing about *this*
    arm, so the activation figure withholds and names why.
    """
    rig = _Rig()
    rig.c._arm_saw_dispatch = True
    rig.claim()
    rig.tick(FIRST_SEEN)

    assert rig.arm["activation_latency_s"] is None
    assert rig.arm["observation_latency_s"] is not None, (
        "observation is still measurable -- only causation is missing"
    )


def test_a_register_predating_the_claim_is_refused() -> None:
    """A negative latency is invalid and must never publish.

    *Mutation: drop the ordering guard and this publishes a negative number.*
    """
    rig = _Rig(register=CLAIM - timedelta(seconds=30))
    rig.claim()
    rig.tick(FIRST_SEEN)

    assert rig.arm["activation_latency_s"] is None
    assert rig.arm["evidence"] == ARM_EVIDENCE_STALE_REGISTER


def test_an_unreadable_register_names_its_absence() -> None:
    """No transition timestamp, no figure -- and it says which evidence was missing."""
    rig = _Rig()
    rig.c._dispatch_state_changed_at = lambda: None
    rig.claim()
    rig.tick(FIRST_SEEN)

    assert rig.arm["activation_latency_s"] is None
    assert rig.arm["evidence"] == ARM_EVIDENCE_NO_TRANSITION


def test_observation_waits_for_proven_ownership() -> None:
    """An active dispatch we cannot prove is ours is not our arm starting."""
    rig = _Rig()
    rig.ownership = OWNERSHIP_NONE
    rig.claim()
    rig.tick(FIRST_SEEN)

    assert rig.arm["observation_latency_s"] is None


def test_export_delivery_ignores_pre_existing_pv_export() -> None:
    """**The attribution guard, meter side.**

    Production already crossing the meter is not the dispatch delivering. Only
    export *above* the measured surplus counts.

    *Mutation: compare raw export against the deadband and this fires immediately.*
    """
    rig = _Rig()
    rig.claim()
    rig.surplus = 2.0
    rig.flows = SimpleNamespace(grid_export_w=2000.0, battery_charge_w=0.0)
    rig.tick(FIRST_SEEN)
    assert rig.arm["delivery_latency_s"] is None, "all of it was production"

    rig.flows = SimpleNamespace(grid_export_w=3000.0, battery_charge_w=0.0)
    rig.tick(FIRST_SEEN + timedelta(seconds=60))
    assert rig.arm["delivery_latency_s"] == pytest.approx(151.7, abs=0.1)


def test_charge_delivery_does_not_credit_ambient_pv_charging() -> None:
    """**The attribution guard, battery side, and the one the brief singled out.**

    PV can charge the pack before a forced grid charge activates. That is real
    battery energy and it is not proof the dispatch started, so it moves the
    battery figure and leaves the dispatch-attributable one null.

    *Mutation: use raw battery charge for delivery_latency_s and this fails.*
    """
    rig = _Rig(intent=EXECUTION_INTENT_GRID_CHARGE)
    rig.claim()
    rig.surplus = 3.0
    rig.flows = SimpleNamespace(grid_export_w=0.0, battery_charge_w=3000.0)
    rig.tick(FIRST_SEEN)

    assert rig.arm["battery_delivery_latency_s"] is not None, (
        "the pack really is charging, and that is published"
    )
    assert rig.arm["delivery_latency_s"] is None, (
        "but none of it is grid-caused, so nothing is attributed to the arm"
    )

    rig.flows = SimpleNamespace(grid_export_w=0.0, battery_charge_w=4000.0)
    rig.tick(FIRST_SEEN + timedelta(seconds=60))
    assert rig.arm["delivery_latency_s"] is not None, (
        "charge above the surplus is grid-caused and does attribute"
    )


def test_an_incoherent_sample_yields_no_delivery_figure() -> None:
    """Sources that disagree produce nothing, never a number from a stale reading."""
    rig = _Rig()
    rig.c._coherence = coherence(COHERENCE_HOLDING)
    rig.claim()
    rig.surplus = 0.0
    rig.flows = SimpleNamespace(grid_export_w=5000.0, battery_charge_w=0.0)
    rig.tick(FIRST_SEEN)

    assert rig.arm["delivery_latency_s"] is None
    assert rig.arm["delivery_evidence"] == ARM_EVIDENCE_INCOHERENT


def test_delivery_must_clear_the_deadband() -> None:
    """Movement inside the band the controller will not correct is not delivery."""
    rig = _Rig()
    rig.claim()
    rig.surplus = 0.0
    rig.flows = SimpleNamespace(
        grid_export_w=DISPATCH_POWER_DEADBAND_KW * 1000.0 - 1.0, battery_charge_w=0.0
    )
    rig.tick(FIRST_SEEN)
    assert rig.arm["delivery_latency_s"] is None


def test_a_new_claim_files_the_previous_arm() -> None:
    """One arm per claim id, and the record's own comment says it names one."""
    rig = _Rig()
    rig.claim(claim_id="first")
    rig.tick(FIRST_SEEN)
    rig.claim(claim_id="second", written=FIRST_SEEN)
    rig.tick(FIRST_SEEN + timedelta(seconds=60))

    filed = list(rig.c._arm_measurements)
    assert len(filed) == 1
    assert filed[0]["claim_id"] == "first"
    assert filed[0]["closed_at"] is not None
    assert rig.arm["claim_id"] == "second"


def test_a_cleared_record_files_the_arm_and_opens_none() -> None:
    """A stop clears the record; the measurement closes with whatever it proved."""
    rig = _Rig()
    rig.claim()
    rig.tick(FIRST_SEEN)
    rig.c.store.execution_record = None
    rig.tick(FIRST_SEEN + timedelta(seconds=60))

    assert rig.arm is None
    assert len(rig.c._arm_measurements) == 1


def test_an_arm_with_no_evidence_files_null_never_zero() -> None:
    """Unknown is never encoded as zero. That is the whole rule."""
    rig = _Rig()
    rig.c._dispatch_state_changed_at = lambda: None
    rig.ownership = OWNERSHIP_NONE
    rig.surplus = None
    rig.claim()
    rig.tick(FIRST_SEEN, active=False)
    rig.c.store.execution_record = None
    rig.tick(FIRST_SEEN + timedelta(seconds=60))

    filed = next(iter(rig.c._arm_measurements))
    assert filed["activation_latency_s"] is None
    assert filed["observation_latency_s"] is None
    assert filed["delivery_latency_s"] is None
    assert filed["objective_forgone_to_activation_kwh"] is None
    assert filed["evidence"] == ARM_EVIDENCE_INCOMPLETE
    assert filed["basis"], "and it says how every figure was defined"


def test_the_measurement_ring_is_bounded() -> None:
    """Calibration evidence, not a log.

    **Asserted at the declaration, not on the rig.** The rig builds its own deque,
    so a behavioural check here would exercise the test's bound rather than the
    coordinator's -- and would pass against an unbounded ring. The line that has to
    carry the cap is the one that is read.

    *Mutation: drop ``maxlen`` from the declaration and this fails.*
    """
    source = inspect.getsource(AlphaEmsCoordinator.__init__)
    declaration = next(
        line for line in source.splitlines() if "_arm_measurements" in line
    )
    following = source.split(declaration, 1)[1]
    assert "maxlen=MAX_ARM_MEASUREMENTS_REPORTED" in following.split(")")[0], (
        "the ring is declared with its cap, so it cannot grow without bound"
    )

    # And the cap really does hold, on a ring built the same way.
    rig = _Rig()
    for index in range(MAX_ARM_MEASUREMENTS_REPORTED * 2):
        rig.claim(claim_id=f"c{index}")
        rig.tick(FIRST_SEEN + timedelta(seconds=60 * index))

    assert len(rig.c._arm_measurements) == MAX_ARM_MEASUREMENTS_REPORTED


def test_an_unreadable_production_surplus_refuses_to_attribute() -> None:
    """**Without the surplus there is no attribution, so there is no figure.**

    Treating an unreadable surplus as zero would credit the whole measured flow to
    the dispatch -- exactly the fabricated attribution this release refuses. The
    flow below is large enough that a zero surplus would fire delivery immediately.

    *Mutation: default the surplus to zero and this fails.*
    """
    rig = _Rig()
    rig.claim()
    rig.surplus = None
    rig.flows = SimpleNamespace(grid_export_w=3000.0, battery_charge_w=0.0)
    rig.tick(FIRST_SEEN)

    assert rig.arm["delivery_latency_s"] is None
    assert rig.arm["delivery_evidence"] == ARM_EVIDENCE_UNATTRIBUTABLE
