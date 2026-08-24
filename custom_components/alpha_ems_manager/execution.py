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

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from .const import (
    EXECUTION_BASIS_ACCUMULATED,
    EXECUTION_BASIS_BOTH,
    EXECUTION_BASIS_SOC_DELTA,
    EXECUTION_BASIS_UNAVAILABLE,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_HOLD,
    EXECUTION_INTENT_NET_EXPORT,
    EXECUTION_QUALITY_MEASURED,
    EXECUTION_QUALITY_PARTIAL,
    EXECUTION_QUALITY_RECONSTRUCTED,
    EXECUTION_QUALITY_UNAVAILABLE,
    EXECUTION_REDUCTION_HEADROOM,
    EXECUTION_REDUCTION_NONE,
    EXECUTION_REDUCTION_PV_AHEAD,
    EXECUTION_REDUCTION_TARGET_MET,
    EXECUTION_STATE_ARMED,
    EXECUTION_STATE_IDLE,
    EXECUTION_STATE_INHIBITED,
    EXECUTION_STATE_RUNNING,
    EXECUTION_STATE_STOPPING,
    EXECUTION_STATE_UNPROVEN,
    EXECUTION_STOP_PLAN_REPLACED,
    EXECUTION_STOP_STAGE_A_HOLD,
    EXECUTION_STOP_STALE_PLAN,
    EXECUTION_STOP_SWITCHED_OFF,
    EXECUTION_STOP_SWITCHED_TO_SHADOW,
    EXECUTION_STOP_TARGET_REACHED,
    EXECUTION_STOP_WINDOW_ENDED,
    OWNERSHIP_FOREIGN,
    OWNERSHIP_NONE,
    OWNERSHIP_OWNED,
    OWNERSHIP_UNPROVEN,
)

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


def _finite(value: Any) -> float | None:
    """Return ``value`` as a float when it is a usable number, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _instant(value: Any) -> datetime | None:
    """Return an ISO-8601 string as a datetime, or ``None`` if it is not one."""
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
    #: Which plan the caller is currently trying to execute, if any. A record for
    #: a different plan is contradictory rather than merely old.
    plan_id: str | None = None

    @property
    def record_present(self) -> bool:
        """Return whether a causal record exists at all."""
        return isinstance(self.record, dict) and bool(self.record)

    @property
    def record_matches(self) -> bool:
        """Return whether the record can be tied to the live dispatch.

        Requires the record to name a plan, that plan to be the one being
        executed, and -- when both instants are known -- the dispatch the inverter
        reports to be the one the record says was armed.

        A missing ``dispatch_start`` on the device is not treated as a match. The
        settle window after arming genuinely does not report one, and reading
        silence as agreement would grant ownership on the strength of nothing.
        """
        if not self.record_present:
            return False
        record = self.record or {}
        recorded_plan = record.get("plan_id")
        if not isinstance(recorded_plan, str) or not recorded_plan:
            return False
        if self.plan_id is not None and recorded_plan != self.plan_id:
            return False
        observed = _instant(record.get("dispatch_start"))
        if observed is None or self.dispatch_start is None:
            return False
        delta = abs((self.dispatch_start - observed).total_seconds())
        return delta <= OWNERSHIP_START_TOLERANCE_SECONDS


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
        """Return whether this run is the one to be arming for at ``moment``.

        Two conditions: the window has not ended, and it starts now or within one
        planning interval. The second is what makes this work at all -- the
        economic horizon begins at the *next* boundary, so a run planned at 09:00
        opens at 09:15, and a dispatch armed at 09:00 is what carries it out.

        Strict containment would have been the obvious test and would have
        selected nothing, ever.
        """
        if moment >= self.window_end:
            return False
        ahead = (self.window_start - moment).total_seconds() / 60.0
        return ahead <= lead_minutes

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
    opens = _instant(raw.get("window_start"))
    closes = _instant(raw.get("window_end"))
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
        issued_at=_instant(raw.get("issued_at")),
        stale_after=_instant(raw.get("stale_after")),
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
        headroom_until=_instant(raw.get("headroom_until")),
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
    remaining_expected_pv_kwh: float,
    remaining_minutes: float,
) -> float | None:
    """Return the cap the Stage-A headroom constraint puts on active charging.

    ``None`` when Stage A published no constraint. **Absent means unconstrained**,
    which is not the same as a cap of zero -- reading it that way would forbid the
    pack from filling at all, which is the opposite of the intent.

    The arithmetic is deliberately dull, because every judgement in it was made by
    Stage A:

        allowance = max_end_energy - current_energy - remaining_expected_pv
        cap       = allowance / remaining_hours

    Stage A decided how much stored energy this plan should land on, knowing what
    production was forecast afterwards. So the room still owed to production is
    what is left once that landing figure has accounted for what is already in the
    pack and what production is still expected to put there. Charge into the
    remainder and no further.

    If production is running ahead of the forecast Stage A used, the allowance
    shrinks and so does the cap -- reaching zero, which means stop. Nothing here
    consults a price, an export window, or what any of it is worth.
    """
    if target.max_end_energy_kwh is None:
        return None
    if remaining_minutes <= 0.0:
        return 0.0
    allowance = (
        target.max_end_energy_kwh
        - current_energy_kwh
        - max(0.0, remaining_expected_pv_kwh)
    )
    if allowance <= 0.0:
        return 0.0
    return allowance / (remaining_minutes / 60.0)


def demand_for(
    target: Target,
    *,
    now: datetime,
    progress: Progress,
    current_energy_kwh: float | None = None,
    remaining_expected_pv_kwh: float | None = None,
) -> Demand:
    """Return what Stage B would ask for this refresh.

    Order matters and is contractual: the rolling controller first, honestly, then
    the headroom cap on top of it. Doing it the other way round would let a
    constraint that exists to *limit* charging decide how much charging to do.
    """
    remaining_kwh = max(0.0, target.battery_target_kwh - progress.realized_kwh)
    remaining_minutes = max(0.0, (target.window_end - now).total_seconds() / 60.0)
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
        still_expected = (
            0.0
            if remaining_expected_pv_kwh is None
            else max(0.0, remaining_expected_pv_kwh)
        )
        ceiling_kw = headroom_ceiling_kw(
            target,
            current_energy_kwh=stored,
            remaining_expected_pv_kwh=still_expected,
            remaining_minutes=remaining_minutes,
        )
        charge_kw = rolling_kw if ceiling_kw is None else min(rolling_kw, ceiling_kw)
        projected = (
            stored + still_expected + max(0.0, charge_kw) * (remaining_minutes / 60.0)
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
    )


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
    stop_reason: str | None = None
    reset_required: bool = False
    clear_stale_marker: bool = False
    inhibit_reason: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def wants_command(self) -> bool:
        """Return whether a physical request exists this refresh."""
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
    running_plan_id: str | None = None,
    inhibit_reason: str | None = None,
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
            stop_reason=EXECUTION_STOP_SWITCHED_OFF if owned else None,
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

    target = actionable_target(targets, now)

    if target is None:
        # Distinguish a window that ran out from a plan that was withdrawn. Both
        # stop, and both reset -- but the first has a shortfall to report and the
        # second does not, and reporting the wrong one sends a reader looking for a
        # fault that is not there.
        expired = (
            None
            if running_plan_id is None
            else target_by_plan_id(targets, running_plan_id)
        )
        ended = expired is not None and now >= expired.window_end
        return Decision(
            state=EXECUTION_STATE_STOPPING if owned else EXECUTION_STATE_IDLE,
            ownership=ownership,
            target=expired,
            progress=progress,
            stop_reason=(
                (EXECUTION_STOP_WINDOW_ENDED if ended else EXECUTION_STOP_STAGE_A_HOLD)
                if owned
                else None
            ),
            reset_required=owned,
            clear_stale_marker=clear_marker,
            notes=(("the window closed before the target was met",) if ended else ()),
        )

    if owned and running_plan_id is not None and running_plan_id != target.plan_id:
        # A different run. The old dispatch is ended before the new intent starts,
        # rather than being mutated into it -- a direction change especially must
        # not be expressed as a parameter edit.
        return Decision(
            state=EXECUTION_STATE_STOPPING,
            ownership=ownership,
            target=target,
            progress=progress,
            stop_reason=EXECUTION_STOP_PLAN_REPLACED,
            reset_required=True,
        )

    if target.stale_at(now):
        return Decision(
            state=EXECUTION_STATE_STOPPING if owned else EXECUTION_STATE_INHIBITED,
            ownership=ownership,
            target=target,
            progress=progress,
            stop_reason=EXECUTION_STOP_STALE_PLAN if owned else None,
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
            stop_reason=EXECUTION_STOP_WINDOW_ENDED if owned else None,
            reset_required=owned,
        )

    demand = demand_for(
        target,
        now=now,
        progress=progress,
        current_energy_kwh=current_energy_kwh,
        remaining_expected_pv_kwh=remaining_expected_pv_kwh,
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
            stop_reason=EXECUTION_STOP_TARGET_REACHED if owned else None,
            reset_required=owned,
        )

    if demand.finished:
        # Reduced all the way to nothing by the headroom cap: hold, and let Stage A
        # decide whether the remaining energy is still worth buying.
        return Decision(
            state=EXECUTION_STATE_STOPPING if owned else EXECUTION_STATE_IDLE,
            ownership=ownership,
            target=target,
            demand=demand,
            progress=progress,
            stop_reason=EXECUTION_STOP_TARGET_REACHED if owned else None,
            reset_required=owned,
            notes=(
                "reduced to zero by the Stage-A headroom constraint; awaiting a "
                "fresh economic decision rather than making one",
            ),
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
                "reduction_reason": demand.reduction,
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
        "controls_nothing": (
            "Stage B computes the command a Live run would send and sends none. "
            "the execution barrier is closed, applied_kw is always zero, and no "
            "service call is reachable from this path in this release"
        ),
    }
