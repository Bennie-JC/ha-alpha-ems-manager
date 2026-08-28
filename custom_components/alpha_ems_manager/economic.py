"""What the cheapest way through the known horizon is. **It commands nothing.**

Phase 8 answers one question: given the prices, the load, the production and the
reserve requirement that are *actually known*, what is the least-cost way to move
the battery? It publishes the answer. Since beta.25 the buying half of it
is executed; the selling half is still advisory.

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

**This module executes neither, and that is a fact about the module.** It reads
no entity, calls no service and names no helper -- checked at the syntax level in
``test_phase_eight_boundaries``. Whether anything downstream acts on the plan is a
separate question with a separate answer: since beta.24 a charge is executed and
since beta.27 an export is, so ``execution_blocked_reason`` reports the live
barrier -- the user's enable, the mode, then the action -- rather than the constant
``execution_unavailable`` it published through beta.32.

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
from dataclasses import dataclass, field
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
    ADAPT_PROTECTION_CEILING,
    BATTERY_KWH_PRECISION,
    BUY_REASON_ARBITRAGE,
    BUY_REASON_FUTURE_SELF_USE,
    BUY_REASON_MIXED,
    BUY_REASON_REACHABILITY,
    BUY_REASON_UNCERTAINTY,
    BUY_REASON_UNKNOWN,
    CAMPAIGN_BOUNDARY_BATTERY,
    CAMPAIGN_BOUNDARY_METER,
    CONTROL_EXECUTION_AVAILABLE,
    CONTROL_LIVE_DISPATCH_INTENTS,
    COUNTERFACTUAL_AMBIENT_SELF_CONSUMPTION,
    COUNTERFACTUAL_IDLE_IMPORT,
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
    ECONOMIC_IMMATERIAL_BELOW_TRADE_GAIN,
    ECONOMIC_IMMATERIAL_NOT_EXECUTABLE,
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
    EXECUTION_INTENT_ACTIONS,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_HOLD,
    EXECUTION_INTENT_NET_EXPORT,
    EXECUTION_INTENT_SERVE_LOAD,
    MAX_ECONOMIC_RUN_INTERVALS_REPORTED,
    MAX_ECONOMIC_RUNS_REPORTED,
    MIN_EXECUTABLE_QUARTER_KWH,
    MODE_CHARGE,
    MODE_DISCHARGE,
    MODE_IDLE,
    QUARTER_NOT_EXECUTABLE_INTENT,
    QUARTER_NOT_EXECUTABLE_NO_OBJECTIVE,
    QUARTER_NOT_EXECUTABLE_SUB_RESOLUTION,
    SURVIVAL_WINDOW_ACTIONABLE_PREFIX,
    SURVIVAL_WINDOW_PLAN_CAMPAIGN,
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
#:
#: **``export`` joined in beta.32, and its absence was a lie with consequences.**
#: ``CONTROL_EXECUTABLE_ACTIONS_BY_INTENT`` has authorised an admitted
#: ``net_export`` since beta.27, ``CONTROL_LIVE_DISPATCH_INTENTS`` contains it,
#: and the hardware has performed one -- while this set said no actuator existed.
#: Two sets disagreeing about the same fact, and this one was the wrong one.
#:
#: Not cosmetic: it also bounded the **capability solve**, so every export day
#: reported value the plant could supposedly not capture, and it put an
#: ``Advisory`` marker on Live export lines that a command was about to be sent
#: for. Widening it changes the capability plan, which is the point.
#:
#: Still gated by ``allow_battery_export`` at every decision -- an actuator
#: existing and a user permitting its use are different questions, and only the
#: first one lives here.
IMPLEMENTED_ACTIONS: frozenset[str] = frozenset(
    {ECONOMIC_ACTION_CHARGE, ECONOMIC_ACTION_DISCHARGE, ECONOMIC_ACTION_EXPORT}
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

#: The published name of each run state, so a campaign's direction and the
#: solver's own state are the same vocabulary rather than two that must be kept
#: in step.
_RUN_STATE_NAMES = {
    _RUN_IDLE: ECONOMIC_DIRECTION_IDLE,
    _RUN_CHARGE: ECONOMIC_DIRECTION_CHARGE,
    _RUN_DISCHARGE: ECONOMIC_DIRECTION_DISCHARGE,
}

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

    # **The probe sits inside the configured window, and it has to.** Until
    # beta.32 this calibrated at a hardcoded ``soc_percent=50.0``, which is a
    # perfectly good probe on the reference installation and silently fatal
    # elsewhere: with a configured minimum state of charge at or above 50 % the
    # probe state *is* the floor, so the clamp reduces the discharge reading to
    # zero, ``discharge_ratio`` is 0, and this function returns ``None`` -- taking
    # the whole optimiser with it, under a comment asserting ``build_limits``
    # precluded the case. It did not. Measured on the reference pack: a 49 % floor
    # built a table and a 50 % floor did not.
    #
    # The midpoint of floor and ceiling is the only probe that is unclamped for
    # every legal configuration, and the power is scaled to the window so a narrow
    # one still reads cleanly. The ratios are pure efficiency constants and are
    # therefore magnitude-independent -- verified identical at 0.948683 and
    # 1.054093 for every floor from 0 % to 99.5 % -- so scaling costs no accuracy.
    window_kwh = ceiling - max(0.0, floor_energy_kwh)
    if window_kwh <= 0.0:
        # A floor at or above the ceiling. No charge and no discharge is possible,
        # so there is genuinely no lattice to solve on. Named rather than reached
        # by accident.
        return None
    probe_kw = min(
        _CALIBRATION_PROBE_KW, window_kwh / INTERVAL_HOURS / _CALIBRATION_WINDOW_SHARE
    )
    calibration = build_state(
        soc_percent=limits.soc_for_energy(
            max(0.0, floor_energy_kwh) + window_kwh / 2.0
        ),
        limits=limits,
        reserve=reserve,
    )
    if calibration is None:
        return None
    charge_ratio, discharge_ratio = _calibrate(calibration, probe_kw)
    if charge_ratio <= 0.0 or discharge_ratio <= 0.0:
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


#: The calibration probe's power, and how small a share of the usable window it
#: must fit inside.
#:
#: One kilowatt over a quarter is 0.25 kWh AC, so the probe needs roughly 0.53 kWh
#: of window to read cleanly in both directions. An eighth of the window is
#: comfortably inside that for every window the full-power probe can use, and
#: shrinks with the window when it cannot.
_CALIBRATION_PROBE_KW: float = 1.0
_CALIBRATION_WINDOW_SHARE: float = 8.0


def _calibrate(
    probe: Any, power_kw: float = _CALIBRATION_PROBE_KW
) -> tuple[float, float]:
    """Return the measured DC-per-AC ratio in each direction.

    One clamp call per direction, at a power well inside every limit, so neither
    reading is taken through a clamp that bound. The ratios are what the inverse
    conversion needs, and taking them from the clamp rather than from the limits
    means a change to the conversion cannot leave the planner behind.

    ``power_kw`` is scaled by the caller to the configured usable window. A
    reading taken through a clamp is not a ratio, it is the clamp -- which is
    exactly how a hardcoded probe silently disabled the optimiser for anyone with
    a minimum state of charge at or above 50 %.
    """
    charged = apply_request(probe, BatteryRequest.charge(power_kw))
    discharged = apply_request(probe, BatteryRequest.discharge(power_kw))
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
    #: The run state the DP actually occupied for this interval: ``idle``,
    #: ``charge`` or ``discharge``.
    #:
    #: **This is the objective's own unit, and until beta.32 it was computed and
    #: thrown away.** ``_walk_forward`` already resolves it in order to decide
    #: ``run_start``; carrying it is what lets a campaign be grouped on what the
    #: solver did rather than on what a label says. Absorption is already
    #: transparent here, because ``_resolved_run_state`` folds ``_RUN_ABSORB``
    #: into whichever run was in progress.
    run_state: str = ECONOMIC_DIRECTION_IDLE
    #: AC energy the inverter covered from the battery without being dispatched.
    #: Non-zero only on a hold interval where ambient self-consumption is modelled
    #: and the pack had room above the floor to supply it.
    ambient_self_consumption_ac_kwh: float = 0.0
    #: Which counterfactual every marginal figure on this interval rests on. See
    #: ``COUNTERFACTUAL_*``; published so a reader never has to guess.
    counterfactual_basis: str = COUNTERFACTUAL_IDLE_IMPORT

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
    #: The largest battery power any single quarter of this run commands, AC.
    #:
    #: Beside the mean because a run legitimately varies quarter to quarter, and
    #: the mean alone is actively misleading when it does: a real campaign
    #: averaging 3.50 kW bought at 10 kW in two quarters and absorbed free
    #: production in eleven more. A max over the same intervals the sums above
    #: come from -- a reading, not a computation.
    peak_power_kw: float = 0.0
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
    #: AC energy this plan moves through the battery, both directions summed, and
    #: what the throughput term charged for it. **The churn measure**: a plan that
    #: earns the same money on twice the throughput is the worse plan, and before
    #: beta.31 nothing in the payload said so. Notional like the two above.
    battery_throughput_kwh: float = 0.0
    battery_throughput_cost_eur: float = 0.0
    #: What the terminal inventory was worth, and the energy it was worth it on.
    #: ``edge_value_eur`` is a *credit*, so it raises expected value rather than
    #: lowering cost -- energy left in the pack at the price horizon's end is an
    #: asset, not a saving.
    edge_energy_kwh: float = 0.0
    edge_value_eur: float = 0.0

    # -- what the entity reads --------------------------------------------

    #: The campaigns the objective itself formed, grouped on the DP's own run
    #: state. Published **beside** ``runs`` rather than instead of them: a run is
    #: the honest record of what one interval was doing, and a campaign is the
    #: unit the fee was charged against. ``len(campaigns) == direction_changes``
    #: by construction, which is the proof the grouping changed no decision.
    campaigns: tuple[EconomicCampaign, ...] = ()

    @property
    def objective_eur(self) -> float:
        """Return the scalar this plan was chosen to minimise.

        **``cost_eur`` is not it, and assuming otherwise inverts comparisons.**
        ``cost_eur`` is the metered cash flow alone; the recursion also charges the
        switching fee, the grid-charge margin and the throughput cost, and credits
        the terminal inventory at the edge value -- each of them published as its own
        field precisely so a reader can see what money was which. Their sum is what
        the lexicographic pair's second element actually holds.

        Measured on the live 17:45 horizon while auditing the beta.32 export
        permission: ``cost_eur`` fell by 0.022 while the switching fee rose by 0.20,
        so a comparison on ``cost_eur`` alone reported that a restriction had *saved*
        money. Over a 64-shape sweep the margin term inverted the sign again on its
        own. Two plans may only be compared on this figure.

        Comparing two plans is meaningful only when both carry the same
        ``violation_kwh``: the objective is lexicographic, and no amount of money
        outranks a violation.
        """
        return (
            self.cost_eur
            + self.switching_cost_eur
            + self.grid_charge_margin_eur
            + self.battery_throughput_cost_eur
            - self.edge_value_eur
        )

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
        return (
            (self.hold_cost_eur - self.cost_eur)
            - self.switching_cost_eur
            # Terminal inventory is an asset the plan ends holding, so it *raises*
            # what the plan is worth. Reported inside the gain rather than folded
            # into ``cost_eur``, which must keep reconciling to grid energy at the
            # interval's own prices -- the same discipline as the margin.
            + self.edge_value_eur
        )

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
        # **Since beta.32 this also equals ``len(campaigns)``**, because a
        # campaign *is* a maximal stretch of one run state and the fee is charged
        # exactly at each transition into one. Asserted in the tests rather than
        # assumed here: that equality is what proves the campaign layer, which
        # exists purely to group, decided nothing.
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


def actionable_intervals(
    demands: Sequence[IntervalDemand],
    prices: Sequence[IntervalPrice],
) -> int:
    """Return how many leading intervals the optimiser can actually act in.

    The contiguous prefix where a price exists *and* a demand is forecast -- the
    same prefix :func:`build_horizon` keeps, computed separately because the
    reachability reserve needs the count *before* the horizon can be built, and
    the horizon needs the reserve. Deriving it from prices and demands alone
    breaks that circularity without either input having to know about the other.

    This is the number handed to the reachability recursion as
    ``grid_credit_intervals``, and it is the whole of what the reserve layer is
    told: a count, with no hint of why the window ends. Inside it a refill can be
    priced and chosen; beyond it, nothing may be assumed.
    """
    count = 0
    for position, demand in enumerate(demands):
        if position >= len(prices) or not prices[position].known:
            break
        if demand.net_demand_kwh is None:
            break
        count += 1
    return count


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


def edge_value_eur_per_kwh(
    prices: Sequence[IntervalPrice],
    *,
    discharge_efficiency: float,
) -> float:
    """Return what a kWh still in the pack is worth when the prices run out.

    **The horizon edge must be neither a wall nor a cliff.** The autonomy reserve
    made it a wall -- provisioning a whole unpriced day with a hard bound, which
    immobilised the pack. Removing it entirely makes it a cliff: energy above the
    terminal floor becomes worth exactly zero, so the plan sells it at whatever
    export pays. Both faults have one cause, an unpriced future represented as a
    *bound* instead of a *value*, and one fix.

    The figure is a **replacement cost**: what it would cost to buy this kWh back
    at a price we have actually seen. So the solver becomes indifferent between
    holding a kWh to the edge and buying one in the cheapest visible quarter,
    which removes the incentive to hoard and the incentive to dump in one number.

    Four deliberate choices, each ruling out a failure:

    * **The 25th percentile, not the minimum.** ``min`` is the least robust
      statistic there is: one freak-cheap quarter drags the whole estimate down
      and the planner *under-holds*. The quantile absorbs an outlier.
    * **Floored at zero.** Negative wholesale intervals are routine here, and a
      negative edge value would make the objective *reward dumping* energy at the
      horizon's end -- a real pathology, not a corner case.
    * **Capped at the dearest price seen.** A kWh can never be worth more than
      the most anyone was ever charged for one, so "pay anything to hold
      inventory" is structurally impossible rather than merely unlikely.
    * **Discharge efficiency only, never round trip.** The energy is *already
      stored*; only the outbound conversion remains. Charging it the inbound loss
      as well would understate stock by about five per cent.

    Only prices that exist. No forecast, no assumed tomorrow, and no slope
    carried from the previous refresh -- that last would be a temporal feedback
    loop, which is precisely the ratcheting beta.18 removed from the old terminal
    constraint.
    """
    known = sorted(
        price.import_eur_kwh
        for price in prices
        if price.import_eur_kwh is not None and math.isfinite(price.import_eur_kwh)
    )
    if not known or discharge_efficiency <= 0.0:
        return 0.0
    index = max(0, min(len(known) - 1, int(0.25 * (len(known) - 1) + 0.5)))
    ceiling = discharge_efficiency * known[-1]
    if ceiling <= 0.0:
        return 0.0
    return max(0.0, min(discharge_efficiency * known[index], ceiling))


def edge_creditable_energy_kwh(
    *,
    ceiling_kwh: float,
    forecast_surplus_kwh: float,
) -> float:
    """Return how much terminal inventory may be *valued*, given incoming sun.

    **Terminal value must not pay for displacing free energy.** A pack held full
    at the horizon edge has nowhere to put the surplus a forecast says is
    arriving, so its last kWh is not worth its replacement cost -- it is worth
    that cost *minus* the production it locks out, which for free production is
    the whole of it.

    So the credited energy is capped at the room the surplus needs. Above the cap
    the inventory is still physically there and still reported in
    ``edge_energy_kwh``; it simply earns nothing, which is the honest price of
    energy that is about to arrive for free.

    No PV accounting is duplicated: the surplus is the same quantity the reserve
    recursion already nets, and the ceiling is the battery's own.
    """
    return max(0.0, ceiling_kwh - max(0.0, forecast_surplus_kwh))


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
    battery_throughput_cost_eur_per_kwh: float = 0.0,
    edge_value_eur_per_kwh: float = 0.0,
    edge_creditable_kwh: float = float("inf"),
    #: The configured hard floor, in DC kWh. Read only to decide whether the pack
    #: can physically cover a residual load; defaults to the terminal floor, which
    #: every production caller passes as the same figure.
    floor_energy_kwh: float | None = None,
    #: Whether the inverter covers residual house load from the battery when
    #: nothing is dispatched. **Defaults to false, so behaviour is byte-identical
    #: to beta.31 unless a caller has evidence.** See ``_interval_outcomes``.
    ambient_self_consumption: bool = False,
    #: The export permission, per interval. ``export_floor`` is the DC energy the
    #: pack should still hold to reach the next refill it expects to use, and
    #: ``export_free`` says the export price already beats what that energy is
    #: worth to the house. Both ``None`` disables the gate entirely, which is what
    #: the ungated first pass and every legacy caller get.
    #:
    #: **This is a permission on a caused-export delta and nothing else.** It never
    #: enters ``violations``, never gates a hold, a charge or a load-serving
    #: discharge, and never changes the reserve curve -- so it cannot become a hard
    #: inventory bound, and the zero delta always remains available from every
    #: bucket.
    export_floor_kwh: Sequence[float] | None = None,
    export_free: Sequence[bool] | None = None,
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
    hard_floor_kwh = (
        terminal_floor_kwh if floor_energy_kwh is None else floor_energy_kwh
    )
    # The most the pack can deliver to the house in one quarter. Read from the
    # limits the table was built from, so there is one authority for it.
    # The gate is on only when the caller supplied both halves for every
    # interval. A partial curve is a programming error, not a weaker gate.
    gated = (
        export_floor_kwh is not None
        and export_free is not None
        and len(export_floor_kwh) >= count
        and len(export_free) >= count
    )
    max_discharge_ac_kwh = table.limits.max_discharge_kw * INTERVAL_HOURS
    # The smallest discharge the state space can express, read from the table's own
    # moves rather than derived from the bucket -- the clamp is the authority for
    # what a delta actually delivers.
    discharges = [
        discharge_ac
        for delta, (_charge_ac, discharge_ac) in ac_by_delta.items()
        if delta < 0 and discharge_ac > 0.0
    ]
    smallest_discharge_ac_kwh = min(discharges) if discharges else 0.0
    outcomes_per_interval: list[dict[int, _DeltaOutcome]] = [
        _interval_outcomes(
            ac_by_delta=ac_by_delta,
            load_ac_kwh=demand.baseline_kwh or 0.0,
            pv_ac_kwh=demand.pv_kwh or 0.0,
            price=horizon.prices[position],
            permitted=permitted,
            ambient_self_consumption=ambient_self_consumption,
            max_discharge_ac_kwh=max_discharge_ac_kwh,
            discharge_efficiency=table.limits.discharge_efficiency,
            smallest_discharge_ac_kwh=smallest_discharge_ac_kwh,
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
    # **Where the edge value enters, and it is one line of arithmetic.** The
    # terminal condition used to be a pure feasibility test carrying no price,
    # which is exactly what made energy above the floor worth nothing at the
    # horizon end. Seeding it with ``-v_edge * E`` gives terminal inventory an
    # explicit worth, so the backward induction trades it against today prices at
    # the margin instead of against a wall. A *credit*, hence a negative cost.
    #
    # It cannot reintroduce "pay anything": it is finite, bounded above by the
    # dearest price seen, and it lives in the second element of the pair, so
    # reserve feasibility still dominates it lexicographically.
    edge_credit = [
        edge_value_eur_per_kwh * min(table.energy(bucket), edge_creditable_kwh)
        for bucket in range(buckets + 1)
    ]
    for bucket in range(buckets + 1):
        feasible = bucket >= terminal_bucket
        for run in _RUN_STATES:
            value[bucket][run] = (
                (0.0, -edge_credit[bucket]) if feasible else _UNREACHABLE
            )

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
                    # **The export permission.** Refuse only a move that (a) pushes
                    # energy across the meter beyond what the site would have
                    # spilled anyway, (b) at a price that does not beat what that
                    # energy is worth to the house before the next refill, and (c)
                    # would leave the pack below what it needs to get there.
                    #
                    # All three, because any one alone would be wrong: (a) keeps
                    # self-consumption untouched, (b) keeps a genuinely good sale
                    # available, and (c) keeps the protection about *inventory* the
                    # plan actually needs rather than a blanket reluctance to sell.
                    if (
                        gated
                        and outcome.caused_export
                        and not export_free[position]
                        and energies[move.target] < export_floor_kwh[position]
                    ):
                        continue
                    onward_state = _resolved_run_state(outcome.run_state, run)
                    onward = following[move.target][onward_state]
                    if onward >= _UNREACHABLE:
                        continue
                    cost = outcome.cost_eur
                    # **Holding does not mean importing.** Where the inverter can
                    # cover the residual load from the pack, that -- not a full
                    # purchase -- is what holding costs. Chosen per bucket because
                    # a pack at the floor cannot serve anything.
                    if outcome.ambient is not None and _ambient_applies(
                        outcome,
                        energy_kwh=energies[bucket],
                        floor_energy_kwh=hard_floor_kwh,
                    ):
                        cost = outcome.ambient.cost_eur
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
                    # **The only per-kWh cost on the discharge side.** Charged on
                    # AC throughput in *both* directions, so four shallow cycles
                    # cost four times what one equivalent cycle does and the
                    # search prefers the deep one at equal revenue. Local for the
                    # same reason as the margin above: it depends only on this
                    # interval's own movement, so it needs no accumulated-energy
                    # axis and adds no solver dimension.
                    #
                    # A different base from the margin, deliberately: that one
                    # measures *purchased* energy, this one measures *movement*.
                    # Neither is depreciation, and conflating them would charge
                    # grid charging twice.
                    if battery_throughput_cost_eur_per_kwh > 0.0:
                        cost += battery_throughput_cost_eur_per_kwh * (
                            outcome.charge_ac_kwh + outcome.discharge_ac_kwh
                        )
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
        hard_floor_kwh=hard_floor_kwh,
        minimum_trade_gain_eur=minimum_trade_gain_eur,
        permitted=permitted,
        grid_charge_margin_eur_per_kwh=grid_charge_margin_eur_per_kwh,
        battery_throughput_cost_eur_per_kwh=battery_throughput_cost_eur_per_kwh,
        edge_value_eur_per_kwh=edge_value_eur_per_kwh,
        edge_creditable_kwh=edge_creditable_kwh,
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
    #: Whether this move pushes energy across the meter *beyond* what the site
    #: would have exported anyway, by more than the actuator can express.
    #:
    #: Computed at the label site and carried rather than re-derived, because the
    #: export gate needs exactly this predicate and a second derivation is a second
    #: thing to keep in step. ``False`` for every charge, every hold, and every
    #: discharge whose spill is a lattice remainder.
    caused_export: bool = False
    #: The same, for grid-caused import. Carried for symmetry and for diagnostics.
    caused_import: bool = False
    #: The hold delta's *ambient* alternative: what the interval imports, exports
    #: and costs if the inverter covers the residual load from the battery instead
    #: of buying it, together with the AC and DC energy that costs.
    #:
    #: Carried rather than substituted because whether it applies depends on the
    #: **bucket** -- a pack at the floor cannot serve anything -- and this object is
    #: shared by every bucket at this interval. ``solve`` and ``_walk_forward``
    #: choose between the two with the bucket in hand. ``None`` on every non-hold
    #: delta and whenever ambient self-consumption is not modelled, in which case
    #: behaviour is byte-identical to beta.31.
    ambient: _AmbientOutcome | None = None


@dataclass(frozen=True, slots=True)
class ForecastRisk:
    """The measured forecast-quality evidence the export gate is allowed to use.

    Every field is a quantity the learning stack already computes and, before
    beta.32, published to nobody who could act on it. There is **no distribution
    here and no quantile**: ``WindowSummary`` carries first moments only, so a
    "p80 demand" would be a normality claim the system has never tested. What it
    does carry is a mean signed error, a mean absolute error split by provenance,
    and -- new in beta.32 -- how much of a day's error points the same way.

    ``None`` everywhere means "no claim", and the cascade in :func:`err_for` then
    falls back *conservatively*: a thin history yields **more** protection, not
    less, which is the correct direction when the model is least trustworthy.
    """

    #: Mean signed error, positive when the model over-predicts. Only the negative
    #: side is used: over-predicting cannot strand the pack.
    bias_kwh: float | None = None
    mae_kwh: float | None = None
    mae_modelled_kwh: float | None = None
    mae_filled_kwh: float | None = None
    mae_by_band: dict[str, float] | None = None
    #: ``rho``: 1.0 when a day's errors are one-directional, ~1/sqrt(n) when they
    #: cancel. Unavailable means 1.0 -- the conservative end.
    error_persistence: float | None = None
    #: Today's measured consumption against what was expected by now. Applied to
    #: **protection only**, one-sided, clipped, and to today's intervals alone.
    adaptation_ratio: float | None = None
    #: How many leading intervals of the horizon belong to today. Adaptation is
    #: meaningless beyond it.
    today_interval_count: int = 0

    def mae_for(self, filled: bool | None, band: str | None) -> float | None:
        """Return the most specific measured error available for one interval.

        Provenance first: an extrapolated interval is not as trustworthy as a
        modelled one, and ``mae_filled_kwh`` / ``mae_modelled_kwh`` measure exactly
        that difference. Then the band, then the window as a whole.
        """
        if filled and self.mae_filled_kwh is not None:
            return self.mae_filled_kwh
        if filled is False and self.mae_modelled_kwh is not None:
            return self.mae_modelled_kwh
        if band and self.mae_by_band:
            value = self.mae_by_band.get(band)
            if value is not None:
                return value
        return self.mae_kwh


def err_for(demand: IntervalDemand, risk: ForecastRisk) -> float:
    """Return the one-sided AC allowance for one interval's forecast error.

    ``max(0, -bias) + rho * mae`` -- both terms measured, neither assumed.

    One-sided on the bias because only *under*-prediction can strand the pack: a
    model that habitually over-predicts is already protected by its own optimism.
    Scaled by ``rho`` because whether error accumulates as ``mae * sqrt(n)`` or
    ``mae * n`` is a fact about the household, and the window's rows measure it.

    Fallback cascade, each rung an existing measured quantity, and each *more*
    conservative than the one above:

    1. ``rho`` unavailable -> treat it as 1.0, the persistent end;
    2. ``bias`` unavailable -> the MAE term alone;
    3. no MAE at all -> no statistical term, and the caller's own floors apply.
    """
    bias = 0.0 if risk.bias_kwh is None else max(0.0, -risk.bias_kwh)
    mae = risk.mae_for(demand.filled, None)
    if mae is None:
        return bias
    rho = 1.0 if risk.error_persistence is None else risk.error_persistence
    return bias + max(0.0, rho) * max(0.0, mae)


def upper_net_demand_curve(
    demands: Sequence[IntervalDemand],
    risk: ForecastRisk,
    *,
    adaptation_ceiling: float,
) -> tuple[tuple[float, ...], float, bool]:
    """Return the protective net-demand curve, the ratio used, and whether it clipped.

    ``max(0, baseline * adapt - pv + err)`` per interval, in **AC kWh** -- the same
    boundary ``baseline_kwh`` and ``pv_kwh`` are already at, so nothing here needs
    an efficiency.

    **This curve reaches the export permission and the Safety-Buy extension, and
    nothing else.** It must never enter a priced quantity: the cost objective keeps
    using ``demand.baseline_kwh``, because smuggling a pessimistic forecast into
    the objective would be building a second forecast, which is a stated non-goal.

    Adaptation is one-sided and clipped. A *quiet* today may not license selling
    more -- only "today is busier than the model expected" can strand the pack --
    and the ceiling stops an unstable early-morning ratio, when expected-so-far is
    small, from becoming a runaway multiplier.

    The cumulative error allowance is capped by the P50 demand it corrects: an
    allowance larger than the forecast is not a correction, it is a different
    forecast.
    """
    raw = 1.0 if risk.adaptation_ratio is None else risk.adaptation_ratio
    adapt = min(max(1.0, raw), adaptation_ceiling)
    clipped = raw > adaptation_ceiling

    curve: list[float] = []
    p50_total = 0.0
    err_total = 0.0
    for position, demand in enumerate(demands):
        baseline = demand.baseline_kwh or 0.0
        pv = demand.pv_kwh or 0.0
        factor = adapt if position < risk.today_interval_count else 1.0
        p50 = max(0.0, baseline - pv)
        err = err_for(demand, risk)
        p50_total += p50
        err_total += err
        curve.append(max(0.0, baseline * factor - pv + err))

    if err_total > p50_total > 0.0:
        # Scale the whole allowance back rather than truncating the tail, so the
        # shape of the protection still follows the shape of the demand.
        scale = p50_total / err_total
        curve = [
            max(
                0.0,
                (demand.baseline_kwh or 0.0)
                * (adapt if position < risk.today_interval_count else 1.0)
                - (demand.pv_kwh or 0.0)
                + err_for(demand, risk) * scale,
            )
            for position, demand in enumerate(demands)
        ]
    return tuple(curve), adapt, clipped


def anti_churn_buffer_kwh(
    demands: Sequence[IntervalDemand],
    risk: ForecastRisk,
    *,
    window_end: int,
    bucket_kwh: float,
    discharge_efficiency: float,
) -> float:
    """Return how much a triggered Safety Buy should over-buy, in **DC kWh**.

    ``bucket_kwh + min(sum p50_net, sum err) / eta_discharge`` over the survival
    window. Every term is measured or physical and there is no decay constant --
    an earlier draft had one, applied backwards, so that the buffer was *largest*
    when the refill was closest.

    **The distance lives in the quantity, not in a decay factor.** Both sums run
    to the refill the plan expects to use, so a refill next quarter yields
    essentially the bucket floor and a refill four hours out yields four hours of
    the smaller of the two terms. Monotonic non-decreasing in the distance, by
    construction: both sums are of non-negative terms, and the minimum of two
    non-decreasing sequences is non-decreasing.

    **The floor is one lattice bucket**, because below one bucket the state space
    cannot represent a difference -- so a purchase flip driven by less than a
    bucket is driven by noise the model cannot resolve. ``_safety_buy_runs``
    already uses the bucket for the same judgement.

    **The cap is the P50 load term, not the bridge.** Bounding it by the immediate
    bridge would defeat the point: the requirement is to cover the household until
    the *meaningful* refill, which is generally more than the head deficit. What
    the buffer may never exceed is the demand it is protecting.

    This quantity **cannot initiate a purchase** -- see :func:`build_outcome`,
    where it is applied to the enforced head only while a bridge already exists.
    """
    if window_end <= 0:
        return bucket_kwh
    p50_total = 0.0
    err_total = 0.0
    for demand in demands[:window_end]:
        p50_total += max(0.0, (demand.baseline_kwh or 0.0) - (demand.pv_kwh or 0.0))
        err_total += err_for(demand, risk)
    if discharge_efficiency <= 0.0:
        return bucket_kwh
    return bucket_kwh + min(p50_total, err_total) / discharge_efficiency


def survival_window_end(
    plan: EconomicPlan, *, actionable_intervals: int
) -> tuple[int, str]:
    """Return where the survival window closes, and on what basis.

    **The refill the plan expects to use, not the first tolerable price.** A
    price-only rule cannot get this right: with ``[0.30 now, 0.24 tonight, 0.35 x
    n, 0.12 tomorrow]`` every relative test picks tonight's mediocre 0.24, because
    it genuinely beats everything seen so far -- and the household would be far
    better off surviving to 0.12. The only definition that cannot drift from the
    plan is the plan's own choice.

    So: the start of the first **material, executable charge campaign**. Absent
    one, the window is the reliably known horizon -- the actionable prefix, which
    breaks at the first unpriced *or* unforecast interval. Nothing is invented
    beyond it, and no price is guessed.
    """
    for campaign in plan.campaigns:
        if campaign.direction != ECONOMIC_DIRECTION_CHARGE:
            continue
        if not campaign.sell_announcement_material:
            continue
        return campaign.start_index, SURVIVAL_WINDOW_PLAN_CAMPAIGN
    return actionable_intervals, SURVIVAL_WINDOW_ACTIONABLE_PREFIX


def survival_curves(
    upper_net_demand: Sequence[float],
    prices: Sequence[IntervalPrice],
    *,
    window_end: int,
    floor_energy_kwh: float,
    discharge_efficiency: float,
) -> tuple[tuple[float, ...], tuple[float | None, ...]]:
    """Return ``(economic_survival_to_refill_kwh, p_protect)`` per interval.

    The energy curve is **DC**: the house is served at AC, so the pack must hold
    ``AC / eta_discharge`` to deliver it. The floor is added because survival means
    *reaching the refill without crossing the floor*, not *without emptying*.

    The price curve is **EUR per grid AC kWh on both sides, with no efficiency at
    all**, and that is worth stating because it looks wrong at first glance. One DC
    kWh held to serve the house later avoids ``p_import * eta_discharge``; the same
    DC kWh exported now earns ``p_export * eta_discharge``. Same energy, same
    single conversion -- **the efficiency cancels** and the comparison is prices
    directly. An earlier draft divided by the round trip, which made the gate about
    11 % too strict and would have refused genuinely good trades.

    Demand-weighted, because the energy is claimed by demand rather than by time:
    an hour of darkness and an hour of cooking do not have equal call on it.
    """
    count = len(upper_net_demand)
    energy: list[float] = []
    price: list[float | None] = []
    for position in range(count):
        stop = min(window_end, count)
        window = range(position, max(position, stop))
        needed_ac = sum(upper_net_demand[k] for k in window)
        energy.append(
            floor_energy_kwh
            + (needed_ac / discharge_efficiency if discharge_efficiency > 0.0 else 0.0)
        )
        weight = 0.0
        weighted = 0.0
        for k in window:
            if k >= len(prices):
                break
            import_price = prices[k].import_eur_kwh
            if import_price is None:
                continue
            weight += upper_net_demand[k]
            weighted += upper_net_demand[k] * import_price
        # No demand in the window means nothing is spoken for, so there is nothing
        # to protect and no price to protect it at. ``None``, never zero: zero would
        # read as "any export beats this".
        price.append(weighted / weight if weight > 0.0 else None)
    return tuple(energy), tuple(price)


def _ambient_applies(
    outcome: _DeltaOutcome, *, energy_kwh: float, floor_energy_kwh: float
) -> bool:
    """Return whether the pack can actually cover this interval's residual load.

    **The third clamp, and the one that needs the bucket.** Net demand and
    discharge power are bounded once per interval; the energy available above the
    floor is a property of where the trajectory *is*. A pack at the floor cannot
    self-consume, and a model that said otherwise would tell the optimiser that
    holding at 20 % costs nothing -- removing the very pressure to buy that keeps
    the floor safe.

    The floor here is the configured hard floor, not the reserve curve: the clamp
    is about what the hardware can physically deliver, and the reserve curve is an
    economic instrument layered above it.
    """
    ambient = outcome.ambient
    if ambient is None:
        return False
    return energy_kwh - ambient.dc_kwh >= floor_energy_kwh - 1e-9


@dataclass(frozen=True, slots=True)
class _AmbientOutcome:
    """What a hold interval costs when the inverter serves the house itself.

    Every figure is at a named boundary: the grid pair at the meter, the discharge
    at the battery AC terminal, and ``dc_kwh`` at the pack -- the last being what
    must be available above the floor for any of it to be possible.
    """

    grid_import_kwh: float
    grid_export_kwh: float
    cost_eur: float
    discharge_ac_kwh: float
    dc_kwh: float


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
    ambient_self_consumption: bool = False,
    max_discharge_ac_kwh: float = 0.0,
    discharge_efficiency: float = 1.0,
    smallest_discharge_ac_kwh: float = 0.0,
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

    # **The ambient alternative to holding, computed once per interval.**
    #
    # An inverter with nothing dispatched does not let the house import while the
    # pack sits: it covers the residual load from the battery. That is the same
    # ambient behaviour beta.31 already models in the charge direction as surplus
    # absorption, and modelling only one direction of it is what left the discharge
    # side to a lattice that cannot express it (see the module note on R5).
    #
    # Clamped by the residual load -- it never *creates* a discharge -- and by the
    # quarter's discharge power. The third clamp, the energy actually available
    # above the floor, needs the bucket and is applied by the callers.
    ambient: _AmbientOutcome | None = None
    # **Only where the lattice cannot express the service.** This model exists to
    # fill exactly one gap: a residual load smaller than the smallest discharge the
    # state space can represent. Above that threshold the solver's own discharge
    # moves fit, and it must keep choosing them -- a load-serving discharge is a
    # real economic decision with a real published action, and replacing it with
    # ``hold`` would hide the battery covering an expensive evening.
    #
    # So the trigger is the lattice, not the price and not the mode. Below one
    # discharge bucket there is no move that serves without overshooting, and
    # beta.31 answered that by importing the whole load.
    if (
        ambient_self_consumption
        and 0.0 < unavoidable_import < smallest_discharge_ac_kwh
    ):
        served_ac = min(unavoidable_import, max_discharge_ac_kwh)
        if served_ac > 0.0:
            ambient_flows = split_grid_energy(
                load_ac_kwh=load_ac_kwh,
                pv_ac_kwh=pv_ac_kwh,
                charge_ac_kwh=0.0,
                discharge_ac_kwh=served_ac,
            )
            ambient = _AmbientOutcome(
                grid_import_kwh=ambient_flows.import_kwh,
                grid_export_kwh=ambient_flows.export_kwh,
                cost_eur=(
                    import_price * ambient_flows.import_kwh
                    - export_price * ambient_flows.export_kwh
                ),
                discharge_ac_kwh=served_ac,
                dc_kwh=(
                    served_ac / discharge_efficiency
                    if discharge_efficiency > 0.0
                    else 0.0
                ),
            )

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
        # **The export label is a materiality judgement, not an equality test.**
        # The bucket lattice cannot cancel a continuous residual load exactly:
        # ``_move_to`` discards any clamp-reduced move, so battery movement is
        # either zero or at least one bucket (0.15-0.40 kWh DC), while load and PV
        # are continuous forecasts. A discharge therefore *must* either under-serve
        # -- leaving an import remainder -- or over-serve, leaving an export one.
        #
        # Through beta.31 the threshold was ``1e-9`` kWh, so a lattice remainder of
        # a few watt-hours renamed the whole interval ``export``. Measured
        # consequences, all from that one epsilon: the DP's three run-state
        # transitions were published as ten runs; seven intervals carried an
        # ``export`` label for a rounding artefact; ``grid_export_target_kwh`` rows
        # appeared below what any command can realise; and -- the serious one --
        # with ``allow_battery_export`` off, *every* discharge became impermissible
        # (the remainder needs the export permission), so a household whose load
        # sits below one bucket could not use its battery at all: 0.000 kWh
        # discharged against 4.560 kWh imported at 0.35, EUR 2.79/day worse.
        #
        # ``MIN_EXECUTABLE_QUARTER_KWH`` is 0.1 kW held for a whole quarter -- the
        # finest energy the actuator can express. Below it there is no commanded
        # export, only arithmetic, so calling it one was the error.
        caused_export = (
            flows.export_kwh > unavoidable_export + MIN_EXECUTABLE_QUARTER_KWH
        )
        # **Deliberately asymmetric, and it must stay that way.** An import
        # remainder is a real purchase, and ``allow_grid_charging`` is a user
        # instruction *about purchases*; deadbanding it would buy ~0.02 kWh a
        # quarter, ~2.3 kWh a day, against the user's wishes. An export remainder
        # is a lattice artefact. Same epsilon as beta.31.
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
            caused_export=caused_export,
            caused_import=caused_import,
            ambient=ambient if delta == 0 else None,
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
    battery_throughput_cost_eur_per_kwh: float = 0.0,
    edge_value_eur_per_kwh: float = 0.0,
    edge_creditable_kwh: float = float("inf"),
    hard_floor_kwh: float = 0.0,
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
    # DC energy the inverter has drawn ambiently so far. Not representable on the
    # bucket lattice -- that is the whole reason ambient service exists as a
    # separate model -- so it is carried here and subtracted from every reported
    # state of charge. Bounded per interval by the residual load, so the divergence
    # from the lattice is always under one bucket per quarter, which is the
    # resolution the trajectory already has; and the plan is rebuilt from measured
    # state of charge every quarter, so the accumulation window is one interval.
    ambient_drained_dc_kwh = 0.0
    total_switching = 0.0
    total_margin = 0.0
    total_grid_charge = 0.0
    total_throughput = 0.0
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
        # **The report must describe the move the DP priced, not a different
        # one.** Where the pack could cover the residual load ambiently, the DP
        # charged the ambient cost for holding, so the published interval carries
        # the ambient flows too. Same predicate, same floor, one helper -- a second
        # derivation here is how a plan and its own cost drift apart.
        # **Measured against the drained pack, not the lattice bucket.** The
        # ambient discharge is real energy that the bucket cannot represent, so the
        # forward walk carries it as a running offset. Without it the clamp would
        # read a state of charge that never falls and keep authorising ambient
        # service straight through the floor -- the one thing this must not do.
        energy_now_kwh = max(0.0, table.energy(bucket) - ambient_drained_dc_kwh)
        serves_ambiently = outcome.ambient is not None and _ambient_applies(
            outcome, energy_kwh=energy_now_kwh, floor_energy_kwh=hard_floor_kwh
        )
        ambient = outcome.ambient if serves_ambiently else None
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
        # Both directions, because churn is movement rather than purchase.
        total_throughput += outcome.charge_ac_kwh + outcome.discharge_ac_kwh
        total_cost += ambient.cost_eur if ambient is not None else outcome.cost_eur

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
                start_energy_dc_kwh=energy_now_kwh,
                battery_delta_dc_kwh=move.delta_dc_kwh,
                battery_charge_ac_kwh=outcome.charge_ac_kwh,
                battery_discharge_ac_kwh=(
                    outcome.discharge_ac_kwh
                    if ambient is None
                    else ambient.discharge_ac_kwh
                ),
                grid_import_kwh=(
                    outcome.grid_import_kwh
                    if ambient is None
                    else ambient.grid_import_kwh
                ),
                grid_export_kwh=(
                    outcome.grid_export_kwh
                    if ambient is None
                    else ambient.grid_export_kwh
                ),
                pv_curtailed_kwh=outcome.curtailed_kwh,
                cost_eur=(outcome.cost_eur if ambient is None else ambient.cost_eur),
                import_price_eur_kwh=price.import_eur_kwh,
                export_price_eur_kwh=price.export_eur_kwh,
                run_start=run_start,
                constraints=move.constraints,
                # **The counterfactual moves with the model.** A marginal figure is
                # a difference against what would otherwise have happened; where the
                # inverter would have served the house itself, *that* is the
                # alternative, not a full purchase. Leaving beta.31's
                # import-everything baseline here would have credited the battery
                # for savings the inverter delivers by itself.
                idle_import_kwh=(
                    outcome.idle_import_kwh
                    if ambient is None
                    else ambient.grid_import_kwh
                ),
                idle_export_kwh=(
                    outcome.idle_export_kwh
                    if ambient is None
                    else ambient.grid_export_kwh
                ),
                idle_cost_eur=(
                    outcome.idle_cost_eur if ambient is None else ambient.cost_eur
                ),
                ambient_self_consumption_ac_kwh=(
                    0.0 if ambient is None else ambient.discharge_ac_kwh
                ),
                counterfactual_basis=(
                    COUNTERFACTUAL_IDLE_IMPORT
                    if ambient is None
                    else COUNTERFACTUAL_AMBIENT_SELF_CONSUMPTION
                ),
                absorbing=outcome.run_state == _RUN_ABSORB,
                run_state=_RUN_STATE_NAMES[resolved],
            )
        )
        if ambient is not None:
            ambient_drained_dc_kwh += ambient.dc_kwh
        bucket = move.target
        run = resolved

    return EconomicPlan(
        intervals=tuple(entries),
        runs=runs_from(tuple(entries)),
        campaigns=campaigns_from(
            tuple(entries), minimum_trade_gain_eur=minimum_trade_gain_eur
        ),
        violation_kwh=total_violation,
        cost_eur=total_cost,
        hold_cost_eur=hold_cost(
            horizon=horizon,
            table=table,
            start_energy_kwh=table.energy(start_bucket),
        ),
        switching_cost_eur=total_switching,
        grid_charge_margin_eur=total_margin,
        battery_throughput_kwh=total_throughput,
        battery_throughput_cost_eur=(
            battery_throughput_cost_eur_per_kwh * total_throughput
        ),
        edge_energy_kwh=table.energy(bucket),
        edge_value_eur=(
            edge_value_eur_per_kwh * min(table.energy(bucket), edge_creditable_kwh)
        ),
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


@dataclass(frozen=True, slots=True)
class EconomicSegment:
    """One contiguous stretch of a campaign that shares an execution intent.

    **The layer that exists because a campaign must not collapse the intent.**
    ``CONTROL_LIVE_DISPATCH_INTENTS`` is ``{grid_charge, net_export}``,
    ``carry_plan`` refuses anything else, and ``admit_quarter`` takes the intent
    from the *plan* rather than the row -- so handing Stage B a whole discharge
    campaign under one intent would either dispatch export in quarters planned as
    self-consumption or lose the genuine export quarters entirely.

    So a campaign is the economic and identity unit, and a segment is what Stage B
    can be handed. A ``serve_load`` segment emits nothing: ordinary
    self-consumption stays inverter behaviour, exactly as it is today.
    """

    intent: str
    start_index: int
    end_index: int
    interval_count: int
    #: The objective at the boundary this intent is paid at: meter export for
    #: ``net_export``, battery charge for ``grid_charge``, and zero for
    #: ``serve_load`` -- which has no objective because it commands nothing.
    objective_kwh: float
    battery_ac_kwh: float
    #: Whether Stage B can be handed this segment at all.
    executable: bool


@dataclass(frozen=True, slots=True)
class EconomicCampaign:
    """One maximal contiguous stretch of a single DP run state.

    **The objective's own unit, and the first object in this codebase to
    represent it.** ``EconomicRun`` is a *label slice* of a campaign: one physical
    discharge carries both ``discharge`` and ``export`` as house load rises and
    falls beneath it, and ``runs_from`` starts a new run at every flip. Measured on
    a realistic today+tomorrow horizon: the DP flagged **three** run-state
    transitions and charged three switching fees, while ``runs_from`` published
    **ten** runs -- with ``charged_switching_fee`` false on every artefact split,
    because the DP never saw them.

    Grouping on ``EconomicInterval.run_state`` reproduces the transitions the fee
    was charged against, so ``len(campaigns) == sum(run_start)`` by construction.
    That equality is asserted in the tests, and it is the proof that this layer
    changed no decision.

    Runs are still published beside campaigns: the label slices remain the honest
    record of what each interval was doing, and nothing about the economics is
    lost by grouping them.
    """

    index: int
    direction: str
    start_index: int
    end_index: int
    interval_count: int
    battery_charge_ac_kwh: float
    battery_discharge_ac_kwh: float
    grid_import_kwh: float
    grid_export_kwh: float
    marginal_grid_import_kwh: float
    marginal_grid_export_kwh: float
    #: The campaign's advantage over leaving the battery alone through the same
    #: intervals. Negative means it saved money.
    marginal_cost_eur: float
    #: The fee the DP charged this campaign, which is exactly one
    #: ``minimum_trade_gain_eur`` -- or zero for a campaign the DP did not
    #: consider a run start.
    switching_fee_eur: float
    segments: tuple[EconomicSegment, ...]
    #: Whether this campaign is worth *announcing as a Sell*.
    #:
    #: **Scoped, and the scope is the whole point.** This is not a verdict on the
    #: campaign's economic validity -- the optimiser is the authority for that, and
    #: for a Buy it is the only possible authority. See :func:`campaigns_from`.
    sell_announcement_material: bool
    immaterial_reason: str | None = None

    @property
    def battery_ac_kwh(self) -> float:
        """Return the total AC energy the battery moved. One direction only."""
        return self.battery_charge_ac_kwh + self.battery_discharge_ac_kwh

    @property
    def objective_kwh(self) -> float:
        """Return the campaign's own objective, at the boundary it is paid at.

        A charge is judged at the battery; a discharge campaign is judged at the
        **meter**, summed over its executable export segments only. A discharge
        campaign whose segments are all ``serve_load`` therefore has an objective
        of zero, which is correct: it sells nothing, so it is not a sell.
        """
        if self.direction == ECONOMIC_DIRECTION_CHARGE:
            return self.battery_charge_ac_kwh
        return sum(
            segment.objective_kwh
            for segment in self.segments
            if segment.intent == EXECUTION_INTENT_NET_EXPORT
        )

    @property
    def self_consumption_ac_kwh(self) -> float:
        """Return the AC energy this campaign spent on the house, not the meter.

        **AC, and the name says so.** Both inputs are AC -- battery discharge at
        the battery boundary, export at the meter -- so their difference is AC and
        no efficiency belongs anywhere near it. An earlier draft called this
        ``_dc_kwh`` while computing exactly this subtraction, and the plan it came
        from computed ``discharge_ac - export/eta``, which subtracts a DC quantity
        from an AC one. Both were the boundary error this project forbids
        elsewhere; measured cleanly it is 8.750 - 2.648 = 6.102 kWh AC.

        Published because it is the largest quantity in a live discharge campaign
        and beta.31 made it invisible. Ambient inverter behaviour -- no target, no
        command, no Activity line.
        """
        if self.direction != ECONOMIC_DIRECTION_DISCHARGE:
            return 0.0
        return max(0.0, self.battery_discharge_ac_kwh - self.grid_export_kwh)

    def self_consumption_dc_kwh(self, discharge_efficiency: float) -> float:
        """Return the same energy at the pack, given the outbound efficiency.

        A **method, not a property**, because it needs a figure this dataclass does
        not carry and must not invent. Deriving it costs one division; guessing the
        efficiency would cost correctness.
        """
        if discharge_efficiency <= 0.0:
            return 0.0
        return self.self_consumption_ac_kwh / discharge_efficiency


def _segments_from(
    intervals: Sequence[EconomicInterval], direction: str
) -> tuple[EconomicSegment, ...]:
    """Split one campaign into contiguous same-intent stretches."""
    found: list[EconomicSegment] = []
    current: list[EconomicInterval] = []
    current_intent = ""

    def flush() -> None:
        if not current:
            return
        intent = current_intent
        battery = sum(
            entry.battery_charge_ac_kwh + entry.battery_discharge_ac_kwh
            for entry in current
        )
        if intent == EXECUTION_INTENT_NET_EXPORT:
            objective = sum(entry.grid_export_kwh for entry in current)
        elif intent == EXECUTION_INTENT_GRID_CHARGE:
            objective = sum(entry.battery_charge_ac_kwh for entry in current)
        else:
            # ``serve_load`` and ``hold`` command nothing, so they have no
            # objective to fall short of. Not zero-because-unknown: zero because
            # there is nothing to deliver.
            objective = 0.0
        found.append(
            EconomicSegment(
                intent=intent,
                start_index=current[0].index,
                end_index=current[-1].index,
                interval_count=len(current),
                objective_kwh=objective,
                battery_ac_kwh=battery,
                executable=intent in CONTROL_LIVE_DISPATCH_INTENTS,
            )
        )
        current.clear()

    for entry in intervals:
        intent = intent_for_action(entry.action)
        if current and intent != current_intent:
            flush()
        current_intent = intent
        current.append(entry)
    flush()
    return tuple(found)


def campaigns_from(
    intervals: Sequence[EconomicInterval],
    *,
    minimum_trade_gain_eur: float,
) -> tuple[EconomicCampaign, ...]:
    """Group solved intervals into the campaigns the objective itself formed.

    Maximal contiguous stretches of one non-idle ``run_state``. Idle intervals
    separate campaigns and belong to none, which is the same treatment
    ``runs_from`` gives ``hold`` -- except that absorption is already folded into
    the surrounding charge by ``_resolved_run_state``, so a solar quarter no longer
    splits anything.

    **``sell_announcement_material`` is a Sell-announcement rule, and nothing
    wider.** It answers "is this discretionary *sell* worth telling a person
    about", which is the question the observed micro-run spam actually raised. It
    is deliberately **asymmetric by direction**, because the two directions realise
    value in different places:

    * **A discharge campaign realises its value locally** -- avoided import,
      export revenue, or both, inside its own intervals. So a local test is
      meaningful::

          sell_announcement_material = (-marginal_cost_eur) > switching_fee_eur

      The campaign must save more, against leaving the battery alone through the
      same intervals, than the fee the DP charged it. ``marginal_cost_eur``
      excludes that fee (tracked separately as ``charged_switching_fee`` and
      ``plan.switching_cost_eur``), so gross advantage meets the fee exactly once.
      An earlier draft compared a *net-of-fee* value against the fee, demanding
      twice the gain.

    * **A charge campaign cannot be judged this way, and measurement proved it.**
      Buying always costs money locally -- that is what buying *is* -- so
      ``-marginal_cost_eur`` is negative for every Buy and the local test can never
      pass. Applied universally it marked a deliberate 16.944 kWh DP-selected
      charge campaign immaterial, whose value was realised in the discharge
      campaigns it enabled (-2.720 and -4.292 EUR). A Buy's value is
      **inter-temporal** and no campaign-local quantity can express it.

      So for a charge campaign the test is **executability, not economics**: it is
      material when it has a real charge objective the actuator can deliver. The DP
      already applied the whole-horizon objective, ``minimum_trade_gain_eur``,
      ``grid_charge_margin_eur_per_kwh``, future avoided-import value and the
      reachability constraints when it chose to buy. **No second economic Buy gate
      is invented here**, and an executable DP-selected Buy is never suppressed.

    It gates **announcements only**: nothing here withholds an execution target,
    because the DP's own trajectory assumes the energy moved.
    """
    campaigns: list[EconomicCampaign] = []
    current: list[EconomicInterval] = []

    def flush() -> None:
        if not current:
            return
        direction = current[0].run_state
        marginal = sum(entry.marginal_cost_eur for entry in current)
        fee = minimum_trade_gain_eur if any(e.run_start for e in current) else 0.0
        segments = _segments_from(current, direction)
        if direction == ECONOMIC_DIRECTION_CHARGE:
            # Executability, not economics. See the docstring.
            objective = sum(entry.battery_charge_ac_kwh for entry in current)
            material = objective >= MIN_EXECUTABLE_QUARTER_KWH
            reason = None if material else ECONOMIC_IMMATERIAL_NOT_EXECUTABLE
        else:
            advantage = -marginal
            material = advantage > fee
            reason = None if material else ECONOMIC_IMMATERIAL_BELOW_TRADE_GAIN
        campaigns.append(
            EconomicCampaign(
                index=len(campaigns),
                direction=direction,
                start_index=current[0].index,
                end_index=current[-1].index,
                interval_count=len(current),
                battery_charge_ac_kwh=sum(
                    entry.battery_charge_ac_kwh for entry in current
                ),
                battery_discharge_ac_kwh=sum(
                    entry.battery_discharge_ac_kwh for entry in current
                ),
                grid_import_kwh=sum(entry.grid_import_kwh for entry in current),
                grid_export_kwh=sum(entry.grid_export_kwh for entry in current),
                marginal_grid_import_kwh=sum(
                    entry.marginal_grid_import_kwh for entry in current
                ),
                marginal_grid_export_kwh=sum(
                    entry.marginal_grid_export_kwh for entry in current
                ),
                marginal_cost_eur=marginal,
                switching_fee_eur=fee,
                segments=segments,
                sell_announcement_material=material,
                immaterial_reason=reason,
            )
        )
        current.clear()

    for entry in intervals:
        if entry.run_state == ECONOMIC_DIRECTION_IDLE:
            flush()
            continue
        if current and entry.run_state != current[0].run_state:
            flush()
        current.append(entry)
    flush()
    return tuple(campaigns)


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
        # **A run with nothing in it is not a run.** Observed on a live plan: a
        # ``curtail_pv`` run carrying zero battery movement, zero import, zero
        # export and zero curtailment -- published, counted, and given an execution
        # plan id. Nothing downstream can act on it and no reader can learn
        # anything from it. Checked on all four flows rather than on the label, so a
        # genuine curtailment (which moves no battery energy at all) still forms.
        if not any(
            (
                entry.battery_charge_ac_kwh
                or entry.battery_discharge_ac_kwh
                or entry.grid_import_kwh
                or entry.grid_export_kwh
                or entry.pv_curtailed_kwh
            )
            for entry in current
        ):
            current.clear()
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
                peak_power_kw=max(
                    (
                        entry.battery_charge_ac_kwh + entry.battery_discharge_ac_kwh
                        for entry in current
                    ),
                    default=0.0,
                )
                / INTERVAL_HOURS,
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
    #: The economic terms this outcome was solved under, published so a reader
    #: never has to guess which thresholds produced the plan -- and so a replay
    #: can reproduce it exactly. ``grid_charge_margin`` in particular spent a
    #: release invisible, which is why no historical decision is reproducible.
    edge_value_eur_per_kwh: float = 0.0
    edge_creditable_kwh: float = float("inf")
    battery_throughput_cost_eur_per_kwh: float = 0.0
    grid_charge_margin_eur_per_kwh: float = 0.0
    #: The **autonomy** curve, verbatim, and consumed by no solve since beta.31.
    #: Kept because "could this pack ride out the forecast with no grid at all?"
    #: is a real question -- it simply is not the question a hard bound may ask.
    autonomy: tuple[float | None, ...] = ()
    #: The **reachability** projection the solver actually obeyed, and the bounded
    #: margin added to its floor. Published so a reader can see the constraint
    #: rather than infer it from the plan it produced.
    reachability: Any = None
    uncertainty: Any = None
    #: How many leading intervals could be priced and acted in. The boundary that
    #: makes reachability honest: grid replenishment is credited inside it and
    #: never beyond it.
    actionable_interval_count: int = 0
    #: The only compulsory purchase, ``max(0, reachability_now - stored)``. Carried
    #: on the outcome because it needs the pack's *state*, which the solver does
    #: not see -- and because every published purchase is attributed against it.
    bridge_kwh_now: float | None = None
    #: The pack's outbound conversion efficiency, carried so a purchase can be
    #: attributed to a future avoided import without this module having to name an
    #: efficiency of its own.
    discharge_efficiency: float = 1.0
    #: The same horizon solved with the export permission **off**. Load bearing:
    #: it is where the survival window's refill comes from, and its cost is what
    #: makes the permission auditable.
    ungated: EconomicPlan | None = None
    #: The triggered Safety extension, DC kWh. **Zero whenever no bridge exists**,
    #: which is what makes it incapable of initiating a purchase; see
    #: :func:`anti_churn_buffer_kwh`.
    anti_churn_buffer_kwh: float = 0.0
    #: The physical requirement at the head, and the head actually enforced. They
    #: differ by exactly ``anti_churn_buffer_kwh``, and every later interval is
    #: equal in both curves by construction.
    physical_reserve_head_kwh: float | None = None
    enforced_reserve_head_kwh: float | None = None
    #: The measured evidence this plan was made with, carried rather than re-read.
    #: A diagnostic that fetched it again could describe a different refresh from
    #: the decision printed beside it -- the fault the ``issued_at`` note records.
    forecast_risk: ForecastRisk | None = None
    #: Whether the idle counterfactual modelled ambient self-consumption. Decides
    #: which basis every marginal euro figure in this outcome was measured against.
    ambient_self_consumption_modelled: bool = False
    #: Per interval, the DC energy the pack should still hold to reach the refill
    #: the plan expects to use. **A permission input, never a reserve** -- it does
    #: not appear in any violation term and cannot force a purchase.
    export_floor_kwh: tuple[float, ...] = ()
    #: Per interval, what the protected energy is worth to the house: the
    #: demand-weighted mean import price across the survival window, in EUR per
    #: grid AC kWh. ``None`` where no demand is spoken for.
    protect_price_eur_per_kwh: tuple[float | None, ...] = ()
    #: Per interval, whether the export price already beats that.
    export_free: tuple[bool, ...] = ()
    #: The protective demand estimate, AC kWh. Reaches the permission and the
    #: Safety-Buy extension only -- never a priced quantity.
    upper_net_demand_ac_kwh: tuple[float, ...] = ()
    survival_window_end: int = 0
    survival_window_basis: str = SURVIVAL_WINDOW_ACTIONABLE_PREFIX
    adaptation_ratio_applied: float = 1.0
    adaptation_clipped: bool = False

    @property
    def export_gate_cost_eur(self) -> float | None:
        """Return what the export permission cost, or ``None`` if it was off.

        ``desired - ungated`` on identical inputs, measured on
        :attr:`EconomicPlan.objective_eur` -- the scalar the recursion minimises.
        Positive means the permission declined a sale the objective would otherwise
        have taken; if that figure is materially positive on ordinary shapes, the
        permission is wrong and this number is how anyone finds out.

        **It cannot be negative, because the gate only removes moves** -- and that
        is worth stating because two earlier formulas here made it negative. The
        first read ``cost_eur``, which is the metered cash flow alone: on the live
        17:45 horizon it fell 0.4879 -> 0.4659 while ``switching_cost_eur`` rose
        0.60 -> 0.80, publishing **-0.022** for a permission that had cost 0.178.
        The second added the fee back and was still wrong on a 96-interval shape at
        1.2 kWh/quarter -- there the gated plan imported 4.6 kWh more, and the
        grid-charge margin that buys is 0.05/kWh, which the metered figure also
        excludes. Only the whole objective is monotone; over a 64-shape sweep its
        worst case is -4e-16, which is float noise.
        """
        if self.ungated is None:
            return None
        if self.desired.violation_kwh != self.ungated.violation_kwh:
            # Lexicographic: with different violations the money figures are not
            # comparable, and a difference computed anyway would be a number with
            # no meaning. ``None`` says so.
            return None
        return self.desired.objective_eur - self.ungated.objective_eur

    #: The same problem solved under beta.30's economics, for comparison only.
    #: ``None`` unless Shadow asked for it. **Temporary**, and flagged as such in
    #: the payload: it doubles the solve to publish something no decision reads.
    legacy: EconomicPlan | None = None
    #: Per charge run, ``(safety_buy_kwh, economic_buy_kwh)``, keyed by start
    #: index. Diagnostics only: nothing in :func:`solve` reads it, and it is
    #: derived from a solve that already happened rather than a new one.
    safety_buy_attribution: dict[int, tuple[float, float]] = field(default_factory=dict)

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
    battery_throughput_cost_eur_per_kwh: float = 0.0,
    edge_value_eur_per_kwh: float = 0.0,
    edge_creditable_kwh: float = float("inf"),
    autonomy: tuple[float | None, ...] = (),
    reachability: Any = None,
    uncertainty: Any = None,
    actionable_interval_count: int = 0,
    compare_legacy: bool = False,
    #: Measured evidence that the inverter covers residual house load from the
    #: battery when nothing is dispatched. Defaults to false: unknown means not
    #: modelled, exactly as surplus absorption already treats an unreadable
    #: control surface.
    ambient_self_consumption: bool = False,
    #: The measured forecast-quality evidence the export permission may use.
    #: ``None`` disables the permission entirely, so a caller with no evidence
    #: plans exactly as beta.31 did.
    forecast_risk: ForecastRisk | None = None,
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

    # Shared by every solve, so the only thing that differs between them stays
    # the *reserve*. A relaxed plan priced on different economics would not be a
    # counterfactual, it would be a different question.
    # **The ambient gate, and why export permission is one of its two triggers.**
    #
    # An inverter forbidden to export cannot answer a residual house load any way
    # but from the battery or the grid, and Stage B commands neither -- so with
    # ``allow_battery_export`` off, ambient self-consumption is not a hypothesis,
    # it is the only physical possibility. Modelling it is what stops the lattice
    # (whose smallest discharge is a whole 0.25 kWh AC bucket) from freezing the
    # pack at low load; see the R5 note on ``_interval_outcomes``.
    #
    # ``ambient_self_consumption`` is the measured evidence for the export-enabled
    # case and defaults to false, so an installation whose inverter does *not*
    # self-consume, with export enabled, plans exactly as beta.31 did.
    ambient_modelled = ambient_self_consumption or not allow_battery_export
    economics = {
        "minimum_trade_gain_eur": minimum_trade_gain_eur,
        "grid_charge_margin_eur_per_kwh": grid_charge_margin_eur_per_kwh,
        "battery_throughput_cost_eur_per_kwh": battery_throughput_cost_eur_per_kwh,
        "edge_value_eur_per_kwh": edge_value_eur_per_kwh,
        "edge_creditable_kwh": edge_creditable_kwh,
        "floor_energy_kwh": floor_energy_kwh,
        "ambient_self_consumption": ambient_modelled,
    }

    started = time.perf_counter()
    # ------------------------------------------------------------------ pass 1
    #
    # **The ungated solve, and it is load bearing twice over.**
    #
    # The export permission needs to know which refill the plan expects to use,
    # and the only definition that cannot drift from the plan is the plan's own
    # choice. That is circular unless the circle is cut, so it is cut here: solve
    # once with the permission off, read the refill it selects, build the
    # protection from that, and solve once more. Two passes, fixed, terminating,
    # no iteration.
    #
    # Safe by construction: removing the permission can only *free* inventory, so
    # the ungated plan's charge campaign is no later and no smaller than the gated
    # one's. The distance it yields is a lower bound on the true distance, so the
    # protection is never overstated by the pass structure.
    #
    # And the same solve is the audit: ``export_gate_cost_eur`` is what the
    # permission cost, published beside ``reserve_protection_cost_eur`` for
    # exactly the reason that field exists. A protection nobody can price is a
    # protection nobody can challenge.
    # **And it is conditional.** With no measured evidence the permission is off, so
    # an ungated solve would be byte-identical to ``desired`` -- and a solve whose
    # difference from another is identically zero by construction is precisely the
    # fourth solve beta.18 deleted. It runs when there is a permission to establish,
    # and ``export_gate_cost_eur`` is ``None`` rather than a meaningless zero when
    # there is not.
    ungated: EconomicPlan | None = None

    export_floor_kwh: tuple[float, ...] = ()
    protect_price: tuple[float | None, ...] = ()
    export_free: tuple[bool, ...] = ()
    upper_net_demand: tuple[float, ...] = ()
    survival_window = 0
    survival_basis = SURVIVAL_WINDOW_ACTIONABLE_PREFIX
    adaptation_applied = 1.0
    adaptation_clipped = False
    if forecast_risk is not None and horizon.intervals:
        ungated = solve(
            table=table,
            horizon=horizon,
            start_energy_kwh=start_energy_kwh,
            terminal_floor_kwh=terminal_floor_kwh,
            permitted=desired_permitted,
            **economics,
        )
        upper_net_demand, adaptation_applied, adaptation_clipped = (
            upper_net_demand_curve(
                horizon.demands,
                forecast_risk,
                adaptation_ceiling=ADAPT_PROTECTION_CEILING,
            )
        )
        survival_window, survival_basis = survival_window_end(
            ungated,  # the plan's own refill, read from the pass without the gate
            actionable_intervals=(actionable_interval_count or horizon.intervals),
        )
        export_floor_kwh, protect_price = survival_curves(
            upper_net_demand,
            horizon.prices,
            window_end=survival_window,
            floor_energy_kwh=floor_energy_kwh,
            discharge_efficiency=table.limits.discharge_efficiency,
        )
        export_free = tuple(
            # No protection price means nothing is spoken for, so the export is
            # free. And the comparison is prices directly, on the same grid
            # boundary: the discharge efficiency cancels between holding a kWh for
            # the house and exporting it, because both pay it exactly once.
            True
            if protect is None
            else (horizon.prices[position].export_eur_kwh or 0.0) >= protect
            for position, protect in enumerate(protect_price)
        )

    # ---------------------------------------------- the anti-churn head bump
    #
    # **The one place a measured forecast may enlarge a purchase, and it is
    # deliberately not a reserve.** Four properties, each structural rather than
    # asserted, and together they are why this cannot become a second autonomy
    # curve:
    #
    # 1. **It is gated on a condition its own action destroys.** The bump exists
    #    only while a bridge exists. The purchase it causes lifts stored energy
    #    above the physical head, so at the next refresh the bridge is zero, the
    #    bump is zero, and the enforced curve is the physical one again. It cannot
    #    survive two consecutive refreshes after being satisfied.
    # 2. **It touches interval 0 and nothing else.** Every later interval carries
    #    the unmodified physical requirement, so nothing downstream is protected
    #    and the pack may spend the buffer on the house immediately.
    # 3. **It never raises the physical curve itself.** ``reachability`` and
    #    ``bridge_kwh_now`` are computed before this and are untouched, so the
    #    compulsory/discretionary split stays measured against pure physics.
    # 4. **It cannot initiate a purchase.** No bridge, no bump -- so the buffer can
    #    only enlarge a Safety Buy the physics already compelled. That is its own
    #    third attribution category, not ordinary discretionary energy.
    gated_horizon = horizon
    buffer_kwh = 0.0
    physical_head_kwh = horizon.planning_reserve_kwh[0] if horizon.intervals else None
    if (
        forecast_risk is not None
        and horizon.intervals
        and max(0.0, horizon.planning_reserve_kwh[0] - start_energy_kwh) > 0.0
    ):
        buffer_kwh = anti_churn_buffer_kwh(
            horizon.demands,
            forecast_risk,
            window_end=min(survival_window, horizon.intervals),
            bucket_kwh=table.bucket_kwh,
            discharge_efficiency=table.limits.discharge_efficiency,
        )
        raw_head = horizon.planning_reserve_kwh[0] + buffer_kwh
        head = table.energy(min(table.bucket_at_or_above(raw_head), table.buckets))
        buffer_kwh = max(0.0, head - horizon.planning_reserve_kwh[0])
        if buffer_kwh > 0.0:
            gated_horizon = EconomicHorizon(
                demands=horizon.demands,
                prices=horizon.prices,
                planning_reserve_kwh=(head, *horizon.planning_reserve_kwh[1:]),
                limited_by=horizon.limited_by,
            )
            # **The audit baseline moves with it**, so ``export_gate_cost_eur``
            # prices the permission and not the buffer. One extra solve, and only
            # on a refresh where a Safety Buy is already compelled -- the rare
            # case, and the one where an extra 100 ms buys a real answer.
            ungated = solve(
                table=table,
                horizon=gated_horizon,
                start_energy_kwh=start_energy_kwh,
                terminal_floor_kwh=terminal_floor_kwh,
                permitted=desired_permitted,
                **economics,
            )

    # ------------------------------------------------------------------ pass 2
    desired = solve(
        table=table,
        horizon=gated_horizon,
        start_energy_kwh=start_energy_kwh,
        terminal_floor_kwh=terminal_floor_kwh,
        permitted=desired_permitted,
        export_floor_kwh=export_floor_kwh or None,
        export_free=export_free or None,
        **economics,
    )
    capability = solve(
        table=table,
        horizon=gated_horizon,
        start_energy_kwh=start_energy_kwh,
        terminal_floor_kwh=terminal_floor_kwh,
        permitted=capability_permitted,
        **economics,
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
        permitted=desired_permitted,
        **economics,
    )

    # **A fourth solve, and only in Shadow.** ``legacy`` is the same problem under
    # beta.30's economics: the whole-horizon autonomy curve as a hard floor, and no
    # value on terminal inventory. It exists so the change can be *watched* against
    # live inputs before it is trusted with money -- which is the only honest way to
    # gate a change of this size, since the historical days needed to replay it
    # offline were never recorded.
    #
    # Off by default and off in Live, because it doubles the solve for a payload
    # nobody reads while the plan is executing. **Temporary**: it should be removed
    # once the replay shows the new architecture dominating on recorded days.
    legacy = None
    if compare_legacy and autonomy:
        legacy_horizon = EconomicHorizon(
            demands=horizon.demands,
            prices=horizon.prices,
            planning_reserve_kwh=tuple(
                table.energy(min(table.bucket_at_or_above(value), table.buckets))
                if value is not None
                else floor_energy_kwh
                for value in autonomy[: len(horizon.demands)]
            ),
            limited_by=horizon.limited_by,
        )
        if len(legacy_horizon.planning_reserve_kwh) == len(horizon.demands):
            legacy = solve(
                table=table,
                horizon=legacy_horizon,
                start_energy_kwh=start_energy_kwh,
                terminal_floor_kwh=terminal_floor_kwh,
                permitted=desired_permitted,
                minimum_trade_gain_eur=minimum_trade_gain_eur,
                grid_charge_margin_eur_per_kwh=grid_charge_margin_eur_per_kwh,
                battery_throughput_cost_eur_per_kwh=(
                    battery_throughput_cost_eur_per_kwh
                ),
                # No terminal value: that is precisely what beta.30 did not have.
                edge_value_eur_per_kwh=0.0,
            )

    # There is no *fifth* solve, and no comparison to publish beyond it.
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
        edge_value_eur_per_kwh=edge_value_eur_per_kwh,
        edge_creditable_kwh=edge_creditable_kwh,
        battery_throughput_cost_eur_per_kwh=battery_throughput_cost_eur_per_kwh,
        grid_charge_margin_eur_per_kwh=grid_charge_margin_eur_per_kwh,
        autonomy=autonomy,
        reachability=reachability,
        uncertainty=uncertainty,
        actionable_interval_count=actionable_interval_count,
        bridge_kwh_now=(
            None if reachability is None else reachability.bridge_kwh(start_energy_kwh)
        ),
        discharge_efficiency=table.limits.discharge_efficiency,
        ungated=ungated,
        anti_churn_buffer_kwh=buffer_kwh,
        forecast_risk=forecast_risk,
        ambient_self_consumption_modelled=ambient_modelled,
        physical_reserve_head_kwh=physical_head_kwh,
        enforced_reserve_head_kwh=(
            gated_horizon.planning_reserve_kwh[0] if gated_horizon.intervals else None
        ),
        export_floor_kwh=export_floor_kwh,
        protect_price_eur_per_kwh=protect_price,
        export_free=export_free,
        upper_net_demand_ac_kwh=upper_net_demand,
        survival_window_end=survival_window,
        survival_window_basis=survival_basis,
        adaptation_ratio_applied=adaptation_applied,
        adaptation_clipped=adaptation_clipped,
        safety_buy_runs=_safety_buy_runs(desired, relaxed, table.bucket_kwh),
        safety_buy_attribution=_safety_buy_attribution(desired, relaxed),
        legacy=legacy,
    )


def _safety_buy_attribution(
    desired: EconomicPlan, relaxed: EconomicPlan
) -> dict[int, tuple[float, float]]:
    """Return, per charge run, how much of it the reserve is responsible for.

    Keyed by start index, valued ``(safety_buy_kwh, economic_buy_kwh)``.

    Attributed by comparison rather than by inspection of prices: a charge is
    reserve-driven to the extent that the same horizon, solved with the reserve
    relaxed to the configured floor, charges *less* over the same intervals. That
    is a statement about **why** the charge is there, which a price threshold
    could never make -- a cheap interval and a reserve deadline often coincide.

    **The relaxed solve already happens.** Until beta.25 the difference was
    computed here, compared against one bucket, and then thrown away, so the split
    a user actually wants needed no new solve and no change to the objective --
    only that the figure be returned instead of a boolean.

    **Documented limitation, not papered over.** The relaxed solve is free to move
    economically desirable charging to *different* intervals, so these are
    "reserve-attributable and economic energy **within this run window**", not a
    globally exact decomposition. The diagnostics say so in the field own rule
    string, because a figure that looks exact and is not is worse than one that
    admits its boundary.
    """
    if not relaxed.available:
        return {}
    relaxed_by_index = {
        entry.index: entry.battery_charge_ac_kwh for entry in relaxed.intervals
    }
    found: dict[int, tuple[float, float]] = {}
    for run in desired.runs:
        if run.action != ECONOMIC_ACTION_CHARGE:
            continue
        relaxed_charge = sum(
            relaxed_by_index.get(index, 0.0)
            for index in range(run.start_index, run.end_index + 1)
        )
        # Both clamped into the run own charge, so they always sum to it and
        # neither can go negative when the relaxed solve buys *more* here.
        economic = max(0.0, min(relaxed_charge, run.battery_charge_ac_kwh))
        safety = max(0.0, run.battery_charge_ac_kwh - economic)
        found[run.start_index] = (safety, economic)
    return found


def _safety_buy_runs(
    desired: EconomicPlan, relaxed: EconomicPlan, bucket_kwh: float
) -> tuple[int, ...]:
    """Return the start indices of charge runs that exist because of the reserve.

    Derived from :func:`_safety_buy_attribution` so the label and the published
    figures can never disagree: a run is a safety buy exactly when the energy
    attributed to the reserve exceeds one state-space bucket, which is the same
    threshold beta.16 used and is unchanged.
    """
    attribution = _safety_buy_attribution(desired, relaxed)
    return tuple(
        index
        for index, (safety, _economic) in sorted(attribution.items())
        if safety > bucket_kwh
    )


def quarter_schedule_for(
    intervals: tuple[EconomicInterval, ...],
    *,
    start_index: int,
    end_index: int,
    intent: str,
    moment: Any,
) -> list[dict[str, Any]]:
    """Return the per-quarter execution rows for one run. **No new solve.**

    Every figure is read off a row the optimizer already produced. This exists
    because the run-level publication is too coarse to execute against: a run's
    ``desired_grid_kw`` is its *first* interval's rate, and a multi-quarter run
    executed against that rate follows the wrong target from its second quarter on.

    Three grid quantities, and they are not interchangeable:

    * ``grid_authorised_kwh`` -- :attr:`marginal_grid_import_kwh`, the import the
      charge *causes*. A **ceiling** on how much of the battery target may be
      bought, never an amount to consume.
    * ``grid_export_target_kwh`` -- :attr:`grid_export_kwh`, the **actual** meter
      export. The **objective** for an export, and the same quantity the run-level
      ``grid_target_kwh`` is summed from, so the quarter rows and the run agree.
    * ``grid_export_caused_kwh`` -- :attr:`marginal_grid_export_kwh`, attribution
      and diagnostics only. Using it as the objective would under-export by exactly
      the production the site was exporting anyway.
    """
    rows: list[dict[str, Any]] = []
    for entry in intervals:
        if entry.index < start_index or entry.index > end_index:
            continue
        start = moment(entry.index)
        end = moment(entry.index + 1)
        if start is None or end is None:
            continue
        # **Read the interval, not the intent.** Through beta.31 this asked the
        # *intent* which battery figure to use, so a ``serve_load`` row resolved to
        # ``battery_charge_ac_kwh`` -- which is 0.0 on a discharging interval. Every
        # quarter of every load-serving run was therefore stamped ``no_objective``,
        # a false diagnostic on exactly the rows a reader most wants to understand.
        #
        # An interval moves the battery in one direction only, so the sum is a read
        # rather than a mix: whichever term is non-zero is the movement.
        battery_kwh = entry.battery_charge_ac_kwh + entry.battery_discharge_ac_kwh
        # **Whether this row is physically deliverable, decided here.**
        #
        # The objective is the battery figure for a charge and the actual meter
        # export for an export -- the contract's asymmetry. Below the actuator's
        # resolution there is no command that realises it: the Dispatch power helper
        # quantises to 0.1 kW, so the smallest non-zero energy a quarter can deliver
        # is 0.025 kWh. beta.29 published export rows of 0.01 and 0.02 kWh, which the
        # actuator can only answer with 0.025 -- a 150 % overshoot -- or with nothing.
        #
        # Decided in Stage A because "is this run worth forming" is an economic
        # question, and because the row must stay **visible** either way: the
        # economics are still true, and a reader has to be able to see what was
        # planned and why it was not armed. Stage B carries its own backstop.
        objective_kwh = (
            entry.grid_export_kwh
            if intent == EXECUTION_INTENT_NET_EXPORT
            else battery_kwh
        )
        not_executable: str | None = None
        if intent not in EXECUTION_INTENT_ACTIONS:
            # **No actuator exists for this intent**, so no magnitude makes the row
            # armable. ``serve_load`` runs are published like every other run --
            # they carry the campaign identity across the gap between two exports --
            # and through beta.32 their rows reported ``not_executable: null``,
            # which this contract reads as "Stage B may arm this". It never could.
            not_executable = QUARTER_NOT_EXECUTABLE_INTENT
        elif objective_kwh <= 0.0:
            not_executable = QUARTER_NOT_EXECUTABLE_NO_OBJECTIVE
        elif objective_kwh < MIN_EXECUTABLE_QUARTER_KWH:
            not_executable = QUARTER_NOT_EXECUTABLE_SUB_RESOLUTION
        rows.append(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "not_executable": not_executable,
                "battery_kwh": _round_kwh(battery_kwh),
                "grid_authorised_kwh": _round_kwh(
                    max(0.0, entry.marginal_grid_import_kwh)
                ),
                "grid_export_target_kwh": _round_kwh(entry.grid_export_kwh),
                "grid_export_caused_kwh": _round_kwh(
                    max(0.0, entry.marginal_grid_export_kwh)
                ),
                "desired_grid_kw": _round_kw(
                    (entry.grid_import_kwh - entry.grid_export_kwh) / INTERVAL_HOURS
                ),
            }
        )
    return rows


def desired_grid_kw_at(
    intervals: tuple[EconomicInterval, ...], index: int
) -> float | None:
    """Return the signed grid target for one interval, or ``None`` if absent.

    ``> 0`` is intended net **import**, ``< 0`` intended net **export**, matching
    the sign convention in :mod:`.dispatch`.

    Read off the solved plan own per-interval grid energies -- which already exist
    and are already what the cost was computed from -- and converted to a rate.
    Never parsed out of a reason string, and never inferred from the action: an
    action says which direction the *battery* moves, and this is a **meter**
    quantity.

    A quarter average of a forecast, and it has to be: Stage A solves in quarter
    energies. Stage B holds it fixed for the quarter and follows it with live
    measurements, which is exactly the point -- the average is the *money* the plan
    authorised, and the instantaneous setpoint is how that money is spent as
    production and load move.
    """
    for entry in intervals:
        if entry.index == index:
            return (entry.grid_import_kwh - entry.grid_export_kwh) / INTERVAL_HOURS
    return None


# -- reporting ---------------------------------------------------------------


ECONOMIC_BASIS: str = (
    "the least-cost way through the prices, load and production that are "
    "actually known, subject to the Phase-7 reserve. beta.25 executes the "
    "buying half of this plan and refuses the rest: export and photovoltaic "
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

#: **Scoped to this module, since beta.33.** It read "no service call reaches the
#: inverter", which was true of the whole integration when it was written and false
#: from beta.24 on -- and it is published in diagnostics, so a user auditing safety
#: read it as a live guarantee. What is still true, and is what the field is for,
#: is that Stage A itself actuates nothing: the plan beside it describes what an
#: actuator could achieve, never what was done.
ECONOMIC_DECIDES_NOTHING: str = (
    "Phase 8 calculates a plan. It never executes one: this module calls no "
    "service and names no helper, and the capability plan beside the desired one "
    "describes what implemented actuators could achieve rather than what was "
    "done. What is actually sent, and what is standing in the way, is reported "
    "by the control section and by execution_blocked_reason"
)


def classify_purchase(
    run: EconomicRun,
    *,
    bridge_kwh_now: float | None,
    uncertainty_dc_kwh: float | None,
    edge_value_eur_per_kwh: float,
    survives_to_edge_kwh: float,
    attribution: tuple[float, float] | None = None,
    future_spread_eur_kwh: float | None = None,
    future_spread_price_eur_kwh: float | None = None,
    anti_churn_buffer_kwh: float = 0.0,
) -> dict[str, Any]:
    """Return why this charge run exists, and how much of it was unavoidable.

    **Derived, never asserted.** The rule is the split between energy the physics
    compelled and energy an economic gate let through:

    * the compulsory share is what the **reserve-relaxed counterfactual declines
      to buy** over the same intervals -- the same solve, the same prices, with the
      reserve relaxed to the hard floor. That is a statement about *why* the
      energy is there, which no price threshold could make.
    * whatever the run buys beyond it is discretionary, and had to clear the trade
      gain, the grid-charge margin and the throughput cost to be here.

    **``bridge_kwh_now`` is supporting evidence, not the measure**, and an earlier
    draft of this function used it as the measure and was wrong. The requirement is
    a curve that usually peaks *ahead* of the head, so a head deficit of zero is
    entirely compatible with compulsory purchasing: on a winter shape the head
    asked 9.59 kWh while the curve peaked at 10.855 four quarters later, and a pack
    at 10.80 had a zero head bridge and 0.56 kWh it could not decline. Reporting
    that as discretionary would have been the mirror of the fault this release
    exists to fix -- and it is the reason the counterfactual, not the head figure,
    decides.

    The discretionary half is then attributed by **where its value comes from**,
    and the test is a known future spread rather than a run that pays for itself:

    * ``economic_arbitrage`` -- a *concrete* future interval, inside the priced
      horizon, whose import price beats this purchase by more than the outbound
      conversion costs. Buying at 0.10 to displace a 0.38 import tonight is
      arbitrage, and an earlier draft of this function said it was not: it asked
      whether the *charging window itself* showed a net saving, which for a charge
      run measured against its own idle counterfactual is essentially never true.
      That made the label unreachable and pushed every discretionary purchase into
      the strategic bucket.
    * ``strategic_future_self_use`` -- the energy is worth holding on general
      grounds, but no specific window in the horizon can be pointed at. Its payoff
      is the terminal value, which is a replacement cost rather than a spread.

    The distinction is worth keeping precisely because one of them is auditable
    against a price a reader can look up and the other is not.

    **beta.32 splits the compulsory share in two, because "can it force a
    purchase?" was one question standing in for two.** Physical reachability can
    *initiate* a Safety Buy; the anti-churn extension cannot, but while it sits in
    the enforced head the solver must buy it, and it is released for household use
    the moment the buy lands. Folding it into either neighbour would misdescribe
    it -- as physics it is not, and as ordinary discretionary energy it was not
    optional. So it is published as its own third category.

    The physical requirement gets first claim on the compelled energy, deliberately:
    attributing genuine physical need to the buffer would make the physical
    requirement look smaller than it is, which is the wrong direction to be wrong
    in. The buffer only claims compelled energy the head deficit does not explain,
    and never more than the buffer itself.

    A figure that cannot be derived honestly is published as ``None``. That is the
    whole difference from ``safety_buy``, which always had an answer because the
    answer was a label.
    """
    energy = max(0.0, run.energy_kwh)
    if run.direction != ECONOMIC_DIRECTION_CHARGE:
        return {
            "classification": BUY_REASON_UNKNOWN,
            "bridge_kwh_now": None,
            "economic_extra_kwh": None,
        }

    if bridge_kwh_now is None:
        return {
            "classification": BUY_REASON_UNKNOWN,
            "bridge_kwh_now": None,
            "economic_extra_kwh": None,
            "why_now": "reachability was not computed for this refresh",
        }

    if attribution is not None:
        compelled = min(energy, max(0.0, attribution[0]))
    else:
        compelled = min(energy, max(0.0, bridge_kwh_now))
    discretionary = max(0.0, energy - compelled)
    # Physics first, then the extension, then nothing: the three shares sum to the
    # compelled energy exactly, and the buffer can never exceed itself.
    beyond_head = max(0.0, compelled - min(compelled, max(0.0, bridge_kwh_now)))
    anti_churn = min(beyond_head, max(0.0, anti_churn_buffer_kwh))
    safety_bridge = max(0.0, compelled - anti_churn)
    pays_for_itself = run.marginal_cost_eur < 0.0
    # A concrete spread beats a general claim: it names an interval and a price.
    has_spread = future_spread_eur_kwh is not None and future_spread_eur_kwh > 0.0

    if compelled > 0.0 and discretionary > 0.0:
        label = BUY_REASON_MIXED
    elif compelled > 0.0:
        # The margin is part of the floor the bridge was measured against, so a
        # bridge that exists only because of it is named for it.
        label = (
            BUY_REASON_UNCERTAINTY
            if uncertainty_dc_kwh is not None and compelled <= uncertainty_dc_kwh
            else BUY_REASON_REACHABILITY
        )
    elif has_spread:
        label = BUY_REASON_ARBITRAGE
    elif edge_value_eur_per_kwh > 0.0 and survives_to_edge_kwh > 0.0:
        label = BUY_REASON_FUTURE_SELF_USE
    else:
        label = BUY_REASON_UNKNOWN

    return {
        "classification": label,
        "bridge_kwh_now": _round_kwh(bridge_kwh_now),
        "compulsory_basis": (
            "reserve_relaxed_counterfactual"
            if attribution is not None
            else "head_bridge"
        ),
        "compulsory_kwh": _round_kwh(compelled),
        # The three-way split. ``safety_bridge_kwh + safety_anti_churn_buffer_kwh``
        # is ``compulsory_kwh`` exactly, and ``economic_buy_kwh`` is the same figure
        # as ``economic_extra_kwh`` under its attribution name -- both published,
        # because the older name is what existing readers look for.
        "safety_bridge_kwh": _round_kwh(safety_bridge),
        "safety_anti_churn_buffer_kwh": _round_kwh(anti_churn),
        "economic_buy_kwh": _round_kwh(discretionary),
        "can_initiate_grid_purchase": {
            "safety_bridge_kwh": True,
            # The whole point of the category: no bridge, no bump.
            "safety_anti_churn_buffer_kwh": False,
            "economic_buy_kwh": False,
        },
        "can_increase_triggered_grid_purchase": {
            "safety_bridge_kwh": True,
            "safety_anti_churn_buffer_kwh": True,
            "economic_buy_kwh": False,
        },
        "anti_churn_released_after_buy": anti_churn > 0.0,
        "economic_extra_kwh": _round_kwh(discretionary),
        "why_now": (
            "the pack cannot stay above its floor without this energy"
            if compelled > 0.0
            else "no energy was compulsory; this run cleared the economic gates"
        ),
        "why_not_earlier": (
            "the requirement binds ahead of this interval, not at it"
            if compelled > 0.0 and not (bridge_kwh_now or 0.0) > 0.0
            else None
        ),
        "why_this_much": (
            f"{_round_kwh(compelled)} kWh compelled by reachability, "
            f"{_round_kwh(discretionary)} kWh discretionary"
        ),
        "why_not_wait": (
            "waiting would cross the floor"
            if compelled >= energy and compelled > 0.0
            else (
                "a later interval in this horizon imports at "
                f"{_round_eur(future_spread_price_eur_kwh)} EUR/kWh, which this "
                "purchase displaces"
                if has_spread
                else "the energy is worth more held than the cost of buying it"
            )
        ),
        "marginal_cost_eur": _round_eur(run.marginal_cost_eur),
        "pays_for_itself_in_horizon": pays_for_itself,
        # The spread the label rests on, and the price it was measured against, so
        # a reader can check the attribution against a figure they can look up.
        "future_spread_eur_kwh": (
            None if future_spread_eur_kwh is None else _round_eur(future_spread_eur_kwh)
        ),
        "future_spread_price_eur_kwh": (
            None
            if future_spread_price_eur_kwh is None
            else _round_eur(future_spread_price_eur_kwh)
        ),
        "edge_value_eur_kwh": _round_eur(edge_value_eur_per_kwh),
        "uncertainty_dc_kwh": (
            None if uncertainty_dc_kwh is None else _round_kwh(uncertainty_dc_kwh)
        ),
        "rule": (
            "the compulsory share is what the reserve-relaxed solve declines to "
            "buy over the same intervals; everything above it is discretionary and "
            "had to clear the trade gain, the grid-charge margin and the "
            "throughput cost. bridge_kwh_now is the deficit at *this* interval and "
            "is supporting evidence only -- the requirement is a curve and usually "
            "binds ahead of the head, so a zero there does not mean nothing was "
            "compulsory. a figure that cannot be derived honestly is null"
        ),
    }


def _run_as_dict(
    run: EconomicRun,
    *,
    safety_buy: bool,
    intervals: list[dict[str, Any]] | None = None,
    omitted: int = 0,
    purchase: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return one planned run, bounded and flat.

    Every boundary is stated separately because a euro figure is only meaningful
    against the boundary it was measured at, and this is where a reader audits
    that the energy-volume scheduling did what it claims.
    """
    return {
        "action": ECONOMIC_ACTION_SAFETY_BUY if safety_buy else run.action,
        # **Why this purchase exists, derived rather than labelled.** ``None`` for a
        # run that is not a charge, and for a refresh where reachability was not
        # computed -- which is the honest answer rather than a reassuring one.
        "purchase": purchase,
        "start_interval": run.start_index,
        "end_interval": run.end_index,
        "interval_count": run.interval_count,
        "energy_kwh": _round_kwh(run.energy_kwh),
        "first_power_kw": _round_kw(run.first_power_kw),
        "peak_power_kw": _round_kw(run.peak_power_kw),
        # A mean over the whole run. Published beside the peak rather than alone,
        # because on a campaign that varies it describes no quarter of it.
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
        # **The per-quarter allocation, so a window can be audited rather than
        # inferred.** ``average_power_kw`` above is a mean over the whole run and
        # a run legitimately varies quarter to quarter -- on a real plan it read
        # 3.50 kW for a campaign that bought at 10 kW in two quarters and absorbed
        # free production in eleven more. Nothing here changes what was allocated.
        "intervals": intervals,
        "intervals_omitted": omitted,
        "intervals_rule": (
            "one row per quarter of this run, read off the solved plan. a broad "
            "window is not the same thing as energy spread across it: check "
            "battery_power_kw per quarter, and absorbing, which marks a quarter "
            "that stored production and bought nothing. direction is the action "
            "and power is an unsigned magnitude"
        ),
    }


def _interval_as_dict(
    entry: EconomicInterval, reserve_kwh: float | None
) -> dict[str, Any]:
    """Return one quarter of a run's allocation, for auditing it.

    **Every figure here already existed.** This is a reading of the solved plan,
    not a computation over it: no economics is introduced, nothing is
    apportioned, and no allocation is changed by publishing it. ``marginal_*`` are
    the existing per-interval properties -- exact differences against that
    interval's own idle counterfactual.

    Power is an unsigned magnitude and ``action`` carries the direction, the same
    convention the control surface uses. A signed power would be a second way to
    express direction and the two would eventually disagree.
    """
    charge = entry.battery_charge_ac_kwh
    discharge = entry.battery_discharge_ac_kwh
    return {
        "interval": entry.index,
        "action": entry.action,
        "import_price_eur_kwh": entry.import_price_eur_kwh,
        "export_price_eur_kwh": entry.export_price_eur_kwh,
        # Direction is the action; this is a magnitude.
        "battery_power_kw": _round_kw(max(charge, discharge) / INTERVAL_HOURS),
        "battery_charge_ac_kwh": _round_kwh(charge),
        "battery_discharge_ac_kwh": _round_kwh(discharge),
        "start_energy_dc_kwh": _round_kwh(entry.start_energy_dc_kwh),
        # Site flows, then the part this interval actually caused.
        "grid_import_kwh": _round_kwh(entry.grid_import_kwh),
        "grid_export_kwh": _round_kwh(entry.grid_export_kwh),
        "marginal_grid_import_kwh": _round_kwh(entry.marginal_grid_import_kwh),
        "marginal_grid_export_kwh": _round_kwh(entry.marginal_grid_export_kwh),
        # What this quarter cost against leaving the battery alone through it.
        "marginal_cost_eur": _round_eur(entry.cost_eur - entry.idle_cost_eur),
        # **The field that resolves the ambiguity.** True means the pack stored
        # production the house could not use: real battery movement that bought
        # nothing, so it widens a reported charge window without buying.
        "absorbing": entry.absorbing,
        "reserve_requirement_kwh": (
            None if reserve_kwh is None else _round_kwh(reserve_kwh)
        ),
        "run_start": entry.run_start,
    }


def _run_intervals(
    plan: EconomicPlan,
    run: EconomicRun,
    reserve: Mapping[int, float],
    budget: int,
) -> tuple[list[dict[str, Any]], int]:
    """Return this run's per-quarter rows within ``budget``, and how many were cut.

    Runs are contiguous by construction, so the slice is the run. Truncation
    takes from the tail and is reported, because a silently short list would read
    as a short campaign -- the exact misreading this breakdown exists to prevent.
    """
    if budget <= 0:
        return [], run.interval_count
    entries = [
        entry
        for entry in plan.intervals
        if run.start_index <= entry.index <= run.end_index
    ]
    omitted = max(0, len(entries) - budget)
    rows = [
        _interval_as_dict(entry, reserve.get(entry.index)) for entry in entries[:budget]
    ]
    return rows, omitted


def future_spread_for(
    run: EconomicRun,
    plan: EconomicPlan,
    *,
    discharge_efficiency: float,
) -> tuple[float | None, float | None]:
    """Return the best known spread this purchase can be attributed to.

    ``(spread_eur_kwh, the import price it was measured against)``, or
    ``(None, None)`` when nothing in the horizon after this run can be pointed at.

    The comparison is the honest one for stored energy: a kWh bought now and
    discharged later returns ``price * eta_discharge`` of avoided import, because
    only the outbound conversion is still to come -- the inbound loss is already
    paid for in what was bought. Measured strictly *after* the run ends, since a
    purchase cannot displace an import that has already happened.
    """
    buy_price = run.average_price_eur_kwh
    if buy_price is None or discharge_efficiency <= 0.0:
        return (None, None)
    later = [
        entry.import_price_eur_kwh
        for entry in plan.intervals
        if entry.index > run.end_index and entry.import_price_eur_kwh is not None
    ]
    if not later:
        return (None, None)
    best = max(later)
    return (best * discharge_efficiency - buy_price, best)


def _campaigns_as_dicts(
    plan: EconomicPlan, discharge_efficiency: float
) -> list[dict[str, Any]]:
    """Return the four-layer figures for each campaign the objective formed.

    **Four layers, named separately so they cannot be conflated.** The live example
    is the argument: one discharge campaign over intervals 0-12 moved 8.750 kWh of
    battery and sold 2.648 kWh at the meter. The difference -- 6.102 kWh AC -- went
    to the house, and beta.31 made the largest quantity in the campaign invisible.

    * **campaign**: the physical direction. The identity, materiality and Activity
      unit.
    * **segment**: a contiguous stretch of one *intent* inside it. ``net_export``
      and ``grid_charge`` become execution targets; ``serve_load`` emits nothing.
    * **quarter row**: the frozen objective, published per run.
    * **economic export objective**: the meter energy the campaign genuinely
      intends to sell, which is what the Activity announcement quotes.
    """
    payload: list[dict[str, Any]] = []
    for campaign in plan.campaigns:
        payload.append(
            {
                "index": campaign.index,
                "direction": campaign.direction,
                "start_index": campaign.start_index,
                "end_index": campaign.end_index,
                "interval_count": campaign.interval_count,
                # Layer one: the battery, at the battery boundary.
                "battery_ac_kwh": _round_kwh(campaign.battery_ac_kwh),
                "battery_charge_ac_kwh": _round_kwh(campaign.battery_charge_ac_kwh),
                "battery_discharge_ac_kwh": _round_kwh(
                    campaign.battery_discharge_ac_kwh
                ),
                # Layer two: the meter, at the meter boundary.
                "grid_import_kwh": _round_kwh(campaign.grid_import_kwh),
                "grid_export_kwh": _round_kwh(campaign.grid_export_kwh),
                "marginal_grid_import_kwh": _round_kwh(
                    campaign.marginal_grid_import_kwh
                ),
                "marginal_grid_export_kwh": _round_kwh(
                    campaign.marginal_grid_export_kwh
                ),
                # Layer three: what went to the house, in both boundaries and each
                # labelled -- an earlier draft named this ``_dc`` while computing
                # the AC subtraction, which is the boundary error this project
                # forbids everywhere else.
                "self_consumption_ac_kwh": _round_kwh(campaign.self_consumption_ac_kwh),
                "self_consumption_dc_kwh": _round_kwh(
                    campaign.self_consumption_dc_kwh(discharge_efficiency)
                ),
                # Layer four: the objective, at the boundary it is paid at.
                "objective_kwh": _round_kwh(campaign.objective_kwh),
                "objective_boundary": (
                    CAMPAIGN_BOUNDARY_BATTERY
                    if campaign.direction == ECONOMIC_DIRECTION_CHARGE
                    else CAMPAIGN_BOUNDARY_METER
                ),
                "marginal_cost_eur": _round_eur(campaign.marginal_cost_eur),
                "switching_fee_eur": _round_eur(campaign.switching_fee_eur),
                "material": campaign.sell_announcement_material,
                "immaterial_reason": campaign.immaterial_reason,
                "segments": [
                    {
                        "intent": segment.intent,
                        "start_index": segment.start_index,
                        "end_index": segment.end_index,
                        "objective_kwh": _round_kwh(segment.objective_kwh),
                        "executable": segment.intent
                        in (EXECUTION_INTENT_NET_EXPORT, EXECUTION_INTENT_GRID_CHARGE),
                    }
                    for segment in campaign.segments
                ],
                "rule": (
                    "campaign figures do not sum to the plan's: each is measured "
                    "against its own idle counterfactual over its own intervals. "
                    "materiality is a verdict on whether this is worth announcing "
                    "as a Sell, never on whether the optimiser was right"
                ),
            }
        )
    return payload


def _runs_as_dicts(
    outcome: EconomicOutcome,
    desired: EconomicPlan,
    runs: Sequence[EconomicRun],
) -> list[dict[str, Any]]:
    """Return the published runs, each with its per-quarter allocation.

    The interval budget is shared in run order, so the runs a reader sees first
    are the ones that are complete.
    """
    # **Keyed by the absolute interval index, because that is what a run
    # carries.** ``planning_reserve_kwh`` is a list positioned by *horizon
    # offset*, and beta.21 indexed it with the interval's own index -- so on a
    # horizon starting at interval 44 every published requirement was the one
    # belonging forty-four intervals later, and everything past the horizon
    # length read ``null``. A real snapshot showed interval 44 carrying 12.39 kWh
    # where its requirement was 5.67. Zipping against ``demands`` removes the
    # possibility: both sides now name the same interval.
    # The spread each charge run can be attributed to, computed once from the
    # plan's own published prices so the label and the figure cannot disagree.
    _spreads = {
        run.start_index: future_spread_for(
            run, desired, discharge_efficiency=outcome.discharge_efficiency
        )
        for run in runs
        if run.direction == ECONOMIC_DIRECTION_CHARGE
    }
    reserve = {
        demand.index: value
        for demand, value in zip(
            outcome.horizon.demands,
            outcome.horizon.planning_reserve_kwh,
            strict=False,
        )
    }
    budget = MAX_ECONOMIC_RUN_INTERVALS_REPORTED
    payload: list[dict[str, Any]] = []
    for run in runs:
        rows, omitted = _run_intervals(desired, run, reserve, budget)
        budget -= len(rows)
        payload.append(
            _run_as_dict(
                run,
                safety_buy=run.start_index in outcome.safety_buy_runs,
                intervals=rows,
                omitted=omitted,
                purchase=classify_purchase(
                    run,
                    attribution=outcome.safety_buy_attribution.get(run.start_index),
                    future_spread_eur_kwh=_spreads.get(run.start_index, (None, None))[
                        0
                    ],
                    future_spread_price_eur_kwh=(
                        _spreads.get(run.start_index, (None, None))[1]
                    ),
                    bridge_kwh_now=outcome.bridge_kwh_now,
                    uncertainty_dc_kwh=(
                        None
                        if outcome.uncertainty is None
                        else outcome.uncertainty.total_dc_kwh
                    ),
                    edge_value_eur_per_kwh=outcome.edge_value_eur_per_kwh,
                    survives_to_edge_kwh=desired.edge_energy_kwh,
                    anti_churn_buffer_kwh=outcome.anti_churn_buffer_kwh,
                ),
            )
        )
    return payload


def _legacy_comparison(outcome: EconomicOutcome) -> dict[str, Any] | None:
    """Return beta.30's plan beside beta.31's, on identical inputs.

    ``None`` unless the fourth solve ran. Published so the change of architecture
    can be *watched* on live data before it is trusted with money: the two plans
    saw the same prices, the same forecast and the same pack, and differ only in
    which reserve they obeyed and whether terminal inventory was worth anything.

    **Temporary and diagnostic.** Only one planner controls hardware, and it is
    always the new one; this is the other one's answer, for reading.
    """
    legacy = outcome.legacy
    if legacy is None:
        return None
    new = outcome.desired
    return {
        "legacy_beta30_plan": _comparison_row(legacy),
        "new_beta31_plan": _comparison_row(new),
        "delta": {
            "grid_import_kwh": _round_kwh(
                new.planned_grid_import_kwh - legacy.planned_grid_import_kwh
            ),
            "cost_eur": _round_eur(new.cost_eur - legacy.cost_eur),
            "throughput_kwh": _round_kwh(
                new.battery_throughput_kwh - legacy.battery_throughput_kwh
            ),
            "end_energy_dc_kwh": _round_kwh(
                new.end_energy_dc_kwh - legacy.end_energy_dc_kwh
            ),
            "reserve_violation_kwh": _round_kwh(
                new.violation_kwh - legacy.violation_kwh
            ),
        },
        "rule": (
            "the same inputs under both architectures. legacy obeys the "
            "whole-horizon autonomy curve as a hard floor and puts no value on "
            "terminal inventory, which is beta.30 exactly. only the new plan ever "
            "reaches hardware -- this is comparison only, it is computed in shadow "
            "alone, and it is temporary: it should be removed once the replay "
            "shows the new architecture dominating on recorded days"
        ),
    }


def _comparison_row(plan: EconomicPlan) -> dict[str, Any]:
    """Return the figures the two architectures are compared on."""
    return {
        "action": plan.action,
        "cost_eur": _round_eur(plan.cost_eur),
        "expected_net_value_eur": _round_eur(plan.expected_net_value_eur),
        "grid_import_kwh": _round_kwh(plan.planned_grid_import_kwh),
        "grid_export_kwh": _round_kwh(plan.planned_grid_export_kwh),
        "battery_charge_ac_kwh": _round_kwh(plan.planned_charge_ac_kwh),
        "battery_discharge_ac_kwh": _round_kwh(plan.planned_discharge_ac_kwh),
        "battery_throughput_kwh": _round_kwh(plan.battery_throughput_kwh),
        "end_energy_dc_kwh": _round_kwh(plan.end_energy_dc_kwh),
        "edge_energy_kwh": _round_kwh(plan.edge_energy_kwh),
        "edge_value_eur": _round_eur(plan.edge_value_eur),
        "reserve_violation_kwh": _round_kwh(plan.violation_kwh),
        "run_count": len(plan.runs),
        "direction_changes": plan.direction_changes,
        "switching_cost_eur": _round_eur(plan.switching_cost_eur),
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
    return intent_for_action(run.action)


def intent_for_action(action: str) -> str:
    """Return the execution intent one action label implies.

    Split out of :func:`execution_intent` in beta.32 so a *segment* -- which is a
    stretch of intervals rather than a run -- can ask the same question of the same
    table. One mapping, two callers; a second copy is a second thing to keep in
    step, and this mapping decides which physical quantity Stage B targets.
    """
    if action in (ECONOMIC_ACTION_CHARGE, ECONOMIC_ACTION_SAFETY_BUY):
        return EXECUTION_INTENT_GRID_CHARGE
    if action == ECONOMIC_ACTION_DISCHARGE:
        return EXECUTION_INTENT_SERVE_LOAD
    if action == ECONOMIC_ACTION_EXPORT:
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
    desired_grid_kw: float | None = None,
    safety_buy_kwh: float | None = None,
    economic_buy_kwh: float | None = None,
    intervals: tuple[EconomicInterval, ...] = (),
    moment: Any = None,
    campaign_id: str | None = None,
    campaign_end: datetime | None = None,
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
    # One row per solved interval of this run, off the rows the optimizer already
    # produced. No new solve, and no economics touched.
    quarter_rows = (
        quarter_schedule_for(
            intervals,
            start_index=run.start_index,
            end_index=run.end_index,
            intent=intent,
            moment=moment,
        )
        if intervals and moment is not None
        else []
    )
    battery = run.battery_charge_ac_kwh + run.battery_discharge_ac_kwh
    return {
        "plan_id": _execution_plan_id(intent, window_start),
        # **Which campaign this target belongs to**, so the surfaces can hold one
        # lifecycle over a campaign that Stage B necessarily sees as several
        # separate windows. ``None`` on a pre-beta.32 record and on any target the
        # caller could not place -- and absent means *fall back to run-level
        # behaviour*, the beta.27 ``quarter_schedule`` precedent, never an error.
        "campaign_id": campaign_id,
        "campaign_end": None if campaign_end is None else campaign_end.isoformat(),
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
        # **The signed grid target for the current quarter, and the beta.25
        # contract.** A *rate*, not an energy, and deliberately a separate field
        # from ``grid_target_kwh`` above -- that one is an export energy, present
        # only for a net-export intent, and merging the two is exactly the
        # confusion its own comment guards against. Positive is intended import,
        # negative intended export.
        "desired_grid_kw": _round_kw(desired_grid_kw),
        # What the reserve is responsible for, and what it is not. Both within
        # this run window; see ``_safety_buy_attribution`` for why that boundary
        # is stated rather than glossed.
        "safety_buy_kwh": _round_kwh(safety_buy_kwh),
        "economic_buy_kwh": _round_kwh(economic_buy_kwh),
        # **The per-quarter execution rows, since beta.27.** The run aggregates
        # above are unchanged and still published, so nothing that read the
        # contract before has to change; this is what Stage B executes against,
        # because a run-level rate cannot describe a run's later quarters.
        #
        # **Built here rather than handed in**, which is the beta.27.1 fix. The
        # rows depend on ``intent``, and ``intent`` is derived *inside* this
        # function -- so a caller assembling the schedule itself would need a
        # second copy of that derivation. It had a worse failure than drift: the
        # parameter was optional, the production call site never passed it, and
        # every run published an empty list beside a rule describing what the list
        # would have contained. Taking the rows instead of the result means the
        # only way to get an empty schedule is to have no rows.
        "quarter_schedule": quarter_rows,
        # **Why the list is the length it is.** An empty schedule is a real answer
        # when a run has no solved rows to publish, and was a *silent bug* when the
        # call site forgot to pass them. Saying which of the two happened is what
        # makes the difference visible in a hardware download rather than only in
        # the source.
        "quarter_schedule_source": (
            "solved_intervals" if intervals else "no_intervals_supplied"
        ),
        "quarter_schedule_rule": (
            "one row per solved interval of this run. battery_kwh is the "
            "objective for a charge and the ceiling for an export; "
            "grid_authorised_kwh is the marginal import ceiling for a charge; "
            "grid_export_target_kwh is the ACTUAL meter export objective for an "
            "export and is what the run-level grid_target_kwh is summed from. "
            "grid_export_caused_kwh is attribution only. not_executable names why "
            "a row cannot be armed and is null when it can: an objective below the "
            "actuator's 0.025 kWh resolution is economically real and physically "
            "meaningless, so it stays visible here and is never sent"
        ),
        "buy_attribution_rule": (
            "reserve-attributable and economic energy within this run window, "
            "from the reserve-relaxed counterfactual the plan already solves. "
            "not a globally exact decomposition: the relaxed solve may move "
            "economic charging to other quarters"
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
            "consumed by the Stage B controller, which since beta.25 executes "
            "an authorised charge on the Hillview Dispatch surface in mode 2 with "
            "a negative power, and nothing else: discharge, export, curtailment "
            "and modes 6 and 7 are refused at the authorisation and send "
            "boundaries. "
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


def campaign_identity(direction: str, campaign_end_utc: datetime) -> str:
    """Return a short stable identifier for one economic campaign.

    ``(direction, end instant)``, and every part of that is a correction.

    **Absolute time, not an index.** ``EconomicInterval.index`` is day-absolute
    within the plan's *target day* and rebases at midnight, so an index-derived
    identity is stable within a day and silently different across the boundary.

    **The end, not the start** -- the same reason ``PlanIdentity`` already settled
    on. The horizon's head advances every refresh, so a campaign that is running
    has a start instant moving underneath it while its end sits still. Anchoring on
    the start is what made the beta.29/beta.30 plan ids churn, and this is the
    identity that has to survive twenty refreshes of one campaign.

    **Minute resolution**, because the end is a quarter boundary: seconds and
    microseconds can only carry noise from whichever clock resolved the instant.
    """
    stamp = campaign_end_utc.strftime("%Y-%m-%dT%H:%M")
    digest = hashlib.sha256(f"{direction}|{stamp}".encode()).hexdigest()
    return digest[:ECONOMIC_FINGERPRINT_CHARS]


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
    reachability: Any = None,
    uncertainty: Any = None,
    bridge_kwh_now: float | None = None,
    actionable_interval_count: int = 0,
    edge_value_eur_per_kwh: float = 0.0,
    edge_creditable_kwh: float = float("inf"),
    minimum_trade_gain_eur: float = 0.0,
    grid_charge_margin_eur_per_kwh: float = 0.0,
    battery_throughput_cost_eur_per_kwh: float = 0.0,
    floor_energy_kwh: float = 0.0,
    #: The pack's measured stored energy, DC kWh, and the discharge efficiency the
    #: meter boundary is crossed at. Both supplied rather than derived: this
    #: function has no plan and no battery, and inventing either would be a figure
    #: that could disagree with the decision it is describing.
    stored_dc_kwh: float | None = None,
    discharge_efficiency: float = 1.0,
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
            # **Both read the runtime, since beta.33.** This pair was a literal
            # ``False`` and a constant ``execution_unavailable`` -- correct in
            # beta.18, when nothing was sent, and a plain untruth from beta.24 on.
            # Publishing them side by side made the contradiction worse: the same
            # download showed a running dispatch and a capability block saying
            # execution was unavailable.
            "execution_available": CONTROL_EXECUTION_AVAILABLE,
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
        # **What the planner actually obeyed, and what it valued.** Published
        # whole rather than as a single number, because the whole point of beta.31
        # is that a reader must be able to tell a physical bound from a price.
        "planning": {
            "reserve_semantics": (
                None if reachability is None else reachability.semantics
            ),
            "reachability_now_dc_kwh": (
                None
                if reachability is None
                else _round_kwh(reachability.required_now_dc_kwh)
            ),
            # **Where the curve actually binds**, which is rarely the head. A
            # reader who sees only the head figure will conclude that a purchase
            # was discretionary when it was not.
            "reachability_peak_dc_kwh": (
                None
                if reachability is None
                else _round_kwh(reachability.peak_required_dc_kwh)
            ),
            "reachability_peak_interval": (
                None if reachability is None else reachability.peak_required_interval
            ),
            "hard_floor_dc_kwh": (
                None if reachability is None else _round_kwh(floor_energy_kwh)
            ),
            "bridge_kwh_now": _round_kwh(bridge_kwh_now),
            # **The surplus, with the floor counted exactly once.** An earlier draft
            # published ``stored - floor - reachability_now``, which subtracts the
            # floor twice: ``reserve.py`` computes ``required = floor + deficit``,
            # and production calls it a second time at ``floor + margin``, so
            # ``reachability_now`` already *contains* both. Live figures:
            # 14.77 stored, 4.32 floor, 5.13 reachable -> the surplus is
            # 14.77 - 5.13 = 9.64 DC, and the wrong form gave 5.32.
            "exportable_surplus_dc_kwh": (
                None
                if reachability is None or stored_dc_kwh is None
                else _round_kwh(
                    max(0.0, stored_dc_kwh - reachability.required_now_dc_kwh)
                )
            ),
            "exportable_surplus_ac_kwh": (
                None
                if reachability is None or stored_dc_kwh is None
                else _round_kwh(
                    max(0.0, stored_dc_kwh - reachability.required_now_dc_kwh)
                    * discharge_efficiency
                )
            ),
            # **Published separately and explicitly not the surplus.** It ignores
            # the uncertainty margin, so a reader who subtracted one from the other
            # would be subtracting the margin twice. Both appear, both named.
            "deliverable_above_floor_ac_kwh": (
                None
                if stored_dc_kwh is None
                else _round_kwh(
                    max(0.0, stored_dc_kwh - floor_energy_kwh) * discharge_efficiency
                )
            ),
            "surplus_rule": (
                "exportable_surplus = stored - reachability_now, and nothing else. "
                "reachability_now already contains the hard floor and the "
                "uncertainty margin, so subtracting either again double counts it. "
                "deliverable_above_floor_ac_kwh is a different figure: it ignores "
                "the margin and is not the surplus"
            ),
            "forecast_uncertainty_protection_kwh": (
                None if uncertainty is None else _round_kwh(uncertainty.total_dc_kwh)
            ),
            "forecast_uncertainty_role": (
                "provenance of reachability_now, never a further subtraction"
            ),
            # **The evidence the protection is built from, published in full.**
            # Every figure here already existed and was read by nobody who could
            # act on it. ``error_persistence`` is new in beta.32 and is the answer
            # to a question no assumption could settle: an allowance for cumulative
            # error over n intervals grows as ``mae * sqrt(n)`` if the errors are
            # independent and as ``mae * n`` if they are perfectly persistent, and
            # neither is defensible -- beta.31's implicit sqrt(n) is why its
            # statistical term was inert at 0.06 * sqrt(48) = 0.42 kWh against a
            # 21.6 kWh pack. The window's own rows hold the answer, so it is
            # measured, and no free statistical constant is introduced.
            "forecast_evidence": (
                None
                if outcome.forecast_risk is None
                else {
                    "bias_kwh": _round_kwh(outcome.forecast_risk.bias_kwh),
                    "mae_kwh": _round_kwh(outcome.forecast_risk.mae_kwh),
                    "mae_modelled_kwh": _round_kwh(
                        outcome.forecast_risk.mae_modelled_kwh
                    ),
                    "mae_filled_kwh": _round_kwh(outcome.forecast_risk.mae_filled_kwh),
                    "error_persistence": (
                        None
                        if outcome.forecast_risk.error_persistence is None
                        else round(outcome.forecast_risk.error_persistence, 4)
                    ),
                    "adaptation_ratio": (
                        None
                        if outcome.forecast_risk.adaptation_ratio is None
                        else round(outcome.forecast_risk.adaptation_ratio, 4)
                    ),
                    "today_interval_count": outcome.forecast_risk.today_interval_count,
                    # Which rung of the cascade the allowance actually used, so a
                    # reader can tell a mature installation from a thin one without
                    # inferring it from the size of the number.
                    "allowance_basis": (
                        "bias_and_persistent_mae"
                        if outcome.forecast_risk.error_persistence is not None
                        and outcome.forecast_risk.bias_kwh is not None
                        else "bias_and_mae"
                        if outcome.forecast_risk.bias_kwh is not None
                        else "mae_only"
                        if outcome.forecast_risk.mae_kwh is not None
                        else "none"
                    ),
                    "provenance_split_rule": (
                        "mae_modelled_kwh and mae_filled_kwh are null on the "
                        "refresh path by design: they live on WindowMetrics, which "
                        "needs a partition load, and the refresh must not touch "
                        "disk. the allowance falls back to the pooled mae_kwh, "
                        "which is the honest figure the cheap path can establish"
                    ),
                    "rule": (
                        "err(k) = max(0, -bias) + rho * mae. one-sided, because "
                        "only under-prediction can strand the pack -- bias is "
                        "positive when the model over-predicts. rho absent means "
                        "rho = 1, the conservative end: sparse history yields "
                        "*more* protection, never zero"
                    ),
                }
            ),
            "actionable_intervals": actionable_interval_count,
            "grid_credit_intervals": (
                None if reachability is None else reachability.grid_credit_intervals
            ),
            "uncertainty": None if uncertainty is None else uncertainty.as_dict(),
            "edge_value_eur_kwh": _round_eur(edge_value_eur_per_kwh),
            "edge_creditable_kwh": (
                None
                if edge_creditable_kwh == float("inf")
                else _round_kwh(edge_creditable_kwh)
            ),
            "edge_energy_kwh": _round_kwh(desired.edge_energy_kwh),
            "edge_value_eur": _round_eur(desired.edge_value_eur),
            "battery_throughput_kwh": _round_kwh(desired.battery_throughput_kwh),
            "battery_throughput_cost_eur": _round_eur(
                desired.battery_throughput_cost_eur
            ),
            # **Which counterfactual every marginal euro figure was measured
            # against.** ``docs/ARCHITECTURE.md`` has asserted since Phase 2 that
            # baseline self-consumption is real in the default configuration, and
            # until beta.32 nothing checked -- the only measurement in the codebase
            # detects the *charge* direction. The idle counterfactual charged the
            # house full import price for an interval whose real import may be zero.
            #
            # It matters beyond reporting: ``unavoidable_import`` feeds
            # ``grid_charge_kwh``, which is the basis for the grid-charge margin, so
            # an overstated unavoidable import *understates* the margin and biases
            # the plan toward charging too readily.
            "counterfactual": {
                "basis": (
                    COUNTERFACTUAL_AMBIENT_SELF_CONSUMPTION
                    if outcome.ambient_self_consumption_modelled
                    else COUNTERFACTUAL_IDLE_IMPORT
                ),
                "ambient_self_consumption_modelled": (
                    outcome.ambient_self_consumption_modelled
                ),
                "rule": (
                    "with ambient self-consumption unmodelled every published euro "
                    "figure is byte-identical to beta.31, so an installation where "
                    "the inverter does not self-consume sees no change at all. "
                    "unknown means not modelled: the optimistic error would be a "
                    "plan that believes the house is fed for free"
                ),
                "deferred": (
                    "the state *transition* is not corrected, and the reason is "
                    "recorded rather than left implicit: 0.105 kWh DC of ambient "
                    "discharge against a 0.264 kWh lattice bucket is not "
                    "representable, and _move_to discards clamp-reduced moves"
                ),
            },
            "gates": {
                "minimum_trade_gain_eur": minimum_trade_gain_eur,
                "grid_charge_margin_eur_per_kwh": grid_charge_margin_eur_per_kwh,
                "battery_throughput_cost_eur_per_kwh": (
                    battery_throughput_cost_eur_per_kwh
                ),
                "bases_rule": (
                    "three disjoint bases and they must stay disjoint: the trade "
                    "gain is per discretionary run, the grid-charge margin is per "
                    "kWh of grid-caused charging, the throughput cost is per kWh "
                    "of movement in either direction. none of the three is a "
                    "degradation model, and setting two of them to depreciation "
                    "would charge the buy side twice"
                ),
            },
            # ------------------------------------------------ beta.32
            #
            # **The export permission, and the four quantities it must not become.**
            # Two booleans each rather than one, because initiating a purchase and
            # enlarging an already-triggered one are different powers.
            "export_permission": {
                "active": bool(outcome.export_free),
                "export_floor_dc_kwh": [
                    _round_kwh(value) for value in outcome.export_floor_kwh[:12]
                ],
                "protect_price_eur_per_kwh": [
                    None if value is None else _round_eur(value)
                    for value in outcome.protect_price_eur_per_kwh[:12]
                ],
                "export_free": list(outcome.export_free[:12]),
                "upper_net_demand_ac_kwh": [
                    _round_kwh(value) for value in outcome.upper_net_demand_ac_kwh[:12]
                ],
                "survival_window_end": outcome.survival_window_end,
                "survival_window_basis": outcome.survival_window_basis,
                "survival_window_quarters": max(0, outcome.survival_window_end),
                "adaptation_ratio_applied": _round_eur(
                    outcome.adaptation_ratio_applied
                ),
                "adaptation_clipped": outcome.adaptation_clipped,
                # **The counterfactual that keeps it honest.** If this is
                # materially positive on ordinary shapes the permission is wrong,
                # and this figure is how anyone finds out. Measured on the DP's own
                # objective, not on ``cost_eur``: two earlier formulas published a
                # negative number for a permission that had genuinely cost money.
                "export_gate_cost_eur": _round_eur(outcome.export_gate_cost_eur),
                "selected_export_energy_kwh": _round_kwh(
                    sum(entry.grid_export_kwh for entry in desired.intervals)
                ),
                "rule": (
                    "a permission on a caused-export delta and nothing else. it "
                    "never enters a violation term, never gates a hold, a charge "
                    "or a load-serving discharge, and never changes the reserve "
                    "curve -- so self-consumption is never gated and the pack can "
                    "always reach its floor feeding the house. the price test "
                    "compares two prices at the same grid boundary with no "
                    "efficiency: one DC kWh held avoids p_import * eta_d and the "
                    "same kWh exported earns p_export * eta_d, so it cancels"
                ),
            },
            "purchase_powers": {
                "physical_reachability_now_dc_kwh": {
                    "value": (
                        None
                        if reachability is None
                        else _round_kwh(reachability.required_now_dc_kwh)
                    ),
                    "can_initiate_grid_purchase": True,
                    "can_increase_triggered_grid_purchase": True,
                },
                "safety_bridge_kwh": {
                    "value": _round_kwh(bridge_kwh_now),
                    "can_initiate_grid_purchase": True,
                    "can_increase_triggered_grid_purchase": True,
                },
                "safety_anti_churn_buffer_kwh": {
                    "value": _round_kwh(outcome.anti_churn_buffer_kwh),
                    # The whole point of the category: no bridge, no bump.
                    "can_initiate_grid_purchase": False,
                    "can_increase_triggered_grid_purchase": True,
                    "released_for_household_use_after_buy": True,
                },
                "economic_survival_to_refill_kwh": {
                    "value": (
                        None
                        if not outcome.export_floor_kwh
                        else _round_kwh(outcome.export_floor_kwh[0])
                    ),
                    "can_initiate_grid_purchase": False,
                    "can_increase_triggered_grid_purchase": False,
                },
                "physical_reserve_head_dc_kwh": _round_kwh(
                    outcome.physical_reserve_head_kwh
                ),
                "enforced_reserve_head_dc_kwh": _round_kwh(
                    outcome.enforced_reserve_head_kwh
                ),
                "enforced_reserve_equals_physical_beyond_head": True,
                "rule": (
                    "only physical reachability may *initiate* a Safety Buy. the "
                    "anti-churn extension may enlarge one already triggered and is "
                    "zero whenever the bridge is zero, vanishes from the enforced "
                    "head on the refresh after the buy lands, and is then free for "
                    "household self-consumption. the survival figure and the "
                    "export protection can do neither"
                ),
            },
            "rule": (
                "bridge_kwh_now is the only compulsory purchase: max(0, "
                "reachability_now - stored). zero means nothing is compulsory and "
                "every further kWh must clear the economic gates. the reserve "
                "here contains physics only -- it never prefers a cheap refill to "
                "a dear one, which is the objective's job"
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
            "the Stage B controller consumes these; only an authorised charge "
            "is executable"
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
        "legacy_comparison": _legacy_comparison(outcome),
        "runs": _runs_as_dicts(outcome, desired, runs),
        # **The campaigns, beside the runs and not instead of them.** A run is the
        # honest per-interval record of what the battery was doing; a campaign is
        # the unit the switching fee was charged against, and the unit the user made
        # one decision about. Measured on the live 17:45 horizon: the DP flagged
        # three run-state transitions and ``runs_from`` published fifteen label
        # slices, with ``charged_switching_fee`` false on every artefact split.
        "campaigns": _campaigns_as_dicts(desired, discharge_efficiency),
        "campaign_counts": {
            "economic_campaign_count": len(desired.campaigns),
            "buy_campaign_count": sum(
                1
                for campaign in desired.campaigns
                if campaign.direction == ECONOMIC_DIRECTION_CHARGE
            ),
            "sell_campaign_count": sum(
                1
                for campaign in desired.campaigns
                if campaign.direction == ECONOMIC_DIRECTION_DISCHARGE
                and campaign.objective_kwh > 0.0
            ),
            # **Published, because buy + sell does not equal the total and a reader
            # will otherwise file a bug.** A discharge campaign whose segments are
            # all ``serve_load`` sells nothing: it is self-consumption, which is
            # inverter behaviour and not an event.
            "serve_load_campaign_count": sum(
                1
                for campaign in desired.campaigns
                if campaign.direction == ECONOMIC_DIRECTION_DISCHARGE
                and campaign.objective_kwh <= 0.0
            ),
            "campaign_count_rule": (
                "grouped on the DP's own contiguous run state, so "
                "len(campaigns) == direction_changes by construction -- which is "
                "the proof this layer changed no decision. buy + sell does not sum "
                "to the total: a discharge campaign that sells nothing is a "
                "self-consumption campaign and is counted on its own"
            ),
        },
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
    grid_charge_margin_eur_per_kwh: float,
    battery_throughput_cost_eur_per_kwh: float,
    allow_grid_charging: bool,
    allow_battery_export: bool,
    bucket_kwh: float,
) -> str:
    """Return a digest of the economic settings a plan rests on.

    Separate from the battery-configuration digest because these are the user's
    *economic* choices rather than their hardware, and a later phase asking "why
    did it want that" needs to know which threshold was in force.

    **All three economic terms are required arguments, and beta.31 added the two
    that were missing.** Until then this digest covered only the fixed per-run
    threshold, so two installations differing by a whole per-kWh margin produced
    the *same* fingerprint -- which means a recorded decision could not be
    replayed, because the settings it was made under were not recoverable from
    the evidence. They are keyword-only and **have no defaults** on purpose: a
    parameter with a default is a setting a future caller can silently drop, and
    that is exactly how ``grid_charge_margin_eur_per_kwh`` spent a release doing
    nothing.
    """
    return _digest(
        {
            "minimum_trade_gain_eur": minimum_trade_gain_eur,
            "grid_charge_margin_eur_per_kwh": grid_charge_margin_eur_per_kwh,
            "battery_throughput_cost_eur_per_kwh": (
                battery_throughput_cost_eur_per_kwh
            ),
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
