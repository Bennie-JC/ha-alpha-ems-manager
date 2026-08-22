"""Sensor platform for Alpha EMS Manager.

Twelve sensors: the four Phase-1 forecast and learning ones, the two Phase-2
forecast-error ones, the three Phase-3 battery ones, the single Phase-4
control state, the single Phase-7 dynamic reserve, and the single Phase-8
economic action. Every other quantity the integration computes -- per-slot
profiles, window means, balance residuals, coverage statistics, per-horizon error
breakdowns, the snapshot inventory, the simulated battery trajectory, the
per-interval reserve curve, the economic counterfactuals and every planned run --
is available through diagnostics instead. Ninety-six quarter sensors and five
window averages would be technically easy and practically awful.

The three Phase-3 sensors describe a plan that is never executed, the Phase-4 one
describes what the control pipeline made of that plan -- including whether it
would have been safe to carry out -- and the Phase-8 one describes what the
optimizer would *want* to do, beside what implemented actuators could achieve and
why nothing is sent. Nothing in this integration issues a command to a battery:
all of it is published so it can be watched for weeks before anything is allowed
to act on any of it.

The two Phase-2 sensors measure error that has already happened, so unlike the
forecast sensors they do carry a state class: a record of how wrong last week
was belongs in long-term statistics, while a prediction does not. Neither
carries an energy device class. The yesterday figure is signed, and labelling a
quantity that is routinely negative as energy would offer it to the Energy
dashboard alongside real consumption.

Entity names are literal English rather than translation keys, matching the
sibling Frank Quarter Prices integration. Home Assistant derives an entity id
from the *translated* name, so a translation key would hand a Dutch user
``sensor.alpha_ems_verwachte_huisbelasting_vandaag``. Stable ids that automations
can rely on are worth more here than a translated default name the user can
override in the UI anyway.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EVENT_LOGBOOK_ENTRY,
    PERCENTAGE,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import AlphaEmsConfigEntry
from .activity import (
    ActivityEntry,
    ActivityState,
    PlannedRun,
    RunContent,
    RunIdentity,
    direction_of,
    logbook_payload,
    next_activity,
)
from .const import (
    BATTERY_ACTION_OPTIONS,
    BATTERY_KW_PRECISION,
    BATTERY_KWH_PRECISION,
    BATTERY_SOC_PRECISION,
    CONTROL_EXECUTION_AVAILABLE,
    CONTROL_MODE_ACTIVE,
    CONTROL_STATE_OPTIONS,
    DOMAIN,
    ECONOMIC_ACTION_CURTAIL,
    ECONOMIC_ACTION_EXPORT,
    ECONOMIC_ACTION_OPTIONS,
    ECONOMIC_ACTION_SAFETY_BUY,
    ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE,
    ECONOMIC_BLOCKED_MODE_NOT_ACTIVE,
    ECONOMIC_BLOCKED_NO_PRIMITIVE_CURTAIL,
    ECONOMIC_BLOCKED_NO_PRIMITIVE_EXPORT,
    ECONOMIC_BLOCKED_NOT_ENABLED,
    ECONOMIC_EUR_PRECISION,
    ECONOMIC_GAP_NONE,
    FORECAST_ERROR_WINDOW_DAYS,
    NAME,
    SENSOR_BATTERY_PLANNED_POWER,
    SENSOR_BATTERY_RECOMMENDATION,
    SENSOR_BATTERY_USABLE_ENERGY,
    SENSOR_CONTROL_STATE,
    SENSOR_DYNAMIC_RESERVE,
    SENSOR_ECONOMIC_ACTION,
    SENSOR_EXPECTED_LOAD_TODAY,
    SENSOR_EXPECTED_LOAD_TOMORROW,
    SENSOR_FORECAST_ERROR_WINDOW,
    SENSOR_FORECAST_ERROR_YESTERDAY,
    SENSOR_LEARNING_CONFIDENCE,
    SENSOR_LEARNING_DAYS,
)
from .coordinator import AlphaEmsCoordinator
from .economic import EconomicOutcome
from .plan import BatteryPlan
from .reserve import RESERVE_BASIS, shortfall
from .storage import interval_start_utc

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class AlphaEmsSensorDescription(SensorEntityDescription):
    """Describes one Alpha EMS sensor and how to derive it."""

    value_fn: Callable[[AlphaEmsCoordinator], float | int | str | None]
    attributes_fn: Callable[[AlphaEmsCoordinator], dict[str, Any]] | None = None
    #: Optional, and set on exactly one sensor. Given the previous entry, returns
    #: the Activity line this refresh deserves -- or ``None``, which is the
    #: overwhelmingly common answer. Declared on the description rather than
    #: subclassing the entity so the twelve sensors stay one class.
    activity_fn: (
        Callable[[AlphaEmsCoordinator, ActivityState | None], ActivityEntry | None]
        | None
    ) = None


def _round(value: float | None, digits: int = 2) -> float | None:
    """Round a forecast, preserving ``None``."""
    return None if value is None else round(value, digits)


def _today_value(coordinator: AlphaEmsCoordinator) -> float | None:
    """Return today's expected total household consumption."""
    forecast = coordinator.today_forecast
    baseline = (coordinator.data or {}).get("today_baseline")
    if forecast is None or baseline is None or not baseline.available:
        return None
    return _round(forecast.forecast_total_kwh)


def _today_attributes(coordinator: AlphaEmsCoordinator) -> dict[str, Any]:
    """Return the small attribute set for today's forecast."""
    forecast = coordinator.today_forecast
    baseline = (coordinator.data or {}).get("today_baseline")
    confidence = coordinator.confidence
    if forecast is None or baseline is None:
        return {}
    data = coordinator.data or {}
    # The same gate the state uses. Without it an unavailable forecast still
    # published `forecast_total_kwh: 0.0`, so a template reading the attribute
    # got a plausible-looking zero-kWh prediction instead of nothing -- the
    # "learned nothing must never read as zero" failure the storage layer goes to
    # such lengths to avoid, reintroduced one layer up.
    predicted = baseline.available
    return {
        # Baseline: measured household load minus any configured flexible load.
        "actual_so_far_kwh": _round(forecast.actual_so_far_kwh),
        "forecast_remaining_kwh": (
            _round(forecast.forecast_remaining_kwh) if predicted else None
        ),
        "forecast_total_kwh": (
            _round(forecast.forecast_total_kwh) if predicted else None
        ),
        # Measured ground truth, shown alongside so the two never get confused.
        "measured_so_far_kwh": _round(data.get("measured_so_far_kwh")),
        "flexible_load_so_far_kwh": (
            _round(data.get("ev_so_far_kwh")) if coordinator.ev_configured else None
        ),
        "model_days": baseline.source_days,
        "confidence_percent": (
            None if confidence is None else round(confidence.percent, 1)
        ),
        "adaptation_applied": forecast.adapted if predicted else False,
        "adaptation_ratio": round(forecast.adaptation_ratio, 3),
        "day_type": baseline.day_type,
        "intervals_today": baseline.interval_count,
    }


def _tomorrow_value(coordinator: AlphaEmsCoordinator) -> float | None:
    """Return tomorrow's expected total household consumption."""
    forecast = coordinator.tomorrow_forecast
    if forecast is None or not forecast.available:
        return None
    return _round(forecast.total_kwh)


def _tomorrow_attributes(coordinator: AlphaEmsCoordinator) -> dict[str, Any]:
    """Return the small attribute set for tomorrow's forecast."""
    forecast = coordinator.tomorrow_forecast
    confidence = coordinator.confidence
    if forecast is None:
        return {}
    # The same gate today's attributes use. Without it a withheld forecast still
    # published the five look-back windows and a day-type decision, so a
    # template reading the attributes saw a fully described model behind a
    # sensor reading `unknown` -- the two entities disagreeing about the same
    # question in opposite directions.
    predicted = forecast.available
    return {
        "forecast_total_kwh": _round(forecast.total_kwh),
        "model_days": forecast.source_days,
        "day_type": forecast.day_type,
        "day_type_pooled": forecast.day_type_pooled if predicted else None,
        "windows_used_days": list(forecast.windows_used) if predicted else [],
        # 92 / 96 / 100 depending on the target day's daylight-saving shape.
        "intervals_tomorrow": forecast.interval_count,
        "confidence_percent": (
            None if confidence is None else round(confidence.percent, 1)
        ),
    }


def _confidence_value(coordinator: AlphaEmsCoordinator) -> float | None:
    """Return the learning confidence percentage."""
    confidence = coordinator.confidence
    return None if confidence is None else round(confidence.percent, 1)


def _confidence_attributes(coordinator: AlphaEmsCoordinator) -> dict[str, Any]:
    """Return the component breakdown behind the confidence score."""
    confidence = coordinator.confidence
    if confidence is None:
        return {}
    breakdown = confidence.as_dict()
    breakdown.pop("percent", None)
    return breakdown


def _days_value(coordinator: AlphaEmsCoordinator) -> int | None:
    """Return the number of calendar days that count as learned."""
    confidence = coordinator.confidence
    return None if confidence is None else confidence.learned_days


def _days_attributes(coordinator: AlphaEmsCoordinator) -> dict[str, Any]:
    """Return retention and rejection context for the learned-day count."""
    oldest, newest = coordinator.store.span
    return {
        "retained_days": len(coordinator.store.days),
        "retained_intervals": coordinator.store.retained_intervals,
        "history_start": None if oldest is None else oldest.isoformat(),
        "history_end": None if newest is None else newest.isoformat(),
        "rejected_quarters": coordinator.rejected_quarters,
        "flexible_load_configured": coordinator.ev_configured,
        "intervals_without_flexible_data": coordinator.invalid_ev_quarters,
        # ``open_quarter_coverage`` deliberately absent. Attributes are captured
        # when the coordinator writes state, and it only writes at the quarter
        # tick plus five seconds -- by which point the open quarter is five
        # seconds old and its coverage is always about 0.0. The number was true
        # and useless, and reads as a fault. Diagnostics keep it, because that
        # payload is built on demand and the figure means something there.
        "last_rejected_reason": coordinator.last_rejected_reason,
    }


#: Repeated on both Phase-2 sensors, because the single most likely misreading
#: of either number is that it compares whole days against whole days.
_COMPARISON_BASIS: str = (
    "baseline house load (measured minus any configured flexible load), "
    "compared only on intervals where both a prediction and a trustworthy "
    "measurement exist"
)


def _forecast_error_yesterday_value(
    coordinator: AlphaEmsCoordinator,
) -> float | None:
    """Return yesterday's signed day-level forecast error in kWh.

    Positive means the model predicted more than the house went on to use.
    ``None`` whenever there is nothing honest to report: the day was never
    matched, it carries a flag that makes the two sides incomparable, or no
    interval of it resolved. Zero here would mean a perfect forecast, so
    "no data" must never be allowed to render as one.
    """
    facts = (coordinator.data or {}).get("forecast_yesterday_error")
    if not facts:
        return None
    return facts.get("signed_error_kwh")


def _forecast_error_yesterday_attributes(
    coordinator: AlphaEmsCoordinator,
) -> dict[str, Any]:
    """Return the small attribute set behind yesterday's error."""
    facts = (coordinator.data or {}).get("forecast_yesterday_error")
    if not facts:
        return {"comparison_basis": _COMPARISON_BASIS, "intervals_compared": None}
    return {
        "absolute_error_kwh": facts.get("absolute_error_kwh"),
        "error_percent": facts.get("error_percent"),
        "predicted_kwh": facts.get("predicted_kwh"),
        "actual_kwh": facts.get("actual_kwh"),
        "mae_kwh_per_interval": facts.get("mae_kwh_per_interval"),
        # How much of the day could actually be scored. A day with an outage
        # compares fewer intervals, and the totals above are over those
        # intervals only -- never a whole-day prediction against a part-day
        # measurement.
        "intervals_compared": facts.get("intervals_compared"),
        "intervals_in_day": facts.get("intervals_in_day"),
        "horizon_days": facts.get("horizon_days"),
        "comparison_basis": _COMPARISON_BASIS,
    }


def _forecast_error_window_value(coordinator: AlphaEmsCoordinator) -> float | None:
    """Return the rolling weighted absolute percentage error.

    ``sum(abs(predicted - actual)) / sum(actual)`` over the window, as a
    percentage. Deliberately not an accuracy figure: ``100 - error`` is
    unbounded below and would read as a score rather than a measurement.
    """
    window = (coordinator.data or {}).get("forecast_error_window")
    if window is None:
        return None
    return None if window.wape_percent is None else round(window.wape_percent, 1)


def _forecast_error_window_attributes(
    coordinator: AlphaEmsCoordinator,
) -> dict[str, Any]:
    """Return the derivation behind the rolling error figure."""
    window = (coordinator.data or {}).get("forecast_error_window")
    if window is None:
        return {"window_days": FORECAST_ERROR_WINDOW_DAYS}
    return {
        "window_days": FORECAST_ERROR_WINDOW_DAYS,
        "days_compared": window.days_compared,
        "intervals_compared": window.intervals_compared,
        "mae_kwh_per_interval": _round(window.mae_kwh, 5),
        # Signed, so a persistent over- or under-prediction stays visible
        # instead of being averaged away by the absolute figure above.
        "bias_kwh_per_interval": _round(window.bias_kwh, 5),
        "predicted_kwh": _round(window.predicted_kwh, 3),
        "actual_kwh": _round(window.actual_kwh, 3),
        "comparison_basis": _COMPARISON_BASIS,
    }


# -- Phase 3: the battery decision -------------------------------------------

#: Repeated on all three battery sensors, because the single most likely
#: misreading of any of them is that something acts on it.
_SHADOW_BASIS: str = (
    "advisory only: Phase 3 issues no command to the battery and nothing "
    "executes this plan"
)


def _plan(coordinator: AlphaEmsCoordinator) -> BatteryPlan | None:
    """Return this refresh's battery plan, or ``None``."""
    return (coordinator.data or {}).get("battery_plan")


def _recommendation_value(coordinator: AlphaEmsCoordinator) -> str | None:
    """Return the recommended action, or ``None`` when none could be reached.

    ``None`` -- which Home Assistant renders as ``unknown`` -- is deliberate for
    the no-decision case. Internally that case is ``ACTION_NO_DECISION`` and is
    kept distinct from a deliberate hold, because "hold because that is best" and
    "hold because a hardware fact is missing" are different facts and a later
    phase needs them apart. Publishing it as a fourth state would make ``unknown``
    mean two things at once, and every other sensor here already uses ``unknown``
    for "nothing honest to report".
    """
    plan = _plan(coordinator)
    if plan is None or not plan.decision.decided:
        return None
    return plan.decision.action


def _recommendation_attributes(coordinator: AlphaEmsCoordinator) -> dict[str, Any]:
    """Return why the recommendation is what it is.

    The reason survives a non-decision: an entity reading ``unknown`` with no
    explanation is the thing a maintainer cannot act on.
    """
    plan = _plan(coordinator)
    if plan is None:
        return {"reason": None, "basis": _SHADOW_BASIS}
    decision = plan.decision
    state = plan.state
    return {
        "reason": decision.reason,
        "planned_power_kw": (
            _round(decision.published_power_kw, BATTERY_KW_PRECISION)
            if decision.decided
            else None
        ),
        "usable_energy_kwh": _round(plan.usable_energy_kwh, BATTERY_KWH_PRECISION),
        "battery_soc_percent": (
            None if state is None else _round(state.soc_percent, BATTERY_SOC_PRECISION)
        ),
        # Both floors, always. The configured one is what the user set and what
        # the simulator refuses to cross; the effective one is what the policy
        # aims at. They are equal in this phase, and publishing both is what lets
        # a user see immediately if anything has raised their floor.
        "configured_min_soc_percent": plan.reserve.configured_min_soc_percent,
        "effective_min_soc_percent": plan.reserve.effective_min_soc_percent,
        "constraints": list(decision.constraints),
        # The one fact a user needs in order to read this recommendation, and the
        # only one that cannot be folded into the prose beside it: prose cannot be
        # automated against. False means the sun is not in this figure at all, so
        # a sunny midday recommendation is asking the battery to supply energy the
        # array is already supplying.
        "pv_aware": (plan.candidate is not None and plan.candidate.pv_aware),
        "basis": _SHADOW_BASIS,
    }


def _planned_power_value(coordinator: AlphaEmsCoordinator) -> float | None:
    """Return the planned battery power in kW, positive for charging.

    The only signed battery power in Phase 3, and signed only here. Internally a
    request carries a direction and a non-negative magnitude, which is what makes
    a negative discharge -- which would *add* energy -- unrepresentable.

    This sign convention is the plan's own. It has nothing to do with the
    configured ``battery_power_sign``, which describes how the user's own sensor
    reports and is resolved away long before this point.
    """
    plan = _plan(coordinator)
    if plan is None or not plan.decision.decided:
        return None
    return _round(plan.decision.published_power_kw, BATTERY_KW_PRECISION)


def _planned_power_attributes(coordinator: AlphaEmsCoordinator) -> dict[str, Any]:
    """Return what was asked for and what the limits allowed."""
    plan = _plan(coordinator)
    if plan is None:
        return {"basis": _PLANNED_POWER_BASIS, "sign_convention": _SIGN_CONVENTION}
    decision = plan.decision
    return {
        "requested_mode": decision.request.mode,
        "requested_power_kw": _round(decision.request.power_kw, BATTERY_KW_PRECISION),
        "allowed_energy_kwh": (
            _round(decision.allowed_energy_ac_kwh, BATTERY_KWH_PRECISION)
            if decision.decided
            else None
        ),
        "limiting_constraints": list(decision.constraints),
        "policy": decision.policy,
        "policy_version": decision.policy_version,
        "sign_convention": _SIGN_CONVENTION,
        "basis": _PLANNED_POWER_BASIS,
    }


def _usable_energy_value(coordinator: AlphaEmsCoordinator) -> float | None:
    """Return the AC energy available above the reserve, in kWh.

    Needs no forecast, so this survives a young installation where the forecast
    is still withheld -- which is why it is published and a projected state of
    charge is not.
    """
    plan = _plan(coordinator)
    if plan is None:
        return None
    return _round(plan.usable_energy_kwh, BATTERY_KWH_PRECISION)


def _usable_energy_attributes(coordinator: AlphaEmsCoordinator) -> dict[str, Any]:
    """Return the derivation behind the usable figure."""
    plan = _plan(coordinator)
    if plan is None:
        return {"basis": _USABLE_ENERGY_BASIS}
    state = plan.state
    return {
        "battery_soc_percent": (
            None if state is None else _round(state.soc_percent, BATTERY_SOC_PRECISION)
        ),
        "capacity_kwh": plan.inputs.capacity_kwh,
        "stored_energy_kwh": (
            None if state is None else _round(state.energy_kwh, BATTERY_KWH_PRECISION)
        ),
        "configured_min_soc_percent": plan.reserve.configured_min_soc_percent,
        "effective_min_soc_percent": plan.reserve.effective_min_soc_percent,
        "reserve_source": plan.reserve.source,
        # ``None`` without a forecast rather than taking the entity with it.
        "coverage_hours": _round(plan.coverage_hours, 2),
        "basis": _USABLE_ENERGY_BASIS,
    }


def _dynamic_reserve_value(coordinator: AlphaEmsCoordinator) -> float | None:
    """Return the calculated requirement, in DC kWh.

    An **energy**, because that is the quantity the model conserves and the one a
    person compares against ``Usable Battery Energy``. The state of charge it
    implies is derived and travels as an attribute, never the other way round.

    ``None`` -- published as ``unknown`` -- whenever the recursion could not
    reach the present: no forecast, an unforecast interval inside the horizon, or
    no usable battery configuration. Never a fabricated zero, which would read as
    "nothing is needed" rather than "nothing is known".
    """
    plan = _plan(coordinator)
    if plan is None or plan.reserve_projection is None:
        return None
    return _round(plan.reserve_projection.required_now_dc_kwh, BATTERY_KWH_PRECISION)


def _economic_outcome(coordinator: AlphaEmsCoordinator) -> EconomicOutcome | None:
    """Return this refresh's economic plan, or ``None``."""
    outcome = (coordinator.data or {}).get("economic")
    return outcome if isinstance(outcome, EconomicOutcome) else None


def _economic_action_value(coordinator: AlphaEmsCoordinator) -> str | None:
    """Return the action the optimizer economically wants.

    The **desired** action, not what would be executed -- and this release
    executes nothing at all. What implemented actuators could achieve is the
    ``capability_action`` attribute beside it, and whether anything is sent is
    ``execution_blocked_reason``, which reads ``execution_unavailable`` on every
    reading while the release barrier stands.

    ``unknown`` rather than ``hold`` when no plan could be built: "nothing is
    worth doing" and "nothing could be worked out" are different answers, and a
    reassuring ``hold`` derived from absent prices would be the second wearing the
    first's clothes.
    """
    outcome = _economic_outcome(coordinator)
    if outcome is None or not outcome.available:
        return None
    action = outcome.action
    return action if action in ECONOMIC_ACTION_OPTIONS else None


def _economic_action_attributes(coordinator: AlphaEmsCoordinator) -> dict[str, Any]:
    """Return the eight flat values behind the economic action.

    Eight, which is the cap, and chosen to answer one question each: what would
    implemented actuators do, why can nothing be sent, what would it do *now* and
    at what power, over how much energy and when, why, at what price, and for
    what gain. Everything else -- the capability plan's own totals, the per-run
    detail, the counterfactuals, the solver figures and the provenance -- is in
    diagnostics.

    ``power_kw`` is the **first actionable interval's** power, never the run
    average. A multi-interval run varies with load, production, headroom, the
    reserve trajectory and the clamp, so an average would describe none of its
    intervals; the average is published per run in diagnostics where it belongs.
    """
    outcome = _economic_outcome(coordinator)
    if outcome is None or not outcome.available:
        return {"execution_blocked_reason": ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE}
    run = outcome.desired.published_run
    return {
        "capability_action": outcome.capability_action,
        "execution_blocked_reason": _economic_blocked_reason(coordinator),
        "power_kw": _round(outcome.desired.power_kw, BATTERY_KW_PRECISION),
        "energy_kwh": _round(
            0.0 if run is None else run.energy_kwh, BATTERY_KWH_PRECISION
        ),
        "window": _economic_window(coordinator, run),
        "reason": outcome.reason,
        "price_eur_kwh": outcome.price_eur_kwh,
        "expected_net_value_eur": _round(
            outcome.desired.expected_net_value_eur, ECONOMIC_EUR_PRECISION
        ),
    }


def _planned_runs(coordinator: AlphaEmsCoordinator) -> tuple[PlannedRun, ...]:
    """Return every planned run with its instants resolved to absolute time.

    This is the boundary where the calendar lives. The optimizer indexes a
    continuous chronological run through today and on into tomorrow; only here is
    the civil day and its real length -- 92, 96 or 100 intervals -- available to
    turn an index into an instant. Handing ``activity`` plain instants is what
    lets the whole announcement policy be exercised against values, with no plan
    and no clock.

    Every run is offered, not just the published one. The announcement policy
    decides which is imminent; that is not a decision this function should
    pre-empt by only showing it the first.
    """
    outcome = _economic_outcome(coordinator)
    if outcome is None or not outcome.available:
        return ()
    plan = _plan(coordinator)
    if plan is None or plan.target_day is None or plan.forecast is None:
        return ()
    count = _today_interval_count(plan)
    if count <= 0:
        return ()

    tz = dt_util.get_default_time_zone()
    refused = outcome.capability_gap_reason != ECONOMIC_GAP_NONE
    runs: list[PlannedRun] = []
    for run in outcome.desired.runs:
        start = _economic_instant(plan.target_day, run.start_index, count, tz)
        end = _economic_instant(plan.target_day, run.end_index + 1, count, tz)
        if start is None or end is None:
            continue
        action = (
            ECONOMIC_ACTION_SAFETY_BUY
            if run.start_index in outcome.safety_buy_runs
            else run.action
        )
        runs.append(
            PlannedRun(
                identity=RunIdentity(
                    direction=direction_of(action),
                    start_utc=dt_util.as_utc(start),
                ),
                content=RunContent(
                    action=action,
                    capability_action=outcome.capability_action,
                    reason=outcome.reason,
                    energy_kwh=run.energy_kwh,
                    power_kw=run.first_power_kw,
                    end_utc=dt_util.as_utc(end),
                    charge_source=run.charge_source,
                    price_eur_kwh=run.average_price_eur_kwh,
                    value_eur=-run.marginal_cost_eur,
                    refused=refused,
                    window=f"{start:%H:%M}-{end:%H:%M}",
                ),
            )
        )
    return tuple(runs)


def _economic_activity(
    coordinator: AlphaEmsCoordinator, previous: ActivityState | None
) -> ActivityEntry | None:
    """Return the Activity line this refresh deserves, or ``None`` for silence.

    ``value_eur`` is the run's **marginal** cost, sign-flipped -- what it saved
    against leaving the battery alone through the same intervals. Not the raw cash
    flow, which is negative for every charge by construction and zero for the most
    valuable discharge there is.

    ``now`` is the instant the coordinator published, not a fresh clock reading.
    The announcement must describe the same moment the plan does, or a run could
    be judged imminent against one clock and rendered against another.
    """
    issued = (coordinator.data or {}).get("issued_at")
    if issued is None:
        return None
    return next_activity(
        previous=previous,
        runs=_planned_runs(coordinator),
        now=dt_util.as_utc(issued),
    )


def _economic_blocked_reason(coordinator: AlphaEmsCoordinator) -> str:
    """Return why nothing is sent, most fundamental reason first.

    The global barrier wins. While ``CONTROL_EXECUTION_AVAILABLE`` is false this
    is the only value the field can take, and that is the point: no per-action
    reason may mask the fact that the release sends nothing.
    """
    if not CONTROL_EXECUTION_AVAILABLE:
        return ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE
    if not coordinator.config.control_execution_enabled:  # pragma: no cover
        return ECONOMIC_BLOCKED_NOT_ENABLED
    if coordinator.control_mode != CONTROL_MODE_ACTIVE:  # pragma: no cover
        return ECONOMIC_BLOCKED_MODE_NOT_ACTIVE
    outcome = _economic_outcome(coordinator)  # pragma: no cover
    if outcome is not None:  # pragma: no cover
        if outcome.action == ECONOMIC_ACTION_EXPORT:
            return ECONOMIC_BLOCKED_NO_PRIMITIVE_EXPORT
        if outcome.action == ECONOMIC_ACTION_CURTAIL:
            return ECONOMIC_BLOCKED_NO_PRIMITIVE_CURTAIL
    return ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE  # pragma: no cover


def _economic_window(coordinator: AlphaEmsCoordinator, run: Any) -> str | None:
    """Return the planned run's local clock window as one string.

    One attribute rather than two, because a start without an end says nothing
    useful and the pair costs a slot the capability action needs more.
    """
    if run is None:
        return None
    plan = _plan(coordinator)
    if plan is None or plan.target_day is None:
        return None
    tz = dt_util.get_default_time_zone()
    count = 0 if plan.forecast is None else _today_interval_count(plan)
    start = _economic_instant(plan.target_day, run.start_index, count, tz)
    end = _economic_instant(plan.target_day, run.end_index + 1, count, tz)
    if start is None or end is None:
        return None
    return f"{start:%H:%M}-{end:%H:%M}"


def _today_interval_count(plan: BatteryPlan) -> int:
    """Return today's real interval count, as the plan recorded it."""
    today = plan.forecast.get("today") or {}
    count = today.get("interval_count")
    return count if isinstance(count, int) else 0


def _economic_instant(day: Any, index: int, today_count: int, tz: Any) -> Any:
    """Return the local instant a chronological index begins at.

    The plan indexes a continuous run through today and on into tomorrow, so an
    index at or beyond today's real length names an interval of tomorrow -- and
    the length is 92, 96 or 100 depending on the civil day, which is why it is
    read rather than assumed.
    """
    if today_count <= 0:
        return None
    if index < today_count:
        return dt_util.as_local(interval_start_utc(day, index, tz))
    return dt_util.as_local(
        interval_start_utc(day + timedelta(days=1), index - today_count, tz)
    )


def _dynamic_reserve_attributes(coordinator: AlphaEmsCoordinator) -> dict[str, Any]:
    """Return the eight flat values behind the requirement.

    Eight, which is the cap. The counterfactuals this figure is bracketed by, the
    peak, the per-interval detail, the constraint tallies and the provenance are
    all in diagnostics: a requirement with six ways of being wrong has no
    business unpacking all six into an entity.

    ``replenishment_dependency_kwh`` earns its slot because it is the one number
    a user can act on -- how much of the reduction rests on forecast sunshine
    arriving. On a sunny midday it is large, and that is the signal that the
    figure beside it is optimistic.
    """
    plan = _plan(coordinator)
    if plan is None or plan.reserve_projection is None:
        return {"basis": RESERVE_BASIS}
    projection = plan.reserve_projection
    required = projection.required_now_dc_kwh
    same = (
        None
        if plan.reserve_same_interval_only is None
        else plan.reserve_same_interval_only.required_now_dc_kwh
    )
    return {
        "required_reserve_soc_percent": _round(
            projection.required_now_soc_percent, BATTERY_SOC_PRECISION
        ),
        "configured_min_soc_percent": plan.reserve.configured_min_soc_percent,
        "reserve_shortfall_kwh": shortfall(projection, plan.state)[
            "reserve_shortfall_kwh"
        ],
        "reserve_reachable": projection.reachable,
        "replenishment_dependency_kwh": (
            None
            if same is None or required is None
            else _round(same - required, BATTERY_KWH_PRECISION)
        ),
        "lower_bound_reason": projection.lower_bound_reason,
        "intervals_evaluated": projection.intervals_evaluated,
        "basis": RESERVE_BASIS,
    }


_SIGN_CONVENTION: str = (
    "positive is energy into the battery; this is the plan's own convention and "
    "is unrelated to the configured battery power sign, which describes the "
    "source sensor only"
)

_PLANNED_POWER_BASIS: str = (
    "interval-average AC power over the quarter-hour, after every hardware "
    "limit; not an instantaneous inverter setpoint. Advisory only: nothing "
    "executes it"
)

_USABLE_ENERGY_BASIS: str = (
    "AC energy deliverable above the reserve, one conversion below the stored "
    "DC energy. An upper bound: a single efficiency figure flatters a real "
    "inverter at low power and inverter auxiliary draw is not modelled. "
    "Advisory only: nothing executes this plan"
)


_CONTROL_STATE_BASIS: str = (
    "what the control pipeline made of this interval's recommendation. "
    "'inhibited' means the safety gate refused; 'eligible' means it did not and "
    "only the execution barrier stopped a command; 'idle' means there was "
    "nothing to send. Nothing executes: this release cannot command the inverter"
)


def _control_state_value(coordinator: AlphaEmsCoordinator) -> str | None:
    """Return what the control pipeline decided this refresh."""
    report = coordinator.control_report
    if report is None:
        return None
    state = report.get("state")
    return state if state in CONTROL_STATE_OPTIONS else None


def _control_state_attributes(
    coordinator: AlphaEmsCoordinator,
) -> dict[str, Any]:
    """Return the small flat attribute set for the control state.

    Eight flat values, no mappings. Everything the pipeline computed -- the
    capability report, the read-back snapshot, the full intent, the quantised
    command, the ordered command list, the event trail -- is in diagnostics. A
    gate with twenty-five possible reasons has no business unpacking itself into
    an entity's attributes.
    """
    report = coordinator.control_report
    if report is None:
        return {"basis": _CONTROL_STATE_BASIS}

    safety = report.get("safety") or {}
    authorization = report.get("authorization") or {}
    command = report.get("command") or {}
    capability = report.get("capability") or {}
    device = report.get("device") or {}
    return {
        "inhibit_reason": safety.get("inhibit_reason"),
        "authorization_refusal": authorization.get("refusal"),
        "action": command.get("action"),
        "device_power_kw": command.get("power_kw"),
        "commands_planned": report.get("commands_planned", 0),
        "capability_ready": capability.get("ready"),
        "dispatch_active": device.get("dispatch_active"),
        "basis": _CONTROL_STATE_BASIS,
    }


SENSORS: tuple[AlphaEmsSensorDescription, ...] = (
    AlphaEmsSensorDescription(
        key=SENSOR_EXPECTED_LOAD_TODAY,
        name="Expected House Load Today",
        icon="mdi:home-lightning-bolt",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        # No state_class: this is a forecast, and long-term statistics or an
        # Energy dashboard entry built on a prediction would be misleading.
        value_fn=_today_value,
        attributes_fn=_today_attributes,
    ),
    AlphaEmsSensorDescription(
        key=SENSOR_EXPECTED_LOAD_TOMORROW,
        name="Expected House Load Tomorrow",
        icon="mdi:home-clock",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=_tomorrow_value,
        attributes_fn=_tomorrow_attributes,
    ),
    AlphaEmsSensorDescription(
        key=SENSOR_LEARNING_CONFIDENCE,
        name="Learning Confidence",
        icon="mdi:gauge",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_confidence_value,
        attributes_fn=_confidence_attributes,
    ),
    AlphaEmsSensorDescription(
        key=SENSOR_LEARNING_DAYS,
        name="Learning Days",
        icon="mdi:calendar-check",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_days_value,
        attributes_fn=_days_attributes,
    ),
    AlphaEmsSensorDescription(
        key=SENSOR_FORECAST_ERROR_YESTERDAY,
        name="Forecast Error Yesterday",
        icon="mdi:delta",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        # No device class: this is a signed difference, not consumption, and
        # SensorDeviceClass.ENERGY would offer it to the Energy dashboard.
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_forecast_error_yesterday_value,
        attributes_fn=_forecast_error_yesterday_attributes,
    ),
    AlphaEmsSensorDescription(
        key=SENSOR_FORECAST_ERROR_WINDOW,
        name="Forecast Error 7 Days",
        icon="mdi:chart-timeline-variant",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_forecast_error_window_value,
        attributes_fn=_forecast_error_window_attributes,
    ),
    AlphaEmsSensorDescription(
        key=SENSOR_BATTERY_RECOMMENDATION,
        name="Battery Recommendation",
        icon="mdi:battery-heart-variant",
        # An enum, so Home Assistant renders and records it as one rather than as
        # free text. No state class: a device class of ENUM permits none, and a
        # long-term statistic over a categorical value would mean nothing anyway.
        device_class=SensorDeviceClass.ENUM,
        options=list(BATTERY_ACTION_OPTIONS),
        value_fn=_recommendation_value,
        attributes_fn=_recommendation_attributes,
    ),
    AlphaEmsSensorDescription(
        key=SENSOR_BATTERY_PLANNED_POWER,
        name="Planned Battery Power",
        icon="mdi:transmission-tower",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        # No device class, for the same reason ``Forecast Error Yesterday`` has
        # none: this is a signed quantity, and SensorDeviceClass.POWER would
        # invite it into dashboards that assume a physical measurement rather
        # than an intention.
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_planned_power_value,
        attributes_fn=_planned_power_attributes,
    ),
    AlphaEmsSensorDescription(
        key=SENSOR_BATTERY_USABLE_ENERGY,
        name="Usable Battery Energy",
        icon="mdi:battery-charging-medium",
        # Stored energy, which is exactly what ENERGY_STORAGE describes -- and
        # deliberately not ENERGY, which would offer a reserve figure to the
        # Energy dashboard as though it were consumption.
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_usable_energy_value,
        attributes_fn=_usable_energy_attributes,
    ),
    AlphaEmsSensorDescription(
        key=SENSOR_DYNAMIC_RESERVE,
        name="Dynamic Battery Reserve",
        icon="mdi:battery-lock",
        # A stored-energy level, exactly like Usable Battery Energy, and
        # deliberately not ENERGY: a requirement is not consumption and has no
        # business on the Energy dashboard.
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_dynamic_reserve_value,
        attributes_fn=_dynamic_reserve_attributes,
    ),
    AlphaEmsSensorDescription(
        key=SENSOR_ECONOMIC_ACTION,
        name="Economic Action",
        icon="mdi:cash-clock",
        # An enum, like Battery Recommendation and Control State, and for the
        # same reason: it is categorical, so a long-term statistic over it would
        # mean nothing.
        device_class=SensorDeviceClass.ENUM,
        options=list(ECONOMIC_ACTION_OPTIONS),
        value_fn=_economic_action_value,
        attributes_fn=_economic_action_attributes,
        activity_fn=_economic_activity,
    ),
    AlphaEmsSensorDescription(
        key=SENSOR_CONTROL_STATE,
        name="Control State",
        icon="mdi:shield-check-outline",
        # An enum, like the battery recommendation, and for the same reason: it
        # is categorical, so Home Assistant should record it as one. No state
        # class, because a long-term statistic over a category means nothing.
        device_class=SensorDeviceClass.ENUM,
        options=list(CONTROL_STATE_OPTIONS),
        value_fn=_control_state_value,
        attributes_fn=_control_state_attributes,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AlphaEmsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Alpha EMS sensors."""
    coordinator: AlphaEmsCoordinator = entry.runtime_data
    async_add_entities(
        AlphaEmsSensor(coordinator, description) for description in SENSORS
    )


class AlphaEmsSensor(CoordinatorEntity[AlphaEmsCoordinator], SensorEntity):
    """A coordinator-backed Alpha EMS sensor."""

    _attr_has_entity_name = True
    entity_description: AlphaEmsSensorDescription

    def __init__(
        self,
        coordinator: AlphaEmsCoordinator,
        description: AlphaEmsSensorDescription,
    ) -> None:
        """Bind the sensor to its coordinator and description."""
        super().__init__(coordinator)
        self.entity_description = description
        # Reset by a reload, which costs one redundant "planned" line and buys
        # not persisting a logbook cursor. The alternative -- storing it -- would
        # make an observational surface a thing that can be restored wrong.
        self._activity: ActivityState | None = None
        entry = coordinator.entry
        # Config-entry scoped, so two Alpha EMS instances never collide.
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Alpha EMS",
            model=NAME,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> float | int | str | None:
        """Return the current sensor value."""
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the sensor's small attribute set."""
        if self.entity_description.attributes_fn is None:
            return {}
        return self.entity_description.attributes_fn(self.coordinator)

    async def async_added_to_hass(self) -> None:
        """Subscribe, then file the opening Activity line if one is due.

        Here as well as on every update, because a plan that already exists at
        startup would otherwise go unrecorded until it next changed -- and the
        line a user most wants after a restart is the one saying what the
        integration currently intends.
        """
        await super().async_added_to_hass()
        self._file_activity()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Write the new state, and file an Activity line if one is due."""
        self._file_activity()
        super()._handle_coordinator_update()

    @callback
    def _file_activity(self) -> None:
        """Fire one logbook entry, or nothing.

        Wrapped, because Activity is decoration. A refresh that cannot render a
        sentence must still publish its state: the entry is what is lost, never
        the figure.
        """
        activity_fn = self.entity_description.activity_fn
        if activity_fn is None:
            return
        try:
            entry = activity_fn(self.coordinator, self._activity)
            if entry is None:
                return
            payload = logbook_payload(entry, domain=DOMAIN, entity_id=self.entity_id)
        except Exception:
            _LOGGER.debug("activity entry could not be built", exc_info=True)
            return
        # Advanced only once the payload is built, so a failure retries rather
        # than swallowing the transition it failed on.
        self._activity = entry.state
        self.hass.bus.async_fire(EVENT_LOGBOOK_ENTRY, payload)
