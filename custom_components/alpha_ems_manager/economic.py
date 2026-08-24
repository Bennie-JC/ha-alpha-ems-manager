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

**The only physical floor is the reserve.** Since beta.18 ``terminal_floor_kwh``
is the user's configured minimum expressed on the solver's own bucketed grid, and
nothing more. It used to be the idle-with-absorption endpoint -- "end no lower than
doing nothing would have" -- which sounds like a rule against dumping the battery
and is not one: with no surplus production ahead the idle walk is flat, so it read
"end no lower than you are now" and forbade net discharge outright. Recomputed from
the current charge every refresh it also ratcheted upward as the pack filled.

What protects stored energy is the pointwise Phase-7 reserve, enforced at every
interval rather than only the last, and forecast further ahead than the prices
reach -- production is forecast for today and tomorrow while prices extend only as
far as they have published, so the requirement is at its largest exactly where the
prices stop. Energy above it is discretionary, and the objective may trade it.

``HoldPolicy`` remains the conceptual counterfactual every euro is measured
against. The floor is still expressed on the bucket grid, because a floor the state
space cannot represent would make an otherwise valid horizon artificially
infeasible; ``TERMINAL_BASIS`` records which quantity is being read.
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
    ECONOMIC_BUCKET_BAND_KWH,
    ECONOMIC_BUCKET_KWH,
    ECONOMIC_BUCKET_MAX_DIVISOR,
    ECONOMIC_BUCKET_RULE_ALIGNED,
    ECONOMIC_BUCKET_RULE_CONSTANT,
    ECONOMIC_BUCKET_STATE_BUDGET,
    ECONOMIC_CHARGE_SOURCE_GRID,
    ECONOMIC_CHARGE_SOURCE_MIXED,
    ECONOMIC_CHARGE_SOURCE_NONE,
    ECONOMIC_CHARGE_SOURCE_PRODUCTION,
    ECONOMIC_DIRECTION_CHARGE,
    ECONOMIC_DIRECTION_DISCHARGE,
    ECONOMIC_DIRECTION_IDLE,
    ECONOMIC_EUR_PRECISION,
    ECONOMIC_FINGERPRINT_CHARS,
    ECONOMIC_GAP_FORECAST_INFEASIBLE,
    ECONOMIC_GAP_NO_PRIMITIVE,
    ECONOMIC_GAP_NONE,
    ECONOMIC_MODEL_VERSION,
    ECONOMIC_POWER_PRECISION,
    ECONOMIC_REASON_CHEAP_WINDOW,
    ECONOMIC_REASON_EXPENSIVE_WINDOW,
    ECONOMIC_REASON_MAKE_HEADROOM,
    ECONOMIC_REASON_NEGATIVE_EXPORT,
    ECONOMIC_REASON_NO_ACTION,
    ECONOMIC_REASON_RESERVE_RECOVERY,
    ECONOMIC_REASON_SAFETY_BUY,
    ECONOMIC_TOMORROW_ABSENT,
    ECONOMIC_UNAVAILABLE_HORIZON_EMPTY,
    ECONOMIC_UNAVAILABLE_TERMINAL_UNREACHABLE,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_HOLD,
    EXECUTION_INTENT_NET_EXPORT,
    EXECUTION_INTENT_SERVE_LOAD,
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

#: Every action the physics allows, for callers that need a permission set but
#: whose answer cannot depend on it -- the ambient walk takes only idle and
#: absorption moves, and both are permitted unconditionally.
_ALL_ACTIONS: frozenset[str] = frozenset(
    {
        ECONOMIC_ACTION_CHARGE,
        ECONOMIC_ACTION_DISCHARGE,
        ECONOMIC_ACTION_EXPORT,
        ECONOMIC_ACTION_CURTAIL,
    }
)

#: Run-state of the previous interval, which is what makes a *run* detectable and
#: therefore what makes ``minimum_trade_gain_eur`` a per-run cost rather than a
#: per-interval one.
_RUN_IDLE = 0
_RUN_CHARGE = 1
_RUN_DISCHARGE = 2
_RUN_STATES = (_RUN_IDLE, _RUN_CHARGE, _RUN_DISCHARGE)

#: A fourth *classification*, never a fourth dimension.
#:
#: An interval that stores production the house cannot use is ambient physical
#: behaviour, not a decision -- so it must be **transparent** to whatever run is
#: in progress: it neither starts one nor breaks one. Resolving it to the
#: incoming run state is what stops a sunny quarter in the middle of a paid
#: charging campaign splitting the campaign and charging
#: ``minimum_trade_gain_eur`` a second time.
#:
#: Deliberately outside ``_RUN_STATES``: it is resolved away before the value
#: table is indexed, so the search space stays three-deep and costs nothing.
#: A **true** idle interval -- one that moves no energy at all -- still breaks a
#: run, which is the documented and intended behaviour.
_RUN_ABSORB = 3


def _resolved_run_state(outcome_state: int, incoming: int) -> int:
    """Return the run state an outcome leaves behind, given the one it inherits.

    The single definition of the absorption rule, used by the search and by the
    forward walk so the fee the objective charged and the fee the plan reports
    cannot disagree.

    Absorption is transparent to a **charge** campaign and to nothing else. It is
    itself a charge, so it can continue one -- that is the whole point, and it is
    what stops a sunny quarter splitting a paid charging window in two and paying
    the fee again. Against a *discharge* run it is not transparent at all: the
    battery has reversed, and pretending the discharge continued would suppress a
    fee that a genuine direction change has to pay. There it behaves exactly as a
    true idle interval does, and breaks the run.
    """
    if outcome_state != _RUN_ABSORB:
        return outcome_state
    return _RUN_CHARGE if incoming == _RUN_CHARGE else _RUN_IDLE


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

    def _peak_power_kw(self, sign: int) -> float:
        """Return the largest AC power representable in one direction.

        Separately per direction, because the two are **not** the same figure and
        beta.16 published only their maximum. The state space is quantised in
        **DC** energy while the nameplate limit is an **AC** power, and the DC
        energy of a maximum-power quarter differs between charging and
        discharging by the round-trip efficiency -- so a lattice that expresses
        one exactly generally truncates the other. Reporting the larger of the
        two hid losses of up to thirty per cent on the smaller side.
        """
        best = 0.0
        for source, row in enumerate(self.moves):
            for move in row:
                delta = move.target - source
                if (delta > 0 and sign > 0) or (delta < 0 and sign < 0):
                    best = max(best, move.power_kw)
        return round(best, 4)

    @property
    def max_representable_charge_kw(self) -> float:
        """Return the largest AC charge power a single transition can express."""
        return self._peak_power_kw(1)

    @property
    def max_representable_discharge_kw(self) -> float:
        """Return the largest AC discharge power a single transition can express."""
        return self._peak_power_kw(-1)

    @property
    def max_representable_power_kw(self) -> float:
        """Return the larger of the two directional peaks.

        Kept because it is the figure beta.16 published, and because a single
        headline number is still the right thing for a summary line. Read the
        directional pair when the question is whether power binds: on the
        reference pack with the beta.16 lattice both were 9.4868 kW, but on a
        3 kW installation charging reached only 2.1082 kW -- **29.7 %** short --
        while discharging reached 2.8460 kW, and one number cannot say that.
        """
        return round(
            max(self.max_representable_charge_kw, self.max_representable_discharge_kw),
            4,
        )

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


def _directional_peaks(table: PhysicsTable) -> tuple[float, float]:
    """Return ``(charge, discharge)`` representable peak power for a table."""
    return table.max_representable_charge_kw, table.max_representable_discharge_kw


def select_bucket_kwh(
    limits: BatteryLimits,
    *,
    floor_energy_kwh: float,
) -> tuple[float, str]:
    """Return the state-space bucket to solve on, and the rule that chose it.

    **The bucket is a rounding, not a refinement, and that is the whole idea.**

    A maximum-power quarter is a fixed amount of DC energy. If the bucket divides
    it exactly, that power is representable; otherwise the lattice truncates and
    the clamp correctly discards the over-large move. On the reference pack the
    beta.16 constant bucket left 9.4868 kW of a configured 10 kW reachable --
    five per cent unusable in both directions, and on a 3 kW installation nearly
    thirty per cent unusable on the charge side.

    So this searches integer ``k`` for ``bucket = quarter_dc / k``. Because the
    bucket stays constant *within* a solve, every surviving move is still exactly
    linear in its delta -- the invariant the per-delta pricing table and the
    whole performance argument rest on is untouched. That is what makes this
    cheap where refining the grid was not: on the reference pack it produces
    **fewer** states and a **faster** solve than beta.16.

    Three constraints, all hard, and a candidate failing any of them is
    discarded rather than traded off:

    * **No regression, in either direction.** Both representable peaks must be
      greater than or equal to the ones the beta.16 bucket produced. A lattice
      that gained charge power by losing discharge power would be a different
      compromise, not an improvement -- measured on a 22 kWh / 5 kW pack a naive
      alignment did exactly that, taking discharge from 5.13 % short to 10.0 %.
    * **Energy resolution cannot collapse.** The bucket must stay inside
      ``ECONOMIC_BUCKET_BAND_KWH``. Left unconstrained the search happily
      proposed ten states for a 22 kWh pack: peak power exact, state of charge
      resolved to 2.4 kWh, and every reserve and energy figure ruined.
    * **Complexity must not grow for nothing.** The state count may exceed the
      beta.16 count by at most ``ECONOMIC_BUCKET_STATE_BUDGET``.

    When no candidate qualifies the beta.16 bucket is returned unchanged, so an
    installation can only ever be left as it was or improved. Both the bucket and
    the rule are published, because two installations may legitimately end up on
    different lattices and a support question is unanswerable without knowing
    which.
    """
    baseline = build_physics_table(
        limits, floor_energy_kwh=floor_energy_kwh, bucket_kwh=ECONOMIC_BUCKET_KWH
    )
    if baseline is None:
        return ECONOMIC_BUCKET_KWH, ECONOMIC_BUCKET_RULE_CONSTANT

    base_charge, base_discharge = _directional_peaks(baseline)
    budget = int(baseline.buckets * (1.0 + ECONOMIC_BUCKET_STATE_BUDGET)) + 1
    quarter_dc = limits.max_charge_kw * INTERVAL_HOURS * limits.charge_efficiency
    if quarter_dc <= 0.0:  # pragma: no cover - build_limits precludes it
        return ECONOMIC_BUCKET_KWH, ECONOMIC_BUCKET_RULE_CONSTANT

    low, high = ECONOMIC_BUCKET_BAND_KWH
    best: tuple[float, int, float] | None = None
    for k in range(1, ECONOMIC_BUCKET_MAX_DIVISOR + 1):
        bucket = quarter_dc / k
        if bucket < low - 1e-12 or bucket > high + 1e-12:
            continue
        candidate = build_physics_table(
            limits, floor_energy_kwh=floor_energy_kwh, bucket_kwh=bucket
        )
        if candidate is None or candidate.buckets > budget:
            continue
        charge, discharge = _directional_peaks(candidate)
        if charge < base_charge - 1e-9 or discharge < base_discharge - 1e-9:
            continue
        gain = (charge - base_charge) + (discharge - base_discharge)
        # Most power recovered, then fewest states. Never the reverse: a lattice
        # is chosen for what it can express, and only then for what it costs.
        if best is None or (-gain, candidate.buckets) < (-best[0], best[1]):
            best = (gain, candidate.buckets, bucket)

    if best is None or best[0] <= 1e-9:
        return ECONOMIC_BUCKET_KWH, ECONOMIC_BUCKET_RULE_CONSTANT
    return best[2], ECONOMIC_BUCKET_RULE_ALIGNED


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
    #: What this interval would have imported, exported and cost with the battery
    #: left alone. The interval's own counterfactual, carried so every marginal
    #: figure below is an exact difference rather than an estimate.
    idle_import_kwh: float = 0.0
    idle_export_kwh: float = 0.0
    idle_cost_eur: float = 0.0
    #: Whether this interval stored production the house could not use. Ambient
    #: physical behaviour, so it is transparent to a run in progress -- it neither
    #: starts one nor breaks one.
    absorbing: bool = False

    @property
    def moves_battery(self) -> bool:
        """Return whether the battery moved at all in this interval."""
        return self.battery_charge_ac_kwh > 0.0 or self.battery_discharge_ac_kwh > 0.0

    @property
    def marginal_grid_import_kwh(self) -> float:
        """Return the grid import the battery caused, signed.

        Positive when the battery bought; negative when it displaced an import
        that would have happened anyway. Exact within the model -- the idle
        baseline is computed from the same ``split_grid_energy`` as the flow it is
        subtracted from -- so this is attribution, not a heuristic.
        """
        return self.grid_import_kwh - self.idle_import_kwh

    @property
    def marginal_grid_export_kwh(self) -> float:
        """Return the grid export the battery caused, signed."""
        return self.grid_export_kwh - self.idle_export_kwh

    @property
    def marginal_cost_eur(self) -> float:
        """Return what this interval cost *versus leaving the battery alone*.

        Negative means the battery saved money here. This is the figure a reader
        wants and the one the old per-run ``expected_value_eur`` could not
        express: a discharge that exactly covers house load has a raw cash flow
        of zero while saving the entire import bill, and only a difference
        against the counterfactual shows it.
        """
        return self.cost_eur - self.idle_cost_eur


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
    #: The run's raw cash flow, sign-flipped: ``-sum(cost_eur)``.
    #:
    #: **Named for what it is.** Until beta.16 this was called
    #: ``expected_value_eur``, which it never was: every charge run is negative by
    #: construction because it imports at a positive price, and a discharge that
    #: exactly covers house load reads zero while saving the whole import bill.
    #: Use :attr:`marginal_cost_eur` for the economics.
    net_cash_flow_eur: float
    min_price_eur_kwh: float | None
    max_price_eur_kwh: float | None
    average_price_eur_kwh: float | None
    #: The grid flows and cost this run caused, each measured against the run's
    #: own idle counterfactual. Exact, not estimated -- see
    #: :attr:`EconomicInterval.marginal_grid_import_kwh`.
    marginal_grid_import_kwh: float = 0.0
    marginal_grid_export_kwh: float = 0.0
    #: What the run cost versus leaving the battery alone through the same
    #: intervals. **Negative means it saved money.** This is the economics.
    marginal_cost_eur: float = 0.0
    #: Which direction the battery actually moved, as the objective saw it. A
    #: single physical discharge can carry both the ``discharge`` and ``export``
    #: labels as house load rises and falls, so the label is not the direction and
    #: the run count is not the number of switches.
    direction: str = ECONOMIC_DIRECTION_IDLE
    #: Whether the switching fee was charged at this run's start.
    charged_switching_fee: bool = False

    @property
    def charge_source(self) -> str:
        """Return where a charge run's energy came from: production, grid, or both.

        Derived from the exact marginal import, not a heuristic. The boundary is one
        state-space bucket of energy (``ECONOMIC_BUCKET_KWH``): below that the grid
        contribution is unrepresentable, so calling it anything but production would
        be over-claiming.

        Meaningless for a non-charging run, which reports
        ``ECONOMIC_CHARGE_SOURCE_NONE``.
        """
        if self.battery_charge_ac_kwh <= 0.0:
            return ECONOMIC_CHARGE_SOURCE_NONE
        from_grid = max(0.0, self.marginal_grid_import_kwh)
        if from_grid <= ECONOMIC_BUCKET_KWH:
            return ECONOMIC_CHARGE_SOURCE_PRODUCTION
        if self.battery_charge_ac_kwh - from_grid <= ECONOMIC_BUCKET_KWH:
            return ECONOMIC_CHARGE_SOURCE_GRID
        return ECONOMIC_CHARGE_SOURCE_MIXED

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
    #: What the per-kWh grid-charge margin charged this plan, and the energy it
    #: was charged on. Notional, exactly like the switching cost: nobody pays it,
    #: so it is added back in :attr:`expected_net_value_eur` rather than being
    #: allowed to understate what the plan earns.
    grid_charge_margin_eur: float = 0.0
    marginal_grid_charge_kwh: float = 0.0

    # -- what the entity reads --------------------------------------------

    @property
    def action(self) -> str:
        """Return the action of the run in progress, or ``hold``."""
        run = self.current_run
        return ECONOMIC_ACTION_HOLD if run is None else run.action

    @property
    def current_run(self) -> EconomicRun | None:
        """Return the run in progress, if one starts at the horizon's head.

        "In progress" means starting at the **first interval of this horizon**,
        not at interval zero of the civil day. Until beta.16 this tested
        ``start_index == 0``, which production could never satisfy: the horizon
        begins at ``elapsed_intervals + 1``, so index zero is midnight and is
        already in the past. The property was therefore always ``None`` and
        :attr:`published_run` silently degraded to :attr:`next_run` -- which is
        why the published action could not distinguish "happening now" from
        "planned for tomorrow evening".
        """
        if not self.intervals:
            return None
        head = self.intervals[0].index
        for run in self.runs:
            if run.start_index == head:
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
    def direction_changes(self) -> int:
        """Return how many times the battery actually changed direction.

        The number the switching fee was charged against, and **not** the run
        count: one physical discharge can be reported as several runs as its
        label flips between ``discharge`` and ``export`` beneath a varying house
        load. On the live installation seven reported runs were three direction
        changes.
        """
        return sum(1 for entry in self.intervals if entry.run_start)

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
    grid_charge_margin_eur_per_kwh: float = 0.0,
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

    # ``terminal_floor_kwh`` is a **physical floor the plan must still hold at the
    # end of the horizon**, and since beta.18 its only production value is the
    # user's configured minimum. Down, so the plan never assumes more stored
    # energy than the pack holds, and then clamped to what the state space can
    # actually reach.
    #
    # The clamp stays because it is a *reachability guard*, not a policy: a floor
    # the lattice cannot express would make every state infeasible and refuse to
    # plan at all. What changed in beta.18 is what the caller supplies.
    #
    # Until beta.18 the coordinator passed the **hold trajectory's endpoint**,
    # meaning "end no lower than doing nothing would have". That reads like an
    # anti-dumping rule and is not one. On a horizon with no surplus production
    # ahead the idle walk is flat, so the requirement collapsed to "end no lower
    # than you are now" -- a prohibition on *net discharge*. Recomputed from the
    # current state each refresh it also ratcheted: a charge raised the floor, the
    # next refresh inherited it, and the pack was locked out of late-horizon value
    # for good. On a nineteen-quarter horizon ending in four quarters at
    # 1.20 EUR/kWh it sold nothing into the peak and *bought* 4.74 kWh at peak
    # prices.
    #
    # It was also a second, hidden reserve, and "doing nothing" is neither a
    # physical requirement nor an economic one. The authoritative requirement
    # already exists: the pointwise dynamic reserve, whose own forecast
    # legitimately outlives the price horizon -- 143 intervals against 47 on the
    # live installation -- so it is substantial exactly where the prices stop.
    # Energy above it is discretionary, and the objective is free to trade it.
    ambient = _ambient_walk(
        table=table,
        outcomes_per_interval=outcomes_per_interval,
        start_bucket=start_bucket,
    )
    terminal_bucket = min(
        table.bucket_at_or_below(terminal_floor_kwh),
        ambient[-1] if ambient else start_bucket,
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
                    onward_state = _resolved_run_state(outcome.run_state, run)
                    onward = following[move.target][onward_state]
                    if onward >= _UNREACHABLE:
                        continue
                    cost = outcome.cost_eur
                    if onward_state != _RUN_IDLE and onward_state != run:
                        cost += minimum_trade_gain_eur
                    # The per-kWh margin, charged **locally** on this interval's
                    # own marginal grid-caused charging. Local is what makes it
                    # free: the value table is indexed by bucket and run state
                    # with no accumulated-energy axis, so a cost that depends on
                    # a whole run's size could not be charged here at all --
                    # while a cost that depends only on this interval can.
                    #
                    # Adding ``margin * kWh`` to the cost is exactly the
                    # requirement "this energy must earn at least ``margin`` per
                    # kWh beyond what it costs to buy": the search takes the move
                    # only when its benefit clears purchase cost plus margin.
                    if grid_charge_margin_eur_per_kwh > 0.0:
                        cost += grid_charge_margin_eur_per_kwh * outcome.grid_charge_kwh
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
        # Note the margin cannot reach this branch. It is a *cost*, and the
        # objective compares ``(violation, cost)`` lexicographically, so no cost
        # can make a state unreachable or outrank reserve feasibility -- it can
        # only order paths that violate the reserve equally. That is why the
        # margin needs no exemption for reserve or safety charging.
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
        grid_charge_margin_eur_per_kwh=grid_charge_margin_eur_per_kwh,
    )


def _ambient_walk(
    *,
    table: PhysicsTable,
    outcomes_per_interval: list[dict[int, _DeltaOutcome]],
    start_bucket: int,
) -> list[int]:
    """Return the bucket doing nothing lands on, interval by interval.

    The bucketed counterpart of the Phase-3 hold trajectory, and **the one
    definition of "doing nothing" this module has.** Two callers depend on it and
    they must agree: the terminal bound (where it ends) and
    :func:`hold_cost` (what it costs along the way). Two expressions of one
    counterfactual is one too many -- the same reasoning that makes
    ``split_grid_energy`` the sole grid-residual authority.

    Computed here rather than read off ``plan.reference`` because that trajectory
    is continuous while this state space is not, and a bound expressed in the
    wrong resolution is a bound that can be unsatisfiable.

    At each interval it takes the highest reachable target among the deltas that
    are *not a decision* -- true idle and ambient absorption. Never a discharge,
    never a purchase, so the endpoint is a lower bound on what any feasible plan
    can reach and is therefore always itself feasible.

    Returns one bucket per interval, the bucket the interval **ends** on, so
    ``len(result) == len(outcomes_per_interval)``.
    """
    bucket = start_bucket
    walk: list[int] = []
    for outcomes in outcomes_per_interval:
        best = bucket
        for move in table.moves[bucket]:
            outcome = outcomes.get(move.target - bucket)
            if outcome is None or not outcome.permitted:
                continue
            if outcome.run_state in (_RUN_IDLE, _RUN_ABSORB) and move.target > best:
                best = move.target
        bucket = best
        walk.append(bucket)
    return walk


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
    #: May be ``_RUN_ABSORB``, which :func:`_resolved_run_state` turns into
    #: whatever run was already in progress.
    run_state: int
    #: What the interval would have imported, exported and cost with the battery
    #: left alone. Identical for every delta -- it is a property of the interval,
    #: not of the choice -- and carried here so the chosen outcome and its own
    #: counterfactual travel together.
    #:
    #: This is what makes marginal attribution *exact* rather than a heuristic:
    #: the grid import a run caused is its own import minus this.
    idle_import_kwh: float
    idle_export_kwh: float
    idle_cost_eur: float
    #: The grid import this move *caused*, and only when it caused it by charging:
    #: ``flows.import - idle.import`` for a discretionary charge, zero otherwise.
    #:
    #: The basis for ``grid_charge_margin_eur_per_kwh``, and the reason that margin
    #: needs no exemption rules. Ambient absorption causes no extra import, so the
    #: basis is zero for it. A discharge is not charging, so the basis is zero for
    #: load avoidance and for export. A quarter that mixes sun and grid is charged
    #: on the grid share alone, because the share is what this measures.
    grid_charge_kwh: float = 0.0


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
    # The interval's cost with the battery left alone. Priced on exactly the same
    # grid quantities and the same prices as every other cost in this module, so
    # a marginal figure derived from it is a difference of like with like.
    idle_cost_eur = (
        import_price * unavoidable_import - export_price * unavoidable_export
    )
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
            # ``_RUN_ABSORB`` rather than ``_RUN_IDLE``: absorption must not break
            # a paid charging campaign it happens to fall inside. See
            # ``_resolved_run_state``.
            run_state = _RUN_CHARGE if caused_import else _RUN_ABSORB
            # Only the grid-caused share, and only for a charge. Never the whole
            # battery movement: on a mixed quarter the sun's contribution owes
            # nothing, and multiplying total charge by the margin would tax it.
            grid_charge = (
                max(0.0, flows.import_kwh - unavoidable_import)
                if caused_import
                else 0.0
            )
        elif delta < 0:
            # A discharge is not charging. Load avoidance and export are outside
            # this margin by construction rather than by exemption.
            grid_charge = 0.0
            allowed = ECONOMIC_ACTION_DISCHARGE in permitted
            action = (
                ECONOMIC_ACTION_EXPORT if caused_export else ECONOMIC_ACTION_DISCHARGE
            )
            if caused_export:
                allowed = allowed and ECONOMIC_ACTION_EXPORT in permitted
            run_state = _RUN_DISCHARGE
        else:
            grid_charge = 0.0
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
            idle_import_kwh=unavoidable_import,
            idle_export_kwh=unavoidable_export,
            idle_cost_eur=idle_cost_eur,
            grid_charge_kwh=grid_charge,
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
        grid_charge_margin_eur=0.0,
        marginal_grid_charge_kwh=0.0,
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
    grid_charge_margin_eur_per_kwh: float = 0.0,
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
    total_margin = 0.0
    total_grid_charge = 0.0
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
        resolved = _resolved_run_state(outcome.run_state, run)
        run_start = resolved != _RUN_IDLE and resolved != run
        if run_start:
            total_switching += minimum_trade_gain_eur
        # Reported separately and **never** folded into ``cost_eur``. Every euro
        # in that field reconciles to grid energy at the interval's own prices,
        # which a notional margin would silently break -- the same reason the
        # switching cost is kept out of it.
        total_margin += grid_charge_margin_eur_per_kwh * outcome.grid_charge_kwh
        total_grid_charge += outcome.grid_charge_kwh
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
                idle_import_kwh=outcome.idle_import_kwh,
                idle_export_kwh=outcome.idle_export_kwh,
                idle_cost_eur=outcome.idle_cost_eur,
                absorbing=outcome.run_state == _RUN_ABSORB,
            )
        )
        bucket = move.target
        run = resolved

    return EconomicPlan(
        intervals=tuple(entries),
        runs=runs_from(tuple(entries)),
        violation_kwh=total_violation,
        cost_eur=total_cost,
        hold_cost_eur=hold_cost(
            horizon=horizon,
            table=table,
            start_energy_kwh=table.energy(start_bucket),
        ),
        switching_cost_eur=total_switching,
        grid_charge_margin_eur=total_margin,
        marginal_grid_charge_kwh=total_grid_charge,
        terminal_floor_kwh=terminal_floor_kwh,
        terminal_binding=bucket <= terminal_bucket,
        permitted=permitted,
        available=bool(entries),
        unavailable_reason=None if entries else ECONOMIC_UNAVAILABLE_HORIZON_EMPTY,
        worst_shortfall_kwh=worst_shortfall,
        first_violation_index=first_violation,
    )


def hold_cost(
    *,
    horizon: EconomicHorizon,
    table: PhysicsTable,
    start_energy_kwh: float,
) -> float:
    """Return what the horizon costs if the battery is left alone.

    The counterfactual every economic figure is measured against, priced on the
    **ambient walk** -- the same trajectory :func:`_ambient_walk` gives the
    terminal bound. That is the correction beta.16 makes, and it matters:

    Until beta.15 this froze the battery entirely, so the baseline *sold* every
    kilowatt-hour of surplus production while the plan -- forced by the terminal
    bound onto the absorbing trajectory -- *banked* it and was given no credit for
    it. The two were not physically comparable, and the published gain was
    understated by roughly the export value of everything absorbed.

    Pricing both on one trajectory makes ``expected_net_value_eur`` a difference
    between two plans in the same physical world. The objective never reads this
    function, so nothing about the chosen plan changes -- only what is reported
    about it.
    """
    if not horizon.intervals:
        return 0.0

    ac_by_delta = _ac_by_delta(table)
    outcomes_per_interval = [
        _interval_outcomes(
            ac_by_delta=ac_by_delta,
            load_ac_kwh=demand.baseline_kwh or 0.0,
            pv_ac_kwh=demand.pv_kwh or 0.0,
            price=horizon.prices[position],
            permitted=_ALL_ACTIONS,
        )
        for position, demand in enumerate(horizon.demands)
    ]
    start_bucket = table.bucket_at_or_below(start_energy_kwh)
    ambient = _ambient_walk(
        table=table,
        outcomes_per_interval=outcomes_per_interval,
        start_bucket=start_bucket,
    )

    total = 0.0
    bucket = start_bucket
    for position, target in enumerate(ambient):
        outcome = outcomes_per_interval[position].get(target - bucket)
        if outcome is not None:
            total += outcome.cost_eur
        bucket = target
    return total


def runs_from(intervals: tuple[EconomicInterval, ...]) -> tuple[EconomicRun, ...]:
    """Group a plan's intervals into maximal contiguous runs of one action.

    Runs, not intervals, are the unit a user reads and the unit the switching cost
    is charged against. A run broken by a **true** idle interval is two runs,
    which is what makes the cost discourage chattering rather than long windows.

    An *absorbing* interval is transparent: it continues whatever run is in
    progress rather than breaking it, so the reported run and the fee the
    objective charged describe the same campaign. Without that, a sunny quarter in
    the middle of a paid charging window split the window in two and the second
    half paid the fee again.

    Note that the run's ``action`` is a *label*, and one physical discharge can
    carry both ``discharge`` and ``export`` as house load rises and falls beneath
    it. ``direction`` is what the objective actually saw, and it is what the fee
    was charged against -- so the number of runs is an upper bound on the number
    of switches, never the switches themselves.
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
                net_cash_flow_eur=-sum(e.cost_eur for e in current),
                min_price_eur_kwh=min(known) if known else None,
                max_price_eur_kwh=max(known) if known else None,
                average_price_eur_kwh=(sum(known) / len(known)) if known else None,
                marginal_grid_import_kwh=sum(
                    e.marginal_grid_import_kwh for e in current
                ),
                marginal_grid_export_kwh=sum(
                    e.marginal_grid_export_kwh for e in current
                ),
                marginal_cost_eur=sum(e.marginal_cost_eur for e in current),
                direction=_direction_of_run(current),
                charged_switching_fee=any(e.run_start for e in current),
            )
        )
        current.clear()

    for entry in intervals:
        # Absorption is ambient, so inside a charge campaign it neither starts a
        # run nor breaks one. Only inside a charge campaign: absorption *is* a
        # charge, so it cannot continue a discharge run -- there it breaks the run
        # exactly as a true idle interval does, which the hold branch below
        # handles.
        if entry.absorbing and current and _charging(current[0]):
            current.append(entry)
            continue
        if entry.action == ECONOMIC_ACTION_HOLD:
            flush()
            continue
        if current and entry.action != current[0].action:
            flush()
        current.append(entry)
    flush()
    return tuple(runs)


def _charging(entry: EconomicInterval) -> bool:
    """Return whether an interval belongs to a charging campaign."""
    return entry.battery_charge_ac_kwh > 0.0


def _direction_of_run(intervals: list[EconomicInterval]) -> str:
    """Return the battery direction a run moved in, ignoring its labels."""
    for entry in intervals:
        if entry.battery_charge_ac_kwh > 0.0 and not entry.absorbing:
            return ECONOMIC_DIRECTION_CHARGE
        if entry.battery_discharge_ac_kwh > 0.0:
            return ECONOMIC_DIRECTION_DISCHARGE
    for entry in intervals:
        if entry.battery_charge_ac_kwh > 0.0:
            return ECONOMIC_DIRECTION_CHARGE
    return ECONOMIC_DIRECTION_IDLE


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
    #: The same solve with the terminal bound dropped to the configured floor.
    #: Instrumentation only -- never published as a plan, never acted on. It
    #: exists so the terminal condition's price is a measurement rather than an
    #: argument. See :attr:`terminal_plan_cost_eur`.
    unbounded: EconomicPlan | None
    horizon: EconomicHorizon
    reserve_above_capacity_kwh: float
    buckets: int
    bucket_kwh: float
    #: The largest AC power any single transition can express, carried from the
    #: table so a reporting layer never has to rebuild one to describe it.
    max_representable_power_kw: float
    table_ms: float
    solve_ms: float
    #: Per-direction peaks, and the configured limits they are measured against.
    #: beta.16 published only the larger of the two, which hid an asymmetry that
    #: reaches thirty per cent on a small-power installation.
    max_representable_charge_kw: float = 0.0
    max_representable_discharge_kw: float = 0.0
    configured_charge_kw: float = 0.0
    configured_discharge_kw: float = 0.0
    #: Which rule chose the lattice. Two installations can legitimately differ:
    #: one where alignment regressed a direction keeps the beta.16 bucket.
    bucket_rule: str = ECONOMIC_BUCKET_RULE_CONSTANT
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
    def terminal_plan_cost_eur(self) -> float | None:
        """Return the **whole-horizon** cost of ending at the hold endpoint.

        The difference between the plan and the same solve with the terminal bound
        dropped to the configured floor. Positive means the bound made the plan
        more expensive.

        **This is not realised money, and beta.16 published it as though it
        were.** The name says ``plan`` because that is its scope: a plan is
        rebuilt every quarter-hour and only its *first* interval is ever
        executed, so a difference concentrated in the tail is discarded before it
        can happen. Measured against a rolling re-solve -- re-plan each quarter,
        execute one interval, roll the state through the same physics -- the
        realised difference between this bound and every alternative was
        0.03-0.10 EUR per day, while this figure read 1.4-5.1 EUR. It overstated
        by roughly fortyfold, and it was read as a reason to redesign the bound.

        Kept, because it is the right measure of *how tightly the bound binds*.
        Read it beside :attr:`terminal_first_run_changed`, which is the part a
        reader can act on.
        """
        if self.unbounded is None or not self.unbounded.available:
            return None
        return self.desired.cost_eur - self.unbounded.cost_eur

    @property
    def terminal_plan_import_kwh(self) -> float | None:
        """Return the whole-horizon extra grid import the bound is responsible for.

        Same scope and the same caveat as :attr:`terminal_plan_cost_eur`. The
        kilowatt-hours are what make the bound's signature recognisable in a
        download: a maximum-power purchase in the final quarters.
        """
        if self.unbounded is None or not self.unbounded.available:
            return None
        return (
            self.desired.planned_grid_import_kwh
            - self.unbounded.planned_grid_import_kwh
        )

    @property
    def terminal_first_run_changed(self) -> bool | None:
        """Return whether the terminal bound altered the run about to happen.

        **The only part of the terminal condition a reader can act on**, and the
        figure beta.16 should have led with. Everything else the bound does lies
        further out than the next refresh, and is replaced by it.

        Compared on identity and quantity rather than on the whole plan: the
        action, the interval it starts in, and the first interval's battery
        movement. A tail that differs is expected and uninteresting; a *first
        run* that differs means the bound is shaping something that will actually
        be executed, which on measurement happens only when the horizon is short
        -- late in the day with tomorrow's prices still unpublished.
        """
        if self.unbounded is None or not self.unbounded.available:
            # No comparison was performed, so there is no answer. A ``False``
            # here would claim the bound left the next run alone, when in fact
            # there is no bound to leave anything alone.
            return None
        if not self.desired.available or not self.desired.intervals:
            return False
        if not self.unbounded.intervals:
            return True
        bounded, free = self.desired.intervals[0], self.unbounded.intervals[0]
        if bounded.action != free.action:
            return True
        moved = bounded.battery_charge_ac_kwh - bounded.battery_discharge_ac_kwh
        otherwise = free.battery_charge_ac_kwh - free.battery_discharge_ac_kwh
        # One state-space bucket: below that the two plans are the same move as
        # far as the lattice can tell, so calling them different would be noise.
        return abs(moved - otherwise) > ECONOMIC_BUCKET_KWH

    @property
    def terminal_near_field_cost_eur(self) -> float | None:
        """Return what the bound costs over the intervals that will be executed.

        The first hour, because a refresh is a quarter and four of them is a
        generous view of "about to happen". Bounded above by
        :attr:`terminal_plan_cost_eur` and normally a small fraction of it -- and
        when the two are close, the bound is binding *now* rather than at the far
        end, which is the case worth investigating.
        """
        if self.unbounded is None or not self.unbounded.available:
            return None
        near = 4
        bounded = sum(e.cost_eur for e in self.desired.intervals[:near])
        free = sum(e.cost_eur for e in self.unbounded.intervals[:near])
        return bounded - free

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
    bucket_rule: str = ECONOMIC_BUCKET_RULE_CONSTANT,
    grid_charge_margin_eur_per_kwh: float = 0.0,
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
        grid_charge_margin_eur_per_kwh=grid_charge_margin_eur_per_kwh,
    )
    capability = solve(
        table=table,
        horizon=horizon,
        start_energy_kwh=start_energy_kwh,
        terminal_floor_kwh=terminal_floor_kwh,
        minimum_trade_gain_eur=minimum_trade_gain_eur,
        permitted=capability_permitted,
        grid_charge_margin_eur_per_kwh=grid_charge_margin_eur_per_kwh,
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
        grid_charge_margin_eur_per_kwh=grid_charge_margin_eur_per_kwh,
    )

    # There is no fourth solve any more, and no comparison to publish.
    #
    # beta.16 and beta.17 ran one with the terminal bound relaxed to the
    # configured floor, to price what the hold-end constraint cost. Candidate B
    # removed that constraint, so the relaxed solve and the desired solve are now
    # the *same problem* -- the difference would be identically zero, and
    # publishing a zero would state that a constraint costs nothing rather than
    # that no constraint exists. The terminal figures are reported absent instead.
    unbounded = None
    solve_ms = (time.perf_counter() - started) * 1000.0

    return EconomicOutcome(
        desired=desired,
        capability=capability,
        relaxed=relaxed,
        unbounded=unbounded,
        horizon=horizon,
        reserve_above_capacity_kwh=reserve_above_capacity_kwh,
        buckets=table.buckets,
        bucket_kwh=table.bucket_kwh,
        max_representable_power_kw=table.max_representable_power_kw,
        max_representable_charge_kw=table.max_representable_charge_kw,
        max_representable_discharge_kw=table.max_representable_discharge_kw,
        configured_charge_kw=table.limits.max_charge_kw,
        configured_discharge_kw=table.limits.max_discharge_kw,
        bucket_rule=bucket_rule,
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

#: What the terminal floor is measured against. Named rather than inlined,
#: because the evidence layer and the diagnostics payload must agree on it.
#:
#: Until beta.18 this read ``hold_trajectory_end_on_bucket_grid``: the floor was
#: the idle-with-absorption endpoint, reproduced on the solver's grid. beta.18
#: removed that rule, and the value has to move with it -- a basis string naming a
#: trajectory the floor no longer follows would be a false statement about a
#: published number, which is worse than no basis string at all.
TERMINAL_BASIS: str = "configured_floor_on_bucket_grid"

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
        # What the run cost against leaving the battery alone through the same
        # intervals. **This is the economics.** Negative means it saved money.
        "marginal_cost_eur": _round_eur(run.marginal_cost_eur),
        # The raw cash flow, named for what it is. Negative for every charge run
        # by construction, and zero for a discharge that exactly covers house
        # load -- which is why it is not the economics.
        "net_cash_flow_eur": _round_eur(run.net_cash_flow_eur),
        "battery_charge_ac_kwh": _round_kwh(run.battery_charge_ac_kwh),
        "battery_discharge_ac_kwh": _round_kwh(run.battery_discharge_ac_kwh),
        # Site flows first, then the part this run actually caused. The two are
        # different quantities: the site figure includes house load, so a charge
        # run's site import is not what the run bought.
        "grid_import_kwh": _round_kwh(run.grid_import_kwh),
        "grid_export_kwh": _round_kwh(run.grid_export_kwh),
        "marginal_grid_import_kwh": _round_kwh(run.marginal_grid_import_kwh),
        "marginal_grid_export_kwh": _round_kwh(run.marginal_grid_export_kwh),
        "charge_source": run.charge_source,
        "direction": run.direction,
        "charged_switching_fee": run.charged_switching_fee,
        "pv_curtailed_kwh": _round_kwh(run.pv_curtailed_kwh),
        "min_price_eur_kwh": run.min_price_eur_kwh,
        "max_price_eur_kwh": run.max_price_eur_kwh,
        "average_price_eur_kwh": _round_eur(run.average_price_eur_kwh),
        "attribution_rule": (
            "marginal figures are measured against this run's own idle "
            "counterfactual, interval by interval, so they are exact rather than "
            "apportioned: a charge whose energy came from production shows a "
            "small marginal import beside a large battery charge"
        ),
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
        # The number the switching fee was actually charged against. A single
        # physical discharge can be reported as several runs as its label flips
        # between discharge and export under a varying house load, so the run
        # count is an upper bound on switches and never the switches themselves.
        "direction_changes": plan.direction_changes,
        "end_energy_dc_kwh": _round_kwh(plan.end_energy_dc_kwh),
    }


def execution_intent(run: EconomicRun) -> str:
    """Return what Stage B would have to *do*, not what the plan calls it.

    The action label answers "what is this economically?"; this answers "which
    physical quantity is the target?". They are not the same question and the
    difference is dangerous: a ``discharge`` and an ``export`` are both the
    battery delivering energy, but one is measured at the battery and the other at
    the meter, and on the live installation those differ by the whole house load
    -- 2.2 kW of battery for 1.3 kW of export.

    ``curtail_pv`` is deliberately **not** an intent. No actuator can decline
    production in this release, so emitting it would imply a capability that does
    not exist; a curtailment plan reports ``hold`` here and says what it wanted in
    ``economic_reason``.
    """
    if run.action in (ECONOMIC_ACTION_CHARGE, ECONOMIC_ACTION_SAFETY_BUY):
        return EXECUTION_INTENT_GRID_CHARGE
    if run.action == ECONOMIC_ACTION_DISCHARGE:
        return EXECUTION_INTENT_SERVE_LOAD
    if run.action == ECONOMIC_ACTION_EXPORT:
        return EXECUTION_INTENT_NET_EXPORT
    return EXECUTION_INTENT_HOLD


def execution_target(
    run: EconomicRun,
    *,
    window_start: datetime,
    window_end: datetime,
    reserve_floor_kwh: float,
    issued_at: datetime,
    stale_after: datetime,
    safety_buy: bool = False,
    margin_passed: bool = True,
    expected_pv_production_kwh: float | None = None,
    expected_house_load_kwh: float | None = None,
    required_headroom_kwh: float | None = None,
    max_end_energy_kwh: float | None = None,
    headroom_until: datetime | None = None,
) -> dict[str, Any]:
    """Return the machine-readable target a future Stage B would consume.

    **Nothing consumes this in beta.18.** It is published so that Stage B can be
    written against a contract that already exists, rather than inventing one and
    discovering the ambiguities afterwards.

    Three properties are the whole point:

    * **Absolute time.** ``window_start`` and ``window_end`` are instants, never
      horizon indices. An index moves every quarter as the horizon advances, which
      is precisely the defect that made the beta.16 Activity log announce the same
      run over and over. Stage B would have inherited it.
    * **Two boundaries, two fields.** ``battery_target_kwh`` is at the battery and
      ``grid_target_kwh`` is at the meter. A single ``energy_kwh`` whose meaning
      changes with the action is exactly how 1.3 kW of intended export becomes a
      1.3 kW battery command and delivers 0.4 kW.
    * **Identity that survives replanning.** ``plan_id`` is ``(intent, start
      instant)``, so a run whose remaining energy shrinks as it is executed keeps
      its identity, while a genuinely different run gets a different one.

    ``revision`` is supplied by the caller, which is the only layer that can
    remember the previous target -- and since beta.19 the only layer that can
    remember it *across a restart*.

    **Two independent clocks, since beta.19.** ``window_start``/``window_end`` say
    when the energy is wanted; ``issued_at``/``stale_after`` say how long this
    statement of intent may be believed. beta.18 derived the second from the first,
    which made it useless for the job it is named for: a run eighteen hours out
    carried a freshness deadline eighteen and a half hours out, so a target could
    be stale by any ordinary meaning of the word and still be inside it.

    **Two power figures, because one was misnamed.**
    ``initial_average_power_kw`` is the run's *mean* -- it always was, despite the
    name -- and is kept unchanged so nothing reading it breaks.
    ``first_power_kw`` is the first interval's power, which is what a controller
    starting a run actually wants. Both are published rather than one being
    quietly redefined.

    **The charge balance and the headroom constraint** are published for a
    ``grid_charge`` so Stage B can preserve headroom without doing economics. See
    :func:`charge_window_balance`. ``required_headroom_kwh``,
    ``max_end_energy_kwh`` and ``headroom_until`` are ``None`` when the plan
    imposes no such constraint, and absent means *unconstrained* -- never zero,
    which would forbid the pack from filling at all.
    """
    intent = EXECUTION_INTENT_GRID_CHARGE if safety_buy else execution_intent(run)
    battery = run.battery_charge_ac_kwh + run.battery_discharge_ac_kwh
    return {
        "plan_id": _execution_plan_id(intent, window_start),
        "intent": intent,
        "purpose": ECONOMIC_ACTION_SAFETY_BUY if safety_buy else run.action,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        # When this was said, and how long it may be believed. Independent of the
        # window on purpose -- see the docstring.
        "issued_at": issued_at.isoformat(),
        "stale_after": stale_after.isoformat(),
        # Battery side. Authoritative for a charge: the AlphaESS dispatch setpoint
        # is a battery figure, and house load must **not** be added to it.
        "battery_target_kwh": _round_kwh(battery),
        # Meter side. Present only when the meter is what the plan is aiming at,
        # so a consumer cannot mistake one for the other by reading whichever is
        # non-zero.
        "grid_target_kwh": (
            _round_kwh(run.grid_export_kwh)
            if intent == EXECUTION_INTENT_NET_EXPORT
            else None
        ),
        # The run mean. Named "initial" since beta.14 and never was; kept
        # unchanged for compatibility, with the honest figure beside it.
        "initial_average_power_kw": _round_kw(run.average_power_kw),
        "average_power_kw": _round_kw(run.average_power_kw),
        "first_power_kw": _round_kw(run.first_power_kw),
        "reserve_floor_kwh": _round_kwh(reserve_floor_kwh),
        "expected_grid_import_kwh": _round_kwh(run.marginal_grid_import_kwh),
        "expected_grid_export_kwh": _round_kwh(run.marginal_grid_export_kwh),
        "economic_reason": run.action,
        "expected_value_eur": _round_eur(-run.marginal_cost_eur),
        "margin_passed": margin_passed,
        # The physical constraint Stage B must preserve, and until when. Absent
        # means unconstrained.
        "required_headroom_kwh": _round_kwh(required_headroom_kwh),
        "max_end_energy_kwh": _round_kwh(max_end_energy_kwh),
        "headroom_until": (
            None if headroom_until is None else headroom_until.isoformat()
        ),
        "headroom_rule": (
            "the physical headroom Stage B must leave available, decided here "
            "because how much headroom is worth keeping is an economic question. "
            "max_end_energy_kwh caps stored energy at headroom_until so forecast "
            "production this plan intends to absorb is not displaced by charging "
            "the pack full early. null means unconstrained, NOT zero. Stage B may "
            "only reduce or stop to honour it: buying the difference when "
            "production disappoints is a new economic decision and belongs here"
        ),
        **(
            charge_window_balance(
                run,
                expected_pv_production_kwh=expected_pv_production_kwh,
                expected_house_load_kwh=expected_house_load_kwh,
            )
            if intent == EXECUTION_INTENT_GRID_CHARGE
            else {}
        ),
        "boundary_rule": (
            "battery_target_kwh is AC energy at the battery and is what a charge "
            "command must aim at -- house load is NOT added to it. "
            "grid_target_kwh is AC energy at the meter and is set only for "
            "net_export, where the battery must deliver the target plus whatever "
            "the house is taking at the time. measured on the live installation: "
            "1.3 kW of net export needed 2.2 kW of battery against 0.9 kW of load"
        ),
        "contract_rule": (
            "consumed by the Stage B controller since beta.19, which computes "
            "the command a Live run would send and sends nothing: "
            "CONTROL_EXECUTION_AVAILABLE is false and no actuator is reachable. "
            "identity is (intent, window_start) so a run keeps its plan_id as its "
            "remaining energy shrinks; revision increments when the target moves "
            "beyond the published deadband and survives a restart; stale_after is "
            "anchored to issued_at, not to the window, and IS enforced -- a stale "
            "target may not start, and an owned run whose target goes stale is "
            "stopped"
        ),
    }


def charge_window_balance(
    run: EconomicRun,
    *,
    expected_pv_production_kwh: float | None,
    expected_house_load_kwh: float | None,
) -> dict[str, Any]:
    """Return the charge-window energy balance Stage B needs, and no economics.

    Stage B has to be able to preserve headroom without deciding *how much*
    headroom is worth preserving. That is an economic judgement, and it belongs
    here. So the balance is published rather than left to be inferred.

    **Expected production is not production available to the battery.** The house
    is consuming throughout the window, and its share is taken first -- so a
    fifteen-kilowatt-hour afternoon with five kilowatt-hours of load offers
    substantially less than fifteen to the pack. Publishing production alone would
    invite Stage B to preserve headroom against energy the house was always going
    to eat.

    The split itself needs no new arithmetic. ``marginal_grid_import_kwh`` is
    already the exact grid share of this run measured against its own idle
    counterfactual, computed from the same ``split_grid_energy`` call as the flow
    it is subtracted from -- so what the grid supplied is known, and what is left
    of the battery's charge came from production.

    ``expected_grid_to_battery_kwh`` is a **maximum**, not an allowance Stage B may
    spend up to. If production disappoints, buying the difference is a fresh
    economic decision and Stage A must make it.
    """
    charge = run.battery_charge_ac_kwh
    grid_share = max(0.0, run.marginal_grid_import_kwh)
    return {
        "expected_pv_production_kwh": _round_kwh(expected_pv_production_kwh),
        "expected_house_load_kwh": _round_kwh(expected_house_load_kwh),
        # What is left of the charge once the grid's measured share is removed.
        # Bounded below at zero: a rounding artefact must not publish a negative
        # contribution, and bounded above by the charge itself.
        "expected_pv_to_battery_kwh": _round_kwh(max(0.0, charge - grid_share)),
        "expected_grid_to_battery_kwh": _round_kwh(min(grid_share, charge)),
        "charge_source": run.charge_source,
    }


def _execution_plan_id(intent: str, window_start: datetime) -> str:
    """Return a short stable identifier for one planned run.

    Over the intent and the absolute start instant, and nothing else. Not the
    energy, which shrinks as the run is executed; not the index, which moves every
    refresh; not the price, which is revised.
    """
    digest = hashlib.sha256(f"{intent}|{window_start.isoformat()}".encode()).hexdigest()
    return digest[:ECONOMIC_FINGERPRINT_CHARS]


def execution_revision(previous: dict[str, Any] | None, current: dict[str, Any]) -> int:
    """Return the revision ``current`` should carry, given what was last published.

    Starts at one. Holds while nothing actionable has moved; increments when it
    has. The deadbands are the same constants the Activity surface uses, for the
    same reason -- a revision that churned on floating-point noise would make a
    future Stage B re-plan continuously, which is the control jitter this contract
    exists to avoid:

    * energy, either boundary, by more than one state-space bucket;
    * the window end by more than one planning interval;
    * the intent at all.

    A different ``plan_id`` is a different run, so it starts again at one rather
    than continuing someone else's numbering.
    """
    if previous is None or previous.get("plan_id") != current.get("plan_id"):
        return 1
    if previous.get("intent") != current.get("intent"):
        return int(previous.get("revision", 1)) + 1

    def moved(key: str, tolerance: float) -> bool:
        before, after = previous.get(key), current.get(key)
        if before is None or after is None:
            return before is not after
        return abs(float(after) - float(before)) > tolerance

    if moved("battery_target_kwh", ECONOMIC_BUCKET_KWH) or moved(
        "grid_target_kwh", ECONOMIC_BUCKET_KWH
    ):
        return int(previous.get("revision", 1)) + 1
    if previous.get("window_end") != current.get("window_end"):
        return int(previous.get("revision", 1)) + 1
    return int(previous.get("revision", 1))


def economic_as_dict(
    outcome: EconomicOutcome | None,
    *,
    execution_blocked_reason: str,
    table_max_power_kw: float = 0.0,
    table_max_charge_kw: float = 0.0,
    table_max_discharge_kw: float = 0.0,
    configured_charge_kw: float = 0.0,
    configured_discharge_kw: float = 0.0,
    bucket_rule: str = ECONOMIC_BUCKET_RULE_CONSTANT,
    tomorrow_prices: str = ECONOMIC_TOMORROW_ABSENT,
    reserve_basis: str | None = None,
    bridge_requirement_kwh: float | None = None,
    pack_ceiling_kwh: float | None = None,
    execution_targets: list[dict[str, Any]] | None = None,
    realized: dict[str, Any] | None = None,
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
            # Whether tomorrow is in the horizon at all, from what the source has
            # actually published. Never from a clock: there is no publication
            # time in this integration, and inventing one would turn a data
            # question into a scheduling assumption.
            "tomorrow_prices": tomorrow_prices,
            # What Phase 7 says about its own tail. "truncated" means its final
            # requirements are *lower bounds* because the drawdown window ran off
            # the end of the forecast -- which is why the reserve alone cannot
            # serve as a terminal condition, and why the bound above exists.
            "reserve_basis": reserve_basis,
            # The energy the same Phase-7 recursion asks for at the horizon's end
            # when the forecast is extended a day, in kWh. Physics only: forecast
            # load less usable production, through the same clamp. Published to
            # be *measured*, and consumed by nothing -- on synthetic shapes it
            # runs 15.7 kWh in summer and 33-61 kWh in winter against a 22 kWh
            # pack, so it cannot be a bound, and pretending otherwise would make
            # every winter horizon infeasible.
            "bridge_requirement_kwh": _round_kwh(bridge_requirement_kwh),
            "bridge_exceeds_pack": (
                None
                if bridge_requirement_kwh is None
                else pack_ceiling_kwh is not None
                and bridge_requirement_kwh > pack_ceiling_kwh
            ),
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
            # Absent, and absent on purpose. beta.16 and beta.17 priced a
            # hold-end terminal constraint against a relaxed re-solve; beta.18
            # removed that constraint, so there is no comparison left to make.
            # Publishing zeroes would say the constraint is free rather than
            # gone.
            "first_run_changed": outcome.terminal_first_run_changed,
            "near_field_cost_eur": _round_eur(outcome.terminal_near_field_cost_eur),
            "plan_cost_eur": _round_eur(outcome.terminal_plan_cost_eur),
            "plan_import_kwh": _round_kwh(outcome.terminal_plan_import_kwh),
            "plan_cost_rule": (
                "null since beta.18. these figures priced a hold-trajectory "
                "terminal constraint that no longer exists: it duplicated "
                "reserve semantics, collapsed to the current state of charge on a "
                "flat idle horizon, and ratcheted upward across refreshes. the "
                "dynamic reserve is now the only physical floor, and documents "
                "written by beta.16 or beta.17 still read back with their "
                "recorded values"
            ),
            "rule": (
                "the configured physical floor, and nothing else. until beta.18 "
                "this was the idle trajectory's endpoint, which on a horizon "
                "with no surplus production ahead is simply the current state of "
                "charge -- so it forbade net discharge rather than forbidding "
                "dumping, and ratcheted upward as the pack charged. the pointwise "
                "dynamic reserve is the authoritative requirement, and its own "
                "forecast outlives the price horizon, so it is substantial "
                "exactly where the prices stop. energy above it is discretionary"
            ),
        },
        # What a future Stage B would consume, and what today actually cost.
        # Neither is read by anything in this release: there is no Stage B, no
        # actuator behind the targets, and the realised figures are measurements
        # rather than inputs. They are published so the contract can be written
        # against and the arithmetic checked.
        "execution_targets": execution_targets or [],
        "execution_contract_rule": (
            "advisory. one target per planned run, addressed by absolute instants "
            "rather than horizon indices so identity survives replanning, with "
            "the battery-side and grid-side quantities in separate fields so a "
            "consumer cannot mistake one boundary for the other. nothing in "
            "beta.18 consumes these and CONTROL_EXECUTION_AVAILABLE is false"
        ),
        "realized": realized or {"available": False, "reason": "not_computed"},
        "solver": {
            "buckets": outcome.buckets,
            "bucket_kwh": outcome.bucket_kwh,
            "table_ms": round(outcome.table_ms, 1),
            "solve_ms": round(outcome.solve_ms, 1),
            "solves": 4,
            # The largest AC power a single transition can express, per
            # direction. beta.16 published only the larger of the two, which hid
            # an asymmetry reaching thirty per cent on a small-power pack.
            "max_representable_power_kw": table_max_power_kw,
            "max_representable_charge_kw": table_max_charge_kw,
            "max_representable_discharge_kw": table_max_discharge_kw,
            "configured_charge_kw": configured_charge_kw,
            "configured_discharge_kw": configured_discharge_kw,
            "bucket_rule": bucket_rule,
            "power_limit_rule": (
                "quantisation, not a configured limit and not a clamp fault: the "
                "next bucket up would need more AC power than the inverter "
                "allows, so the clamp reduces it and the move is discarded. "
                "beta.17 chooses the bucket to divide a maximum-power quarter "
                "exactly where it can do so without regressing either direction, "
                "without collapsing energy resolution and without growing the "
                "state count; bucket_rule says which rule produced this lattice"
            ),
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
    #: What ending the horizon at the hold endpoint cost, and the grid import it
    #: was responsible for. Persisted for the same reason the reserve's own
    #: protection cost is: a constraint that can quietly cost money needs its
    #: price recoverable afterwards, or a later decision about it rests on memory.
    terminal_plan_cost_eur: float | None = None
    terminal_plan_import_kwh: float | None = None
    terminal_first_run_changed: bool | None = None
    #: The lattice this plan was solved on, beyond the bucket size already stored
    #: above. beta.17 chooses the bucket per installation instead of fixing it, so
    #: a figure recorded before the upgrade and one recorded after can differ by
    #: up to one bucket for no reason other than the grid it was quantised on.
    #: Persisting the rule and both directional peaks is what makes that
    #: explicable from the document itself rather than by recomputation -- and
    #: recomputation is exactly what is unavailable later, because the chosen
    #: lattice depends on limits the user may since have changed.
    bucket_rule: str | None = None
    max_representable_charge_kw: float | None = None
    max_representable_discharge_kw: float | None = None
    #: How many times the battery actually changed direction. Stored beside the
    #: plan because the run count over-states it and a later reader cannot
    #: recompute the difference from scalars alone.
    direction_changes: int = 0

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
            # Renamed in beta.17 from ``tpc``/``tpi``. The old keys said
            # "protection cost" for a figure that is a whole-horizon plan
            # difference and not realised money, and it was read as the latter.
            "tplc": self.terminal_plan_cost_eur,
            "tpli": self.terminal_plan_import_kwh,
            "tfrc": self.terminal_first_run_changed,
            "br": self.bucket_rule,
            "mrc": self.max_representable_charge_kw,
            "mrd": self.max_representable_discharge_kw,
            "dc": self.direction_changes,
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
            # Both spellings, so a beta.16 document reads back with every
            # figure intact rather than losing two of them to a rename.
            terminal_plan_cost_eur=_finite(
                raw.get("tplc") if "tplc" in raw else raw.get("tpc")
            ),
            terminal_plan_import_kwh=_finite(
                raw.get("tpli") if "tpli" in raw else raw.get("tpi")
            ),
            terminal_first_run_changed=(
                bool(raw["tfrc"]) if raw.get("tfrc") is not None else None
            ),
            # Absent in every document written before beta.17, and absent is the
            # honest answer for them: those plans were solved on the constant
            # bucket, but saying so here would be inferring it rather than
            # reading it.
            bucket_rule=raw.get("br") if isinstance(raw.get("br"), str) else None,
            max_representable_charge_kw=_finite(raw.get("mrc")),
            max_representable_discharge_kw=_finite(raw.get("mrd")),
            direction_changes=(raw["dc"] if isinstance(raw.get("dc"), int) else 0),
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
        terminal_plan_cost_eur=(
            _round_eur(outcome.terminal_plan_cost_eur) if available else None
        ),
        terminal_plan_import_kwh=(
            _round_kwh(outcome.terminal_plan_import_kwh) if available else None
        ),
        terminal_first_run_changed=(
            outcome.terminal_first_run_changed if available else None
        ),
        bucket_rule=outcome.bucket_rule if available else None,
        max_representable_charge_kw=(
            round(outcome.max_representable_charge_kw, ECONOMIC_POWER_PRECISION)
            if available
            else None
        ),
        max_representable_discharge_kw=(
            round(outcome.max_representable_discharge_kw, ECONOMIC_POWER_PRECISION)
            if available
            else None
        ),
        direction_changes=(outcome.desired.direction_changes if available else 0),
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
