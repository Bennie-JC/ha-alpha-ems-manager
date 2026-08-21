"""One refresh's battery decision, and the evidence behind it.

Assembles the pieces the other three Phase-3 modules provide -- limits and state
from :mod:`battery`, a request from :mod:`policy`, two trajectories from
:mod:`simulation` -- into the single frozen record the sensors and diagnostics
read. It enforces nothing itself.

It reaches Phase 2 only through :mod:`api`, which is the door built for it. The
forecast-history internals stay private, so the storage layout behind them can
still change without a battery model breaking, and ``test_api_boundary.py``
enforces that statically over every module in the package.

Deciding, and declining to decide
---------------------------------

Two different failures are deliberately kept apart:

* **A missing hardware fact** -- no state of charge, no capacity, no power limit,
  an impossible efficiency -- means there is nothing to reason with.
  ``ACTION_NO_DECISION``, and the published recommendation reads ``unknown``.
* **A missing *forecast*** means the battery is fully known but the load is not.
  Holding is then a real, correct, explainable answer, so the action is
  ``ACTION_HOLD`` with the reason ``forecast_unavailable``. The trajectory is
  withheld, because simulating a day of load nobody predicted would mean reading
  the absent forecast as an idle house.

``Usable Battery Energy`` survives the second case, which is the point of
choosing it over a projected state of charge: it needs no forecast at all, so a
young installation still gets the number the minimum-SoC setting controls.

What the horizon is
-------------------

The trajectory starts at the **next** interval boundary, so every interval it
walks is a whole quarter-hour and the partial interval in progress never needs a
different duration. The recommendation is separate: it is for the interval now in
progress and needs no trajectory, being a function of the current state and the
demand expected of that one interval.

It then runs to the end of tomorrow, because Phase 2 already publishes tomorrow's
forecast. That costs nothing and exercises the multi-day path from the first
release rather than leaving it untested until Phase 10.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .api import LoadForecast
from .battery import (
    BatteryInputs,
    BatteryLimits,
    BatteryRequest,
    BatteryReserve,
    BatteryState,
    IntervalOutcome,
    apply_request,
    build_limits,
    build_state,
    static_reserve,
)
from .const import (
    ACTION_NO_DECISION,
    BATTERY_KW_PRECISION,
    BATTERY_KWH_PRECISION,
    BATTERY_SOC_PRECISION,
    MODE_CHARGE,
    MODE_DISCHARGE,
    REASON_MISSING_SOC,
)
from .policy import DEFAULT_POLICY, BatteryPolicy, HoldPolicy
from .reserve import (
    ReserveProjection,
    build_reserve,
    build_reserve_pv_blind,
    build_reserve_same_interval_only,
    compare_to_trajectory,
)
from .simulation import (
    IntervalDemand,
    SimulatedTrajectory,
    compare,
    demands_from_forecast,
    simulate,
)


def _round(value: float | None, digits: int) -> float | None:
    """Round for reporting, preserving ``None``."""
    return None if value is None else round(value, digits)


# -- the decision ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BatteryDecision:
    """What the battery should do in the interval now in progress.

    ``allowed_energy_ac_kwh`` is the primitive and the only field a later phase
    may act on. It has been through :func:`battery.apply_request`, so it is
    already inside every hardware limit; a consumer that recomputed it from
    ``request`` would be reintroducing the clamp it is supposed to trust.
    """

    action: str
    #: What the policy asked for, kept so a plan can say what it wanted.
    request: BatteryRequest
    #: AC energy the clamp permits this interval, ``>= 0``. Zero for a hold and
    #: for a non-decision alike.
    allowed_energy_ac_kwh: float
    reason: str
    constraints: tuple[str, ...]
    policy: str
    policy_version: int

    @property
    def decided(self) -> bool:
        """Return whether a decision was actually reached."""
        return self.action != ACTION_NO_DECISION

    @property
    def average_power_kw(self) -> float:
        """Return the unsigned interval-average AC power.

        An **average**, not an instantaneous setpoint: in the last partial
        interval before the floor a real device delivers full power for part of
        the interval and nothing afterwards. Naming it for what it is now avoids
        Phase 4 reading it as a setpoint and under-delivering.
        """
        from .battery import INTERVAL_HOURS

        return self.allowed_energy_ac_kwh / INTERVAL_HOURS

    @property
    def published_power_kw(self) -> float:
        """Return the signed power for publication. **Presentation only.**

        The one place in Phase 3 where a battery power carries a sign. Positive
        is energy into the battery, so it reads the way a person expects a
        "planned power" to read.

        This convention is the *plan's own*, and is deliberately independent of
        the configured ``battery_power_sign``, which describes only how the
        user's own sensor reports and is resolved away before anything downstream
        sees it. Nothing internal reasons about this sign: requests carry a mode.
        """
        if self.request.mode == MODE_CHARGE:
            return self.average_power_kw
        if self.request.mode == MODE_DISCHARGE:
            return -self.average_power_kw
        return 0.0


def _no_decision(reason: str, policy: BatteryPolicy) -> BatteryDecision:
    """Return the decision that declines to decide.

    Zero allowed energy is what makes ``NO_DECISION`` safe to publish: it is
    behaviourally identical to a hold, so a consumer that ignored the action
    entirely would still do nothing. The distinction exists for the reader and
    for a later phase, not as a behavioural difference.
    """
    return BatteryDecision(
        action=ACTION_NO_DECISION,
        request=BatteryRequest.idle(),
        allowed_energy_ac_kwh=0.0,
        reason=reason,
        constraints=(),
        policy=policy.identity,
        policy_version=policy.version,
    )


def _decide(
    state: BatteryState, demand: IntervalDemand, policy: BatteryPolicy
) -> tuple[BatteryDecision, IntervalOutcome]:
    """Return the decision for one interval, and the outcome it would have."""
    proposal = policy.propose(state, demand)
    outcome = apply_request(state, proposal.request)
    return (
        BatteryDecision(
            action=outcome.action,
            request=proposal.request,
            allowed_energy_ac_kwh=outcome.allowed_energy_ac_kwh,
            reason=proposal.reason,
            constraints=outcome.constraints,
            policy=policy.identity,
            policy_version=policy.version,
        ),
        outcome,
    )


# -- the plan ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BatteryPlan:
    """Everything one refresh concluded about the battery.

    Frozen, and never written to disk. Like ``BalanceSample`` it is a reading of
    facts that are themselves stored elsewhere: the state of charge is recorded in
    the learning history, the forecast in the forecast history, and the
    configuration in the config entry -- so any plan can be recomputed, and
    nothing here needs a storage layer of its own.
    """

    decision: BatteryDecision
    #: ``None`` when a hardware fact was missing.
    state: BatteryState | None
    inputs: BatteryInputs
    reserve: BatteryReserve
    #: Why nothing could be decided, or ``None``.
    unavailable_reason: str | None
    #: The hold reference and the candidate, or ``None`` with no usable forecast.
    reference: SimulatedTrajectory | None = None
    candidate: SimulatedTrajectory | None = None
    #: The civil day the trajectory starts in, and where in it.
    target_day: date | None = None
    start_index: int | None = None
    #: What the plan was built against, for diagnostics.
    forecast: dict[str, Any] = field(default_factory=dict)
    #: Phase 7's requirement over the same horizon, and its two counterfactuals.
    #:
    #: Carried on the plan because they are built from the same demands, so one
    #: refresh cannot end up with a requirement and a trajectory describing
    #: different horizons. **Nothing here is enforced**: the decision above was
    #: taken against ``reserve``, the static one, and is unaffected by these.
    reserve_projection: ReserveProjection | None = None
    reserve_same_interval_only: ReserveProjection | None = None
    reserve_pv_blind: ReserveProjection | None = None
    #: Where the projected trajectory would fall below the requirement.
    reserve_comparison: dict[str, Any] | None = None

    # -- what the sensors read -------------------------------------------

    @property
    def available(self) -> bool:
        """Return whether a decision was reached."""
        return self.unavailable_reason is None and self.decision.decided

    @property
    def usable_energy_kwh(self) -> float | None:
        """Return AC energy deliverable above the reserve, or ``None``.

        The **deliverable** figure, one boundary crossing below the raw DC energy
        above the floor, because that is what the house could actually receive.
        Both are reported in diagnostics under names that say which is which.

        An upper bound, knowingly: a single efficiency figure flatters a real
        inverter at low power and its auxiliary draw is not modelled at all. Both
        biases point the same way.
        """
        if self.state is None:
            return None
        return self.state.deliverable_energy_kwh

    @property
    def coverage_hours(self) -> float | None:
        """Return how long the usable energy covers the predicted demand.

        ``None`` when there is no forecast to divide by, which is why this is an
        attribute of ``Usable Battery Energy`` rather than an entity of its own:
        the kilowatt-hours survive a withheld forecast and the hours do not.
        """
        if self.state is None or self.candidate is None:
            return None
        intervals = self.candidate.intervals_with_demand
        if not intervals:
            return None
        demand = self.candidate.demand_kwh
        if demand <= 0.0:
            return None
        from .battery import INTERVAL_HOURS

        mean_power = demand / (intervals * INTERVAL_HOURS)
        if mean_power <= 0.0:
            return None
        return self.usable_energy_kwh / mean_power

    @property
    def what_if(self) -> dict[str, Any] | None:
        """Return the candidate-against-hold comparison, or ``None``."""
        if self.reference is None or self.candidate is None:
            return None
        return compare(self.reference, self.candidate)


def build_plan(
    *,
    soc_percent: float | None,
    capacity_kwh: Any,
    max_charge_kw: Any,
    max_discharge_kw: Any,
    round_trip_efficiency_percent: Any,
    configured_min_soc_percent: float,
    today_forecast: LoadForecast | None,
    tomorrow_forecast: LoadForecast | None,
    elapsed_intervals: int,
    today: date,
    battery_power_w: float | None = None,
    policy: BatteryPolicy | None = None,
    today_pv: Sequence[float | None] = (),
    tomorrow_pv: Sequence[float | None] = (),
    absorb_surplus: bool = False,
) -> BatteryPlan:
    """Build one refresh's plan. Pure, total, and it never raises.

    The order is the order the failures matter in: without limits there is no
    model, without a state of charge there is nothing to apply it to, and only
    then does the forecast decide whether a *trajectory* is possible.
    """
    chosen: BatteryPolicy = policy or DEFAULT_POLICY()
    reserve = static_reserve(configured_min_soc_percent)
    inputs = BatteryInputs(
        soc_percent=soc_percent,
        capacity_kwh=capacity_kwh if isinstance(capacity_kwh, (int, float)) else None,
        max_charge_kw=(
            max_charge_kw if isinstance(max_charge_kw, (int, float)) else None
        ),
        max_discharge_kw=(
            max_discharge_kw if isinstance(max_discharge_kw, (int, float)) else None
        ),
        round_trip_efficiency_percent=(
            round_trip_efficiency_percent
            if isinstance(round_trip_efficiency_percent, (int, float))
            else None
        ),
        configured_min_soc_percent=reserve.configured_min_soc_percent,
        battery_power_w=battery_power_w,
    )

    limits, limit_reason = build_limits(
        capacity_kwh=capacity_kwh,
        max_charge_kw=max_charge_kw,
        max_discharge_kw=max_discharge_kw,
        round_trip_efficiency_percent=round_trip_efficiency_percent,
    )
    if limits is None:
        return BatteryPlan(
            decision=_no_decision(limit_reason or REASON_MISSING_SOC, chosen),
            state=None,
            inputs=inputs,
            reserve=reserve,
            unavailable_reason=limit_reason,
        )

    state = build_state(soc_percent=soc_percent, limits=limits, reserve=reserve)
    if state is None:
        return BatteryPlan(
            decision=_no_decision(REASON_MISSING_SOC, chosen),
            state=None,
            inputs=inputs,
            reserve=reserve,
            unavailable_reason=REASON_MISSING_SOC,
        )

    # The decision is for the interval in progress, whose predicted demand is at
    # the elapsed index. An index outside the day yields no demand rather than
    # wrapping into a neighbour's.
    current = _current_demand(today_forecast, elapsed_intervals, today_pv)
    decision, _outcome = _decide(state, current, chosen)

    demands = _horizon(
        today_forecast, tomorrow_forecast, elapsed_intervals, today_pv, tomorrow_pv
    )
    if not demands:
        return BatteryPlan(
            decision=decision,
            state=state,
            inputs=inputs,
            reserve=reserve,
            unavailable_reason=None,
            target_day=today,
            start_index=min(elapsed_intervals + 1, _count(today_forecast)),
            forecast=_forecast_report(today_forecast, tomorrow_forecast),
        )

    # Both trajectories absorb on the same terms, so the comparison between them
    # is about the decision rather than about which one was allowed to store the
    # sun. The hold reference is the counterfactual "the battery does nothing it
    # was asked to do", not "the inverter is switched off".
    reference = simulate(
        state, demands, HoldPolicy().provider(), absorb_surplus=absorb_surplus
    )
    candidate = simulate(
        state, demands, chosen.provider(), absorb_surplus=absorb_surplus
    )

    # Phase 7, over the same horizon and from the same demands. The floor is the
    # user's configured minimum expressed as energy -- the recursion's terminal
    # condition and the lowest value its answer can take. It is read here rather
    # than in ``reserve`` so that module never touches either floor name, and so
    # the requirement cannot be computed against anything but the hard floor.
    floor_energy_kwh = limits.energy_for_soc(reserve.configured_min_soc_percent)
    requirement = build_reserve(
        limits=limits, floor_energy_kwh=floor_energy_kwh, demands=demands
    )

    return BatteryPlan(
        decision=decision,
        state=state,
        inputs=inputs,
        reserve=reserve,
        unavailable_reason=None,
        reference=reference,
        candidate=candidate,
        target_day=today,
        start_index=min(elapsed_intervals + 1, _count(today_forecast)),
        forecast=_forecast_report(today_forecast, tomorrow_forecast),
        reserve_projection=requirement,
        reserve_same_interval_only=build_reserve_same_interval_only(
            limits=limits, floor_energy_kwh=floor_energy_kwh, demands=demands
        ),
        reserve_pv_blind=build_reserve_pv_blind(
            limits=limits, floor_energy_kwh=floor_energy_kwh, demands=demands
        ),
        reserve_comparison=compare_to_trajectory(requirement, candidate),
    )


def _count(forecast: LoadForecast | None) -> int:
    """Return a forecast's interval count, or zero when there is none."""
    return 0 if forecast is None else forecast.interval_count


def _current_demand(
    forecast: LoadForecast | None,
    elapsed_intervals: int,
    pv: Sequence[float | None] = (),
) -> IntervalDemand:
    """Return the demand for the interval now in progress.

    A withheld forecast, an absent one and an out-of-range index all yield a
    demand of ``None`` -- an unpredicted interval, not a predicted idle house.
    """
    index = max(0, elapsed_intervals)
    if (
        forecast is None
        or not forecast.available
        or not 0 <= index < len(forecast.intervals)
    ):
        return IntervalDemand(index=index, baseline_kwh=None)
    return IntervalDemand(
        index=index,
        baseline_kwh=forecast.intervals[index],
        filled=bool(index < len(forecast.filled) and forecast.filled[index]),
        pv_kwh=pv[index] if index < len(pv) else None,
    )


def _horizon(
    today_forecast: LoadForecast | None,
    tomorrow_forecast: LoadForecast | None,
    elapsed_intervals: int,
    today_pv: Sequence[float | None] = (),
    tomorrow_pv: Sequence[float | None] = (),
) -> tuple[IntervalDemand, ...]:
    """Return the intervals to simulate: the rest of today, then all of tomorrow.

    Starts at the next whole interval. Returns nothing at all when neither
    forecast is publishable -- a trajectory over invented load would be worse
    than no trajectory, and the caller reports the absence.
    """
    demands: list[IntervalDemand] = []
    if today_forecast is not None and today_forecast.available:
        demands.extend(
            demands_from_forecast(
                today_forecast.intervals,
                today_forecast.filled,
                start_index=elapsed_intervals + 1,
                pv=today_pv,
            )
        )
    if tomorrow_forecast is not None and tomorrow_forecast.available:
        offset = _count(today_forecast)
        demands.extend(
            IntervalDemand(
                index=offset + demand.index,
                baseline_kwh=demand.baseline_kwh,
                filled=demand.filled,
                pv_kwh=demand.pv_kwh,
            )
            for demand in demands_from_forecast(
                tomorrow_forecast.intervals,
                tomorrow_forecast.filled,
                pv=tomorrow_pv,
            )
        )
    return tuple(demands)


def _projection_note(trajectory: SimulatedTrajectory) -> str:
    """Return the caveat that belongs to this projection.

    Conditional rather than deleted. The PV-blind wording was pinned by a test on
    purpose: a projection published without its limitation costs more trust than
    it buys, and the honest fix when the limitation changes is to change the
    words, not to remove them.
    """
    if not trajectory.pv_aware:
        return (
            "diagnostics only, and PV-blind: the simulator has no photovoltaic "
            "production term, so on a sunny day the real state of charge will "
            "be higher than this and the simulated grid import higher than "
            "reality. Not published as an entity for that reason"
        )
    if trajectory.intervals_absorbing:
        return (
            "diagnostics only, and PV-aware: forecast production is netted "
            "against predicted load, and surplus is modelled as stored because "
            "the inverter's own state shows it storing surplus. Still a "
            "projection rather than a measurement, and still not an entity"
        )
    return (
        "diagnostics only, PV-aware, and a lower bound: forecast production is "
        "netted against predicted load, but surplus is treated as exported "
        "because the inverter's state does not show it being stored -- so the "
        "real state of charge may be higher than this. Not an entity"
    )


def _forecast_report(
    today_forecast: LoadForecast | None, tomorrow_forecast: LoadForecast | None
) -> dict[str, Any]:
    """Return what the plan was built against, for diagnostics.

    The forecast's identity rather than its contents: the day, whether it was
    published, why not, and how much history stood behind it. That is what joins
    a plan back to the forecast evidence that produced it.
    """

    def describe(forecast: LoadForecast | None) -> dict[str, Any]:
        if forecast is None:
            return {"available": False, "unavailable_reason": "forecast_not_built"}
        return {
            "day": forecast.day.isoformat(),
            "available": forecast.available,
            "unavailable_reason": forecast.unavailable_reason,
            "interval_count": forecast.interval_count,
            "model_days": forecast.model_days,
            "confidence_percent": forecast.confidence_percent,
            "total_kwh": forecast.total_kwh,
            "timezone": forecast.tz_key,
        }

    return {"today": describe(today_forecast), "tomorrow": describe(tomorrow_forecast)}


# -- reporting ---------------------------------------------------------------


def plan_as_dict(plan: BatteryPlan, tz: Any = None) -> dict[str, Any]:
    """Return the bounded diagnostics form of a plan.

    Bounded deliberately: no list here may exceed sixteen entries and no
    per-interval array appears at all, which is the ceiling the whole diagnostics
    payload is held to.
    """
    state = plan.state
    limits: BatteryLimits | None = None if state is None else state.limits
    decision = plan.decision

    payload: dict[str, Any] = {
        "available": plan.available,
        "unavailable_reason": plan.unavailable_reason,
        "controls_nothing": (
            "Phase 3 is observation only: this plan is never executed, and the "
            "integration issues no command to the battery"
        ),
        "inputs": {
            "soc_percent": plan.inputs.soc_percent,
            "capacity_kwh": plan.inputs.capacity_kwh,
            "max_charge_kw": plan.inputs.max_charge_kw,
            "max_discharge_kw": plan.inputs.max_discharge_kw,
            "round_trip_efficiency_percent": (
                plan.inputs.round_trip_efficiency_percent
            ),
            "battery_power_w": plan.inputs.battery_power_w,
            "battery_power_role": (
                "reported for coherence only; the state of charge is the sole "
                "source of stored energy and an instantaneous power never "
                "redefines it"
            ),
            "boundaries": (
                "capacity is DC-side usable energy; charge and discharge power "
                "and every household energy are AC-side"
            ),
            "soc_quantisation_note": (
                "a state of charge reported in whole percent quantises the "
                "seed energy to one percent of capacity, which dominates every "
                "other error in the model"
            ),
        },
        "reserve": {
            "configured_min_soc_percent": plan.reserve.configured_min_soc_percent,
            "effective_min_soc_percent": plan.reserve.effective_min_soc_percent,
            "source": plan.reserve.source,
            "raised_above_configured": plan.reserve.raised_above_configured,
            "rule": (
                "the configured minimum is the hard floor the simulator clamps "
                "at and never crosses; the effective minimum is the policy "
                "target, equal to it in this phase"
            ),
        },
        "decision": {
            "action": decision.action,
            "reason": decision.reason,
            "requested_mode": decision.request.mode,
            "requested_power_kw": _round(
                decision.request.power_kw, BATTERY_KW_PRECISION
            ),
            "allowed_energy_kwh": _round(
                decision.allowed_energy_ac_kwh, BATTERY_KWH_PRECISION
            ),
            "average_power_kw": _round(decision.average_power_kw, BATTERY_KW_PRECISION),
            "published_power_kw": _round(
                decision.published_power_kw, BATTERY_KW_PRECISION
            ),
            "constraints": list(decision.constraints),
            "policy": decision.policy,
            "policy_version": decision.policy_version,
            "power_sign_note": (
                "published_power_kw is positive for charging and is a "
                "presentation convention of this plan; it is unrelated to the "
                "configured battery power sign, which describes the source "
                "sensor only"
            ),
        },
        "forecast": plan.forecast,
    }

    if limits is not None and state is not None:
        payload["model"] = {
            "capacity_kwh": limits.capacity_kwh,
            "max_soc_percent": limits.max_soc_percent,
            "charge_efficiency": round(limits.charge_efficiency, 6),
            "discharge_efficiency": round(limits.discharge_efficiency, 6),
            "round_trip_efficiency": round(limits.round_trip_efficiency, 6),
            "efficiency_rule": (
                "one configured round-trip figure split symmetrically; applied "
                "exactly once when energy crosses the AC/DC boundary and never "
                "in state-of-charge arithmetic"
            ),
            "known_optimistic_bias": (
                "a single efficiency figure flatters a real inverter at low "
                "power, and inverter auxiliary draw is not modelled; both make "
                "the usable figure an upper bound"
            ),
        }
        payload["state"] = {
            "soc_percent": _round(state.soc_percent, BATTERY_SOC_PRECISION),
            "energy_kwh": _round(state.energy_kwh, BATTERY_KWH_PRECISION),
            "floor_energy_kwh": _round(state.floor_energy_kwh, BATTERY_KWH_PRECISION),
            "usable_energy_dc_kwh": _round(
                state.usable_energy_kwh, BATTERY_KWH_PRECISION
            ),
            "deliverable_energy_ac_kwh": _round(
                state.deliverable_energy_kwh, BATTERY_KWH_PRECISION
            ),
            "headroom_energy_kwh": _round(
                state.headroom_energy_kwh, BATTERY_KWH_PRECISION
            ),
            "at_or_below_floor": state.at_or_below_floor,
            "below_floor": state.below_floor,
        }

    payload["published"] = {
        "battery_recommendation": (decision.action if decision.decided else None),
        "planned_battery_power_kw": (
            _round(decision.published_power_kw, BATTERY_KW_PRECISION)
            if decision.decided
            else None
        ),
        "usable_battery_energy_kwh": _round(
            plan.usable_energy_kwh, BATTERY_KWH_PRECISION
        ),
        "gate": (
            "a recommendation of no_decision is published as unknown; the "
            "three figures here are exactly what the entities show"
        ),
    }

    if plan.candidate is not None:
        payload["trajectory"] = plan.candidate.as_dict(plan.target_day, tz)
        payload["trajectory"]["start_index"] = plan.start_index
        payload["trajectory"]["horizon"] = (
            "from the next whole interval to the end of tomorrow"
        )
        payload["projected_soc_percent"] = _round(
            plan.candidate.end_soc_percent, BATTERY_SOC_PRECISION
        )
        payload["projected_soc_note"] = _projection_note(plan.candidate)
        payload["coverage_hours"] = _round(plan.coverage_hours, 2)
    if plan.reference is not None:
        payload["hold_reference"] = plan.reference.as_dict()
    what_if = plan.what_if
    if what_if is not None:
        payload["what_if"] = what_if

    return payload
