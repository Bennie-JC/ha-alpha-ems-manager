"""Stage B: how to physically achieve a Stage-A target, and nothing more.

Stage A decides *what* should happen, *how much*, *when* and *why*. This module
decides *how* -- what power to ask the inverter for right now so the energy Stage A
asked for arrives inside the window Stage A chose. It is the whole of Stage B's
judgement, and it is deliberately small.

**This module cannot do economics, and that is enforced rather than intended.** It
imports no price module, names no price, and has no access to one. A structural
test asserts it, in the same shape as the test that keeps the realised-economics
layer out of every decision path. The reason is not tidiness: a controller that
could see a price would eventually be asked to use one, and the second economic
optimizer would arrive by increments rather than by decision.

What Stage B is allowed to know
-------------------------------

Physics and published constraints. It reads measured production, house load, grid
flow, battery flow, state of charge and headroom -- because measuring whether
reality matches Stage A's published assumptions is not the same act as deciding a
plan. It reads the target's own fields, including the headroom Stage A calculated.

What it may never do
--------------------

Raise a target. Buy more than Stage A approved. Choose a different window. Decide
what counts as a valuable export opportunity. Compute an economically desirable
headroom of its own. If honouring a Stage-A constraint would need a *different
economic decision* rather than merely less execution, Stage B reduces or stops and
waits for a fresh revision. There is deliberately no branch that could re-plan.

Two kinds of "only reduce", which are not the same rule
------------------------------------------------------

This distinction caused a real design error before it was written down, so it is
stated plainly:

* The **rolling controller** may raise power. Being behind schedule on an
  already-approved target inside its own window is exactly what it exists to
  correct, and refusing to catch up would silently under-deliver a plan Stage A
  chose. It may never raise the *target*, only the rate at which the same target
  is met.
* The **PV/headroom ceiling** may only ever lower what the rolling controller
  asked for. It is a cap, applied afterwards, and it can reach zero -- which means
  stop.

So `required_kw` can go up when the run is late, and the headroom cap can only
bring it down. Both are true at once, and neither is the other.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any

from .battery import INTERVAL_HOURS
from .const import (
    ACTION_CHARGE,
    ECONOMIC_BUCKET_KWH,
    ECONOMIC_FINGERPRINT_CHARS,
    EXECUTION_BASIS_ACCUMULATED,
    EXECUTION_BASIS_BOTH,
    EXECUTION_BASIS_SOC_DELTA,
    EXECUTION_BASIS_UNAVAILABLE,
    EXECUTION_INTENT_ACTIONS,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_HOLD,
    EXECUTION_INTENT_NET_EXPORT,
    EXECUTION_QUALITY_MEASURED,
    EXECUTION_QUALITY_PARTIAL,
    EXECUTION_QUALITY_RECONSTRUCTED,
    EXECUTION_QUALITY_UNAVAILABLE,
    EXECUTION_REDUCTION_BUDGET,
    EXECUTION_REDUCTION_HEADROOM,
    EXECUTION_REDUCTION_NONE,
    EXECUTION_REDUCTION_PV_AHEAD,
    EXECUTION_REDUCTION_TARGET_MET,
    EXECUTION_STATE_ARMED,
    EXECUTION_STATE_IDLE,
    EXECUTION_STATE_INHIBITED,
    EXECUTION_STATE_PREPARED,
    EXECUTION_STATE_RUNNING,
    EXECUTION_STATE_STOPPING,
    EXECUTION_STATE_UNPROVEN,
    EXECUTION_STOP_GRID_CEILING,
    EXECUTION_STOP_HEADROOM_REACHED,
    EXECUTION_STOP_PLAN_REPLACED,
    EXECUTION_STOP_STAGE_A_HOLD,
    EXECUTION_STOP_STALE_PLAN,
    EXECUTION_STOP_SWITCHED_OFF,
    EXECUTION_STOP_SWITCHED_TO_SHADOW,
    EXECUTION_STOP_TARGET_REACHED,
    EXECUTION_STOP_TIMER_NOT_REFRESHED,
    EXECUTION_STOP_WINDOW_ENDED,
    EXECUTION_TARGET_STALE_MINUTES,
    OWNERSHIP_FOREIGN,
    OWNERSHIP_NONE,
    OWNERSHIP_OWNED,
    OWNERSHIP_PROVENANCE_EXACT,
    OWNERSHIP_PROVENANCE_SETTLING,
    OWNERSHIP_UNPROVEN,
)
from .control import ControlIntent

#: How close to the target counts as reaching it, in kWh.
#:
#: One state-space bucket. Chasing the last fraction of a bucket would keep a
#: dispatch armed for energy the lattice cannot express, and the plan was quantised
#: on that grid in the first place.
TARGET_TOLERANCE_KWH: float = 0.25

#: How far ahead a run may start and still be worth arming for, in minutes.
#:
#: One planning interval. The economic horizon begins at the next interval
#: boundary, so at any refresh the earliest run starts one interval from now -- and
#: a dispatch armed now runs through that interval. Asking "does the window contain
#: this instant?" would therefore never be true of anything, and the controller
#: would sit idle beside a perfectly good target.
#:
#: The same figure and the same reasoning as ``ECONOMIC_ANNOUNCE_LEAD_MINUTES`` on
#: the Activity surface, which announces a run in the last refresh before it
#: starts.
ACTIONABLE_LEAD_MINUTES: float = 15.0

#: How far the causal record's observed dispatch start may sit from the live one
#: and still be the same dispatch, in seconds.
#:
#: The inverter reports a start instant with its own resolution and settling
#: behaviour, so an exact match is not available. Kept tight: this is a
#: corroborating condition, and a loose one would corroborate anything.
OWNERSHIP_START_TOLERANCE_SECONDS: float = 120.0

#: How long after our write a dispatch may *begin* and still be ours.
#:
#: **The narrowest weakening of the two-factor rule that makes a Live charge
#: stoppable, and it is a weakening, so it is bounded and named.** The causal
#: record is written *before* the writes -- deliberately, so a mid-sequence
#: failure cannot leave a dispatch nothing can prove -- which means the device
#: reports no ``dispatch_start`` yet and the record stores ``None``. Requiring an
#: exact match from that state is unsatisfiable: ownership would stay ``unproven``
#: for the life of the run, ``reset_required`` is gated on ``owned``, and Alpha
#: EMS would arm a charge it could never stop.
#:
#: So a record of ours, naming the run being executed, is accepted when the
#: dispatch the device reports **began at the moment we wrote** -- within this
#: window of it. That is a genuine causal chain, not the parameter matching this
#: project rejects: the dispatch started because we wrote, and the device's own
#: start instant says so.
#:
#: **Measured against the dispatch start, not against how long ago we wrote**, and
#: that distinction was a real error caught in testing. An earlier version compared
#: ``now - written_at`` against this window, which can never succeed: the record is
#: written on one refresh and the dispatch is first observed on the *next*, a
#: quarter of an hour later, so a three-minute window closed before the evidence
#: arrived and ownership never left ``unproven``. Comparing the two instants that
#: are actually causally linked works regardless of the refresh interval, and stays
#: tight.
#:
#: **The residual, stated rather than buried:** a dispatch a person started by hand
#: within this window of one of our writes, with our marker already on, would be
#: adopted. Three minutes, and the marker lives outside the vendor namespace.
OWNERSHIP_CLAIM_WINDOW_SECONDS: float = 180.0

#: Characters of the run identity. Same width as a publication id, so the two are
#: visually comparable in a diagnostics download without being confusable.
RUN_ID_CHARS: int = ECONOMIC_FINGERPRINT_CHARS

#: Fallback freshness, in minutes, when a publication carries no deadline.
STALE_MINUTES: float = float(EXECUTION_TARGET_STALE_MINUTES)

#: What counts as a material move in an executable figure, in kWh. One
#: state-space bucket -- the same deadband the publication layer uses.
BUCKET_KWH: float = ECONOMIC_BUCKET_KWH


def _finite(value: Any) -> float | None:
    """Return ``value`` as a float when it is a usable number, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def instant_of(value: Any) -> datetime | None:
    """Return an ISO-8601 string as a datetime, or ``None`` if it is not one.

    Public since beta.24 because the device adapter has the same job -- reading a
    timer's ``finishes_at`` -- and two parsers would eventually disagree about
    what a timestamp is.
    """
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# ===========================================================================
# Ownership
# ===========================================================================


@dataclass(frozen=True, slots=True)
class OwnershipEvidence:
    """What is known about who armed the dispatch that is running.

    Two independent facts, and both are required. The marker is positive evidence
    that Alpha EMS armed something; the record is what ties that claim to *this*
    dispatch. Neither is sufficient alone, and the reason is asymmetric:

    * A marker left on by a crash would otherwise claim a dispatch armed by hand
      afterwards.
    * A record alone is parameter matching, which the control surface makes
      actively misleading -- the person watching Shadow is exactly the person who
      would arm those same figures by hand, so a match is most confident precisely
      when it is most likely to be wrong.
    """

    dispatch_active: bool
    marker_on: bool
    record: dict[str, Any] | None = None
    dispatch_start: datetime | None = None
    #: Which **run** the caller is currently trying to execute, if any. A record
    #: for a different run is contradictory rather than merely old.
    #:
    #: The run identity rather than the publication identity, and that is the
    #: whole of the difference: ``plan_id`` churns every refresh as the horizon
    #: rolls, so keying ownership on it would have dropped the record to
    #: "contradictory" every fifteen minutes and lost ownership of a run nothing
    #: had replaced.
    run_id: str | None = None

    @property
    def record_present(self) -> bool:
        """Return whether a causal record exists at all."""
        return isinstance(self.record, dict) and bool(self.record)

    #: When this evidence was read, so a record can be aged. ``None`` disables
    #: the settle path entirely, which is the conservative direction.
    now: datetime | None = None

    @property
    def record_names_this_run(self) -> bool:
        """Return whether the record names the run being executed."""
        if not self.record_present:
            return False
        recorded_run = (self.record or {}).get("run_id")
        if not isinstance(recorded_run, str) or not recorded_run:
            return False
        return self.run_id is None or recorded_run == self.run_id

    @property
    def record_provenance(self) -> str | None:
        """Return *how* ownership was established, or ``None`` if it was not.

        Published so a reader can tell an exact match from a settling one rather
        than having to infer which of the two granted control.

        ``exact`` -- the dispatch the inverter reports is the one the record says
        was armed. The steady state, and the only one that survives a device
        restart or a register reset.

        ``settling`` -- our own record, naming this run, and a dispatch that *began
        when we wrote it*. Covers the refresh after an arm or a sustaining re-arm, in
        which the recorded start is absent or has legitimately moved because we moved
        it. Bounded by :data:`OWNERSHIP_CLAIM_WINDOW_SECONDS`, measured between the
        write and the dispatch start rather than between the write and now.
        """
        # Both factors, before either kind of match is considered. ``ownership_of``
        # would refuse a marker-less dispatch as foreign before reaching here, but a
        # property that reports a match without the marker is a misleading property,
        # and this one is published in diagnostics.
        if not self.marker_on or not self.dispatch_active:
            return None
        if not self.record_names_this_run:
            return None
        record = self.record or {}
        observed = instant_of(record.get("dispatch_start"))
        if observed is not None and self.dispatch_start is not None:
            delta = abs((self.dispatch_start - observed).total_seconds())
            if delta <= OWNERSHIP_START_TOLERANCE_SECONDS:
                return OWNERSHIP_PROVENANCE_EXACT
        # The settle path. It requires the running dispatch to have *begun* when we
        # wrote, so it cannot adopt one that merely happens to be running now.
        written = instant_of(record.get("written_at"))
        if written is None or self.dispatch_start is None:
            return None
        lag = (self.dispatch_start - written).total_seconds()
        # A small negative lag is allowed: the register resolves to whole seconds
        # and the surface can report a start a moment before our write completes.
        if -OWNERSHIP_START_TOLERANCE_SECONDS <= lag <= OWNERSHIP_CLAIM_WINDOW_SECONDS:
            return OWNERSHIP_PROVENANCE_SETTLING
        return None

    @property
    def record_matches(self) -> bool:
        """Return whether the record can be tied to the live dispatch.

        Requires the record to name a run, that run to be the one being executed,
        and either an exact agreement about which dispatch is running or a
        bounded settle window after our own write. See
        :attr:`record_provenance`, which is where the two are separated, and
        :data:`OWNERSHIP_CLAIM_WINDOW_SECONDS` for why the second exists at all.

        A missing ``dispatch_start`` is still never read as agreement on its own.
        The settle path does not read it: it reads *our own record's age*, which is
        evidence of a different kind.
        """
        return self.record_provenance is not None


def ownership_of(evidence: OwnershipEvidence) -> str:
    """Return the ownership state, refusing to guess.

    Four outcomes, and the two that look alike are kept apart on purpose:

    * no dispatch, no marker -> ``none``. Nothing to own.
    * a dispatch with the marker off -> ``foreign``. Somebody else's. Never
      touched, never reset, never claimed.
    * a dispatch with the marker on but no matching record -> ``unproven``. It
      might be ours. Still never touched -- but this is a fault worth reporting
      rather than an ordinary condition, which is why it is not ``foreign``.
    * a dispatch with both -> ``owned``. Controllable.

    A marker on with no dispatch running is ``none``: the marker is stale and the
    caller clears it. Clearing a stale marker is not an ownership claim, and it is
    the one write that is safe without one.
    """
    if not evidence.dispatch_active:
        return OWNERSHIP_NONE
    if not evidence.marker_on:
        return OWNERSHIP_FOREIGN
    if not evidence.record_matches:
        return OWNERSHIP_UNPROVEN
    return OWNERSHIP_OWNED


def stale_marker(evidence: OwnershipEvidence) -> bool:
    """Return whether the marker is on with nothing running behind it."""
    return evidence.marker_on and not evidence.dispatch_active


# ===========================================================================
# Progress
# ===========================================================================


@dataclass(frozen=True, slots=True)
class Progress:
    """How much of the target has actually been delivered, and how well it is known.

    Measured, never inferred from the setpoint. ``power x elapsed`` is what the
    inverter was *asked* for; a clamp, a limit, a cloud or a full pack all make it
    a different number from what arrived, and a controller trusting it would
    compound its own error every quarter.

    Two bases, published together rather than reconciled. Where they disagree the
    disagreement is the information, and picking one silently would hide it.
    """

    realized_kwh: float
    basis: str
    quality: str
    #: The in-flight quarter, from the accumulator. Absent between quarters.
    current_quarter_kwh: float | None = None
    accumulated_kwh: float | None = None
    soc_delta_kwh: float | None = None

    @property
    def available(self) -> bool:
        """Return whether any figure could be established."""
        return self.basis != EXECUTION_BASIS_UNAVAILABLE


def measure_progress(
    *,
    accumulated_kwh: float | None,
    soc_delta_kwh: float | None,
    current_quarter_kwh: float | None = None,
    coverage: float | None = None,
    minimum_coverage: float = 0.8,
    reconstructed: bool = False,
) -> Progress:
    """Return delivered battery energy from whichever evidence exists.

    Prefers the integral where its coverage is good, because integrating measured
    power is the better answer within a quarter. Falls back to the state-of-charge
    difference, which is the only basis that survives a restart -- and says so, so
    a reader is not left to assume the figure was watched rather than inferred.

    Reports ``partial`` honestly. The coverage threshold measures against the whole
    quarter, so the first quarter after a restart can never reach it: dressing that
    gap as a reading is exactly the kind of small lie that makes a controller
    untrustworthy at the moment it matters.
    """
    accumulated = _finite(accumulated_kwh)
    soc_delta = _finite(soc_delta_kwh)
    covered = coverage is None or coverage >= minimum_coverage

    if accumulated is not None and soc_delta is not None:
        return Progress(
            realized_kwh=max(0.0, accumulated if covered else soc_delta),
            basis=EXECUTION_BASIS_BOTH,
            quality=(
                EXECUTION_QUALITY_MEASURED
                if covered and not reconstructed
                else EXECUTION_QUALITY_PARTIAL
            ),
            current_quarter_kwh=_finite(current_quarter_kwh),
            accumulated_kwh=accumulated,
            soc_delta_kwh=soc_delta,
        )
    if accumulated is not None:
        return Progress(
            realized_kwh=max(0.0, accumulated),
            basis=EXECUTION_BASIS_ACCUMULATED,
            quality=(
                EXECUTION_QUALITY_MEASURED if covered else EXECUTION_QUALITY_PARTIAL
            ),
            current_quarter_kwh=_finite(current_quarter_kwh),
            accumulated_kwh=accumulated,
        )
    if soc_delta is not None:
        return Progress(
            realized_kwh=max(0.0, soc_delta),
            basis=EXECUTION_BASIS_SOC_DELTA,
            quality=(
                EXECUTION_QUALITY_RECONSTRUCTED
                if reconstructed
                else EXECUTION_QUALITY_MEASURED
            ),
            current_quarter_kwh=_finite(current_quarter_kwh),
            soc_delta_kwh=soc_delta,
        )
    return Progress(
        realized_kwh=0.0,
        basis=EXECUTION_BASIS_UNAVAILABLE,
        quality=EXECUTION_QUALITY_UNAVAILABLE,
        current_quarter_kwh=_finite(current_quarter_kwh),
    )


# ===========================================================================
# The target, read rather than interpreted
# ===========================================================================


@dataclass(frozen=True, slots=True)
class Target:
    """One Stage-A execution target, parsed and nothing more.

    A thin reading of the published dict. Every economic quantity arrives here as
    data and leaves unchanged: this class has no method that computes a value, a
    price or a preference, and it must not acquire one.
    """

    plan_id: str
    revision: int
    intent: str
    purpose: str
    window_start: datetime
    window_end: datetime
    issued_at: datetime | None
    stale_after: datetime | None
    battery_target_kwh: float
    grid_target_kwh: float | None
    average_power_kw: float
    first_power_kw: float
    reserve_floor_kwh: float
    #: The charge-window balance. Absent for anything but a charge.
    expected_pv_production_kwh: float | None = None
    expected_house_load_kwh: float | None = None
    expected_pv_to_battery_kwh: float | None = None
    expected_grid_to_battery_kwh: float | None = None
    charge_source: str | None = None
    #: The headroom constraint. ``None`` means **unconstrained**, never zero.
    required_headroom_kwh: float | None = None
    max_end_energy_kwh: float | None = None
    headroom_until: datetime | None = None

    @property
    def constrained(self) -> bool:
        """Return whether Stage A published a headroom cap for this run."""
        return self.max_end_energy_kwh is not None

    def covers(self, moment: datetime) -> bool:
        """Return whether ``moment`` falls strictly inside the window.

        End exclusive. Used where the question really is containment; selection
        uses :meth:`actionable_at`, which is a different and looser question.
        """
        return self.window_start <= moment < self.window_end

    def actionable_at(
        self, moment: datetime, lead_minutes: float = ACTIONABLE_LEAD_MINUTES
    ) -> bool:
        """Return whether this run is the one to be *preparing* for at ``moment``.

        **Selection only.** The window has not ended, and it starts now or within
        one planning interval. The lead is what makes selection work at all -- the
        economic horizon begins at the *next* boundary, so a run planned at 09:00
        opens at 09:15 and strict containment would select nothing, ever.

        It emphatically does **not** authorise activation. On this hardware arming
        *is* delivering -- measured -- so beta.19 reaching a live power request
        fifteen minutes early would have begun charging before the window. See
        :meth:`activatable_at`.
        """
        if moment >= self.window_end:
            return False
        ahead = (self.window_start - moment).total_seconds() / 60.0
        return ahead <= lead_minutes

    def activatable_at(self, moment: datetime) -> bool:
        """Return whether the window is actually open at ``moment``.

        The whole of the timing fix. Strict containment, no lead, no tolerance: a
        refresh landing a few seconds after the boundary starts a few seconds
        late, which is correct and unavoidable, while a refresh a minute before it
        starts nothing at all.
        """
        return self.covers(moment)

    def stale_at(self, moment: datetime) -> bool:
        """Return whether this target is too old to be believed.

        Against ``stale_after``, which is anchored to the issue instant. Anchoring
        it to the window -- as beta.18 did -- measured the wrong thing entirely: a
        run eighteen hours out carried a deadline eighteen and a half hours out, so
        nothing was ever stale until long after it mattered.
        """
        return self.stale_after is not None and moment >= self.stale_after


def parse_target(raw: dict[str, Any]) -> Target | None:
    """Return a :class:`Target` from a published dict, or ``None`` if unusable.

    Total and forgiving of absence, strict about nonsense. A field that cannot be
    read is absent, and absent is never silently zero -- the headroom constraint
    especially, where zero would forbid the pack from filling at all.
    """
    plan_id = raw.get("plan_id")
    intent = raw.get("intent")
    opens = instant_of(raw.get("window_start"))
    closes = instant_of(raw.get("window_end"))
    battery = _finite(raw.get("battery_target_kwh"))
    if not isinstance(plan_id, str) or not isinstance(intent, str):
        return None
    if opens is None or closes is None or battery is None:
        return None
    revision = raw.get("revision")
    mean_kw = _finite(raw.get("average_power_kw"))
    if mean_kw is None:
        mean_kw = _finite(raw.get("initial_average_power_kw")) or 0.0
    first_kw = _finite(raw.get("first_power_kw"))
    return Target(
        plan_id=plan_id,
        revision=int(revision) if isinstance(revision, int) else 1,
        intent=intent,
        purpose=str(raw.get("purpose") or intent),
        window_start=opens,
        window_end=closes,
        issued_at=instant_of(raw.get("issued_at")),
        stale_after=instant_of(raw.get("stale_after")),
        battery_target_kwh=max(0.0, battery),
        grid_target_kwh=_finite(raw.get("grid_target_kwh")),
        average_power_kw=mean_kw,
        # Falls back to the mean rather than to zero: a controller with no first
        # figure should start at the run's average, not refuse to start.
        first_power_kw=mean_kw if first_kw is None else first_kw,
        reserve_floor_kwh=_finite(raw.get("reserve_floor_kwh")) or 0.0,
        expected_pv_production_kwh=_finite(raw.get("expected_pv_production_kwh")),
        expected_house_load_kwh=_finite(raw.get("expected_house_load_kwh")),
        expected_pv_to_battery_kwh=_finite(raw.get("expected_pv_to_battery_kwh")),
        expected_grid_to_battery_kwh=_finite(raw.get("expected_grid_to_battery_kwh")),
        charge_source=(
            raw.get("charge_source")
            if isinstance(raw.get("charge_source"), str)
            else None
        ),
        required_headroom_kwh=_finite(raw.get("required_headroom_kwh")),
        max_end_energy_kwh=_finite(raw.get("max_end_energy_kwh")),
        headroom_until=instant_of(raw.get("headroom_until")),
    )


def target_by_plan_id(
    targets: tuple[dict[str, Any], ...] | list[dict[str, Any]], plan_id: str
) -> Target | None:
    """Return the published target for ``plan_id``, whatever its window says.

    Needed for exactly one thing: a run whose window has closed. ``actionable_target``
    filters on the window containing *now*, so once it closes the run vanishes from
    that view -- and an owned dispatch would then stop for "the plan was withdrawn"
    when in fact its time simply ran out. The two are different events and only one
    of them carries a shortfall worth reporting.
    """
    for raw in targets:
        if raw.get("plan_id") != plan_id:
            continue
        return parse_target(raw)
    return None


def actionable_target(
    targets: tuple[dict[str, Any], ...] | list[dict[str, Any]], now: datetime
) -> Target | None:
    """Return the one target that is executable right now, or ``None``.

    The window containing ``now``, and nothing else. Not the most valuable, not the
    nearest, not the largest -- Stage B does not rank targets, because ranking them
    is choosing between economic options and that is not its job. If two are both
    actionable the earlier window wins, and its later revision -- a tie-break on
    time and freshness, never on worth.
    """
    best: Target | None = None
    for raw in targets:
        target = parse_target(raw)
        if target is None or not target.actionable_at(now):
            continue
        if target.intent == EXECUTION_INTENT_HOLD:
            continue
        if best is None or (target.window_start, target.revision) > (
            best.window_start,
            best.revision,
        ):
            best = target
    return best


# ===========================================================================
# Carry-forward: keeping one accepted run across a rolling horizon
# ===========================================================================


def mint_run_id(intent: str, window_start: datetime, admitted_at: datetime) -> str:
    """Return a stable identity for one accepted execution run.

    Minted by Stage B, because Stage A has nothing stable to offer. Its
    ``plan_id`` is ``sha256(intent | window_start)`` and ``window_start`` advances
    every refresh as the horizon truncates from the front -- so a publication
    identity is a statement about the clock, not about the run.

    Same shape as ``_execution_plan_id``: deterministic, reproducible in a test,
    no random source. Over the *admitted* window start, which never moves, so the
    identity holds for the life of the run.
    """
    digest = hashlib.sha256(
        f"{intent}|{window_start.isoformat()}|{admitted_at.isoformat()}".encode()
    ).hexdigest()
    return digest[:RUN_ID_CHARS]


@dataclass(frozen=True, slots=True)
class CarriedRun:
    """One Stage-A target Stage B has accepted and is carrying forward.

    **Why this exists.** Every refresh rebuilds the horizon from the next interval
    boundary, so a freshly published target always opens fifteen minutes from now.
    Strict activation -- which the hardware requires, because arming delivers
    energy immediately -- can therefore never be satisfied by a *fresh*
    publication. The target whose window opens is the one accepted a refresh
    earlier. Carrying it is execution continuity, not a new decision.

    **The admitted target is immutable.** Its window never moves: that is
    precisely what makes activation reachable, because the accepted 10:30 start
    becomes past at the 10:30 refresh while the fresh 10:45 publication does not
    overwrite it. Its energy figures are immutable too, and for a subtler reason:
    a publication's ``battery_target_kwh`` shrinks as the horizon eats the run, so
    adopting the fresh figure *and* subtracting measured progress would count the
    same delivered kilowatt-hours twice.
    """

    run_id: str
    #: The publication that was admitted. Kept for traceability only -- it is
    #: stale by one refresh from the moment it is stored, by design.
    plan_id: str
    target: Target
    revision: int
    admitted_at: datetime
    affirmed_at: datetime
    #: Re-anchored on every affirmation, because an affirming publication is
    #: Stage A restating the intent.
    stale_after: datetime

    @property
    def intent(self) -> str:
        """Return the accepted intent."""
        return self.target.intent

    @property
    def window_start(self) -> datetime:
        """Return the accepted window start. Never moves."""
        return self.target.window_start

    @property
    def window_end(self) -> datetime:
        """Return the accepted window end."""
        return self.target.window_end

    def actionable_at(self, moment: datetime) -> bool:
        """Return whether the accepted window is open at ``moment``."""
        return self.target.activatable_at(moment)

    def stale_at(self, moment: datetime) -> bool:
        """Return whether this run has outlived its last affirmation."""
        return moment >= self.stale_after

    def as_dict(self) -> dict[str, Any]:
        """Return the persistable form. Only what re-execution would need."""
        return {
            "run_id": self.run_id,
            "plan_id": self.plan_id,
            "revision": self.revision,
            "admitted_at": self.admitted_at.isoformat(),
            "affirmed_at": self.affirmed_at.isoformat(),
            "stale_after": self.stale_after.isoformat(),
            "intent": self.target.intent,
            "purpose": self.target.purpose,
            "window_start": self.target.window_start.isoformat(),
            "window_end": self.target.window_end.isoformat(),
            "battery_target_kwh": self.target.battery_target_kwh,
            "expected_grid_to_battery_kwh": self.target.expected_grid_to_battery_kwh,
            "expected_pv_to_battery_kwh": self.target.expected_pv_to_battery_kwh,
            "required_headroom_kwh": self.target.required_headroom_kwh,
            "max_end_energy_kwh": self.target.max_end_energy_kwh,
            "reserve_floor_kwh": self.target.reserve_floor_kwh,
        }


def affirms(carried: CarriedRun, published: Target) -> bool:
    """Return whether ``published`` re-affirms the carried run.

    **Same intent, and windows that overlap.** Rolling movement always overlaps:
    the new start is one interval later, deep inside the accepted window. A
    campaign that has genuinely moved elsewhere -- Stage A now wants to charge
    tonight -- starts after the accepted window ends, and does not.

    Purely temporal. No prices, no ranking, no judgement about which target is
    better. Stage B is only preserving the continuity of something Stage A chose.

    **This is an inference, not a cancellation signal**, and the contract cannot
    do better: a withdrawn run and a rolled-forward run are both simply absent
    from the next publication. Overlap is a good proxy and is deliberately not
    described as more than that.

    Overlap rather than a tighter key on ``window_end``, which for a
    price-driven campaign is genuinely stable. A reserve-driven safety buy is
    anchored to the head instead, so its end advances with its start -- and a
    tighter key would mint a new identity every refresh for exactly those runs,
    resetting their progress. The looser test is the one that preserves
    continuity.
    """
    if published.intent != carried.intent:
        return False
    return published.window_start <= carried.window_end


def action_for_intent(intent: str | None) -> str | None:
    """Return the battery action an intent becomes, or ``None`` if it has none.

    **Read at arm time, so a reset needs no inference at all.** The stop path has no
    command to look at -- that is what made the reset plan a marker release and
    nothing else -- so what it stops has to come from a record of what was armed.
    Deriving the action once, here, and persisting it means the reset reads a fact
    rather than reconstructing one.

    ``None`` for anything unmapped, and the caller must fail closed. ``serve_load``
    keeps the Phase-3 reserve-guard behaviour and ``net_export`` has no primitive,
    so neither is something Alpha EMS can own -- or stop.
    """
    if not isinstance(intent, str):
        return None
    return EXECUTION_INTENT_ACTIONS.get(intent)


def target_as_published(target: Target) -> dict[str, Any]:
    """Return ``target`` in the shape Stage A published it, exactly.

    **A round trip, not a summary**, and it exists so a restart can reconstruct the
    run a live dispatch belongs to. ``parse_target(target_as_published(t))`` is
    ``t`` -- asserted rather than described, because a serialiser that quietly
    dropped the headroom cap would restore a run allowed to charge past a ceiling
    the user chose.

    Instants go out as ISO-8601 strings, which is what the persisted document holds
    and what ``parse_target`` reads back.
    """

    def moment(value: datetime | None) -> str | None:
        return None if value is None else value.isoformat()

    return {
        "plan_id": target.plan_id,
        "revision": target.revision,
        "intent": target.intent,
        "purpose": target.purpose,
        "window_start": moment(target.window_start),
        "window_end": moment(target.window_end),
        "issued_at": moment(target.issued_at),
        "stale_after": moment(target.stale_after),
        "battery_target_kwh": target.battery_target_kwh,
        "grid_target_kwh": target.grid_target_kwh,
        "average_power_kw": target.average_power_kw,
        "first_power_kw": target.first_power_kw,
        "reserve_floor_kwh": target.reserve_floor_kwh,
        "expected_pv_production_kwh": target.expected_pv_production_kwh,
        "expected_house_load_kwh": target.expected_house_load_kwh,
        "expected_pv_to_battery_kwh": target.expected_pv_to_battery_kwh,
        "expected_grid_to_battery_kwh": target.expected_grid_to_battery_kwh,
        "charge_source": target.charge_source,
        "required_headroom_kwh": target.required_headroom_kwh,
        "max_end_energy_kwh": target.max_end_energy_kwh,
        "headroom_until": moment(target.headroom_until),
    }


def carried_from_record(record: dict[str, Any] | None) -> CarriedRun | None:
    """Return the run a persisted causal record describes, or ``None``.

    **Adoption after a restart, and ``None`` is a defined answer rather than a
    failure.** A restart discards the carried run but keeps the record, so without
    this a live dispatch of ours would be met by a freshly minted run with a
    different id -- ownership would read ``unproven`` for a contradiction rather
    than for a real doubt, and the run could be neither continued nor stopped.

    ``None`` means *this record cannot prove which run is running*: a record written
    before beta.24 carries no target to rebuild, and one whose payload no longer
    parses is not going to be guessed at. The caller must then write nothing at all
    while a dispatch is active -- see the restart rule in ``coordinator``. Returning
    a half-built run here so that something could be reset would be the same
    mistake as resetting a dispatch we cannot attribute.
    """
    if not isinstance(record, dict):
        return None
    run_id = record.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return None
    raw = record.get("target")
    if not isinstance(raw, dict):
        return None
    target = parse_target(raw)
    if target is None:
        return None
    admitted = instant_of(record.get("admitted_at"))
    affirmed = instant_of(record.get("affirmed_at")) or admitted
    stale = instant_of(record.get("stale_after"))
    if admitted is None or affirmed is None or stale is None:
        return None
    revision = record.get("revision")
    return CarriedRun(
        run_id=run_id,
        plan_id=str(record.get("plan_id") or target.plan_id),
        target=target,
        revision=revision if isinstance(revision, int) and revision > 0 else 1,
        admitted_at=admitted,
        affirmed_at=affirmed,
        stale_after=stale,
    )


def admit(target: Target, now: datetime, *, revision: int = 1) -> CarriedRun:
    """Return a new carried run for ``target``."""
    return CarriedRun(
        run_id=mint_run_id(target.intent, target.window_start, now),
        plan_id=target.plan_id,
        target=target,
        revision=revision,
        admitted_at=now,
        affirmed_at=now,
        stale_after=target.stale_after or (now + timedelta(minutes=STALE_MINUTES)),
    )


def affirm(carried: CarriedRun, published: Target, now: datetime) -> CarriedRun:
    """Return the carried run re-affirmed by ``published``.

    Refreshes the freshness deadline and records the affirmation. Bumps the
    revision when Stage A has materially moved an executable figure, which is
    informational: a reader should be able to see that the plan changed under a
    run that is still the same run.

    **The accepted figures are not overwritten.** They are what progress and the
    grid ceiling are measured against, and swapping them mid-run would rebase
    both. A change large enough to warrant abandoning the run is a supersession,
    which is decided by direction rather than by magnitude.
    """
    moved = _materially_moved(carried.target, published)
    return CarriedRun(
        run_id=carried.run_id,
        plan_id=carried.plan_id,
        target=carried.target,
        revision=carried.revision + (1 if moved else 0),
        admitted_at=carried.admitted_at,
        affirmed_at=now,
        stale_after=published.stale_after or (now + timedelta(minutes=STALE_MINUTES)),
    )


def _materially_moved(accepted: Target, published: Target) -> bool:
    """Return whether Stage A has moved an executable figure beyond its deadband.

    The same deadband the publication layer uses for its own revisions, so the
    two agree about what "material" means.

    **Both window bounds are excluded, and the second one is measured rather than
    assumed.** ``window_start`` advances every refresh because the horizon begins
    at the next boundary -- that is the rolling horizon, not news, and reading it
    as novelty is the mistake beta.19 made. ``window_end`` is subtler: for a
    price-driven campaign it is pinned to an absolute interval and genuinely
    stable, but a reserve-driven safety buy is anchored to the *head* of the run,
    so its end advances with its start. A day-long runtime probe showed a real
    campaign whose end moved every fifteen minutes, which turned the revision into
    a refresh counter -- meaningless in the opposite direction to beta.19's, where
    it never left 1.

    So a revision means Stage A moved an *energy* figure. Distinguishing a slid
    window end from a deliberately extended one would need a model of Stage A's
    own arithmetic, and building one here would be Stage B inferring economics.
    """
    if abs(published.battery_target_kwh - accepted.battery_target_kwh) > BUCKET_KWH:
        return True
    for left, right in (
        (accepted.expected_grid_to_battery_kwh, published.expected_grid_to_battery_kwh),
        (accepted.max_end_energy_kwh, published.max_end_energy_kwh),
    ):
        if left is None or right is None:
            if left is not right:
                return True
        elif abs(right - left) > BUCKET_KWH:
            return True
    return False


@dataclass(frozen=True, slots=True)
class CarryOutcome:
    """What the carry-forward machine concluded this refresh."""

    carried: CarriedRun | None
    #: Why the previous run ended, when one did. ``None`` while it continues.
    ended: str | None = None
    #: The run that ended, kept so a shortfall can be reported against the target
    #: it was actually trying to meet rather than against whatever is published
    #: now.
    ended_run: CarriedRun | None = None
    #: Whether this refresh's publication re-affirmed the carried run.
    affirmed: bool = False
    admitted: bool = False


def carry_forward(
    carried: CarriedRun | None,
    targets: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    now: datetime,
    *,
    executable_intents: frozenset[str] = frozenset({EXECUTION_INTENT_GRID_CHARGE}),
) -> CarryOutcome:
    """Return the carried run after this refresh's publication.

    The state machine, in one place and in priority order. Every transition is
    driven by an instant, an intent or a published figure -- never by a price and
    never by a preference between two targets.
    """
    published = [
        parsed
        for parsed in (parse_target(raw) for raw in targets)
        if parsed is not None and parsed.intent in executable_intents
    ]

    if carried is None:
        candidate = actionable_target(targets, now)
        if candidate is None or candidate.intent not in executable_intents:
            return CarryOutcome(carried=None)
        return CarryOutcome(carried=admit(candidate, now), admitted=True)

    # A carried run outliving its own limits ends regardless of what was
    # published: these are the bounds that keep a carried plan from becoming an
    # indefinite one.
    if carried.stale_at(now):
        return CarryOutcome(
            carried=None, ended=EXECUTION_STOP_STALE_PLAN, ended_run=carried
        )
    if now >= carried.window_end:
        return CarryOutcome(
            carried=None, ended=EXECUTION_STOP_WINDOW_ENDED, ended_run=carried
        )

    affirming = next((entry for entry in published if affirms(carried, entry)), None)
    if affirming is not None:
        return CarryOutcome(carried=affirm(carried, affirming, now), affirmed=True)

    # Nothing re-affirmed it. Either Stage A moved this campaign elsewhere or
    # dropped it; both are a withdrawal, and both are visible within one refresh.
    return CarryOutcome(
        carried=None, ended=EXECUTION_STOP_STAGE_A_HOLD, ended_run=carried
    )


# ===========================================================================
# The rolling controller
# ===========================================================================


@dataclass(frozen=True, slots=True)
class Demand:
    """What Stage B would ask the battery for, and why it is not more.

    ``rolling_kw`` is the honest rate needed to finish on time. ``ceiling_kw`` is
    the cap the Stage-A headroom constraint imposes, if any. ``required_kw`` is the
    lesser, which is what a command would be built from.
    """

    rolling_kw: float
    ceiling_kw: float | None
    required_kw: float
    reduction: str
    remaining_kwh: float
    remaining_minutes: float
    ahead_kwh: float
    projected_end_kwh: float | None = None
    #: The cumulative grid-energy cap for this run, and how much has been bought
    #: against it. ``None`` when nothing caps it.
    grid_cap_kwh: float | None = None
    grid_charged_kwh: float = 0.0

    @property
    def reduced(self) -> bool:
        """Return whether the ceiling actually bit."""
        return self.reduction != EXECUTION_REDUCTION_NONE

    @property
    def finished(self) -> bool:
        """Return whether there is nothing left to ask for."""
        return self.required_kw <= 0.0


def rolling_power_kw(*, remaining_kwh: float, remaining_minutes: float) -> float:
    """Return the average power that finishes ``remaining_kwh`` in the time left.

    **This may be higher than what Stage A first suggested, and that is correct.**
    Stage A's figure is the mean over an undisturbed run; a run that lost ground to
    a cloud or a clamp needs more than the mean to deliver the same energy in the
    time that is left. Refusing to catch up would quietly under-deliver a plan
    Stage A chose, which is a different failure from over-delivering and not a
    safer one.

    What may never rise is the *target*. This raises the rate, not the amount.
    """
    if remaining_kwh <= 0.0 or remaining_minutes <= 0.0:
        return 0.0
    return remaining_kwh / (remaining_minutes / 60.0)


def headroom_ceiling_kw(
    target: Target,
    *,
    current_energy_kwh: float,
    remaining_minutes: float,
    charge_efficiency: float | None = None,
) -> float | None:
    """Return the cap the Stage-A headroom constraint puts on charging.

    ``None`` when Stage A published no constraint. **Absent means unconstrained**,
    which is not the same as a cap of zero -- reading it that way would forbid the
    pack from filling at all, which is the opposite of the intent.

    The arithmetic is deliberately dull, because every judgement in it was made by
    Stage A:

        allowance_dc = max_end_energy - current_energy
        cap          = to_ac(allowance_dc) / remaining_hours

    ``max_end_energy_kwh`` is the **optimizer's own projected stored energy** at
    the end of this run -- ``start_energy_dc_kwh + battery_delta_dc_kwh`` off the
    chosen trajectory -- so it already contains every kilowatt-hour the run
    charges, production and grid alike. The room left is therefore simply the gap
    between it and what the pack holds now.

    **Until beta.22 this also subtracted the production still expected inside the
    window, and that removed the same energy twice.** Stage A had already counted
    it when it chose the landing figure, so the cap targeted
    ``max_end - expected_pv`` and the pack finished short by exactly the
    production the plan meant to store. On a sunny capped run that is most of the
    target. The error was fail-safe -- it could only ever under-charge -- which is
    why it survived: nothing overfilled and nothing complained.

    The conversion is the second half of the same mistake. The allowance is a
    **DC** stored-energy figure and the cap is compared against an **AC** rolling
    power, so ``allowance_dc / hours`` was a DC quantity wearing AC units and
    under-allowed by the efficiency again. ``charge_efficiency`` is the pack's own
    one-way figure, threaded in rather than recomputed here.

    With no efficiency to hand the DC allowance is used unconverted, which
    under-allows: for a bound whose only job is to stop the pack overfilling, the
    conservative reading is the safe one.

    If production runs ahead of the forecast Stage A used, stored energy rises
    faster, the allowance shrinks and so does the cap -- reaching zero, which
    means stop. Nothing here consults a price, an export window, or what any of it
    is worth.
    """
    if target.max_end_energy_kwh is None:
        return None
    if remaining_minutes <= 0.0:
        return 0.0
    allowance = target.max_end_energy_kwh - current_energy_kwh
    if allowance <= 0.0:
        return 0.0
    if charge_efficiency is not None and charge_efficiency > 0.0:
        # Storing one kWh DC costs ``1 / efficiency`` kWh AC at the terminals.
        allowance = allowance / charge_efficiency
    return allowance / (remaining_minutes / 60.0)


def effective_grid_cap_kwh(
    stage_a_ceiling_kwh: float | None, configured_budget_kwh: float
) -> float | None:
    """Return the cumulative grid-energy cap for one run, or ``None`` if uncapped.

    Stage A's published figure is **always** the hard ceiling when it exists. The
    configured budget only ever tightens it further, and only above zero.

    The obvious formulation is wrong, and was caught in review: writing
    ``min(stage_a_ceiling, configured)`` with a default of ``0.0`` yields a cap of
    zero and forbids all charging. Zero means *the tightener is off*, not *buy
    nothing* -- the same "absent is not zero" rule the headroom constraint and the
    charge cutoff both follow.
    """
    ceiling = _finite(stage_a_ceiling_kwh)
    configured = _finite(configured_budget_kwh) or 0.0
    if configured > 0.0:
        return configured if ceiling is None else min(ceiling, configured)
    return ceiling


def demand_for(
    target: Target,
    *,
    now: datetime,
    progress: Progress,
    current_energy_kwh: float | None = None,
    remaining_expected_pv_kwh: float | None = None,
    grid_charged_kwh: float | None = None,
    configured_budget_kwh: float = 0.0,
    charge_efficiency: float | None = None,
) -> Demand:
    """Return what Stage B would ask for this refresh.

    Order matters and is contractual: the rolling controller first, honestly, then
    the headroom cap on top of it. Doing it the other way round would let a
    constraint that exists to *limit* charging decide how much charging to do.
    """
    remaining_kwh = max(0.0, target.battery_target_kwh - progress.realized_kwh)
    # Measured from the window, not from ``now``. Before the window opens the two
    # differ, and using ``now`` spread the target over window + lead -- so a
    # 4 kWh / 60-minute target reported 3.2 kW, a rate that cannot deliver it, and
    # the next refresh then had to *raise* power into the cooldown gate.
    effective_now = max(now, target.window_start)
    remaining_minutes = max(
        0.0, (target.window_end - effective_now).total_seconds() / 60.0
    )
    elapsed = max(0.0, (now - target.window_start).total_seconds() / 60.0)
    total = max(1e-9, (target.window_end - target.window_start).total_seconds() / 60.0)
    # Positive when ahead of a straight-line delivery of the same target.
    expected_by_now = target.battery_target_kwh * min(1.0, elapsed / total)
    ahead_kwh = progress.realized_kwh - expected_by_now

    rolling_kw = rolling_power_kw(
        remaining_kwh=remaining_kwh, remaining_minutes=remaining_minutes
    )

    if remaining_kwh <= TARGET_TOLERANCE_KWH:
        return Demand(
            rolling_kw=rolling_kw,
            ceiling_kw=None,
            required_kw=0.0,
            reduction=EXECUTION_REDUCTION_TARGET_MET,
            remaining_kwh=remaining_kwh,
            remaining_minutes=remaining_minutes,
            ahead_kwh=ahead_kwh,
        )

    ceiling_kw: float | None = None
    projected: float | None = None
    reduction = EXECUTION_REDUCTION_NONE
    stored = _finite(current_energy_kwh)
    if target.intent == EXECUTION_INTENT_GRID_CHARGE and stored is not None:
        ceiling_kw = headroom_ceiling_kw(
            target,
            current_energy_kwh=stored,
            remaining_minutes=remaining_minutes,
            charge_efficiency=charge_efficiency,
        )
        charge_kw = rolling_kw if ceiling_kw is None else min(rolling_kw, ceiling_kw)
        # **Projected stored energy, and nothing else.** What the pack will hold
        # at the end of the window if the intended power is held: the DC energy it
        # holds now plus the AC energy still to deliver, converted once.
        #
        # beta.21 added the production still expected inside the window on top,
        # and that production is already *inside* the remaining delivery --
        # ``battery_target_kwh == expected_pv_to_battery_kwh +
        # expected_grid_to_battery_kwh``, both built from the same run charge, so
        # the PV share is a component of the target rather than an addition to it.
        # A real snapshot published 31.946 kWh for a 22 kWh pack: 11.88 stored,
        # 11.038 still to deliver, and 9.028 of production counted a second time.
        #
        # Reported as unavailable rather than approximated when the conversion
        # cannot be made. A stored-energy figure that silently mixed AC into DC is
        # what produced the impossible number in the first place.
        delivery_ac = max(0.0, charge_kw) * (remaining_minutes / 60.0)
        if charge_efficiency is not None and charge_efficiency > 0.0:
            projected = stored + delivery_ac * charge_efficiency
        else:
            projected = None

    # The grid-energy ceiling, before the headroom cap so whichever binds harder
    # wins and the reason names the right one. This bounds *what was bought*; the
    # headroom cap bounds *what the pack holds*, and they are different questions:
    # when production disappoints the headroom ceiling correctly rises, and
    # without this the rolling controller would fill the extra room from the grid.
    grid_cap_kwh = effective_grid_cap_kwh(
        target.expected_grid_to_battery_kwh, configured_budget_kwh
    )
    bought = _finite(grid_charged_kwh) or 0.0
    grid_exhausted = grid_cap_kwh is not None and bought >= grid_cap_kwh - 1e-9
    if grid_exhausted:
        return Demand(
            rolling_kw=rolling_kw,
            ceiling_kw=ceiling_kw,
            required_kw=0.0,
            reduction=EXECUTION_REDUCTION_BUDGET,
            remaining_kwh=remaining_kwh,
            remaining_minutes=remaining_minutes,
            ahead_kwh=ahead_kwh,
            projected_end_kwh=projected,
            grid_cap_kwh=grid_cap_kwh,
            grid_charged_kwh=bought,
        )

    required_kw = rolling_kw
    if ceiling_kw is not None and ceiling_kw < rolling_kw:
        required_kw = max(0.0, ceiling_kw)
        # Which of the two it is matters to a reader: a cap that bit because
        # production overshot the forecast is a different event from one that bit
        # because the plan always meant to stop here.
        reduction = (
            EXECUTION_REDUCTION_PV_AHEAD
            if ahead_kwh > TARGET_TOLERANCE_KWH
            else EXECUTION_REDUCTION_HEADROOM
        )

    return Demand(
        rolling_kw=rolling_kw,
        ceiling_kw=ceiling_kw,
        required_kw=required_kw,
        reduction=reduction,
        remaining_kwh=remaining_kwh,
        remaining_minutes=remaining_minutes,
        ahead_kwh=ahead_kwh,
        projected_end_kwh=projected,
        grid_cap_kwh=grid_cap_kwh,
        grid_charged_kwh=bought,
    )


def withdrawal_basis(reason: str | None, intent: str | None) -> str | None:
    """Return what the lifecycle machine actually observed, in one token.

    Diagnostics only, and named so it cannot be mistaken for a signal Stage A
    sent. It reads nothing new: each value restates the branch that fired, so a
    reader of a later snapshot can tell *withdrawal by absence* from *the window
    running out* without reconstructing it from timestamps.

    ``None`` for reasons that are self-explanatory or that describe a mode change
    rather than an observation about a publication.
    """
    if reason == EXECUTION_STOP_STAGE_A_HOLD:
        # The specific thing that was missing, including which intent, because
        # "withdrawn" alone leaves a reader asking withdrawn from what.
        return f"no_affirming_{intent or 'executable'}_publication"
    if reason == EXECUTION_STOP_WINDOW_ENDED:
        return "admitted_window_end_passed"
    if reason == EXECUTION_STOP_STALE_PLAN:
        return "freshness_deadline_passed_without_affirmation"
    if reason == EXECUTION_STOP_PLAN_REPLACED:
        return "a_different_run_was_running"
    return None


# ===========================================================================
# The state machine
# ===========================================================================


@dataclass(frozen=True, slots=True)
class Decision:
    """What Stage B concluded this refresh.

    ``request_kw`` is what a command would be built from -- in Shadow and in Live
    alike, because there is one calculation. What differs is only whether the
    command is sent, and that is decided one layer further out.
    """

    state: str
    ownership: str
    target: Target | None = None
    demand: Demand | None = None
    progress: Progress | None = None
    request_kw: float = 0.0
    #: Why a run ended, whenever one did. **Reported regardless of ownership**,
    #: and that is a correction rather than a preference.
    #:
    #: Until beta.23 every branch read ``stop_reason=<reason> if owned else None``.
    #: Shadow never acquires ownership by design, and with the release barrier
    #: closed no mode can reach it at all -- so the field was always ``None`` in
    #: practice and the Activity surface could only ever print its fallback. On a
    #: real withdrawal the correct reason was computed one line above and thrown
    #: away by the ternary, while ``target`` on the adjacent line was kept: one
    #: decision contradicting itself, and the reason the observed line said
    #: nothing useful.
    #:
    #: It authorises nothing. Two readers exist -- the Activity wording and the
    #: diagnostics payload -- and a structural test pins that. The physical stop is
    #: driven by :attr:`reset_required`, which keeps its ownership gate and is
    #: re-checked at its only call site.
    stop_reason: str | None = None
    #: Whether an owned dispatch has to be physically stopped. **Still gated on
    #: ownership**, deliberately: there is nothing to reset when nothing is ours.
    reset_required: bool = False
    clear_stale_marker: bool = False
    inhibit_reason: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def wants_command(self) -> bool:
        """Return whether a physical request exists this refresh.

        ``prepared`` is deliberately absent from the list. A prepared decision has
        a computed power and a valid target and still wants no command, because its
        window has not opened -- and on this hardware sending one would move energy
        immediately.
        """
        return self.state in (EXECUTION_STATE_ARMED, EXECUTION_STATE_RUNNING) and (
            self.request_kw > 0.0
        )


def decide(
    *,
    mode_executes: bool,
    mode_off: bool,
    targets: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    now: datetime,
    evidence: OwnershipEvidence,
    progress: Progress,
    current_energy_kwh: float | None = None,
    remaining_expected_pv_kwh: float | None = None,
    grid_charged_kwh: float | None = None,
    configured_budget_kwh: float = 0.0,
    charge_efficiency: float | None = None,
    running_run_id: str | None = None,
    carried: CarriedRun | None = None,
    carry_ended: str | None = None,
    ended_run: CarriedRun | None = None,
    inhibit_reason: str | None = None,
    deadman_stale: bool = False,
) -> Decision:
    """Return this refresh's Stage B decision, and say so when a run ended.

    **A thin shell over the state machine, and it exists because of one refresh.**
    The carry-forward machine *watched* the carried run end and named the reason,
    but the state machine reaches that reason only through the branch it takes when
    no target is selectable. ``execution_targets`` carries every run the plan
    contains -- a ``net_export`` recommendation included -- so on the refresh a
    charge campaign is withdrawn an unrelated publication can be actionable,
    another branch is taken, and the verdict is dropped. That is the same defect as
    the ownership gate wearing different clothes: the reason is computed, and then
    thrown away.

    Filled in here rather than in each branch, so a branch added later cannot
    forget it. **Only where the machine named no reason of its own**: a reason the
    machine reached describes the target it actually selected, and replacing it
    would trade one true statement for another. The withdrawal is not lost in that
    case either -- the coordinator records it from the carry outcome directly,
    before this function is called.

    It adds a reason and changes nothing else. ``reset_required``, ``state``,
    ``request_kw`` and the target are whatever the machine decided.
    """
    decision = _decide(
        mode_executes=mode_executes,
        mode_off=mode_off,
        targets=targets,
        now=now,
        evidence=evidence,
        progress=progress,
        current_energy_kwh=current_energy_kwh,
        remaining_expected_pv_kwh=remaining_expected_pv_kwh,
        grid_charged_kwh=grid_charged_kwh,
        configured_budget_kwh=configured_budget_kwh,
        charge_efficiency=charge_efficiency,
        running_run_id=running_run_id,
        carried=carried,
        carry_ended=carry_ended,
        ended_run=ended_run,
        inhibit_reason=inhibit_reason,
        deadman_stale=deadman_stale,
    )
    if carry_ended is not None and decision.stop_reason is None:
        return replace(decision, stop_reason=carry_ended)
    return decision


def _decide(
    *,
    mode_executes: bool,
    mode_off: bool,
    targets: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    now: datetime,
    evidence: OwnershipEvidence,
    progress: Progress,
    current_energy_kwh: float | None = None,
    remaining_expected_pv_kwh: float | None = None,
    grid_charged_kwh: float | None = None,
    configured_budget_kwh: float = 0.0,
    charge_efficiency: float | None = None,
    running_run_id: str | None = None,
    carried: CarriedRun | None = None,
    carry_ended: str | None = None,
    ended_run: CarriedRun | None = None,
    inhibit_reason: str | None = None,
    deadman_stale: bool = False,
) -> Decision:
    """Return this refresh's Stage B decision.

    One function, used identically in Shadow and in Live. ``mode_executes`` is
    passed in so the *lifecycle* can differ -- Shadow never acquires ownership --
    but it deliberately does not reach the arithmetic: ``demand_for`` never sees it,
    so the power a Shadow refresh computes is the power a Live refresh would
    compute from the same inputs.

    Order of precedence, worst-first, because the cheapest way to be wrong here is
    to check the pleasant cases before the dangerous ones:

    1. a foreign dispatch -- never touched;
    2. ownership that cannot be proven -- also never touched, but reported;
    3. the mode says stop;
    4. the target is gone, replaced, or stale;
    5. the window has closed;
    6. the target is met;
    7. otherwise, run.

    **``carried`` is what makes activation reachable.** A freshly published target
    always opens one interval from now, so evaluating the publication could only
    ever produce ``prepared``. When a carried run is supplied it *is* the run under
    evaluation, and its window is the admitted one -- which the passage of time can
    actually open. With no carried run the behaviour is unchanged, so a published
    run of an intent Stage B does not execute is still observed and diagnosed.
    """
    ownership = ownership_of(evidence)
    owned = ownership == OWNERSHIP_OWNED
    clear_marker = stale_marker(evidence)

    # 1. Somebody else is driving. This is the one case with no discretion at all.
    if ownership == OWNERSHIP_FOREIGN:
        return Decision(
            state=EXECUTION_STATE_INHIBITED,
            ownership=ownership,
            progress=progress,
            inhibit_reason="foreign_dispatch",
            notes=("a dispatch is running that Alpha EMS did not arm",),
        )

    # 2. It might be ours and we cannot show it. Same restraint, different report:
    #    this is a fault, and calling it foreign would hide that.
    if ownership == OWNERSHIP_UNPROVEN:
        return Decision(
            state=EXECUTION_STATE_UNPROVEN,
            ownership=ownership,
            progress=progress,
            inhibit_reason="ownership_unproven",
            notes=("ownership could not be established; nothing will be touched",),
        )

    if mode_off:
        return Decision(
            state=EXECUTION_STATE_STOPPING if owned else EXECUTION_STATE_IDLE,
            ownership=ownership,
            progress=progress,
            stop_reason=EXECUTION_STOP_SWITCHED_OFF,
            reset_required=owned,
            clear_stale_marker=clear_marker,
        )

    if not mode_executes and owned:
        # Live -> Shadow with a run under way. The run stops; the planning does not.
        return Decision(
            state=EXECUTION_STATE_STOPPING,
            ownership=ownership,
            progress=progress,
            stop_reason=EXECUTION_STOP_SWITCHED_TO_SHADOW,
            reset_required=True,
        )

    if owned and deadman_stale:
        # **The dead-man did not move when the run was re-armed.**
        #
        # A run continues only because each refresh re-arms it against a
        # twenty-minute device timer. Whether re-activating an already-active
        # dispatch refreshes that timer is a property of the control surface rather
        # than of this integration, so it is measured instead of assumed -- and when
        # the measurement says no, the run is about to end whatever this controller
        # believes. Ending it deliberately is the honest response: the alternative is
        # a controller reporting a charge that has already stopped.
        #
        # Checked high, above target selection, because a run whose dead-man is not
        # being refreshed should not have its next setpoint computed.
        return Decision(
            state=EXECUTION_STATE_STOPPING,
            ownership=ownership,
            progress=progress,
            stop_reason=EXECUTION_STOP_TIMER_NOT_REFRESHED,
            reset_required=True,
            notes=(
                "the device dead-man did not advance when the run was re-armed, "
                "so the dispatch is stopped rather than assumed to continue",
            ),
        )

    # The carried run wins selection. It is the same intent one refresh older,
    # and it is the only one whose window can be open.
    target = carried.target if carried is not None else actionable_target(targets, now)

    if target is None:
        # Distinguish a window that ran out from a plan that was withdrawn. Both
        # stop, and both reset -- but the first has a shortfall to report and the
        # second does not, and reporting the wrong one sends a reader looking for a
        # fault that is not there.
        # The carry-forward machine already knows why, so its verdict is
        # preferred: it watched the run end, whereas this branch can only infer a
        # reason from whatever happens to be published now.
        expired = None if ended_run is None else ended_run.target
        if expired is None:
            # No carried run was supplied, so fall back to matching the publication
            # directly. Only reachable on the direct path, where the identity the
            # caller holds is a publication id rather than a minted run id.
            expired = (
                None
                if running_run_id is None
                else target_by_plan_id(targets, running_run_id)
            )
        ended = carry_ended == EXECUTION_STOP_WINDOW_ENDED or (
            carry_ended is None and expired is not None and now >= expired.window_end
        )
        reason = carry_ended or (
            EXECUTION_STOP_WINDOW_ENDED if ended else EXECUTION_STOP_STAGE_A_HOLD
        )
        # **A reason exists when a run ended, not merely when there is no target.**
        # An idle controller with nothing carried, nothing running and no record of
        # a run never stopped anything, so it has nothing to report -- and saying
        # "the plan was withdrawn" there would invent a plan to have withdrawn.
        # Ownership used to hide this by accident; the honest test is whether
        # something ended: we own a live dispatch, the carry machine ended a run,
        # the ended run is identifiable, or a record names one that was running.
        #
        # Note the shape -- ``owned or ...``. The old condition was ``owned``
        # alone, so this can only ever *add* a reason where there was none. No
        # case that reported one before reports nothing now.
        stopped = (
            owned
            or carry_ended is not None
            or expired is not None
            or running_run_id is not None
        )
        return Decision(
            state=EXECUTION_STATE_STOPPING if owned else EXECUTION_STATE_IDLE,
            ownership=ownership,
            target=expired,
            progress=progress,
            stop_reason=reason if stopped else None,
            reset_required=owned,
            clear_stale_marker=clear_marker,
            notes=(("the window closed before the target was met",) if ended else ()),
        )

    known = carried.run_id if carried is not None else target.plan_id
    if owned and running_run_id is not None and running_run_id != known:
        # A different run. The old dispatch is ended before the new intent starts,
        # rather than being mutated into it -- a direction change especially must
        # not be expressed as a parameter edit.
        #
        # Against the **run** identity. Against the publication identity this fired
        # every fifteen minutes for the whole of any owned campaign, because
        # ``plan_id`` churns with the horizon -- stopping and resetting a run that
        # nothing had replaced. It was unreachable only because ownership was.
        return Decision(
            state=EXECUTION_STATE_STOPPING,
            ownership=ownership,
            target=target,
            progress=progress,
            stop_reason=EXECUTION_STOP_PLAN_REPLACED,
            reset_required=True,
        )

    # The carried run's own deadline, which affirmation re-anchors, rather than
    # the admitted publication's -- that one is frozen at admission and would
    # expire under a run Stage A is still actively republishing. Carry-forward
    # already ends a genuinely stale run, so for a carried run this is a backstop.
    stale = carried.stale_at(now) if carried is not None else target.stale_at(now)
    if stale:
        return Decision(
            state=EXECUTION_STATE_STOPPING if owned else EXECUTION_STATE_INHIBITED,
            ownership=ownership,
            target=target,
            progress=progress,
            stop_reason=EXECUTION_STOP_STALE_PLAN,
            reset_required=owned,
            inhibit_reason=None if owned else "stale_target",
            clear_stale_marker=clear_marker,
            notes=("the target is older than its freshness deadline",),
        )

    if now >= target.window_end:
        return Decision(
            state=EXECUTION_STATE_STOPPING if owned else EXECUTION_STATE_IDLE,
            ownership=ownership,
            target=target,
            progress=progress,
            stop_reason=EXECUTION_STOP_WINDOW_ENDED,
            reset_required=owned,
        )

    demand = demand_for(
        target,
        now=now,
        progress=progress,
        current_energy_kwh=current_energy_kwh,
        remaining_expected_pv_kwh=remaining_expected_pv_kwh,
        grid_charged_kwh=grid_charged_kwh,
        configured_budget_kwh=configured_budget_kwh,
        charge_efficiency=charge_efficiency,
    )

    if demand.reduction == EXECUTION_REDUCTION_TARGET_MET:
        # Stop now, whatever the device's own timer still says. A dispatch left
        # armed because a countdown has not expired is how a target gets exceeded.
        return Decision(
            state=EXECUTION_STATE_STOPPING if owned else EXECUTION_STATE_IDLE,
            ownership=ownership,
            target=target,
            demand=demand,
            progress=progress,
            stop_reason=EXECUTION_STOP_TARGET_REACHED,
            reset_required=owned,
        )

    if demand.reduction == EXECUTION_REDUCTION_BUDGET:
        # All the grid energy this plan approved has been bought. Stop, and let
        # Stage A decide whether more is worth buying -- that is an economic
        # question and this layer does not answer them.
        return Decision(
            state=EXECUTION_STATE_STOPPING if owned else EXECUTION_STATE_IDLE,
            ownership=ownership,
            target=target,
            demand=demand,
            progress=progress,
            stop_reason=EXECUTION_STOP_GRID_CEILING,
            reset_required=owned,
            notes=(
                "the approved grid energy for this run has been bought; awaiting a "
                "fresh economic decision rather than making one",
            ),
        )

    if demand.finished:
        # Reduced all the way to nothing by the headroom cap: hold, and let Stage A
        # decide whether the remaining energy is still worth buying.
        #
        # Its own reason since beta.24. It reported ``target_reached``, which stops
        # and resets identically but tells a reader the plan was met when in fact
        # the pack ran out of room -- and on a sunny capped run that is the
        # difference between "bought what was asked" and "stopped short".
        return Decision(
            state=EXECUTION_STATE_STOPPING if owned else EXECUTION_STATE_IDLE,
            ownership=ownership,
            target=target,
            demand=demand,
            progress=progress,
            stop_reason=EXECUTION_STOP_HEADROOM_REACHED,
            reset_required=owned,
            notes=(
                "reduced to zero by the Stage-A headroom constraint; awaiting a "
                "fresh economic decision rather than making one",
            ),
        )

    if not target.activatable_at(now):
        # The window has not opened. Everything is computed and published, and
        # nothing may be sent -- because on this hardware activation delivers
        # energy immediately, so "ready" and "go" cannot be the same state.
        return Decision(
            state=EXECUTION_STATE_PREPARED,
            ownership=ownership,
            target=target,
            demand=demand,
            progress=progress,
            request_kw=0.0,
            inhibit_reason=inhibit_reason,
            clear_stale_marker=clear_marker,
            notes=("the window has not opened; prepared but not activated",),
        )

    return Decision(
        state=EXECUTION_STATE_RUNNING if owned else EXECUTION_STATE_ARMED,
        ownership=ownership,
        target=target,
        demand=demand,
        progress=progress,
        request_kw=demand.required_kw,
        inhibit_reason=inhibit_reason,
        clear_stale_marker=clear_marker,
    )


def recovered(decision: Decision) -> Decision:
    """Return ``decision`` marked as a post-restart reconstruction.

    A restart must never replay a target from the beginning. Progress is rebuilt
    from persisted evidence, so what is left is what is left -- but the fact that
    it was reconstructed rather than watched is worth carrying, because a reader
    weighing a deviation should know which it is.
    """
    return replace(
        decision, notes=(*decision.notes, "progress reconstructed after a restart")
    )


def control_intent_for(
    decision: Decision,
    *,
    floor_soc_percent: float,
    ceiling_soc_percent: float | None,
    horizon_minutes: int,
    target_day: date,
    start_index: int,
    built_at: datetime,
) -> ControlIntent | None:
    """Return the command Stage B wants, or ``None`` to leave the path alone.

    **This is the wire beta.19 was missing.** Stage B computed a power that reached
    no actuator: the command was built from the Phase-3 reserve-guard plan, which
    never charges, so flipping the barrier would have armed a discharge and ignored
    the economic plan entirely.

    Three properties make the direction guarantee structural rather than careful:

    * the only action this function can return is ``ACTION_CHARGE``. There is no
      branch that produces a discharge and no parameter that could select one;
    * ``None`` for everything else -- ``serve_load`` keeps the existing
      reserve-guard behaviour untouched, ``net_export`` has no primitive, and a
      hold, a refusal or a prepared decision produce no command at all rather than
      the opposite one;
    * energy is expressed as an **unsigned** magnitude over the interval, which the
      device layer maps to ``CHARGE_FAMILY``. Direction is the family, never a
      sign. The raw dispatch surface takes signed power with the opposite
      convention and Alpha EMS never writes it.

    ``ceiling_soc_percent`` is carried through rather than derived here: a charge
    with no establishable ceiling must be refused, and the refusal belongs at the
    device boundary where the cutoff is actually computed.
    """
    if not decision.wants_command:
        return None
    target = decision.target
    if target is None or target.intent != EXECUTION_INTENT_GRID_CHARGE:
        return None
    if not target.activatable_at(built_at):
        # Belt and braces: ``wants_command`` already excludes ``prepared``, and
        # this says the same thing about the window a second time. A single guard
        # on the one path that can move energy early is a guard with a hole in it.
        return None
    power_kw = max(0.0, decision.request_kw)
    if power_kw <= 0.0:
        return None
    return ControlIntent(
        action=ACTION_CHARGE,
        energy_ac_kwh=power_kw * INTERVAL_HOURS,
        average_power_kw=power_kw,
        interval_hours=INTERVAL_HOURS,
        floor_soc_percent=floor_soc_percent,
        energy_limit_bound=False,
        horizon_minutes=horizon_minutes,
        target_day=target_day,
        start_index=start_index,
        built_at=built_at,
        reason=target.purpose,
        policy="stage_b",
        policy_version=1,
        ceiling_soc_percent=ceiling_soc_percent,
    )


def intent_targets_meter(intent: str) -> bool:
    """Return whether this intent's published target is a meter figure.

    Only ``net_export``. The distinction is the whole reason the contract carries
    two fields: a charge command is a battery figure and house load must not be
    added to it, while a net export must deliver the meter target *plus* whatever
    the house is taking at the time. Getting this backwards is how 1.3 kW of
    intended export becomes a 1.3 kW battery command and delivers 0.4.
    """
    return intent == EXECUTION_INTENT_NET_EXPORT


def battery_power_for_export_kw(
    *, grid_target_kw: float, house_load_kw: float
) -> float:
    """Return the battery power a net export needs, given the load beneath it.

    Measured on the live installation: 1.3 kW of intended export against 0.9 kW of
    house load needs 2.2 kW of battery. Recomputed every refresh, because the load
    moves and a figure fixed at the start of the run stops being true immediately.

    Shadow-only in beta.19: no primitive exists for export, and this is published
    so the arithmetic is reviewable before anything can act on it.
    """
    return max(0.0, grid_target_kw) + max(0.0, house_load_kw)


def serve_load_power_kw(*, house_load_kw: float, remaining_kw: float) -> float:
    """Return the discharge that covers the house without exporting.

    Load avoidance is bounded by the load: discharging past it sends energy across
    the meter, which is a different intent with different economics and is not this
    one. The export-safety bound applies on top, downstream.
    """
    return max(0.0, min(max(0.0, house_load_kw), max(0.0, remaining_kw)))


def as_dict(decision: Decision, *, mode: str, executed: bool) -> dict[str, Any]:
    """Return the diagnostics view of one decision.

    Bounded scalars only, and every Stage-A expectation sits beside what actually
    happened -- a deviation should be readable rather than something a reader has
    to compute. ``applied_kw`` is zero and ``executed`` false in this release,
    which is how the block says out loud that nothing was sent.
    """
    target = decision.target
    demand = decision.demand
    progress = decision.progress
    return {
        "mode": mode,
        "state": decision.state,
        # **Everything from here to ``target`` is frozen at admission.** These are
        # read off the publication Stage B accepted, which a carried run holds by
        # reference for its whole life -- so ``revision`` is the revision that
        # publication carried when it was admitted and does not track the live one,
        # and ``stale_after`` is the admission-time deadline rather than the live
        # one. A real snapshot showed 13 here beside 2 in ``carried.run``: both
        # correct, counting different things.
        "admitted_publication_rule": (
            "plan_id, revision, intent, purpose, window_start, window_end, "
            "issued_at, stale_after and target are the Stage-A publication this "
            "run was admitted from, frozen at admission and never re-read. "
            "revision counts how many times Stage A had materially revised that "
            "publication before Stage B accepted it. for the live figures see "
            "carried.publication for what Stage A says now, and carried.run for "
            "the run Stage B is executing -- carried.run.revision counts material "
            "changes since admission and carried.run.stale_after is the deadline "
            "re-anchored by every affirmation"
        ),
        "plan_id": None if target is None else target.plan_id,
        "revision": None if target is None else target.revision,
        "intent": None if target is None else target.intent,
        "purpose": None if target is None else target.purpose,
        "window_start": None if target is None else target.window_start.isoformat(),
        "window_end": None if target is None else target.window_end.isoformat(),
        "issued_at": (
            None
            if target is None or target.issued_at is None
            else target.issued_at.isoformat()
        ),
        "stale_after": (
            None
            if target is None or target.stale_after is None
            else target.stale_after.isoformat()
        ),
        "target": (
            None
            if target is None
            else {
                "battery_target_kwh": round(target.battery_target_kwh, 3),
                "grid_target_kwh": (
                    None
                    if target.grid_target_kwh is None
                    else round(target.grid_target_kwh, 3)
                ),
                "average_power_kw": round(target.average_power_kw, 3),
                "first_power_kw": round(target.first_power_kw, 3),
                "expected_pv_production_kwh": target.expected_pv_production_kwh,
                "expected_house_load_kwh": target.expected_house_load_kwh,
                "expected_pv_to_battery_kwh": target.expected_pv_to_battery_kwh,
                "expected_grid_to_battery_kwh": target.expected_grid_to_battery_kwh,
                "charge_source": target.charge_source,
                "required_headroom_kwh": target.required_headroom_kwh,
                "max_end_energy_kwh": target.max_end_energy_kwh,
                "headroom_until": (
                    None
                    if target.headroom_until is None
                    else target.headroom_until.isoformat()
                ),
                "headroom_constrained": target.constrained,
            }
        ),
        "progress": (
            None
            if progress is None
            else {
                "battery_realized_kwh": round(progress.realized_kwh, 3),
                "battery_realized_basis": progress.basis,
                "battery_realized_quality": progress.quality,
                "current_quarter_energy_kwh": (
                    None
                    if progress.current_quarter_kwh is None
                    else round(progress.current_quarter_kwh, 3)
                ),
                "accumulated_kwh": (
                    None
                    if progress.accumulated_kwh is None
                    else round(progress.accumulated_kwh, 3)
                ),
                "soc_delta_kwh": (
                    None
                    if progress.soc_delta_kwh is None
                    else round(progress.soc_delta_kwh, 3)
                ),
                "remaining_battery_kwh": (
                    None if demand is None else round(demand.remaining_kwh, 3)
                ),
                "remaining_minutes": (
                    None if demand is None else round(demand.remaining_minutes, 1)
                ),
                "ahead_or_behind_kwh": (
                    None if demand is None else round(demand.ahead_kwh, 3)
                ),
            }
        ),
        "power": (
            None
            if demand is None
            else {
                "stage_a_mean_kw": None if target is None else target.average_power_kw,
                "stage_a_first_kw": None if target is None else target.first_power_kw,
                "rolling_required_kw": round(demand.rolling_kw, 3),
                "headroom_ceiling_kw": (
                    None if demand.ceiling_kw is None else round(demand.ceiling_kw, 3)
                ),
                "requested_kw": round(decision.request_kw, 3),
                "projected_end_energy_kwh": (
                    None
                    if demand.projected_end_kwh is None
                    else round(demand.projected_end_kwh, 3)
                ),
                "projected_end_energy_rule": (
                    "projected stored energy in the pack at the end of the "
                    "window, DC, if the requested power is held: stored energy "
                    "now plus the energy still to deliver, converted from AC "
                    "exactly once. the remaining delivery already includes the "
                    "production Stage A expects to store, so expected production "
                    "is not added again. null means the conversion could not be "
                    "made, never zero. above the pack ceiling means the target "
                    "does not fit, which is information rather than a fault"
                ),
                "reduction_reason": demand.reduction,
                # **An attribution estimate, and the name says so.** There is no
                # physical grid-to-battery channel to meter: the meter sees one net
                # figure and cannot say which electron reached the pack. Production
                # is credited first and the grid second, which is the order Stage A
                # published the ceiling in, so the two are in the same terms.
                "grid_charged_kwh_estimate": (
                    None
                    if demand.grid_charged_kwh is None
                    else round(demand.grid_charged_kwh, 3)
                ),
                "grid_cap_kwh": (
                    None
                    if demand.grid_cap_kwh is None
                    else round(demand.grid_cap_kwh, 3)
                ),
                "grid_remaining_kwh": (
                    None
                    if demand.grid_cap_kwh is None
                    else round(
                        max(
                            0.0,
                            demand.grid_cap_kwh - (demand.grid_charged_kwh or 0.0),
                        ),
                        3,
                    )
                ),
                "grid_attribution_rule": (
                    "an estimate from the measured balance, not a meter reading. "
                    "grid_share = max(0, battery_charge - max(0, pv - house_load)), "
                    "integrated and monotonic by construction. a cap of null means "
                    "unconstrained, never zero"
                ),
                # Zero in this release, and it says so: the barrier is closed and
                # no command is reachable.
                "applied_kw": 0.0,
                "executed": executed,
            }
        ),
        "ownership": {
            "state": decision.ownership,
            "clear_stale_marker": decision.clear_stale_marker,
        },
        "result": {
            "stop_reason": decision.stop_reason,
            "reset_required": decision.reset_required,
            "inhibit_reason": decision.inhibit_reason,
        },
        "notes": list(decision.notes),
        "execution_scope": (
            "since beta.24 this is the one path that can execute, and only for a "
            "grid charge. in shadow it computes the command a live run would send "
            "and sends none, so applied_kw is zero; in live a charge may be sent "
            "and every other direction is refused twice over. the field was called "
            "controls_nothing while that was true of every release"
        ),
    }
