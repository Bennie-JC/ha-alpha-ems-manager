"""How much stored energy the battery must hold, and why. **It decides nothing.**

Phase 7 answers one physical question: *how much usable battery energy must be
present at each future point so that the battery never runs short of the net
demand it is physically capable of serving?* It computes a requirement, reports a
shortfall, and acts on neither. Nothing in this module charges, discharges,
commands, or consults a price.

Why the floor is not the answer
------------------------------

The literal question "what is the minimum energy such that the pack stays above
the hard floor" has the answer *the floor*, always, for every interval:
:func:`battery.apply_request` clamps there and unserved demand spills to the
grid, so no starting energy can drive the pack below it. The floor is enforced,
not risked.

So the requirement is defined against the thing that is actually at stake, which
is the grid rather than the floor:

    ``required_reserve_kwh`` is the minimum DC energy present at the start of an
    interval such that, following the forecast sequence, the battery covers every
    unit of net demand it is physically capable of serving -- the only grid
    import being demand above the discharge power limit.

The recursion, and why it is this one
-------------------------------------

With ``s(k) = x(k) - y(k)`` the signed DC movement of interval ``k``, no shortage
means ``E(j) = E(i) - sum(s(k) for k in [i, j)) >= F`` for every later ``j``,
which holds exactly when ``E(i) >= F + max(0, max_j sum(...))``. That maximum
floored at zero is the standard backward recursion below:

    ``R[n] = F``
    ``R[i] = max(F, R[i+1] + x(i) - y(i))``

``max(F, ...)`` is a **documented backstop, not the safety mechanism**: the term
it guards is already non-negative by construction, exactly as
``battery._clamp_energy`` is a measured backstop rather than the thing that keeps
a trajectory inside its band.

The trajectory is **not monotone**, and that is correct rather than a defect: the
requirement is low while replenishment is imminent and high once it has passed.
An earlier draft of this phase published the running maximum of ``R`` instead --
the largest requirement anywhere in the horizon -- which is a different and wrong
quantity, because it demands that energy needed later already be present now and
so ignores every charging opportunity in between. It survives as
``peak_required_reserve_kwh``, a diagnostic, and ``test_reserve_mutations``
pins the substitution as a caught mutation.

The one assumption this module makes, stated once
------------------------------------------------

**Forecast surplus may offset accumulated deficit in the same window, up to that
deficit and no further.** It is capped by the inverter's charge power, converted
at the charge boundary, never accumulates as stored energy, and never carries
across a zero.

It is arithmetically equivalent to assuming forecast surplus becomes usable
future battery energy, and it is a *forecast* rather than an observation: it does
not prove the real inverter will store that surplus. It replaced an earlier
"same-interval netting only" rule because without it ``R[i]`` degenerates to
``F + `` all remaining net demand to the end of the forecast -- a figure that
grows if the forecast horizon lengthens, and therefore a property of the forecast
rather than of the battery. That superseded definition is still computed every
refresh, as ``required_same_interval_only_kwh``, so the cost of the relaxation is
measured rather than argued.

Blind by construction
---------------------

:func:`build_reserve` takes limits, a floor energy and a sequence of demands.
That is the whole of its input. It cannot see prices, the control mode, the
dispatch state, ``pv_absorption.modelled``, Excess Export or Peak Shaving, so an
identical physical forecast always produces an identical requirement -- which is
the property the live validation showed matters: absorption flipped on the
reference installation because a dispatch flag changed, while the underlying
forecast did not move at all.

No second physics
-----------------

Every conversion and every power limit comes from
:func:`battery.apply_request`. This module divides by no efficiency, multiplies
by none, converts no percentage, and defines no interval duration -- it reads the
DC delta the clamp produced. The AC/DC direction is therefore not merely tested
but unrepresentable.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from .battery import (
    INTERVAL_HOURS,
    BatteryLimits,
    BatteryRequest,
    BatteryState,
    apply_request,
    build_state,
    static_reserve,
)
from .const import (
    BATTERY_KWH_PRECISION,
    BATTERY_SOC_PRECISION,
    CONSTRAINT_MAX_CHARGE_POWER,
    CONSTRAINT_MAX_DISCHARGE_POWER,
    RESERVE_BOUND_HEADROOM,
    RESERVE_BOUND_TRUNCATED,
    RESERVE_BOUND_TRUNCATED_HEADROOM,
    RESERVE_FINGERPRINT_CHARS,
    RESERVE_HORIZON_CLOSED,
    RESERVE_HORIZON_TRUNCATED,
    RESERVE_MODEL_VERSION,
    RESERVE_REPLENISHMENT_ASSUMPTION,
    RESERVE_UNAVAILABLE_FORECAST,
    RESERVE_UNAVAILABLE_HORIZON_INCOMPLETE,
    RESERVE_UNAVAILABLE_LIMITS,
)
from .simulation import IntervalDemand


def _round_kwh(value: float | None) -> float | None:
    """Round an energy for reporting, preserving ``None``."""
    return None if value is None else round(value, BATTERY_KWH_PRECISION)


def _round_soc(value: float | None) -> float | None:
    """Round a state of charge for reporting, preserving ``None``."""
    return None if value is None else round(value, BATTERY_SOC_PRECISION)


# -- the per-interval record -------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReserveInterval:
    """What one interval contributes to the requirement, and what bound it.

    ``required_dc_kwh`` is ``None`` when the recursion could not reach this
    interval, which happens for every interval at or below an unforecast one. It
    is emphatically not the floor: "no answer" and "the floor is enough" are
    different facts, and collapsing them would publish a reassuring number
    derived from a forecast that does not exist.
    """

    index: int
    #: ``F + M[i]``, DC. ``None`` where the horizon was incomplete.
    required_dc_kwh: float | None
    #: AC energy the battery could actually deliver this interval.
    servable_ac_kwh: float
    #: AC demand above what the battery could deliver. Grid demand, whatever the
    #: state of charge, so it raises no requirement. See ``unserved`` in the
    #: module tests for why this cannot understate a later interval.
    unserved_ac_kwh: float
    #: AC surplus credited against accumulated deficit. Zero when the interval
    #: carried no production forecast, and zero in the counterfactuals.
    credited_ac_kwh: float
    #: How far this interval's requirement exceeds the whole pack, DC. Positive
    #: means no starting energy could satisfy it, so any lower figure published
    #: for an earlier interval -- reduced by credit expected in between -- is an
    #: understatement rather than an answer.
    headroom_excess_dc_kwh: float
    #: Which limits bound this interval, from the clamp. Empty when none did.
    constraints: tuple[str, ...] = ()


# -- the projection ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReserveProjection:
    """One refresh's reserve requirement over the forecast horizon.

    Stores the per-interval records and nothing derived, following the same
    discipline as :class:`simulation.SimulatedTrajectory`: a stored summary is a
    second source of truth, and the first time it disagreed with the data beside
    it, it is the stored one that would be believed.

    Never written to disk in this shape. The evidence layer persists scalars and
    fingerprints, because the trajectory is recomputable from the load snapshot,
    the production snapshot and the battery-configuration fingerprint -- the same
    reasoning that keeps ``BatteryPlan`` off the disk.
    """

    floor_energy_kwh: float
    ceiling_energy_kwh: float
    limits: BatteryLimits
    demands: tuple[IntervalDemand, ...]
    intervals: tuple[ReserveInterval, ...]
    #: ``RESERVE_HORIZON_CLOSED`` or ``RESERVE_HORIZON_TRUNCATED``.
    horizon_basis: str
    #: Whether surplus was credited at all. ``False`` for both counterfactuals.
    credited_surplus: bool
    available: bool
    unavailable_reason: str | None = None

    # -- what the entity reads -------------------------------------------

    @property
    def required_now_dc_kwh(self) -> float | None:
        """Return the requirement at the first interval of the horizon.

        The horizon starts at the next whole interval, so this is the requirement
        from the next boundary rather than from this instant. The partial interval
        in progress is deliberately excluded, exactly as the plan's trajectory
        excludes it, so no interval ever needs a different duration.
        """
        if not self.intervals:
            return None
        return self.intervals[0].required_dc_kwh

    @property
    def required_now_soc_percent(self) -> float | None:
        """Return the requirement as a state of charge. Derived, never stored."""
        energy = self.required_now_dc_kwh
        if energy is None:
            return None
        return self.limits.soc_for_energy(energy)

    @property
    def reachable(self) -> bool | None:
        """Return whether the pack could hold the requirement at all.

        ``False`` means no amount of charging would satisfy it -- a different fact
        from "the battery does not have it now", which is the shortfall, and a
        different fact again from :attr:`headroom_bound`, which asks whether some
        *later* requirement exceeds the pack. All three are reported, under names
        that say which is which.
        """
        energy = self.required_now_dc_kwh
        if energy is None:
            return None
        return energy <= self.ceiling_energy_kwh

    # -- bounds and honesty ----------------------------------------------

    @property
    def headroom_bound(self) -> bool:
        """Return whether capacity makes the published requirement understate.

        True when the requirement *somewhere* in the horizon exceeds the whole
        pack. That is the only way a bounded capacity can make this figure
        optimistic, and the proof is short: the recursion is exact under a
        ceiling as long as every intermediate requirement fits, because clipping
        a full pack only ever discards credit that was not needed. What it cannot
        survive is an intermediate requirement larger than the pack -- later
        credit then pulls the published figure back down and erases an
        infeasibility that no starting energy could have fixed.

        An earlier draft tested the cumulative excursion against usable capacity
        instead, and flagged a correct answer: thirty kilowatt-hours of surplus
        followed by fifteen of demand needs only the floor, delivers zero grid
        import from it, and was still labelled a lower bound.
        ``test_reserve_mutations`` pins both halves.
        """
        peak = self.peak_required_reserve_kwh
        return peak is not None and peak > self.ceiling_energy_kwh

    @property
    def headroom_bound_intervals(self) -> int:
        """Return how many intervals were headroom-limited anywhere."""
        return sum(1 for entry in self.intervals if entry.headroom_excess_dc_kwh > 0.0)

    @property
    def surplus_beyond_headroom_kwh(self) -> float:
        """Return how far the worst requirement exceeds the pack, DC."""
        excess = [entry.headroom_excess_dc_kwh for entry in self.intervals]
        return max(excess) if excess else 0.0

    @property
    def lower_bound_reason(self) -> str | None:
        """Return why the published requirement is a lower bound, or ``None``.

        Two independent causes, and a caller needs to know which. A truncated
        horizon understates because demand continues past the last interval
        anybody forecast. A headroom-limited projection understates because
        credit was counted that the pack could not have held.
        """
        truncated = self.horizon_basis == RESERVE_HORIZON_TRUNCATED
        headroom = self.headroom_bound
        if truncated and headroom:
            return RESERVE_BOUND_TRUNCATED_HEADROOM
        if truncated:
            return RESERVE_BOUND_TRUNCATED
        if headroom:
            return RESERVE_BOUND_HEADROOM
        return None

    # -- shape ------------------------------------------------------------

    @property
    def intervals_evaluated(self) -> int:
        """Return how many intervals the recursion actually answered."""
        return sum(1 for entry in self.intervals if entry.required_dc_kwh is not None)

    @property
    def intervals_unknown(self) -> int:
        """Return how many intervals the recursion could not reach."""
        return sum(1 for entry in self.intervals if entry.required_dc_kwh is None)

    @property
    def pv_blind_intervals(self) -> int:
        """Return how many intervals carried no production forecast.

        Such an interval is netted against nothing and credits nothing, which
        raises the requirement. Conservative, and declared rather than silent.
        """
        return sum(1 for demand in self.demands if not demand.pv_aware)

    @property
    def peak_required_reserve_kwh(self) -> float | None:
        """Return the largest requirement anywhere in the horizon.

        **Not a reserve.** Holding this now would reserve energy that intervening
        replenishment is expected to supply. It is published because it is the
        peak the pack will be asked to hold, which a later economic phase needs;
        it is never the current requirement.
        """
        values = [
            entry.required_dc_kwh
            for entry in self.intervals
            if entry.required_dc_kwh is not None
        ]
        return max(values) if values else None

    @property
    def peak_required_at(self) -> int | None:
        """Return the interval index at which the peak is first reached."""
        peak = self.peak_required_reserve_kwh
        if peak is None:
            return None
        for entry in self.intervals:
            if entry.required_dc_kwh is not None and entry.required_dc_kwh >= peak:
                return entry.index
        return None

    # -- totals ------------------------------------------------------------

    @property
    def net_demand_ac_kwh(self) -> float:
        """Return the net demand across the horizon, AC."""
        return sum(
            demand.net_demand_kwh
            for demand in self.demands
            if demand.net_demand_kwh is not None
        )

    @property
    def servable_ac_kwh(self) -> float:
        """Return the net demand the battery could have served, AC."""
        return sum(entry.servable_ac_kwh for entry in self.intervals)

    @property
    def demand_beyond_discharge_power_kwh(self) -> float:
        """Return demand the battery could not have served in time, AC."""
        return sum(entry.unserved_ac_kwh for entry in self.intervals)

    @property
    def surplus_ac_kwh(self) -> float:
        """Return the forecast surplus across the horizon, AC."""
        return sum(demand.surplus_kwh for demand in self.demands)

    @property
    def credited_ac_kwh(self) -> float:
        """Return the surplus actually credited against deficit, AC."""
        return sum(entry.credited_ac_kwh for entry in self.intervals)

    @property
    def surplus_beyond_charge_power_kwh(self) -> float:
        """Return surplus that exceeded the inverter's charge power, AC.

        Only the intervals the clamp actually reduced contribute, so an interval
        whose surplus simply was not credited -- either counterfactual, or a
        PV-blind interval -- adds nothing here.
        """
        return sum(
            max(0.0, demand.surplus_kwh - entry.credited_ac_kwh)
            for demand, entry in zip(self.demands, self.intervals, strict=True)
            if CONSTRAINT_MAX_CHARGE_POWER in entry.constraints
        )

    @property
    def constraint_counts(self) -> dict[str, int]:
        """Return how many intervals each limit bound. Bounded key space."""
        counts = {
            CONSTRAINT_MAX_DISCHARGE_POWER: 0,
            CONSTRAINT_MAX_CHARGE_POWER: 0,
        }
        for entry in self.intervals:
            for name in entry.constraints:
                if name in counts:
                    counts[name] += 1
        return counts


# -- the synthetic states the clamp is asked about ---------------------------


def _probe_states(limits: BatteryLimits) -> tuple[BatteryState, BatteryState] | None:
    """Return a full and an empty pack, for asking the clamp about one interval.

    Both carry a zero reserve, so the energy window is the whole pack and only a
    *power* limit can bind -- which is the point: these exist to ask "what could
    the inverter move in a quarter of an hour", not to model a real state.

    A zero floor here cannot leak into the requirement. The requirement's floor
    is the caller's ``floor_energy_kwh``, and these two states never reach it.
    """
    zero = static_reserve(0.0)
    full = build_state(soc_percent=limits.max_soc_percent, limits=limits, reserve=zero)
    empty = build_state(soc_percent=0.0, limits=limits, reserve=zero)
    if full is None or empty is None:  # pragma: no cover - build_limits precludes it
        return None
    return full, empty


def _withdrawal(
    full: BatteryState, power_kw: float
) -> tuple[float, float, tuple[str, ...]]:
    """Return the DC cost, AC energy served and constraints for one discharge.

    The DC figure is read as the difference the clamp produced rather than
    computed here, so the AC-to-DC crossing happens exactly once, in the one
    place it is implemented, and in the one direction it is written down.
    """
    outcome = apply_request(full, BatteryRequest.discharge(power_kw))
    return (
        full.energy_kwh - outcome.end_energy_kwh,
        outcome.discharge_ac_kwh,
        outcome.constraints,
    )


def _credit(
    empty: BatteryState, power_kw: float
) -> tuple[float, float, tuple[str, ...]]:
    """Return the DC credit, AC energy absorbed and constraints for one charge.

    Converted at the **charge** boundary, which is the conservative direction: a
    surplus expressed through the discharge boundary would credit ``S / eta``
    rather than ``S * eta``, roughly eleven per cent more, and every unit of
    over-credit lowers a safety figure.
    """
    outcome = apply_request(empty, BatteryRequest.charge(power_kw))
    return (
        outcome.end_energy_kwh - empty.energy_kwh,
        outcome.charge_ac_kwh,
        outcome.constraints,
    )


# -- the walk ----------------------------------------------------------------


def _build(
    *,
    limits: BatteryLimits,
    floor_energy_kwh: float,
    demands: Sequence[IntervalDemand],
    credit_surplus: bool,
) -> ReserveProjection:
    """Walk the horizon backwards once. Pure, total, and it never raises."""
    ceiling = limits.energy_for_soc(limits.max_soc_percent)

    probes = _probe_states(limits)
    if probes is None:  # pragma: no cover - build_limits precludes it
        return ReserveProjection(
            floor_energy_kwh=floor_energy_kwh,
            ceiling_energy_kwh=ceiling,
            limits=limits,
            demands=tuple(demands),
            intervals=(),
            horizon_basis=RESERVE_HORIZON_TRUNCATED,
            credited_surplus=credit_surplus,
            available=False,
            unavailable_reason=RESERVE_UNAVAILABLE_LIMITS,
        )
    full, empty = probes

    if not demands:
        return ReserveProjection(
            floor_energy_kwh=floor_energy_kwh,
            ceiling_energy_kwh=ceiling,
            limits=limits,
            demands=(),
            intervals=(),
            horizon_basis=RESERVE_HORIZON_TRUNCATED,
            credited_surplus=credit_surplus,
            available=False,
            unavailable_reason=RESERVE_UNAVAILABLE_FORECAST,
        )

    total = len(demands)
    required: list[float | None] = [None] * total
    servable: list[float] = [0.0] * total
    unserved: list[float] = [0.0] * total
    credited: list[float] = [0.0] * total
    excess: list[float] = [0.0] * total
    constraints: list[tuple[str, ...]] = [()] * total

    # ``deficit`` is M[i+1]: the deepest deficit of any window starting after
    # this interval, floored at zero so credit can undo demand but never bank.
    deficit = 0.0
    last_deficit: float | None = None

    for position in range(total - 1, -1, -1):
        demand = demands[position]
        net = demand.net_demand_kwh
        if net is None:
            # No predicted load, so no honest requirement for this interval or for
            # any earlier one: an unforecast interval is not an interval of no
            # demand, and bridging it would be the fabrication this project
            # exists to avoid.
            break

        power = demand.power_kw or 0.0
        withdrawal_dc, served_ac, discharge_constraints = _withdrawal(full, power)
        servable[position] = served_ac
        unserved[position] = max(0.0, net - served_ac)

        credit_dc = 0.0
        charge_constraints: tuple[str, ...] = ()
        if credit_surplus and demand.surplus_kwh > 0.0:
            credit_dc, credited_ac, charge_constraints = _credit(
                empty, demand.surplus_kwh / INTERVAL_HOURS
            )
            credited[position] = credited_ac

        constraints[position] = tuple(discharge_constraints) + tuple(charge_constraints)

        signed = withdrawal_dc - credit_dc
        deficit = max(0.0, signed + deficit)
        required[position] = floor_energy_kwh + deficit
        excess[position] = max(0.0, required[position] - ceiling)
        if position == total - 1:
            last_deficit = deficit

    intervals = tuple(
        ReserveInterval(
            index=demands[position].index,
            required_dc_kwh=required[position],
            servable_ac_kwh=servable[position],
            unserved_ac_kwh=unserved[position],
            credited_ac_kwh=credited[position],
            headroom_excess_dc_kwh=excess[position],
            constraints=constraints[position],
        )
        for position in range(total)
    )

    # Closed when the last interval carried no residual deficit, which means the
    # final drawdown window ended inside the horizon. Otherwise the window is cut
    # off and every requirement in it is a lower bound. ``None`` means the last
    # interval was never reached, so there is nothing to call closed.
    basis = (
        RESERVE_HORIZON_CLOSED
        if last_deficit is not None and last_deficit <= 0.0
        else RESERVE_HORIZON_TRUNCATED
    )
    answered = required[0] is not None
    return ReserveProjection(
        floor_energy_kwh=floor_energy_kwh,
        ceiling_energy_kwh=ceiling,
        limits=limits,
        demands=tuple(demands),
        intervals=intervals,
        horizon_basis=basis,
        credited_surplus=credit_surplus,
        available=answered,
        unavailable_reason=(
            None if answered else RESERVE_UNAVAILABLE_HORIZON_INCOMPLETE
        ),
    )


def build_reserve(
    *,
    limits: BatteryLimits,
    floor_energy_kwh: float,
    demands: Sequence[IntervalDemand],
) -> ReserveProjection:
    """Return the authoritative requirement. **The only authoritative figure.**

    Three arguments, and that is the whole input. It cannot see a price, a
    control mode, a dispatch state, an absorption flag, Excess Export or Peak
    Shaving, so an identical physical forecast always yields an identical
    requirement. ``test_phase_seven_boundaries`` asserts this parameter set
    exactly, so widening it is a visible decision rather than an easy one.
    """
    return _build(
        limits=limits,
        floor_energy_kwh=floor_energy_kwh,
        demands=demands,
        credit_surplus=True,
    )


def build_reserve_same_interval_only(
    *,
    limits: BatteryLimits,
    floor_energy_kwh: float,
    demands: Sequence[IntervalDemand],
) -> ReserveProjection:
    """Return the superseded definition, as a diagnostic counterfactual.

    Production still reduces simultaneous demand -- the netting is untouched --
    but forecast surplus credits nothing across intervals. This is the
    requirement *if forecast surplus cannot replenish the battery*, and it is
    published so the cost of the relaxation that replaced it is measured rather
    than argued.

    **Phase 7 never substitutes this for the authoritative figure**, for any
    reason, including a live absorption flag. A later policy phase may use it as
    evidence when deciding how conservatively to act.
    """
    return _build(
        limits=limits,
        floor_energy_kwh=floor_energy_kwh,
        demands=demands,
        credit_surplus=False,
    )


def build_reserve_pv_blind(
    *,
    limits: BatteryLimits,
    floor_energy_kwh: float,
    demands: Sequence[IntervalDemand],
) -> ReserveProjection:
    """Return the requirement with no production at all: the upper bracket.

    Not a forecast of darkness -- a counterfactual that answers "how much of this
    reserve do I owe to the sun", in one subtraction.
    """
    blind = tuple(
        IntervalDemand(
            index=demand.index,
            baseline_kwh=demand.baseline_kwh,
            filled=demand.filled,
            pv_kwh=None,
        )
        for demand in demands
    )
    return _build(
        limits=limits,
        floor_energy_kwh=floor_energy_kwh,
        demands=blind,
        credit_surplus=False,
    )


# -- comparison against the present, and against the projection -------------


def shortfall(
    projection: ReserveProjection, state: BatteryState | None
) -> dict[str, Any]:
    """Return what the pack has against what it needs. The only state-aware part.

    Measured against stored DC energy rather than energy above the floor: the
    requirement already includes the floor, so subtracting it twice would report
    a shortfall the size of the floor on a satisfied battery.
    """
    required = projection.required_now_dc_kwh
    if required is None or state is None:
        return {
            "required_reserve_kwh": None,
            "stored_energy_kwh": (
                None if state is None else _round_kwh(state.energy_kwh)
            ),
            "reserve_shortfall_kwh": None,
            "margin_to_reserve_kwh": None,
            "reserve_met": None,
        }
    return {
        "required_reserve_kwh": _round_kwh(required),
        "stored_energy_kwh": _round_kwh(state.energy_kwh),
        "reserve_shortfall_kwh": _round_kwh(max(0.0, required - state.energy_kwh)),
        "margin_to_reserve_kwh": _round_kwh(max(0.0, state.energy_kwh - required)),
        "reserve_met": state.energy_kwh >= required,
    }


def compare_to_trajectory(
    projection: ReserveProjection, trajectory: Any
) -> dict[str, Any] | None:
    """Return where the projected trajectory would fall below the requirement.

    Read-only in both directions. The requirement is computed from limits and
    demands alone and never from a trajectory, and this comparison never feeds
    back into one -- so the thing being validated cannot move because it was
    validated. That dependency direction is the whole reason the requirement is
    state-independent.

    A violation is expected rather than alarming in this release: the shipped
    policy discharges to the *configured* floor, which is below the requirement
    whenever the requirement is above it. Counting them is the evidence a later
    phase needs, not a fault report.
    """
    if trajectory is None or not projection.intervals:
        return None
    first: int | None = None
    minimum: float | None = None
    violations = 0
    energy = trajectory.start_energy_kwh
    for position, entry in enumerate(projection.intervals):
        if position >= len(trajectory.outcomes):
            break
        required = entry.required_dc_kwh
        if required is not None:
            margin = energy - required
            if minimum is None or margin < minimum:
                minimum = margin
            if margin < 0.0:
                violations += 1
                if first is None:
                    first = entry.index
        energy = trajectory.outcomes[position].end_energy_kwh
    return {
        "violation_expected": first is not None,
        "first_violation_interval": first,
        "violation_intervals": violations,
        "minimum_margin_to_reserve_kwh": _round_kwh(minimum),
        "basis": (
            "the shipped policy discharges to the configured floor, which is "
            "below the calculated requirement whenever the requirement is above "
            "it -- so a violation here is the evidence a later phase needs "
            "rather than a fault. Nothing in this release enforces the reserve"
        ),
    }


# -- fingerprints -------------------------------------------------------------


def _digest(payload: Any) -> str:
    """Return a short stable digest of a canonical JSON payload."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[
        :RESERVE_FINGERPRINT_CHARS
    ]


def fingerprint_battery_config(
    *,
    capacity_kwh: float | None,
    min_soc_percent: float | None,
    max_charge_kw: float | None,
    max_discharge_kw: float | None,
    round_trip_efficiency_percent: float | None,
    max_soc_percent: float,
) -> str:
    """Return a digest of the battery configuration a requirement rests on.

    The one fact no existing store holds. A load forecast and a production
    forecast are both persisted, so a past requirement is recomputable from them
    -- but only if the battery it was computed for is known, and configuration
    lives in the config entry, which keeps no history. Without this a user
    changing their capacity or their floor makes every earlier belief
    unrecoverable, which is exactly the hindsight bias the evidence layer exists
    to prevent.
    """
    return _digest(
        {
            "capacity_kwh": capacity_kwh,
            "min_soc_percent": min_soc_percent,
            "max_charge_kw": max_charge_kw,
            "max_discharge_kw": max_discharge_kw,
            "round_trip_efficiency_percent": round_trip_efficiency_percent,
            "max_soc_percent": max_soc_percent,
        }
    )


def fingerprint_reserve(
    projection: ReserveProjection,
    *,
    config_fingerprint: str,
    load_fingerprint: str | None,
    pv_fingerprint: str | None,
) -> str:
    """Return the digest that decides whether to store a snapshot.

    **Keyed on the inputs, not on the answer**, and that distinction is
    load-bearing rather than stylistic. The requirement is a function of the
    interval it is asked from: the horizon starts at the next boundary, so it
    genuinely differs every quarter-hour even when nothing about the forecast has
    changed. A digest over the computed figure would therefore change ninety-six
    times a day, store ninety-six documents, and break the rule that a refresh
    reproducing what it produced fifteen minutes ago costs no I/O at all.

    An earlier draft did exactly that. ``test_forecast_issuance`` caught it --
    "ninety-four refreshes a day must be free" -- and the fix is the one the
    sibling families already use: ``fingerprint_forecast`` likewise excludes
    volatile things, keeping the digest over what the belief was *derived from*.

    Nothing is lost, because the requirement at any other instant of the same
    forecast is recomputable: the two snapshots give the demands, the
    configuration fingerprint gives the battery, ``issued_at`` gives the interval,
    and ``model_version`` gives the recursion.
    """
    return _digest(
        {
            "model_version": RESERVE_MODEL_VERSION,
            # The floor is an input rather than an output: it is the recursion's
            # terminal condition, and a user changing it changes the belief even
            # when both forecasts stand still.
            "floor": _round_kwh(projection.floor_energy_kwh),
            "credited_surplus": projection.credited_surplus,
            "config": config_fingerprint,
            "load": load_fingerprint,
            "pv": pv_fingerprint,
        }
    )


# -- reporting ----------------------------------------------------------------


REPLENISHMENT_NOTE: str = (
    "The authoritative reserve credits forecast photovoltaic surplus as future "
    "battery energy, capped by the inverter's charge power and by usable "
    "capacity. This is a forecast, not an observation: it does not prove the "
    "real inverter will store that surplus. pv_absorption_modelled and "
    "pv_absorption_reason are recorded here as provenance and are read by "
    "nothing -- the reserve calculation never consults them, so an identical "
    "physical forecast always yields an identical requirement. "
    "required_same_interval_only_kwh is the diagnostic counterfactual "
    "representing the requirement if forecast surplus cannot replenish the "
    "battery across intervals. Phase 7 never automatically substitutes it for "
    "required_reserve_kwh. A later policy phase may use this evidence when "
    "deciding how conservatively to act. Nothing in this release enforces or "
    "executes either figure."
)

RESERVE_BASIS: str = (
    "the minimum stored energy that would keep the battery from running short of "
    "the net demand it can physically serve, over the forecast horizon. A point "
    "estimate: no forecast-error margin is applied, and where the measured load "
    "bias is negative the estimate may be biased low. Advisory only -- this "
    "release neither enforces nor executes the reserve, and the configured "
    "minimum state of charge remains the hard floor the planner obeys"
)


def reserve_as_dict(
    projection: ReserveProjection,
    *,
    same_interval_only: ReserveProjection | None = None,
    pv_blind: ReserveProjection | None = None,
    state: BatteryState | None = None,
    comparison: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the bounded diagnostics form.

    Counts, totals, edges and status only. No per-interval array appears at all:
    a hundred and ninety-two values would breach the sixteen-entry ceiling every
    diagnostics list is held to, and a truncated one would read as a short
    horizon rather than as a clipped payload.
    """
    required = projection.required_now_dc_kwh
    same = (
        None if same_interval_only is None else same_interval_only.required_now_dc_kwh
    )
    blind = None if pv_blind is None else pv_blind.required_now_dc_kwh

    payload: dict[str, Any] = {
        "available": projection.available,
        "unavailable_reason": projection.unavailable_reason,
        "decides_nothing": (
            "Phase 7 calculates a requirement. It never enforces it, never "
            "charges or discharges because of it, and never consults a price"
        ),
        "model_version": RESERVE_MODEL_VERSION,
        "authoritative": {
            "required_reserve_kwh": _round_kwh(required),
            "required_reserve_soc_percent": _round_soc(
                projection.required_now_soc_percent
            ),
            "reachable": projection.reachable,
            "lower_bound_reason": projection.lower_bound_reason,
            "basis": RESERVE_BASIS,
        },
        "floor": {
            "floor_energy_kwh": _round_kwh(projection.floor_energy_kwh),
            "ceiling_energy_kwh": _round_kwh(projection.ceiling_energy_kwh),
            "rule": (
                "the configured minimum state of charge is the hard floor and is "
                "unchanged by this phase: it is the terminal condition of the "
                "recursion and the lowest value the requirement can take"
            ),
        },
        "counterfactuals": {
            "required_same_interval_only_kwh": _round_kwh(same),
            "required_pv_blind_kwh": _round_kwh(blind),
            "replenishment_dependency_kwh": (
                None
                if same is None or required is None
                else _round_kwh(same - required)
            ),
            "pv_dependency_kwh": (
                None
                if blind is None or required is None
                else _round_kwh(blind - required)
            ),
            "peak_required_reserve_kwh": _round_kwh(
                projection.peak_required_reserve_kwh
            ),
            "peak_required_at": projection.peak_required_at,
            "rule": (
                "diagnostic counterfactuals, never authoritative and never "
                "substituted automatically. the peak is the largest requirement "
                "anywhere in the horizon and is not the current requirement: "
                "holding it now would reserve energy intervening replenishment "
                "is expected to supply"
            ),
        },
        "horizon": {
            "basis": projection.horizon_basis,
            "intervals_evaluated": projection.intervals_evaluated,
            "intervals_unknown": projection.intervals_unknown,
            "pv_blind_intervals": projection.pv_blind_intervals,
            "net_demand_kwh": _round_kwh(projection.net_demand_ac_kwh),
            "servable_kwh": _round_kwh(projection.servable_ac_kwh),
            "demand_beyond_discharge_power_kwh": _round_kwh(
                projection.demand_beyond_discharge_power_kwh
            ),
            "forecast_surplus_kwh": _round_kwh(projection.surplus_ac_kwh),
            "credited_surplus_kwh": _round_kwh(projection.credited_ac_kwh),
            "constraint_counts": projection.constraint_counts,
        },
        "headroom": {
            "headroom_bound": projection.headroom_bound,
            "headroom_bound_intervals": projection.headroom_bound_intervals,
            "surplus_beyond_headroom_kwh": _round_kwh(
                projection.surplus_beyond_headroom_kwh
            ),
            "rule": (
                "detected and reported, never corrected: where it binds the "
                "requirement is a lower bound and says so, and no alternate "
                "reserve model is substituted"
            ),
        },
        "replenishment_note": REPLENISHMENT_NOTE,
    }
    if state is not None or projection.available:
        payload["shortfall"] = shortfall(projection, state)
    if comparison is not None:
        payload["projected_trajectory"] = comparison
    if provenance is not None:
        payload["provenance"] = provenance
    return payload


# -- evidence ----------------------------------------------------------------


def _parse_stored_moment(value: Any) -> datetime | None:
    """Return a stored ISO instant, or ``None`` when it is unusable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _finite_or_none(value: Any) -> float | None:
    """Return a usable float, or ``None``. Booleans are refused."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


@dataclass(frozen=True, slots=True)
class ReserveSnapshot:
    """What the reserve was believed to be for one day, at one instant.

    Why record this when nothing in this release learns from it: **the battery
    configuration a requirement was computed against is irrecoverable
    afterwards.** Both forecasts are already persisted, so the arithmetic is
    reproducible -- but capacity, the floor, the power limits and the efficiency
    live in the config entry, which keeps no history. Raising a minimum state of
    charge would otherwise make every earlier belief unverifiable, which is
    exactly the hindsight bias the evidence layer exists to prevent.

    **Scalars only.** The per-interval requirement is not stored: it is a hundred
    and ninety-two floats a refresh, and it is recomputable from the three
    fingerprints below plus ``model_version``. Storing it would buy nothing and
    cost a megabyte a month.

    No outcome half, like the price evidence. The natural outcome is the measured
    state of charge, which the learning store already keeps per interval; scoring
    a requirement against it is a later phase, and doing it here would mean
    inventing a verdict this phase has no basis for.

    ``pv_absorption_modelled`` and ``pv_absorption_reason`` are recorded and read
    by nothing. On the reference installation the first flipped from true to false
    inside fifteen minutes because a dispatch began, while both forecasts stood
    still -- so a requirement that consulted it would move for no physical reason
    and an earlier belief would not be reproducible. Keeping the pair beside the
    figure rather than inside it is what makes that checkable afterwards.
    """

    issued_at: datetime
    target_day: date
    tz_key: str

    available: bool
    unavailable_reason: str | None

    required_dc_kwh: float | None
    required_soc_percent: float | None
    required_same_interval_only_dc_kwh: float | None
    required_pv_blind_dc_kwh: float | None
    peak_required_dc_kwh: float | None

    floor_soc_percent: float
    horizon_start: datetime | None
    horizon_end: datetime | None
    horizon_basis: str
    lower_bound_reason: str | None
    headroom_bound: bool
    reachable: bool | None
    intervals_evaluated: int
    intervals_unknown: int

    pv_absorption_modelled: bool | None
    pv_absorption_reason: str | None
    replenishment_assumption: str

    load_fingerprint: str | None
    pv_fingerprint: str | None
    config_fingerprint: str

    fingerprint: str
    model_version: int = RESERVE_MODEL_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Return the compact serialisable form, matching the sibling families."""
        return {
            "at": self.issued_at.isoformat(),
            "tz": self.tz_key,
            "a": 1 if self.available else 0,
            "ur": self.unavailable_reason,
            "rq": self.required_dc_kwh,
            "rs": self.required_soc_percent,
            "si": self.required_same_interval_only_dc_kwh,
            "pb": self.required_pv_blind_dc_kwh,
            "pk": self.peak_required_dc_kwh,
            "fl": self.floor_soc_percent,
            "hs": (
                None if self.horizon_start is None else self.horizon_start.isoformat()
            ),
            "he": None if self.horizon_end is None else self.horizon_end.isoformat(),
            "hb": self.horizon_basis,
            "lb": self.lower_bound_reason,
            "hd": 1 if self.headroom_bound else 0,
            "rc": None if self.reachable is None else (1 if self.reachable else 0),
            "n": self.intervals_evaluated,
            "nu": self.intervals_unknown,
            "am": (
                None
                if self.pv_absorption_modelled is None
                else (1 if self.pv_absorption_modelled else 0)
            ),
            "ar": self.pv_absorption_reason,
            "ra": self.replenishment_assumption,
            "lf": self.load_fingerprint,
            "pf": self.pv_fingerprint,
            "cf": self.config_fingerprint,
            "f": self.fingerprint,
            "mv": self.model_version,
        }

    @classmethod
    def from_dict(cls, target_day: date, raw: Any) -> ReserveSnapshot | None:
        """Rebuild a snapshot, or return ``None`` when the entry is unusable."""
        if not isinstance(raw, Mapping):
            return None
        issued = _parse_stored_moment(raw.get("at"))
        if issued is None:
            return None
        tz_key = raw.get("tz")
        reachable = raw.get("rc")
        modelled = raw.get("am")
        floor = _finite_or_none(raw.get("fl"))
        return cls(
            issued_at=issued,
            target_day=target_day,
            tz_key=tz_key if isinstance(tz_key, str) and tz_key else "UTC",
            available=bool(raw.get("a")),
            unavailable_reason=(raw["ur"] if isinstance(raw.get("ur"), str) else None),
            required_dc_kwh=_finite_or_none(raw.get("rq")),
            required_soc_percent=_finite_or_none(raw.get("rs")),
            required_same_interval_only_dc_kwh=_finite_or_none(raw.get("si")),
            required_pv_blind_dc_kwh=_finite_or_none(raw.get("pb")),
            peak_required_dc_kwh=_finite_or_none(raw.get("pk")),
            floor_soc_percent=0.0 if floor is None else floor,
            horizon_start=_parse_stored_moment(raw.get("hs")),
            horizon_end=_parse_stored_moment(raw.get("he")),
            horizon_basis=(
                raw["hb"]
                if isinstance(raw.get("hb"), str)
                else RESERVE_HORIZON_TRUNCATED
            ),
            lower_bound_reason=(raw["lb"] if isinstance(raw.get("lb"), str) else None),
            headroom_bound=bool(raw.get("hd")),
            reachable=None if reachable is None else bool(reachable),
            intervals_evaluated=(raw["n"] if isinstance(raw.get("n"), int) else 0),
            intervals_unknown=(raw["nu"] if isinstance(raw.get("nu"), int) else 0),
            pv_absorption_modelled=(None if modelled is None else bool(modelled)),
            pv_absorption_reason=(
                raw["ar"] if isinstance(raw.get("ar"), str) else None
            ),
            replenishment_assumption=(
                raw["ra"]
                if isinstance(raw.get("ra"), str)
                else RESERVE_REPLENISHMENT_ASSUMPTION
            ),
            load_fingerprint=(raw["lf"] if isinstance(raw.get("lf"), str) else None),
            pv_fingerprint=(raw["pf"] if isinstance(raw.get("pf"), str) else None),
            config_fingerprint=str(raw.get("cf") or ""),
            fingerprint=str(raw.get("f") or ""),
            model_version=(
                raw["mv"] if isinstance(raw.get("mv"), int) else RESERVE_MODEL_VERSION
            ),
        )


def build_reserve_snapshot(
    projection: ReserveProjection,
    *,
    issued_at: datetime,
    target_day: date,
    tz_key: str,
    floor_soc_percent: float,
    config_fingerprint: str,
    horizon_start: datetime | None = None,
    horizon_end: datetime | None = None,
    same_interval_only: ReserveProjection | None = None,
    pv_blind: ReserveProjection | None = None,
    load_fingerprint: str | None = None,
    pv_fingerprint: str | None = None,
    pv_absorption_modelled: bool | None = None,
    pv_absorption_reason: str | None = None,
) -> ReserveSnapshot:
    """Return the snapshot for one refresh.

    The absorption pair is accepted here and stored verbatim. It reaches no
    figure: every number below comes from ``projection``, which never saw it.

    The horizon edges are supplied rather than derived, because turning a
    chronological index into an instant needs the civil day and its real length
    -- calendar knowledge this module deliberately does not have.
    """
    return ReserveSnapshot(
        issued_at=issued_at,
        target_day=target_day,
        tz_key=tz_key,
        available=projection.available,
        unavailable_reason=projection.unavailable_reason,
        required_dc_kwh=_round_kwh(projection.required_now_dc_kwh),
        required_soc_percent=_round_soc(projection.required_now_soc_percent),
        required_same_interval_only_dc_kwh=(
            None
            if same_interval_only is None
            else _round_kwh(same_interval_only.required_now_dc_kwh)
        ),
        required_pv_blind_dc_kwh=(
            None if pv_blind is None else _round_kwh(pv_blind.required_now_dc_kwh)
        ),
        peak_required_dc_kwh=_round_kwh(projection.peak_required_reserve_kwh),
        floor_soc_percent=floor_soc_percent,
        horizon_start=horizon_start,
        horizon_end=horizon_end,
        horizon_basis=projection.horizon_basis,
        lower_bound_reason=projection.lower_bound_reason,
        headroom_bound=projection.headroom_bound,
        reachable=projection.reachable,
        intervals_evaluated=projection.intervals_evaluated,
        intervals_unknown=projection.intervals_unknown,
        pv_absorption_modelled=pv_absorption_modelled,
        pv_absorption_reason=pv_absorption_reason,
        replenishment_assumption=RESERVE_REPLENISHMENT_ASSUMPTION,
        load_fingerprint=load_fingerprint,
        pv_fingerprint=pv_fingerprint,
        config_fingerprint=config_fingerprint,
        fingerprint=fingerprint_reserve(
            projection,
            config_fingerprint=config_fingerprint,
            load_fingerprint=load_fingerprint,
            pv_fingerprint=pv_fingerprint,
        ),
    )
