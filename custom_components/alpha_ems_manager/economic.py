"""What the cheapest way through the known horizon is. **It commands nothing.**

Phase 8 answers one question: given the prices, the load, the production and the
reserve requirement that are *actually known*, what is the least-cost way to move
the battery? It publishes the answer, and this release sends no command at all.

The objective, and why it is lexicographic
------------------------------------------

    minimise ( reserve_violation , economic_cost )   compared lexicographically

Both terms are additive along a path and the order is monotone, so Bellman
optimality holds and this stays a plain backward induction. In the normal case the
achievable violation is zero for every feasible path, the first term ties, and the
order degenerates to pure cost minimisation -- so there is **no separate
"unreachable reserve" mode** to write or to test. "Unreachable" is simply the case
where the minimum achievable violation is greater than zero, and it can never
unlock a profitable export because exporting deepens the violation, which is
lexically dominant.

Safety Buy is therefore not a mechanism. It is what satisfying the reserve looks
like, and it is *labelled* by comparing against a solve with the reserve relaxed.

Why the physics is not here
---------------------------

Every energy in this module comes from :func:`battery.apply_request` and every
grid flow from :func:`battery.split_grid_energy`. The optimizer holds no battery
model: no power limit, no capacity bound, no efficiency, no interval duration, no
grid residual. One consequence is worth stating because it is easy to lose: the
transition table is built by calling the clamp, and the *inverse* conversion this
phase needs -- how much AC energy buys a given change in stored DC energy -- is
**measured from the clamp** rather than read off ``BatteryLimits``, so a change to
the conversion cannot silently disagree with the planner. ``test_phase_eight_
boundaries`` asserts the measured ratio against the model.

Prices never enter this module as a type. It receives :class:`IntervalPrice`
values -- two floats and a knownness flag -- so the price layer is not importable
from here and the whole optimizer is testable without it.

Two solves, and why neither is a degraded copy of the other
-----------------------------------------------------------

The **desired** solve runs over the full action space, including actions no
actuator in this release can perform. It is the economic intent, undistorted by
what the hardware happens to support.

The **capability** solve runs over the actions that do have a primitive. It is a
separately computed plan, not a downgraded one, which is what lets both hold at
once: the optimum is never bent to fit the actuator, and nothing is ever silently
substituted at execution time. Their difference is the value of the missing
primitives, and it is published.

In this release neither is executed. ``CONTROL_EXECUTION_AVAILABLE`` is false, so
``execution_blocked_reason`` reads ``execution_unavailable`` on every action, and
that is a fact about the release rather than a caveat in prose.

Two ratified refinements of the approved plan
---------------------------------------------

Both are stated here because either one read the other way makes the release
non-functional in its **default** configuration.

**``charge`` means buying.** ``ECONOMIC_ACTION_CHARGE`` and the grid-charging
opt-in refer to discretionary economic *grid purchase*, never to energy moving
into the pack. Absorbing production the house cannot use is ambient physical
behaviour -- the same thing :func:`simulation.simulate` models as
``absorb_surplus`` -- so it is permitted unconditionally, published as ``hold``,
earns no run, and pays no switching cost. The plan specified the permission in
terms of direction; read that way, a battery with the default opt-ins never
absorbed its own solar.

**The terminal bound lives on the solver's grid.** ``HoldPolicy`` remains the
conceptual counterfactual, but what is enforced -- and what ``terminal_floor_kwh``
publishes -- is the idle-with-absorption endpoint expressed on the same bucketed
state space the search runs over, reachable by construction. The plan named the
continuous ``plan.reference.end_energy_kwh`` literally; a terminal requirement the
state space cannot represent makes an otherwise valid sunny horizon artificially
infeasible. ``TERMINAL_BASIS`` says which of the two a reader is looking at, so
the continuous reference can never be silently confused with the bucketed
constraint.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from .battery import (
    INTERVAL_HOURS,
    BatteryLimits,
    BatteryRequest,
    apply_request,
    build_state,
    split_grid_energy,
    static_reserve,
)
from .const import (
    BATTERY_KWH_PRECISION,
    ECONOMIC_ACTION_CHARGE,
    ECONOMIC_ACTION_CURTAIL,
    ECONOMIC_ACTION_DISCHARGE,
    ECONOMIC_ACTION_EXPORT,
    ECONOMIC_ACTION_HOLD,
    ECONOMIC_ACTION_SAFETY_BUY,
    ECONOMIC_BUCKET_KWH,
    ECONOMIC_EUR_PRECISION,
    ECONOMIC_FINGERPRINT_CHARS,
    ECONOMIC_GAP_FORECAST_INFEASIBLE,
    ECONOMIC_GAP_NO_PRIMITIVE,
    ECONOMIC_GAP_NONE,
    ECONOMIC_MATERIAL_ENERGY_KWH,
    ECONOMIC_MATERIAL_POWER_KW,
    ECONOMIC_MODEL_VERSION,
    ECONOMIC_POWER_PRECISION,
    ECONOMIC_REASON_CHEAP_WINDOW,
    ECONOMIC_REASON_EXPENSIVE_WINDOW,
    ECONOMIC_REASON_MAKE_HEADROOM,
    ECONOMIC_REASON_NEGATIVE_EXPORT,
    ECONOMIC_REASON_NO_ACTION,
    ECONOMIC_REASON_RESERVE_RECOVERY,
    ECONOMIC_REASON_SAFETY_BUY,
    ECONOMIC_UNAVAILABLE_HORIZON_EMPTY,
    ECONOMIC_UNAVAILABLE_TERMINAL_UNREACHABLE,
    MAX_ECONOMIC_RUNS_REPORTED,
    MODE_CHARGE,
    MODE_DISCHARGE,
    MODE_IDLE,
)
from .simulation import IntervalDemand

#: The battery-moving actions. ``export`` and ``curtail_pv`` are economic
#: identities rather than separate commands: an export is a discharge whose
#: surplus reaches the grid, and a curtailment is production declined.
ECONOMIC_ACTIONS: tuple[str, ...] = (
    ECONOMIC_ACTION_CHARGE,
    ECONOMIC_ACTION_DISCHARGE,
    ECONOMIC_ACTION_EXPORT,
    ECONOMIC_ACTION_CURTAIL,
)

#: What an actuator exists for in this release. The capability solve is restricted
#: to these; the desired solve is not.
IMPLEMENTED_ACTIONS: frozenset[str] = frozenset(
    {ECONOMIC_ACTION_CHARGE, ECONOMIC_ACTION_DISCHARGE}
)

#: Run-state of the previous interval, which is what makes a *run* detectable and
#: therefore what makes ``minimum_trade_gain_eur`` a per-run cost rather than a
#: per-interval one.
_RUN_IDLE = 0
_RUN_CHARGE = 1
_RUN_DISCHARGE = 2
_RUN_STATES = (_RUN_IDLE, _RUN_CHARGE, _RUN_DISCHARGE)

_RUN_OF_MODE = {
    MODE_IDLE: _RUN_IDLE,
    MODE_CHARGE: _RUN_CHARGE,
    MODE_DISCHARGE: _RUN_DISCHARGE,
}


def _round_kwh(value: float | None) -> float | None:
    """Round an energy for reporting, preserving ``None``."""
    return None if value is None else round(value, BATTERY_KWH_PRECISION)


def _round_eur(value: float | None) -> float | None:
    """Round a money amount for reporting, preserving ``None``."""
    return None if value is None else round(value, ECONOMIC_EUR_PRECISION)


def _round_kw(value: float | None) -> float | None:
    """Round a power for reporting, preserving ``None``."""
    return None if value is None else round(value, ECONOMIC_POWER_PRECISION)


# -- prices, as plain numbers -----------------------------------------------


@dataclass(frozen=True, slots=True)
class IntervalPrice:
    """What one interval costs to import from and earns to export to.

    Two separate figures, never one signed number. The purchase side carries a
    fixed floor of energy tax plus sourcing markup that the export side does not,
    so on a negative wholesale interval importing still costs money while
    exporting earns a negative amount. A single "price" cannot answer both
    questions, and the whole objective depends on not conflating them.
    """

    import_eur_kwh: float | None = None
    export_eur_kwh: float | None = None

    @property
    def known(self) -> bool:
        """Return whether this interval can be priced at all.

        Both sides are needed: a charge is priced on import and a discharge that
        reaches the grid on export, and the optimizer chooses between them.
        """
        return self.import_eur_kwh is not None and self.export_eur_kwh is not None


# -- the physics table -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Move:
    """One candidate transition, with every energy the clamp produced.

    Built once per bucket and reused for every interval, which is possible
    because :func:`battery.apply_request` depends only on the state and the
    request -- never on the interval. So the clamp is called a few tens of
    thousands of times to build a table, and the table is the physics.
    """

    #: Target bucket. Equal to the source for an idle move.
    target: int
    mode: str
    power_kw: float
    delta_dc_kwh: float
    charge_ac_kwh: float
    discharge_ac_kwh: float
    constraints: tuple[str, ...]

    @property
    def run_state(self) -> int:
        """Return the run-state this move leaves behind."""
        return _RUN_OF_MODE[self.mode]


@dataclass(frozen=True, slots=True)
class PhysicsTable:
    """Every transition the battery can make, measured through the clamp.

    ``charge_dc_per_ac`` and ``discharge_dc_per_ac`` are **measured**, not read
    off :class:`~.battery.BatteryLimits`. This phase needs the inverse conversion
    -- how much AC energy buys a given change in stored DC energy -- which Phase 7
    never did, and measuring it from the clamp keeps one authority rather than
    two. A boundary test asserts the measured ratios against the model, so a
    divergence is a failure rather than a drift.
    """

    limits: BatteryLimits
    bucket_kwh: float
    buckets: int
    ceiling_kwh: float
    charge_dc_per_ac: float
    discharge_dc_per_ac: float
    moves: tuple[tuple[_Move, ...], ...]

    def energy(self, bucket: int) -> float:
        """Return the stored DC energy a bucket stands for."""
        return min(bucket * self.bucket_kwh, self.ceiling_kwh)

    def bucket_at_or_below(self, energy_kwh: float) -> int:
        """Return the highest bucket at or below an energy.

        Downwards, always. A measured state of charge that lands between buckets
        is modelled as slightly *less* energy than the pack holds, which is the
        conservative direction and the one that keeps every violation an exact
        multiple of the bucket.
        """
        if energy_kwh <= 0.0:
            return 0
        return min(self.buckets, math.floor(energy_kwh / self.bucket_kwh + 1e-9))

    def bucket_at_or_above(self, energy_kwh: float) -> int:
        """Return the lowest bucket at or above an energy.

        Upwards, and this is the one the reserve requirement uses: quantising a
        *requirement* up protects at most one bucket too much, where quantising it
        down would ignore up to one bucket of genuine shortfall.
        """
        if energy_kwh <= 0.0:
            return 0
        return min(self.buckets, math.ceil(energy_kwh / self.bucket_kwh - 1e-9))


def build_physics_table(
    limits: BatteryLimits,
    *,
    floor_energy_kwh: float,
    bucket_kwh: float = ECONOMIC_BUCKET_KWH,
) -> PhysicsTable | None:
    """Precompute every transition, by asking the clamp.

    The reserve handed to the probe states is the caller's **configured floor**,
    so the clamp enforces the real hard limit and buckets below it are simply
    unreachable by discharge. Nothing here re-implements that bound.
    """
    ceiling = limits.energy_for_soc(limits.max_soc_percent)
    if bucket_kwh <= 0.0 or ceiling <= 0.0:
        return None
    buckets = max(1, math.ceil(ceiling / bucket_kwh - 1e-9))
    floor_percent = limits.soc_for_energy(max(0.0, floor_energy_kwh))
    reserve = static_reserve(floor_percent)

    calibration = build_state(soc_percent=50.0, limits=limits, reserve=reserve)
    if calibration is None:  # pragma: no cover - build_limits precludes it
        return None
    charge_ratio, discharge_ratio = _calibrate(calibration)
    if charge_ratio <= 0.0 or discharge_ratio <= 0.0:  # pragma: no cover
        return None

    def energy_of(bucket: int) -> float:
        return min(bucket * bucket_kwh, ceiling)

    rows: list[tuple[_Move, ...]] = []
    for source in range(buckets + 1):
        start = build_state(
            soc_percent=limits.soc_for_energy(energy_of(source)),
            limits=limits,
            reserve=reserve,
        )
        if start is None:  # pragma: no cover
            rows.append(())
            continue
        moves = [
            _Move(
                target=source,
                mode=MODE_IDLE,
                power_kw=0.0,
                delta_dc_kwh=0.0,
                charge_ac_kwh=0.0,
                discharge_ac_kwh=0.0,
                constraints=(),
            )
        ]
        for target in range(buckets + 1):
            if target == source:
                continue
            delta = energy_of(target) - energy_of(source)
            move = _move_to(start, delta, charge_ratio, discharge_ratio, target)
            if move is not None:
                moves.append(move)
        rows.append(tuple(moves))

    return PhysicsTable(
        limits=limits,
        bucket_kwh=bucket_kwh,
        buckets=buckets,
        ceiling_kwh=ceiling,
        charge_dc_per_ac=charge_ratio,
        discharge_dc_per_ac=discharge_ratio,
        moves=tuple(rows),
    )


def _calibrate(probe: Any) -> tuple[float, float]:
    """Return the measured DC-per-AC ratio in each direction.

    One clamp call per direction, at a power well inside every limit, so neither
    reading is taken through a clamp that bound. The ratios are what the inverse
    conversion needs, and taking them from the clamp rather than from the limits
    means a change to the conversion cannot leave the planner behind.
    """
    charged = apply_request(probe, BatteryRequest.charge(1.0))
    discharged = apply_request(probe, BatteryRequest.discharge(1.0))
    charge_ratio = (
        (charged.end_energy_kwh - probe.energy_kwh) / charged.charge_ac_kwh
        if charged.charge_ac_kwh > 0.0
        else 0.0
    )
    discharge_ratio = (
        (probe.energy_kwh - discharged.end_energy_kwh) / discharged.discharge_ac_kwh
        if discharged.discharge_ac_kwh > 0.0
        else 0.0
    )
    return charge_ratio, discharge_ratio


def _move_to(
    start: Any,
    delta_dc_kwh: float,
    charge_ratio: float,
    discharge_ratio: float,
    target: int,
) -> _Move | None:
    """Return the move that lands exactly on a target, or ``None`` if it cannot.

    The AC energy is derived from the measured ratio and then handed to the clamp,
    which is what validates it: if the clamp reports a different DC delta, the
    move did not fit and is discarded rather than approximated.
    """
    if delta_dc_kwh > 0.0:
        power = (delta_dc_kwh / charge_ratio) / INTERVAL_HOURS
        request = BatteryRequest.charge(power)
    else:
        power = (-delta_dc_kwh / discharge_ratio) / INTERVAL_HOURS
        request = BatteryRequest.discharge(power)
    if power <= 0.0:
        return None
    outcome = apply_request(start, request)
    achieved = outcome.end_energy_kwh - start.energy_kwh
    # The clamp is the arbiter. A move it reduced is not the move that was asked
    # for, so it is dropped -- the reduced version is reachable as its own target.
    if abs(achieved - delta_dc_kwh) > 1e-6:
        return None
    return _Move(
        target=target,
        mode=outcome.mode,
        power_kw=request.power_kw,
        delta_dc_kwh=achieved,
        charge_ac_kwh=outcome.charge_ac_kwh,
        discharge_ac_kwh=outcome.discharge_ac_kwh,
        constraints=outcome.constraints,
    )


# -- what one interval of a plan looks like ---------------------------------


@dataclass(frozen=True, slots=True)
class EconomicInterval:
    """One interval of a solved plan, with every boundary stated separately.

    Six energies, because a euro figure is only meaningful against the boundary
    it was measured at. Prices apply to the **grid** pair; the battery pair is
    what a command would set; the DC figure is the pack's own state change. They
    are not interchangeable, and every one of them comes from an existing
    primitive.
    """

    index: int
    action: str
    start_energy_dc_kwh: float
    battery_delta_dc_kwh: float
    battery_charge_ac_kwh: float
    battery_discharge_ac_kwh: float
    grid_import_kwh: float
    grid_export_kwh: float
    pv_curtailed_kwh: float
    cost_eur: float
    import_price_eur_kwh: float | None
    export_price_eur_kwh: float | None
    run_start: bool
    constraints: tuple[str, ...] = ()

    @property
    def moves_battery(self) -> bool:
        """Return whether the battery moved at all in this interval."""
        return self.battery_charge_ac_kwh > 0.0 or self.battery_discharge_ac_kwh > 0.0


@dataclass(frozen=True, slots=True)
class EconomicRun:
    """A maximal contiguous stretch of one action.

    The same unit ``minimum_trade_gain_eur`` is charged against, deliberately: the
    diagnostics and the objective then talk about the same object, so a user can
    check the threshold against the figure it was applied to.
    """

    action: str
    start_index: int
    end_index: int
    interval_count: int
    battery_charge_ac_kwh: float
    battery_discharge_ac_kwh: float
    grid_import_kwh: float
    grid_export_kwh: float
    pv_curtailed_kwh: float
    first_power_kw: float
    expected_value_eur: float
    min_price_eur_kwh: float | None
    max_price_eur_kwh: float | None
    average_price_eur_kwh: float | None

    @property
    def energy_kwh(self) -> float:
        """Return the AC energy of the flow this action controls.

        Per action, because the economically relevant flow differs. A charge sets
        a battery rate and the pack gains that energy; an export is paid for at
        the meter and the battery movement behind it is larger by the house load;
        a curtailment is production declined. Reporting one number for all four
        would be wrong for three of them.
        """
        if self.action in (ECONOMIC_ACTION_CHARGE, ECONOMIC_ACTION_SAFETY_BUY):
            return self.battery_charge_ac_kwh
        if self.action == ECONOMIC_ACTION_DISCHARGE:
            return self.battery_discharge_ac_kwh
        if self.action == ECONOMIC_ACTION_EXPORT:
            return self.grid_export_kwh
        if self.action == ECONOMIC_ACTION_CURTAIL:
            return self.pv_curtailed_kwh
        return 0.0

    @property
    def average_power_kw(self) -> float:
        """Return the mean battery power across the run, AC.

        A mean, and named as one. The published entity carries the *first*
        interval's power instead, because a run legitimately varies interval to
        interval and an average would describe none of them.
        """
        if not self.interval_count:
            return 0.0
        energy = self.battery_charge_ac_kwh + self.battery_discharge_ac_kwh
        return energy / (self.interval_count * INTERVAL_HOURS)


@dataclass(frozen=True, slots=True)
class EconomicPlan:
    """One solve. Frozen, never written to disk in this shape.

    Recomputable from the price, load, production and reserve fingerprints plus
    the model version, which is why the evidence layer stores scalars.
    """

    intervals: tuple[EconomicInterval, ...]
    runs: tuple[EconomicRun, ...]
    violation_kwh: float
    cost_eur: float
    hold_cost_eur: float
    switching_cost_eur: float
    terminal_floor_kwh: float
    terminal_binding: bool
    permitted: frozenset[str]
    available: bool
    unavailable_reason: str | None = None
    worst_shortfall_kwh: float = 0.0
    first_violation_index: int | None = None

    # -- what the entity reads --------------------------------------------

    @property
    def action(self) -> str:
        """Return the action of the run in progress, or ``hold``."""
        run = self.current_run
        return ECONOMIC_ACTION_HOLD if run is None else run.action

    @property
    def current_run(self) -> EconomicRun | None:
        """Return the first run of the horizon, if it starts immediately.

        "Immediately" means at interval zero. A run further out is reported as
        the next action rather than the current one, because publishing a future
        action as the current state would misread as something happening now.
        """
        for run in self.runs:
            if run.start_index == 0:
                return run
        return None

    @property
    def next_run(self) -> EconomicRun | None:
        """Return the first run that does not start immediately."""
        for run in self.runs:
            if run.start_index > 0:
                return run
        return None

    @property
    def published_run(self) -> EconomicRun | None:
        """Return the run the entity describes: the current one, else the next."""
        return self.current_run or self.next_run

    @property
    def power_kw(self) -> float:
        """Return the battery power of the published run's **first** interval.

        Not the run average. A multi-interval run varies with load, production,
        headroom, the reserve trajectory and the clamp, so the number a user needs
        for "what would it do now" is the first interval's, and the average lives
        in diagnostics.
        """
        run = self.published_run
        return 0.0 if run is None else run.first_power_kw

    @property
    def expected_net_value_eur(self) -> float:
        """Return the gain over doing nothing, **before** the switching cost.

        The switching cost is a notional device for suppressing pointless action;
        it is not money anybody pays. Reporting a gain net of it would understate
        what the plan actually earns.
        """
        return (self.hold_cost_eur - self.cost_eur) - self.switching_cost_eur

    # -- totals -----------------------------------------------------------

    @property
    def planned_charge_ac_kwh(self) -> float:
        """Return AC energy charged into the battery across the plan."""
        return sum(entry.battery_charge_ac_kwh for entry in self.intervals)

    @property
    def planned_discharge_ac_kwh(self) -> float:
        """Return AC energy discharged from the battery across the plan."""
        return sum(entry.battery_discharge_ac_kwh for entry in self.intervals)

    @property
    def planned_grid_import_kwh(self) -> float:
        """Return grid import across the plan."""
        return sum(entry.grid_import_kwh for entry in self.intervals)

    @property
    def planned_grid_export_kwh(self) -> float:
        """Return grid export across the plan."""
        return sum(entry.grid_export_kwh for entry in self.intervals)

    @property
    def planned_curtailed_kwh(self) -> float:
        """Return production declined across the plan."""
        return sum(entry.pv_curtailed_kwh for entry in self.intervals)

    @property
    def intervals_evaluated(self) -> int:
        """Return how many intervals the plan covers."""
        return len(self.intervals)

    @property
    def end_energy_dc_kwh(self) -> float:
        """Return the stored energy the plan ends at."""
        if not self.intervals:
            return 0.0
        last = self.intervals[-1]
        return last.start_energy_dc_kwh + last.battery_delta_dc_kwh


# -- the horizon -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EconomicHorizon:
    """The intersection of everything that is actually known.

    A **contiguous prefix**, deliberately. Prices stop at the first gap because
    Phase 6 defines the horizon that way -- knowing prices either side of a hole
    is not knowing them continuously -- and a plan that spanned the hole would be
    planning over invented data. The same rule applies to a missing forecast
    interval and to an unreachable reserve figure.
    """

    demands: tuple[IntervalDemand, ...]
    prices: tuple[IntervalPrice, ...]
    planning_reserve_kwh: tuple[float, ...]
    limited_by: str

    @property
    def intervals(self) -> int:
        """Return how many intervals are jointly known."""
        return len(self.demands)


def build_horizon(
    *,
    demands: Sequence[IntervalDemand],
    prices: Sequence[IntervalPrice],
    required_reserve_kwh: Sequence[float | None],
    table: PhysicsTable,
) -> EconomicHorizon:
    """Return the contiguous prefix every input agrees on.

    The reserve requirement is quantised **up** to a bucket here, and capped at
    the physical ceiling. Up, because protecting at most one bucket too much is
    the safe error and ignoring up to one bucket of shortfall is not; and because
    a requirement on a bucket boundary makes every violation an exact multiple of
    the bucket, which is what stops the lexicographic objective paying real money
    to avoid an unrepresentable shortfall.
    """
    usable: list[IntervalDemand] = []
    priced: list[IntervalPrice] = []
    reserve: list[float] = []
    limited_by = "complete"

    for position, demand in enumerate(demands):
        if position >= len(prices) or not prices[position].known:
            limited_by = "prices"
            break
        if demand.net_demand_kwh is None:
            limited_by = "load_forecast"
            break
        if position >= len(required_reserve_kwh):
            limited_by = "reserve"
            break
        raw = required_reserve_kwh[position]
        if raw is None:
            limited_by = "reserve"
            break
        usable.append(demand)
        priced.append(prices[position])
        reserve.append(table.energy(min(table.bucket_at_or_above(raw), table.buckets)))

    return EconomicHorizon(
        demands=tuple(usable),
        prices=tuple(priced),
        planning_reserve_kwh=tuple(reserve),
        limited_by=limited_by,
    )


# -- the solver --------------------------------------------------------------

#: A value that is worse than any reachable one, for an infeasible state. A
#: finite sentinel rather than an infinity so an arithmetic slip surfaces as an
#: absurd number in a test rather than as a ``nan`` two steps later.
_UNREACHABLE = (1e18, 1e18)


def solve(
    *,
    table: PhysicsTable,
    horizon: EconomicHorizon,
    start_energy_kwh: float,
    terminal_floor_kwh: float,
    minimum_trade_gain_eur: float,
    permitted: frozenset[str],
) -> EconomicPlan:
    """Return the least-cost plan over the horizon. Pure, total, never raises.

    Backward induction over ``(interval, bucket, run_state)``. The value at each
    state is the pair ``(violation, cost)`` compared lexicographically, so reserve
    feasibility dominates economics without a second mechanism and without a mode
    switch: when no violation is achievable anywhere the first term ties and the
    order is pure cost.

    ``run_state`` is the third dimension and it exists for exactly one reason: a
    per-run switching cost needs to know whether this interval *starts* a run or
    continues one. Charging it per interval instead would suppress the long
    windows that are the profitable ones.
    """
    if not horizon.intervals:
        return _empty_plan(
            terminal_floor_kwh, permitted, ECONOMIC_UNAVAILABLE_HORIZON_EMPTY
        )

    count = horizon.intervals
    buckets = table.buckets
    start_bucket = table.bucket_at_or_below(start_energy_kwh)

    ac_by_delta = _ac_by_delta(table)
    outcomes_per_interval: list[dict[int, _DeltaOutcome]] = [
        _interval_outcomes(
            ac_by_delta=ac_by_delta,
            load_ac_kwh=demand.baseline_kwh or 0.0,
            pv_ac_kwh=demand.pv_kwh or 0.0,
            price=horizon.prices[position],
            permitted=permitted,
        )
        for position, demand in enumerate(horizon.demands)
    ]

    # Down, so the plan never assumes more stored energy than the pack holds --
    # and then clamped to where doing nothing *on this grid* actually lands.
    #
    # The clamp is load-bearing. The requested floor is the continuous hold
    # trajectory's endpoint, and bucketed absorption loses up to one bucket per
    # interval against it, so on a sunny horizon the requested figure can sit a
    # few buckets above anything the state space can reach. Enforcing it then
    # makes every state infeasible and the plan unavailable -- a bound nobody can
    # satisfy is not a bound, it is a refusal to plan.
    terminal_bucket = min(
        table.bucket_at_or_below(terminal_floor_kwh),
        _ambient_end_bucket(
            table=table,
            outcomes_per_interval=outcomes_per_interval,
            start_bucket=start_bucket,
        ),
    )
    enforced_floor_kwh = table.energy(terminal_bucket)

    # Value and policy tables. ``value[b][r]`` is the best (violation, cost) from
    # this interval onwards; ``choice`` remembers which move produced it so the
    # plan can be walked forward afterwards.
    value: list[list[tuple[float, float]]] = [
        [_UNREACHABLE for _ in _RUN_STATES] for _ in range(buckets + 1)
    ]
    for bucket in range(buckets + 1):
        # The terminal condition: end no worse off than doing nothing would have.
        # It carries no price, so it cannot be traded against revenue.
        feasible = bucket >= terminal_bucket
        for run in _RUN_STATES:
            value[bucket][run] = (0.0, 0.0) if feasible else _UNREACHABLE

    choice: list[list[list[int]]] = [
        [[-1 for _ in _RUN_STATES] for _ in range(buckets + 1)] for _ in range(count)
    ]
    energies = [table.energy(bucket) for bucket in range(buckets + 1)]

    for position in range(count - 1, -1, -1):
        reserve_kwh = horizon.planning_reserve_kwh[position]
        outcomes = outcomes_per_interval[position]
        following = value
        value = [[_UNREACHABLE for _ in _RUN_STATES] for _ in range(buckets + 1)]
        # Violation is a property of where the interval *lands*, so it is computed
        # once per bucket rather than once per transition.
        violations = [max(0.0, reserve_kwh - energy) for energy in energies]

        for bucket in range(buckets + 1):
            moves = table.moves[bucket]
            for run in _RUN_STATES:
                best = _UNREACHABLE
                best_move = -1
                for offset, move in enumerate(moves):
                    outcome = outcomes.get(move.target - bucket)
                    if outcome is None or not outcome.permitted:
                        continue
                    onward = following[move.target][outcome.run_state]
                    if onward >= _UNREACHABLE:
                        continue
                    cost = outcome.cost_eur
                    if outcome.run_state != _RUN_IDLE and outcome.run_state != run:
                        cost += minimum_trade_gain_eur
                    candidate = (
                        onward[0] + violations[move.target],
                        onward[1] + cost,
                    )
                    if candidate < best:
                        best = candidate
                        best_move = offset
                value[bucket][run] = best
                choice[position][bucket][run] = best_move

    if value[start_bucket][_RUN_IDLE] >= _UNREACHABLE:  # pragma: no cover
        # The ambient walk is itself a feasible path to ``terminal_bucket``, so
        # this is unreachable by construction. Reported rather than raised, and
        # named honestly: an earlier version reported "horizon empty" here, which
        # was a lie about a horizon that was four intervals long.
        return _empty_plan(
            enforced_floor_kwh, permitted, ECONOMIC_UNAVAILABLE_TERMINAL_UNREACHABLE
        )

    return _walk_forward(
        table=table,
        horizon=horizon,
        choice=choice,
        outcomes_per_interval=outcomes_per_interval,
        start_bucket=start_bucket,
        terminal_floor_kwh=enforced_floor_kwh,
        terminal_bucket=terminal_bucket,
        minimum_trade_gain_eur=minimum_trade_gain_eur,
        permitted=permitted,
    )


def _ambient_end_bucket(
    *,
    table: PhysicsTable,
    outcomes_per_interval: list[dict[int, _DeltaOutcome]],
    start_bucket: int,
) -> int:
    """Return where doing nothing lands, absorbing what the house cannot use.

    The bucketed counterpart of the Phase-3 hold trajectory, and the reason it is
    computed here rather than read off ``plan.reference``: that trajectory is
    continuous, this state space is not, and a bound expressed in the wrong
    resolution is a bound that can be unsatisfiable.

    Highest reachable target among the *idle-run* deltas, which after the import
    permission fix means exactly the ambient absorption ones. Never a discharge,
    never a purchase, so this is a lower bound on what any feasible plan can end
    at and therefore always itself feasible.
    """
    bucket = start_bucket
    for outcomes in outcomes_per_interval:
        best = bucket
        for move in table.moves[bucket]:
            outcome = outcomes.get(move.target - bucket)
            if outcome is None or not outcome.permitted:
                continue
            if outcome.run_state == _RUN_IDLE and move.target > best:
                best = move.target
        bucket = best
    return bucket


@dataclass(frozen=True, slots=True)
class _DeltaOutcome:
    """What one bucket-delta does in one interval, priced and classified.

    Keyed by the delta rather than by the move, because the AC energies for a
    given change in stored energy are identical from every bucket -- the clamp
    rejects any move it had to reduce, so every surviving move is unclamped and
    therefore exactly linear. That makes this table 177 entries per interval
    instead of one per state, which is the difference between calling
    :func:`battery.split_grid_energy` thirty-four thousand times and nine hundred
    thousand times for one solve.
    """

    charge_ac_kwh: float
    discharge_ac_kwh: float
    grid_import_kwh: float
    grid_export_kwh: float
    curtailed_kwh: float
    cost_eur: float
    action: str
    permitted: bool
    #: Which run this delta belongs to, and therefore whether it costs a switching
    #: fee. Derived from the *economic* classification rather than from the move's
    #: physical mode, because a run is an economic object: ambient absorption of
    #: production the house cannot use moves the battery without being a trade.
    run_state: int


def _ac_by_delta(table: PhysicsTable) -> dict[int, tuple[float, float]]:
    """Map each reachable bucket delta to the AC energies the clamp produced.

    Read out of the physics table rather than derived, so the AC figures used for
    pricing are the clamp's own even though they are indexed by delta.
    """
    found: dict[int, tuple[float, float]] = {}
    for source, row in enumerate(table.moves):
        for move in row:
            delta = move.target - source
            if delta not in found:
                found[delta] = (move.charge_ac_kwh, move.discharge_ac_kwh)
    return found


def _interval_outcomes(
    *,
    ac_by_delta: dict[int, tuple[float, float]],
    load_ac_kwh: float,
    pv_ac_kwh: float,
    price: IntervalPrice,
    permitted: frozenset[str],
) -> dict[int, _DeltaOutcome]:
    """Price and classify every candidate delta for one interval.

    **Both permissions are measured against the idle baseline, not against the
    direction.** The idle case is computed first, and only what the battery causes
    *beyond* it counts as a choice the optimizer made:

    * Production exceeding house load spills to the grid whatever the battery
      does, so forbidding a state because of a spill the battery did not cause
      would make the interval infeasible rather than safe.
    * Symmetrically, storing production the house cannot use draws nothing from
      the grid. It is **ambient physical behaviour, never intent** -- the same
      thing :func:`simulation.simulate` models as ``absorb_surplus`` -- so it is
      always permitted, is labelled ``hold``, and costs no switching fee. Gating
      it behind the grid-charging opt-in made the model believe a battery never
      absorbs its own solar, which on any sunny day put the hold trajectory's
      endpoint out of reach and collapsed the whole plan to unavailable.
    """
    idle_flows = split_grid_energy(
        load_ac_kwh=load_ac_kwh,
        pv_ac_kwh=pv_ac_kwh,
        charge_ac_kwh=0.0,
        discharge_ac_kwh=0.0,
    )
    unavoidable_export = idle_flows.export_kwh
    unavoidable_import = idle_flows.import_kwh
    import_price = price.import_eur_kwh or 0.0
    export_price = price.export_eur_kwh or 0.0
    curtail_allowed = ECONOMIC_ACTION_CURTAIL in permitted

    outcomes: dict[int, _DeltaOutcome] = {}
    for delta, (charge_ac, discharge_ac) in ac_by_delta.items():
        curtailed = 0.0
        effective_pv = pv_ac_kwh
        if curtail_allowed and export_price < 0.0:
            # Closed form: decline exactly the energy that would otherwise be
            # exported at a negative price, and no more. Declining past that
            # point would force import at a positive one.
            probe = split_grid_energy(
                load_ac_kwh=load_ac_kwh,
                pv_ac_kwh=pv_ac_kwh,
                charge_ac_kwh=charge_ac,
                discharge_ac_kwh=discharge_ac,
            )
            curtailed = probe.export_kwh
            effective_pv = max(0.0, pv_ac_kwh - curtailed)
        flows = split_grid_energy(
            load_ac_kwh=load_ac_kwh,
            pv_ac_kwh=effective_pv,
            charge_ac_kwh=charge_ac,
            discharge_ac_kwh=discharge_ac,
        )
        caused_export = flows.export_kwh > unavoidable_export + 1e-9
        caused_import = flows.import_kwh > unavoidable_import + 1e-9
        if delta > 0:
            # A charge that draws nothing extra from the grid is absorption, not
            # a purchase, and the two must not share a permission.
            allowed = not caused_import or ECONOMIC_ACTION_CHARGE in permitted
            action = ECONOMIC_ACTION_CHARGE if caused_import else ECONOMIC_ACTION_HOLD
            run_state = _RUN_CHARGE if caused_import else _RUN_IDLE
        elif delta < 0:
            allowed = ECONOMIC_ACTION_DISCHARGE in permitted
            action = (
                ECONOMIC_ACTION_EXPORT if caused_export else ECONOMIC_ACTION_DISCHARGE
            )
            if caused_export:
                allowed = allowed and ECONOMIC_ACTION_EXPORT in permitted
            run_state = _RUN_DISCHARGE
        else:
            allowed = True
            action = ECONOMIC_ACTION_HOLD
            run_state = _RUN_IDLE
        if curtailed > 0.0:
            action = ECONOMIC_ACTION_CURTAIL
        outcomes[delta] = _DeltaOutcome(
            charge_ac_kwh=charge_ac,
            discharge_ac_kwh=discharge_ac,
            grid_import_kwh=flows.import_kwh,
            grid_export_kwh=flows.export_kwh,
            curtailed_kwh=curtailed,
            cost_eur=import_price * flows.import_kwh - export_price * flows.export_kwh,
            action=action,
            permitted=allowed,
            run_state=run_state,
        )
    return outcomes


def _empty_plan(
    terminal_floor_kwh: float, permitted: frozenset[str], reason: str
) -> EconomicPlan:
    """Return the plan that plans nothing, and says why."""
    return EconomicPlan(
        intervals=(),
        runs=(),
        violation_kwh=0.0,
        cost_eur=0.0,
        hold_cost_eur=0.0,
        switching_cost_eur=0.0,
        terminal_floor_kwh=terminal_floor_kwh,
        terminal_binding=False,
        permitted=permitted,
        available=False,
        unavailable_reason=reason,
    )


def _walk_forward(
    *,
    table: PhysicsTable,
    horizon: EconomicHorizon,
    choice: list[list[list[int]]],
    outcomes_per_interval: list[dict[int, _DeltaOutcome]],
    start_bucket: int,
    terminal_floor_kwh: float,
    terminal_bucket: int,
    minimum_trade_gain_eur: float,
    permitted: frozenset[str],
) -> EconomicPlan:
    """Replay the chosen moves to produce the plan the caller reads.

    Every figure below is recomputed from the chosen move rather than carried out
    of the value table, so the published plan's own accounting is exact and cannot
    inherit a rounding from the search.
    """
    entries: list[EconomicInterval] = []
    bucket = start_bucket
    run = _RUN_IDLE
    total_cost = 0.0
    total_switching = 0.0
    total_violation = 0.0
    worst_shortfall = 0.0
    first_violation: int | None = None

    for position, demand in enumerate(horizon.demands):
        offset = choice[position][bucket][run]
        if offset < 0:  # pragma: no cover - the start state was reachable
            break
        move = table.moves[bucket][offset]
        price = horizon.prices[position]
        outcome = outcomes_per_interval[position][move.target - bucket]
        run_start = outcome.run_state != _RUN_IDLE and outcome.run_state != run
        if run_start:
            total_switching += minimum_trade_gain_eur
        total_cost += outcome.cost_eur

        landed = table.energy(move.target)
        shortfall = max(0.0, horizon.planning_reserve_kwh[position] - landed)
        if shortfall > 0.0:
            total_violation += shortfall
            worst_shortfall = max(worst_shortfall, shortfall)
            if first_violation is None:
                first_violation = demand.index

        entries.append(
            EconomicInterval(
                index=demand.index,
                action=outcome.action,
                start_energy_dc_kwh=table.energy(bucket),
                battery_delta_dc_kwh=move.delta_dc_kwh,
                battery_charge_ac_kwh=outcome.charge_ac_kwh,
                battery_discharge_ac_kwh=outcome.discharge_ac_kwh,
                grid_import_kwh=outcome.grid_import_kwh,
                grid_export_kwh=outcome.grid_export_kwh,
                pv_curtailed_kwh=outcome.curtailed_kwh,
                cost_eur=outcome.cost_eur,
                import_price_eur_kwh=price.import_eur_kwh,
                export_price_eur_kwh=price.export_eur_kwh,
                run_start=run_start,
                constraints=move.constraints,
            )
        )
        bucket = move.target
        run = outcome.run_state

    return EconomicPlan(
        intervals=tuple(entries),
        runs=runs_from(tuple(entries)),
        violation_kwh=total_violation,
        cost_eur=total_cost,
        hold_cost_eur=hold_cost(horizon=horizon),
        switching_cost_eur=total_switching,
        terminal_floor_kwh=terminal_floor_kwh,
        terminal_binding=bucket <= terminal_bucket,
        permitted=permitted,
        available=bool(entries),
        unavailable_reason=None if entries else ECONOMIC_UNAVAILABLE_HORIZON_EMPTY,
        worst_shortfall_kwh=worst_shortfall,
        first_violation_index=first_violation,
    )


def hold_cost(*, horizon: EconomicHorizon) -> float:
    """Return what the horizon costs if the battery is left alone.

    The counterfactual every economic figure is measured against, and the same one
    Phase 3 already uses as its reference trajectory. Ambient absorption is not
    modelled here: an idle battery in this table does not move, so this is the
    cost of the grid meeting the whole net load. That makes it a *conservative*
    baseline -- a real inverter absorbing surplus would do better -- and the
    published gain is therefore never flattered by it.
    """
    total = 0.0
    for position, demand in enumerate(horizon.demands):
        price = horizon.prices[position]
        flows = split_grid_energy(
            load_ac_kwh=demand.baseline_kwh or 0.0,
            pv_ac_kwh=demand.pv_kwh or 0.0,
            charge_ac_kwh=0.0,
            discharge_ac_kwh=0.0,
        )
        total += (price.import_eur_kwh or 0.0) * flows.import_kwh
        total -= (price.export_eur_kwh or 0.0) * flows.export_kwh
    return total


def runs_from(intervals: tuple[EconomicInterval, ...]) -> tuple[EconomicRun, ...]:
    """Group a plan's intervals into maximal contiguous runs of one action.

    Runs, not intervals, are the unit a user reads and the unit the switching cost
    is charged against. A run broken by a single idle interval is two runs, which
    is what makes the cost discourage chattering rather than long windows.
    """
    runs: list[EconomicRun] = []
    current: list[EconomicInterval] = []

    def flush() -> None:
        if not current:
            return
        prices = [
            entry.export_price_eur_kwh
            if entry.action in (ECONOMIC_ACTION_EXPORT, ECONOMIC_ACTION_DISCHARGE)
            else entry.import_price_eur_kwh
            for entry in current
        ]
        known = [value for value in prices if value is not None]
        runs.append(
            EconomicRun(
                action=current[0].action,
                start_index=current[0].index,
                end_index=current[-1].index,
                interval_count=len(current),
                battery_charge_ac_kwh=sum(e.battery_charge_ac_kwh for e in current),
                battery_discharge_ac_kwh=sum(
                    e.battery_discharge_ac_kwh for e in current
                ),
                grid_import_kwh=sum(e.grid_import_kwh for e in current),
                grid_export_kwh=sum(e.grid_export_kwh for e in current),
                pv_curtailed_kwh=sum(e.pv_curtailed_kwh for e in current),
                first_power_kw=(
                    (
                        current[0].battery_charge_ac_kwh
                        + current[0].battery_discharge_ac_kwh
                    )
                    / INTERVAL_HOURS
                ),
                expected_value_eur=-sum(e.cost_eur for e in current),
                min_price_eur_kwh=min(known) if known else None,
                max_price_eur_kwh=max(known) if known else None,
                average_price_eur_kwh=(sum(known) / len(known)) if known else None,
            )
        )
        current.clear()

    for entry in intervals:
        if entry.action == ECONOMIC_ACTION_HOLD:
            flush()
            continue
        if current and entry.action != current[0].action:
            flush()
        current.append(entry)
    flush()
    return tuple(runs)


# -- the pair of plans, and what a consumer reads ---------------------------


def _direction_of(action: str) -> str:
    """Return the battery direction an action represents.

    ``safety_buy`` is a *reason* wearing an action's clothes: it is a charge, and
    comparing it against a plain charge as though the two were different actions
    is how a spurious capability gap appears.
    """
    return ECONOMIC_ACTION_CHARGE if action == ECONOMIC_ACTION_SAFETY_BUY else action


@dataclass(frozen=True, slots=True)
class EconomicOutcome:
    """Both solves, the labels derived from them, and nothing executed.

    Two plans rather than one and a downgrade. ``desired`` is the economic intent
    over every action the physics allows, including actions no actuator in this
    release can perform; ``capability`` is a separately computed plan over the
    actions that do have a primitive. Keeping them apart is what lets the optimum
    stay undistorted while execution never silently substitutes anything.

    ``relaxed`` exists only to attribute a label: it is the same solve with the
    reserve relaxed to the configured floor, and the difference tells us which
    charging exists *because* of the reserve. It is never published as a plan and
    never acted on.
    """

    desired: EconomicPlan
    capability: EconomicPlan
    relaxed: EconomicPlan | None
    horizon: EconomicHorizon
    reserve_above_capacity_kwh: float
    buckets: int
    bucket_kwh: float
    table_ms: float
    solve_ms: float
    safety_buy_runs: tuple[int, ...] = ()

    # -- what the entity reads --------------------------------------------

    @property
    def available(self) -> bool:
        """Return whether a plan could be built at all."""
        return self.desired.available

    @property
    def unavailable_reason(self) -> str | None:
        """Return why not, or ``None``."""
        return self.desired.unavailable_reason

    @property
    def action(self) -> str:
        """Return the economically desired action, ``safety_buy`` where it applies."""
        run = self.desired.published_run
        if run is None:
            return ECONOMIC_ACTION_HOLD
        if run.start_index in self.safety_buy_runs:
            return ECONOMIC_ACTION_SAFETY_BUY
        return run.action

    @property
    def capability_action(self) -> str:
        """Return the action selected using only implemented actuators.

        **Before** the global execution barrier, and that is the whole contract:
        it is not a claim that anything was or could be executed in this release,
        only that the actuators which exist could have produced it.
        """
        run = self.capability.published_run
        if run is None:
            return ECONOMIC_ACTION_HOLD
        if run.start_index in self.safety_buy_runs:
            return ECONOMIC_ACTION_SAFETY_BUY
        return run.action

    @property
    def capability_gap_reason(self) -> str:
        """Return why the two differ, for diagnostics rather than the entity.

        Compared on the underlying *direction*, with ``safety_buy`` normalised
        back to the charge it is. Otherwise a desired charge attributed to the
        reserve and a capability charge that was not would read as a capability
        gap, when both plans are doing the same thing for the same reason.
        """
        desired = _direction_of(self.action)
        capability = _direction_of(self.capability_action)
        if desired == capability:
            return ECONOMIC_GAP_NONE
        if desired in (ECONOMIC_ACTION_EXPORT, ECONOMIC_ACTION_CURTAIL):
            return ECONOMIC_GAP_NO_PRIMITIVE
        return ECONOMIC_GAP_FORECAST_INFEASIBLE

    @property
    def reason(self) -> str:
        """Return why the optimizer wants what it wants.

        A bounded vocabulary, read off the plan rather than declared. Reserve
        recovery outranks everything because it is the only reason that is not
        economic, and a user seeing it needs to know the figure beside it was not
        chosen for profit.
        """
        run = self.desired.published_run
        if run is None:
            return ECONOMIC_REASON_NO_ACTION
        if self.desired.violation_kwh > 0.0:
            return ECONOMIC_REASON_RESERVE_RECOVERY
        if run.start_index in self.safety_buy_runs:
            return ECONOMIC_REASON_SAFETY_BUY
        if run.action == ECONOMIC_ACTION_CURTAIL:
            return ECONOMIC_REASON_NEGATIVE_EXPORT
        if run.action == ECONOMIC_ACTION_CHARGE:
            return ECONOMIC_REASON_CHEAP_WINDOW
        if self._makes_headroom(run):
            return ECONOMIC_REASON_MAKE_HEADROOM
        return ECONOMIC_REASON_EXPENSIVE_WINDOW

    def _makes_headroom(self, run: EconomicRun) -> bool:
        """Return whether this discharge is followed by free replenishment.

        "Free" means charged with no grid import at all -- production the house
        could not use. A sale followed by that is a sale that created room for the
        sun, which is a different thing from a sale taken purely on price, and the
        distinction is worth naming for a user staring at a summer evening.
        """
        after = [
            entry
            for entry in self.desired.intervals
            if entry.index > run.end_index
            and entry.battery_charge_ac_kwh > 0.0
            and entry.grid_import_kwh <= 1e-9
        ]
        return sum(entry.battery_charge_ac_kwh for entry in after) > self.bucket_kwh

    @property
    def price_eur_kwh(self) -> float | None:
        """Return the price of the published action, on its own side.

        Import for a charge, export for an export, and for a load-serving
        discharge the import price it avoids -- which is the price that makes
        load shifting worth anything at all.
        """
        run = self.desired.published_run
        if run is None:
            return None
        first = next(
            (
                entry
                for entry in self.desired.intervals
                if entry.index == run.start_index
            ),
            None,
        )
        if first is None:  # pragma: no cover - runs come from these intervals
            return None
        if run.action == ECONOMIC_ACTION_EXPORT:
            return first.export_price_eur_kwh
        return first.import_price_eur_kwh

    @property
    def economic_value_forgone_eur(self) -> float:
        """Return what the missing primitives cost.

        The desired plan's advantage over the capability plan. Zero when every
        action the optimizer wants has an actuator, and the number that justifies
        building one when it does not.
        """
        return self.desired.expected_net_value_eur - (
            self.capability.expected_net_value_eur
        )

    @property
    def reserve_protection_cost_eur(self) -> float:
        """Return what protecting the reserve costs.

        The difference between the lexicographic optimum and the same solve with
        the reserve relaxed to the configured floor. Published prominently because
        it is the figure that would expose a reserve being defended at an absurd
        price, and a lexicographic order needs that visible rather than argued.
        """
        if self.relaxed is None:
            return 0.0
        return self.desired.cost_eur - self.relaxed.cost_eur

    @property
    def safety_buy_ac_kwh(self) -> float:
        """Return the charging that exists because of the reserve."""
        return sum(
            run.battery_charge_ac_kwh
            for run in self.desired.runs
            if run.start_index in self.safety_buy_runs
        )


def build_outcome(
    *,
    table: PhysicsTable,
    horizon: EconomicHorizon,
    start_energy_kwh: float,
    terminal_floor_kwh: float,
    floor_energy_kwh: float,
    minimum_trade_gain_eur: float,
    allow_grid_charging: bool,
    allow_battery_export: bool,
    reserve_above_capacity_kwh: float = 0.0,
    table_ms: float = 0.0,
) -> EconomicOutcome:
    """Run both solves and the label solve, and derive everything published.

    Three solves, and each earns its place: the desired plan is the economic
    intent, the capability plan is what implemented actuators could achieve, and
    the relaxed plan exists only so a charge can be *attributed* to the reserve
    rather than guessed at.
    """
    desired_actions = {ECONOMIC_ACTION_DISCHARGE, ECONOMIC_ACTION_CURTAIL}
    if allow_grid_charging:
        desired_actions.add(ECONOMIC_ACTION_CHARGE)
    if allow_battery_export:
        desired_actions.add(ECONOMIC_ACTION_EXPORT)
    desired_permitted = frozenset(desired_actions)
    capability_permitted = frozenset(desired_actions & IMPLEMENTED_ACTIONS)

    started = time.perf_counter()
    desired = solve(
        table=table,
        horizon=horizon,
        start_energy_kwh=start_energy_kwh,
        terminal_floor_kwh=terminal_floor_kwh,
        minimum_trade_gain_eur=minimum_trade_gain_eur,
        permitted=desired_permitted,
    )
    capability = solve(
        table=table,
        horizon=horizon,
        start_energy_kwh=start_energy_kwh,
        terminal_floor_kwh=terminal_floor_kwh,
        minimum_trade_gain_eur=minimum_trade_gain_eur,
        permitted=capability_permitted,
    )
    relaxed_horizon = EconomicHorizon(
        demands=horizon.demands,
        prices=horizon.prices,
        planning_reserve_kwh=tuple(floor_energy_kwh for _ in horizon.demands),
        limited_by=horizon.limited_by,
    )
    relaxed = solve(
        table=table,
        horizon=relaxed_horizon,
        start_energy_kwh=start_energy_kwh,
        terminal_floor_kwh=terminal_floor_kwh,
        minimum_trade_gain_eur=minimum_trade_gain_eur,
        permitted=desired_permitted,
    )
    solve_ms = (time.perf_counter() - started) * 1000.0

    return EconomicOutcome(
        desired=desired,
        capability=capability,
        relaxed=relaxed,
        horizon=horizon,
        reserve_above_capacity_kwh=reserve_above_capacity_kwh,
        buckets=table.buckets,
        bucket_kwh=table.bucket_kwh,
        table_ms=table_ms,
        solve_ms=solve_ms,
        safety_buy_runs=_safety_buy_runs(desired, relaxed, table.bucket_kwh),
    )


def _safety_buy_runs(
    desired: EconomicPlan, relaxed: EconomicPlan, bucket_kwh: float
) -> tuple[int, ...]:
    """Return the start indices of charge runs that exist because of the reserve.

    Attributed by comparison rather than by inspection of prices: a charge run is
    reserve-driven when the same horizon, solved with the reserve relaxed to the
    configured floor, charges materially less over the same intervals. That is a
    statement about *why* the charge is there, which a price threshold could never
    make -- a cheap interval and a reserve deadline often coincide.
    """
    if not relaxed.available:
        return ()
    relaxed_by_index = {
        entry.index: entry.battery_charge_ac_kwh for entry in relaxed.intervals
    }
    found: list[int] = []
    for run in desired.runs:
        if run.action != ECONOMIC_ACTION_CHARGE:
            continue
        relaxed_charge = sum(
            relaxed_by_index.get(index, 0.0)
            for index in range(run.start_index, run.end_index + 1)
        )
        if run.battery_charge_ac_kwh - relaxed_charge > bucket_kwh:
            found.append(run.start_index)
    return tuple(found)


# -- reporting ---------------------------------------------------------------


ECONOMIC_BASIS: str = (
    "the least-cost way through the prices, load and production that are "
    "actually known, subject to the Phase-7 reserve. Advisory only: this "
    "release sends no command to the inverter, and export and photovoltaic "
    "curtailment have no actuator at all -- they are modelled so the strategy "
    "can be validated before anything is allowed to act on it"
)

#: What the terminal condition is measured against. Named rather than inlined,
#: because the evidence layer and the diagnostics payload must agree on it.
TERMINAL_BASIS: str = "hold_trajectory_end_on_bucket_grid"

ECONOMIC_DECIDES_NOTHING: str = (
    "Phase 8 calculates a plan. It never executes one: no service call reaches "
    "the inverter, and the capability plan beside the desired one describes what "
    "implemented actuators could achieve rather than what was done"
)


def _run_as_dict(run: EconomicRun, *, safety_buy: bool) -> dict[str, Any]:
    """Return one planned run, bounded and flat.

    Every boundary is stated separately because a euro figure is only meaningful
    against the boundary it was measured at, and this is where a reader audits
    that the energy-volume scheduling did what it claims.
    """
    return {
        "action": ECONOMIC_ACTION_SAFETY_BUY if safety_buy else run.action,
        "start_interval": run.start_index,
        "end_interval": run.end_index,
        "interval_count": run.interval_count,
        "energy_kwh": _round_kwh(run.energy_kwh),
        "first_power_kw": _round_kw(run.first_power_kw),
        "average_power_kw": _round_kw(run.average_power_kw),
        "expected_value_eur": _round_eur(run.expected_value_eur),
        "battery_charge_ac_kwh": _round_kwh(run.battery_charge_ac_kwh),
        "battery_discharge_ac_kwh": _round_kwh(run.battery_discharge_ac_kwh),
        "grid_import_kwh": _round_kwh(run.grid_import_kwh),
        "grid_export_kwh": _round_kwh(run.grid_export_kwh),
        "pv_curtailed_kwh": _round_kwh(run.pv_curtailed_kwh),
        "min_price_eur_kwh": run.min_price_eur_kwh,
        "max_price_eur_kwh": run.max_price_eur_kwh,
        "average_price_eur_kwh": _round_eur(run.average_price_eur_kwh),
    }


def _plan_totals(plan: EconomicPlan) -> dict[str, Any]:
    """Return one plan's totals, all at their own boundary."""
    return {
        "expected_net_value_eur": _round_eur(plan.expected_net_value_eur),
        "cost_eur": _round_eur(plan.cost_eur),
        "hold_cost_eur": _round_eur(plan.hold_cost_eur),
        "switching_cost_eur": _round_eur(plan.switching_cost_eur),
        "battery_charge_ac_kwh": _round_kwh(plan.planned_charge_ac_kwh),
        "battery_discharge_ac_kwh": _round_kwh(plan.planned_discharge_ac_kwh),
        "grid_import_kwh": _round_kwh(plan.planned_grid_import_kwh),
        "grid_export_kwh": _round_kwh(plan.planned_grid_export_kwh),
        "pv_curtailed_kwh": _round_kwh(plan.planned_curtailed_kwh),
        "run_count": len(plan.runs),
        "end_energy_dc_kwh": _round_kwh(plan.end_energy_dc_kwh),
    }


def economic_as_dict(
    outcome: EconomicOutcome | None,
    *,
    execution_blocked_reason: str,
    horizon_start: Any = None,
    horizon_end: Any = None,
    provenance: dict[str, Any] | None = None,
    unavailable_reason: str | None = None,
) -> dict[str, Any]:
    """Return the bounded diagnostics form.

    Counts, totals, edges, status and at most eight runs. Never the per-interval
    trajectory: a hundred and ninety-two rows would breach the sixteen-entry
    ceiling every list in this payload is held to, and a truncated one would read
    as a short horizon rather than as a clipped payload.
    """
    if outcome is None or not outcome.available:
        return {
            "available": False,
            "unavailable_reason": (
                unavailable_reason
                or (outcome.unavailable_reason if outcome else None)
                or ECONOMIC_UNAVAILABLE_HORIZON_EMPTY
            ),
            "model_version": ECONOMIC_MODEL_VERSION,
            "decides_nothing": ECONOMIC_DECIDES_NOTHING,
        }

    desired = outcome.desired
    runs = desired.runs[:MAX_ECONOMIC_RUNS_REPORTED]
    payload: dict[str, Any] = {
        "available": True,
        "unavailable_reason": None,
        "model_version": ECONOMIC_MODEL_VERSION,
        "decides_nothing": ECONOMIC_DECIDES_NOTHING,
        "desired": {
            "action": outcome.action,
            "reason": outcome.reason,
            "power_kw": _round_kw(desired.power_kw),
            "energy_kwh": _round_kwh(
                0.0
                if desired.published_run is None
                else desired.published_run.energy_kwh
            ),
            "price_eur_kwh": outcome.price_eur_kwh,
            "totals": _plan_totals(desired),
        },
        "capability": {
            "action": outcome.capability_action,
            "gap_reason": outcome.capability_gap_reason,
            "power_kw": _round_kw(outcome.capability.power_kw),
            "execution_available": False,
            "execution_blocked_reason": execution_blocked_reason,
            "permitted_actions": sorted(outcome.capability.permitted),
            "totals": _plan_totals(outcome.capability),
        },
        "forgone": {
            "economic_value_forgone_eur": _round_eur(
                outcome.economic_value_forgone_eur
            ),
            "rule": (
                "what the missing actuators cost: the desired plan's advantage "
                "over the plan implemented primitives could achieve"
            ),
        },
        "horizon": {
            "start": None if horizon_start is None else horizon_start.isoformat(),
            "end": None if horizon_end is None else horizon_end.isoformat(),
            "intervals": outcome.horizon.intervals,
            "limited_by": outcome.horizon.limited_by,
        },
        "reserve": {
            "violation_kwh_intervals": _round_kwh(desired.violation_kwh),
            "worst_shortfall_kwh": _round_kwh(desired.worst_shortfall_kwh),
            "first_violation_interval": desired.first_violation_index,
            "irreducible": desired.violation_kwh > 0.0,
            "reserve_above_capacity_kwh": _round_kwh(
                outcome.reserve_above_capacity_kwh
            ),
            "reserve_protection_cost_eur": _round_eur(
                outcome.reserve_protection_cost_eur
            ),
            "quantisation_margin_kwh": outcome.bucket_kwh,
            "safety_buy_ac_kwh": _round_kwh(outcome.safety_buy_ac_kwh),
            "rule": (
                "reserve feasibility is compared before economics, so a "
                "shortfall can never unlock a profitable export. The planning "
                "requirement is quantised up to a bucket, so at most one bucket "
                "too much is protected and a violation is always an exact "
                "multiple of it"
            ),
        },
        "terminal": {
            "floor_kwh": _round_kwh(desired.terminal_floor_kwh),
            "basis": TERMINAL_BASIS,
            "binding": desired.terminal_binding,
            "rule": (
                "the plan may not leave the battery worse off than doing "
                "nothing would have. Stored energy is given no price, so this "
                "forecasts nothing -- it only stops the optimizer emptying the "
                "pack in the last priced interval because the data ran out. The "
                "figure is what was *enforced*: the hold trajectory's endpoint "
                "clamped to the same bucket grid the states live on, because a "
                "bound the state space cannot express is a bound nothing can "
                "satisfy"
            ),
        },
        "solver": {
            "buckets": outcome.buckets,
            "bucket_kwh": outcome.bucket_kwh,
            "table_ms": round(outcome.table_ms, 1),
            "solve_ms": round(outcome.solve_ms, 1),
            "solves": 3,
        },
        "runs": [
            _run_as_dict(run, safety_buy=run.start_index in outcome.safety_buy_runs)
            for run in runs
        ],
        "runs_total": len(desired.runs),
        "basis": ECONOMIC_BASIS,
    }
    if provenance is not None:
        payload["provenance"] = provenance
    return payload


# -- fingerprints and evidence ----------------------------------------------


def _digest(payload: Any) -> str:
    """Return a short stable digest of a canonical JSON payload."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[
        :ECONOMIC_FINGERPRINT_CHARS
    ]


def fingerprint_economic(
    *,
    price_fingerprint: str | None,
    load_fingerprint: str | None,
    pv_fingerprint: str | None,
    reserve_fingerprint: str | None,
    config_fingerprint: str,
    settings_fingerprint: str,
) -> str:
    """Return the digest that decides whether to store a snapshot.

    Keyed on the **inputs**, never on the plan. The plan is a function of the
    interval it is asked from -- the horizon starts at the next boundary -- so it
    differs every quarter-hour even when nothing has changed, and a digest over it
    would store ninety-six documents a day and break the rule that a refresh
    reproducing what it produced fifteen minutes ago costs no I/O. The reserve
    layer learned this the hard way; this inherits the lesson rather than
    repeating it.
    """
    return _digest(
        {
            "model_version": ECONOMIC_MODEL_VERSION,
            "price": price_fingerprint,
            "load": load_fingerprint,
            "pv": pv_fingerprint,
            "reserve": reserve_fingerprint,
            "config": config_fingerprint,
            "settings": settings_fingerprint,
        }
    )


def fingerprint_settings(
    *,
    minimum_trade_gain_eur: float,
    allow_grid_charging: bool,
    allow_battery_export: bool,
    bucket_kwh: float,
) -> str:
    """Return a digest of the economic settings a plan rests on.

    Separate from the battery-configuration digest because these are the user's
    *economic* choices rather than their hardware, and a later phase asking "why
    did it want that" needs to know which threshold was in force.
    """
    return _digest(
        {
            "minimum_trade_gain_eur": minimum_trade_gain_eur,
            "allow_grid_charging": allow_grid_charging,
            "allow_battery_export": allow_battery_export,
            "bucket_kwh": bucket_kwh,
            "model_version": ECONOMIC_MODEL_VERSION,
        }
    )


def action_fingerprint(outcome: EconomicOutcome | None) -> str | None:
    """Return the digest that decides whether an Activity entry is worth making.

    Deliberately coarse. Rounded to the material thresholds, so a plan whose
    power shifts by a watt or whose window slides by nothing produces the same
    fingerprint and therefore no entry. Ninety-six unchanged refreshes must be
    silent, and the only way to guarantee that is for the fingerprint not to
    notice noise.
    """
    if outcome is None or not outcome.available:
        return None
    run = outcome.desired.published_run
    if run is None:
        return None
    return _digest(
        {
            "action": outcome.action,
            "capability": outcome.capability_action,
            "start": run.start_index,
            "end": run.end_index,
            "power": round(run.first_power_kw / ECONOMIC_MATERIAL_POWER_KW),
            "energy": round(run.energy_kwh / ECONOMIC_MATERIAL_ENERGY_KWH),
        }
    )


@dataclass(frozen=True, slots=True)
class EconomicSnapshot:
    """What Phase 8 believed, at one instant. Scalars only.

    Why record it when nothing in this release learns from it: the *economic
    settings and the actuator capability* a plan was computed under are not
    recoverable afterwards. Prices, load, production and the reserve are already
    persisted, so the arithmetic is reproducible -- but a threshold the user
    changed, or an opt-in they turned on, would otherwise make every earlier plan
    unverifiable.

    No outcome half. What a plan *actually* cost needs measured grid flows, which
    this release begins recording separately and which Phase 9 will score against.
    """

    issued_at: Any
    target_day: Any
    tz_key: str

    available: bool
    unavailable_reason: str | None

    desired_action: str
    capability_action: str
    reason: str
    execution_blocked_reason: str

    desired_value_eur: float | None
    capability_value_eur: float | None
    value_forgone_eur: float | None
    reserve_protection_cost_eur: float | None

    violation_kwh: float
    safety_buy_ac_kwh: float
    terminal_floor_kwh: float
    terminal_binding: bool

    horizon_intervals: int
    horizon_limited_by: str
    bucket_kwh: float

    price_fingerprint: str | None
    load_fingerprint: str | None
    pv_fingerprint: str | None
    reserve_fingerprint: str | None
    config_fingerprint: str
    settings_fingerprint: str

    fingerprint: str
    model_version: int = ECONOMIC_MODEL_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Return the compact serialisable form, matching the sibling families."""
        return {
            "at": self.issued_at.isoformat(),
            "tz": self.tz_key,
            "a": 1 if self.available else 0,
            "ur": self.unavailable_reason,
            "da": self.desired_action,
            "ca": self.capability_action,
            "rs": self.reason,
            "eb": self.execution_blocked_reason,
            "dv": self.desired_value_eur,
            "cv": self.capability_value_eur,
            "fg": self.value_forgone_eur,
            "rp": self.reserve_protection_cost_eur,
            "vi": self.violation_kwh,
            "sb": self.safety_buy_ac_kwh,
            "tf": self.terminal_floor_kwh,
            "tb": 1 if self.terminal_binding else 0,
            "n": self.horizon_intervals,
            "lb": self.horizon_limited_by,
            "bk": self.bucket_kwh,
            "pf": self.price_fingerprint,
            "lf": self.load_fingerprint,
            "vf": self.pv_fingerprint,
            "rf": self.reserve_fingerprint,
            "cf": self.config_fingerprint,
            "sf": self.settings_fingerprint,
            "f": self.fingerprint,
            "mv": self.model_version,
        }

    @classmethod
    def from_dict(cls, target_day: date, raw: Any) -> EconomicSnapshot | None:
        """Rebuild a snapshot, or return ``None`` when the entry is unusable."""
        if not isinstance(raw, Mapping):
            return None
        stamp = raw.get("at")
        if not isinstance(stamp, str) or not stamp:
            return None
        try:
            issued = datetime.fromisoformat(stamp)
        except ValueError:
            return None
        if issued.tzinfo is None:
            return None
        tz_key = raw.get("tz")
        return cls(
            issued_at=issued,
            target_day=target_day,
            tz_key=tz_key if isinstance(tz_key, str) and tz_key else "UTC",
            available=bool(raw.get("a")),
            unavailable_reason=(raw["ur"] if isinstance(raw.get("ur"), str) else None),
            desired_action=str(raw.get("da") or ECONOMIC_ACTION_HOLD),
            capability_action=str(raw.get("ca") or ECONOMIC_ACTION_HOLD),
            reason=str(raw.get("rs") or ECONOMIC_REASON_NO_ACTION),
            execution_blocked_reason=str(raw.get("eb") or ""),
            desired_value_eur=_finite(raw.get("dv")),
            capability_value_eur=_finite(raw.get("cv")),
            value_forgone_eur=_finite(raw.get("fg")),
            reserve_protection_cost_eur=_finite(raw.get("rp")),
            violation_kwh=_finite(raw.get("vi")) or 0.0,
            safety_buy_ac_kwh=_finite(raw.get("sb")) or 0.0,
            terminal_floor_kwh=_finite(raw.get("tf")) or 0.0,
            terminal_binding=bool(raw.get("tb")),
            horizon_intervals=(raw["n"] if isinstance(raw.get("n"), int) else 0),
            horizon_limited_by=str(raw.get("lb") or "unknown"),
            bucket_kwh=_finite(raw.get("bk")) or ECONOMIC_BUCKET_KWH,
            price_fingerprint=(raw["pf"] if isinstance(raw.get("pf"), str) else None),
            load_fingerprint=(raw["lf"] if isinstance(raw.get("lf"), str) else None),
            pv_fingerprint=(raw["vf"] if isinstance(raw.get("vf"), str) else None),
            reserve_fingerprint=(raw["rf"] if isinstance(raw.get("rf"), str) else None),
            config_fingerprint=str(raw.get("cf") or ""),
            settings_fingerprint=str(raw.get("sf") or ""),
            fingerprint=str(raw.get("f") or ""),
            model_version=(
                raw["mv"] if isinstance(raw.get("mv"), int) else ECONOMIC_MODEL_VERSION
            ),
        )


def _finite(value: Any) -> float | None:
    """Return a usable float, or ``None``. Booleans are refused."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def build_economic_snapshot(
    outcome: EconomicOutcome | None,
    *,
    issued_at: Any,
    target_day: Any,
    tz_key: str,
    execution_blocked_reason: str,
    config_fingerprint: str,
    settings_fingerprint: str,
    price_fingerprint: str | None = None,
    load_fingerprint: str | None = None,
    pv_fingerprint: str | None = None,
    reserve_fingerprint: str | None = None,
    unavailable_reason: str | None = None,
) -> EconomicSnapshot:
    """Return the snapshot for one refresh."""
    available = outcome is not None and outcome.available
    return EconomicSnapshot(
        issued_at=issued_at,
        target_day=target_day,
        tz_key=tz_key,
        available=available,
        unavailable_reason=(
            None
            if available
            else (
                unavailable_reason
                or (outcome.unavailable_reason if outcome else None)
                or ECONOMIC_UNAVAILABLE_HORIZON_EMPTY
            )
        ),
        desired_action=outcome.action if available else ECONOMIC_ACTION_HOLD,
        capability_action=(
            outcome.capability_action if available else ECONOMIC_ACTION_HOLD
        ),
        reason=outcome.reason if available else ECONOMIC_REASON_NO_ACTION,
        execution_blocked_reason=execution_blocked_reason,
        desired_value_eur=(
            _round_eur(outcome.desired.expected_net_value_eur) if available else None
        ),
        capability_value_eur=(
            _round_eur(outcome.capability.expected_net_value_eur) if available else None
        ),
        value_forgone_eur=(
            _round_eur(outcome.economic_value_forgone_eur) if available else None
        ),
        reserve_protection_cost_eur=(
            _round_eur(outcome.reserve_protection_cost_eur) if available else None
        ),
        violation_kwh=(
            _round_kwh(outcome.desired.violation_kwh) or 0.0 if available else 0.0
        ),
        safety_buy_ac_kwh=(
            _round_kwh(outcome.safety_buy_ac_kwh) or 0.0 if available else 0.0
        ),
        terminal_floor_kwh=(
            _round_kwh(outcome.desired.terminal_floor_kwh) or 0.0 if available else 0.0
        ),
        terminal_binding=bool(available and outcome.desired.terminal_binding),
        horizon_intervals=outcome.horizon.intervals if available else 0,
        horizon_limited_by=outcome.horizon.limited_by if available else "unknown",
        bucket_kwh=outcome.bucket_kwh if available else ECONOMIC_BUCKET_KWH,
        price_fingerprint=price_fingerprint,
        load_fingerprint=load_fingerprint,
        pv_fingerprint=pv_fingerprint,
        reserve_fingerprint=reserve_fingerprint,
        config_fingerprint=config_fingerprint,
        settings_fingerprint=settings_fingerprint,
        fingerprint=fingerprint_economic(
            price_fingerprint=price_fingerprint,
            load_fingerprint=load_fingerprint,
            pv_fingerprint=pv_fingerprint,
            reserve_fingerprint=reserve_fingerprint,
            config_fingerprint=config_fingerprint,
            settings_fingerprint=settings_fingerprint,
        ),
    )
