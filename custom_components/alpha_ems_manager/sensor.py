"""Sensor platform for Alpha EMS Manager.

Seventeen sensors: the four Phase-1 forecast and learning ones, the two Phase-2
forecast-error ones, the three Phase-3 battery ones, the single Phase-4 control
state, the single Phase-7 dynamic reserve, and the six Phase-8 ones -- the economic
action, the next planned action, the economic value, and beta.42's battery return
and two campaign lifecycle rows. (The eighteenth entity in the contract is the
control-mode select, which lives on another platform.)

Every other quantity the integration computes -- per-slot profiles, window means,
balance residuals, coverage statistics, per-horizon error breakdowns, the snapshot
inventory, the simulated battery trajectory, the per-interval reserve curve, the
economic counterfactuals and every planned run -- is available through diagnostics
instead. Ninety-six quarter sensors and five window averages would be technically
easy and practically awful.

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
    CURRENCY_EURO,
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
    ExecutionView,
    PlannedRun,
    RunContent,
    RunIdentity,
    TerminalView,
    category_of,
    direction_of,
    logbook_payload,
    next_activity,
)
from .const import (
    BATTERY_ACTION_OPTIONS,
    BATTERY_KW_PRECISION,
    BATTERY_KWH_PRECISION,
    BATTERY_SOC_PRECISION,
    CAMPAIGN_OUTCOMES,
    CAMPAIGN_STATE_CREATED,
    CAMPAIGN_STATE_IDLE,
    CAMPAIGN_STATE_OPTIONS,
    CAMPAIGN_STATE_STARTED,
    CONTROL_MODE_ACTIVE,
    CONTROL_STATE_OPTIONS,
    DOMAIN,
    ECONOMIC_ACTION_CHARGE,
    ECONOMIC_ACTION_EXPORT,
    ECONOMIC_ACTION_IDLE,
    ECONOMIC_ACTION_MIXED_BUY,
    ECONOMIC_ACTION_OPTIONS,
    ECONOMIC_ACTION_SAFETY_BUY,
    ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE,
    ECONOMIC_DIRECTION_CHARGE,
    ECONOMIC_EUR_PRECISION,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_NET_EXPORT,
    EXECUTION_STATE_ARMED,
    EXECUTION_STATE_PREPARED,
    EXECUTION_STATE_RUNNING,
    FORECAST_ERROR_WINDOW_DAYS,
    LEDGER_BASIS_UNCLASSIFIED,
    LIFECYCLE_KIND_CREATED,
    LIFECYCLE_KIND_STARTED,
    NAME,
    OWNERSHIP_OWNED,
    SENSOR_BATTERY_PLANNED_POWER,
    SENSOR_BATTERY_RECOMMENDATION,
    SENSOR_BATTERY_ROI,
    SENSOR_BATTERY_USABLE_ENERGY,
    SENSOR_CONTROL_STATE,
    SENSOR_CURRENT_CAMPAIGN,
    SENSOR_DYNAMIC_RESERVE,
    SENSOR_ECONOMIC_ACTION,
    SENSOR_ECONOMIC_VALUE,
    SENSOR_EXPECTED_LOAD_TODAY,
    SENSOR_EXPECTED_LOAD_TOMORROW,
    SENSOR_FORECAST_ERROR_WINDOW,
    SENSOR_FORECAST_ERROR_YESTERDAY,
    SENSOR_LAST_CAMPAIGN_RESULT,
    SENSOR_LEARNING_CONFIDENCE,
    SENSOR_LEARNING_DAYS,
    SENSOR_NEXT_PLANNED_ACTION,
)
from .coordinator import AlphaEmsCoordinator
from .economic import IMPLEMENTED_ACTIONS, EconomicOutcome
from .plan import BatteryPlan
from .realized import _basis_map
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


def _executing_view(coordinator: AlphaEmsCoordinator) -> dict[str, Any]:
    """Return what Stage B is acting on right now, or an empty mapping.

    **The layer that can answer the question, since beta.35.** Stage A cannot: its
    horizon head is ``elapsed + 1``, so the plan describes the *next* interval and
    structurally never the one in progress. beta.34 made ``Economic Action``
    present-tense on the plan, which moved the fault rather than fixing it -- on
    2026-08-29 a real owned 10 kW export was running and the entity read ``idle``,
    because no planned run happened to start at the head.

    What is executing is a Stage-B fact, and this reads it from the control report.

    **The open row is asked first, and the carried run last.** That order is the
    beta.29 authority model and getting it backwards is why a first draft of this
    still read ``idle`` through a live Sell: under quarter authority the carried
    run is ``None``, so ``execution.intent``, ``execution.target`` and
    ``execution.power`` are all ``None`` too, and every field this entity wants
    lives in the executing quarter and the admitted schedule instead. Those are
    the surfaces that describe what is running; the run block describes what Stage
    A is carrying, which is a different question and was empty at the exact moment
    the answer mattered.
    """
    control = (coordinator.data or {}).get("control") or {}
    report = control.get("execution") or {}
    if not report:
        return {}
    quarter = report.get("quarter") or {}
    admitted = report.get("admitted_plan") or {}
    target = report.get("target") or {}
    intent = quarter.get("intent") or admitted.get("intent") or report.get("intent")
    if not isinstance(intent, str) or not intent:
        return {}
    ownership = report.get("ownership") or {}
    progress = report.get("progress") or {}
    campaign = report.get("open_campaign") or {}
    carried = (report.get("carried") or {}).get("run") or {}
    controller = report.get("controller") or {}
    export = intent == EXECUTION_INTENT_NET_EXPORT
    planned = campaign.get("frozen_target_kwh") if isinstance(campaign, dict) else None
    if planned is None:
        planned = (
            target.get("grid_target_kwh")
            if export
            else target.get("battery_target_kwh")
        )
    if planned is None:
        planned = (
            quarter.get("grid_target_this_quarter_kwh")
            if export
            else quarter.get("battery_target_this_quarter_kwh")
        )
    return {
        "intent": intent,
        "purpose": (
            report.get("purpose") or admitted.get("purpose") or target.get("purpose")
        ),
        "owned": ownership.get("state") == OWNERSHIP_OWNED,
        "mode": control.get("mode"),
        "state": report.get("state"),
        "campaign_id": (
            campaign.get("campaign_id") if isinstance(campaign, dict) else None
        ),
        "run_id": (
            carried.get("run_id") or admitted.get("run_id") or report.get("plan_id")
        ),
        "planned_kwh": planned,
        "realised_kwh": progress.get("objective_realized_kwh"),
        "power_kw": (
            (report.get("power") or {}).get("applied_kw")
            or controller.get("applied_setpoint_kw")
        ),
        "started_at": (
            campaign.get("started_at") if isinstance(campaign, dict) else None
        ),
    }


def _executing_action(view: dict[str, Any]) -> str:
    """Return the economic word for what is executing, or ``idle``.

    ``safety_buy`` comes from the published target's own ``purpose``, which
    ``execution_target`` already sets from the solve's safety attribution -- so the
    entity and the diagnostics cannot disagree about which purchases were
    compelled.

    **``mixed_buy`` reaches the entity the same way, since beta.39.** A charge
    campaign with a compulsory *and* a discretionary component read as
    ``safety_buy`` here, so the 7.22 discretionary kilowatt-hours of the live
    8.06 kWh campaign were presented to the user as compelled survival energy.
    Relayed rather than re-derived: the split is the solve's own attribution and
    this reads the word it produced.
    """
    if not view:
        return ECONOMIC_ACTION_IDLE
    purpose = view.get("purpose")
    if purpose in (ECONOMIC_ACTION_SAFETY_BUY, ECONOMIC_ACTION_MIXED_BUY):
        return purpose
    intent = view.get("intent")
    if intent == EXECUTION_INTENT_GRID_CHARGE:
        return ECONOMIC_ACTION_CHARGE
    if intent == EXECUTION_INTENT_NET_EXPORT:
        return ECONOMIC_ACTION_EXPORT
    # ``serve_load`` and everything else: natural inverter self-consumption is not
    # an economic execution, and never was.
    return ECONOMIC_ACTION_IDLE


def _economic_action_value(coordinator: AlphaEmsCoordinator) -> str | None:
    """Return what Alpha EMS is economically executing **now**.

    **beta.35 moved this off the plan and onto the execution surface, because the
    plan cannot see the present.** The horizon head is ``elapsed + 1``; the quarter
    in progress is behind it by construction. beta.34's ``current_run`` therefore
    reported a run that *starts* at the head -- which has not started, it begins in
    up to fifteen minutes -- and reported ``idle`` for a sale that was physically
    exporting 8.7 kW at that very moment.

    In **Live** this is what the inverter is doing under an owned dispatch. In
    **Shadow** it is the intent Stage B is acting on and would have sent, published
    with ``owned: false`` and ``mode: shadow`` beside it: Shadow exists to be
    watched, and an entity that reads ``idle`` all day cannot be watched. In
    **Off**, nothing is admitted, so ``idle``.

    ``unknown`` only when no plan could be built at all -- "nothing is happening"
    and "nothing could be worked out" are different answers.
    """
    outcome = _economic_outcome(coordinator)
    if outcome is None or not outcome.available:
        return None
    action = _executing_action(_executing_view(coordinator))
    return action if action in ECONOMIC_ACTION_OPTIONS else None


def _economic_action_attributes(coordinator: AlphaEmsCoordinator) -> dict[str, Any]:
    """Return the facts behind what is executing. **The same execution.**

    beta.34 left this reading ``published_run`` while the state read the plan's
    current run, so on the live capture the state said ``idle`` while ``window``,
    ``energy_kwh``, ``price_eur_kwh`` and ``reason`` all described a sale planned
    for 21:00. An entity whose state and attributes describe different things is
    worse than one that describes neither.

    Every field here now comes from the execution view the state came from.
    """
    outcome = _economic_outcome(coordinator)
    if outcome is None or not outcome.available:
        return {"execution_blocked_reason": ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE}
    view = _executing_view(coordinator)
    return {
        "owned": bool(view.get("owned")),
        "mode": view.get("mode"),
        "purpose": view.get("purpose"),
        "campaign_id": view.get("campaign_id"),
        "run_id": view.get("run_id"),
        "planned_kwh": _round(view.get("planned_kwh"), BATTERY_KWH_PRECISION),
        "realised_kwh": _round(view.get("realised_kwh"), BATTERY_KWH_PRECISION),
        "power_kw": _round(view.get("power_kw"), BATTERY_KW_PRECISION),
        "started_at": view.get("started_at"),
        "capability_action": outcome.capability_action,
        "execution_blocked_reason": _economic_blocked_reason(coordinator),
    }


def _economic_run_instant(coordinator: AlphaEmsCoordinator, index: int) -> Any:
    """Return the local instant a horizon index opens at, or ``None``."""
    plan = _plan(coordinator)
    if plan is None or plan.target_day is None or plan.forecast is None:
        return None
    return _economic_instant(
        plan.target_day,
        index,
        _today_interval_count(plan),
        dt_util.get_default_time_zone(),
    )


def _upcoming_target(coordinator: AlphaEmsCoordinator, run: Any) -> dict[str, Any]:
    """Return the published execution target that describes ``run``.

    Matched on the window's opening instant against
    :attr:`AlphaEmsCoordinator.execution_targets` rather than rebuilt here. The
    campaign identity is derived from a calendar the optimizer deliberately does
    not have, and there must go on being exactly one place that owns that
    conversion -- a second implementation living in a sensor is how two surfaces
    come to disagree about which campaign a run belongs to.
    """
    if run is None:
        return {}
    start = _economic_run_instant(coordinator, run.start_index)
    if start is None:
        return {}
    stamp = start.isoformat()
    for target in coordinator.execution_targets or ():
        if target.get("window_start") == stamp:
            return target
    return {}


def _next_planned_run(coordinator: AlphaEmsCoordinator) -> tuple[Any, dict[str, Any]]:
    """Return the nearest run that has not started, and its published target.

    **Two corrections, and they pull in opposite directions. beta.35.**

    *It must not skip the nearest one.* beta.34 used ``upcoming_run``, which takes
    the first run with ``start_index > head``. But the head is ``elapsed + 1`` --
    it is the **next** interval, not the one in progress -- so a run beginning *at*
    the head has not started either; it begins within fifteen minutes and it is
    precisely the next planned action. Skipping it is why the entity read
    ``charge`` at 19:30 while a Sell was about to start at 19:45: the Sell was the
    run at the head, and the reading jumped over it to tomorrow's refill.

    *It must not point at what is already running.* Once that Sell starts, its
    remaining rows can still appear in the plan, and naming them would make "next"
    mean "now". Excluded by campaign identity rather than by index, because the
    identity is the thing that survives the head advancing.

    Returns ``(None, {})`` when nothing is planned ahead.
    """
    outcome = _economic_outcome(coordinator)
    if outcome is None or not outcome.available:
        return None, {}
    plan = outcome.desired
    if not plan.intervals:
        return None, {}
    head = plan.intervals[0].index
    executing = _executing_view(coordinator).get("campaign_id")
    for run in plan.runs:
        if run.start_index < head:
            continue
        target = _upcoming_target(coordinator, run)
        if executing is not None and target.get("campaign_id") == executing:
            continue
        return run, target
    return None, {}


def _next_planned_action_value(coordinator: AlphaEmsCoordinator) -> str | None:
    """Return the action of the nearest campaign that has not started yet."""
    outcome = _economic_outcome(coordinator)
    if outcome is None or not outcome.available:
        return None
    run, target = _next_planned_run(coordinator)
    if run is None:
        return ECONOMIC_ACTION_IDLE
    purpose = target.get("purpose")
    # **The target's own purpose first, and it may say ``mixed_buy``. beta.39.**
    # The fallback below is deliberately *second*: it knows only whether the run
    # is in the Safety-Buy set, which is true of a mixed run too, so consulting it
    # first would overwrite a truthful ``mixed_buy`` with ``safety_buy`` and
    # reinstate the defect on the planned-action entity alone.
    if purpose in (ECONOMIC_ACTION_SAFETY_BUY, ECONOMIC_ACTION_MIXED_BUY):
        return purpose
    if run.start_index in outcome.safety_buy_runs:
        return ECONOMIC_ACTION_SAFETY_BUY
    action = run.action
    return action if action in ECONOMIC_ACTION_OPTIONS else None


def _next_planned_action_attributes(
    coordinator: AlphaEmsCoordinator,
) -> dict[str, Any]:
    """Return when the next planned campaign is, and what it is for.

    **Full ISO instants, and that is the entity's whole reason for existing.**
    ``Economic Action`` describes the present and carries no clock at all; this one
    describes something that is routinely on another day, and a dateless
    ``HH:MM-HH:MM`` window is how a sale planned for tomorrow evening came to be
    read as one starting within the hour.

    ``reason`` is carried only when this run is the one the optimizer's own
    ``reason`` describes -- that field follows ``published_run``, and attaching a
    current charge's rationale to tomorrow's sale would be a fabrication.
    """
    outcome = _economic_outcome(coordinator)
    if outcome is None or not outcome.available:
        return {}
    run, target = _next_planned_run(coordinator)
    if run is None:
        return {"starts_at": None, "ends_at": None, "planned_kwh": None}
    start = _economic_run_instant(coordinator, run.start_index)
    end = _economic_run_instant(coordinator, run.end_index + 1)
    published = outcome.desired.published_run
    return {
        "starts_at": None if start is None else start.isoformat(),
        "ends_at": None if end is None else end.isoformat(),
        "planned_kwh": _round(run.energy_kwh, BATTERY_KWH_PRECISION),
        "power_kw": _round(run.first_power_kw, BATTERY_KW_PRECISION),
        "purpose": target.get("purpose", run.action),
        "campaign_id": target.get("campaign_id"),
        "run_id": target.get("plan_id"),
        "price_eur_kwh": run.average_price_eur_kwh,
        "expected_value_eur": _round(run.net_cash_flow_eur, ECONOMIC_EUR_PRECISION),
        "reason": outcome.reason if published is run else None,
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
    # **Per run, computed inside the loop.** This was a single plan-wide verdict
    # stamped onto every run, so a charge the capability solve executes perfectly
    # well was labelled refused by a comparison it was never part of. A run whose
    # own direction has an actuator is not refused, whatever the plan-level
    # comparison concluded about some other run.
    # **Campaigns, not label slices, since beta.32.** ``runs_from`` splits on the
    # action label, and the label flips between ``discharge`` and ``export`` as
    # house load rises and falls beneath one physical discharge -- so on the live
    # 17:45 horizon the DP charged three switching fees and this loop produced
    # fifteen announcements. Fifteen Planned lines, fifteen cancellations, for
    # three decisions. Grouping on the DP's own run state reproduces exactly the
    # transitions the fee was charged against.
    #
    # The economic runs are still published in diagnostics: the label slices remain
    # the honest per-interval record. What changed is what gets *announced*.
    runs: list[PlannedRun] = []
    for run in outcome.desired.campaigns:
        start = _economic_instant(plan.target_day, run.start_index, count, tz)
        end = _economic_instant(plan.target_day, run.end_index + 1, count, tz)
        if start is None or end is None:
            continue
        # **A campaign that sells nothing is not a Sell**, and the objective is
        # what says so: a discharge campaign whose segments are all ``serve_load``
        # has a meter objective of zero. It is self-consumption, which is inverter
        # behaviour and not an event.
        objective = run.objective_kwh
        if not run.sell_announcement_material:
            continue
        action = (
            ECONOMIC_ACTION_SAFETY_BUY
            if run.start_index in outcome.safety_buy_runs
            else _campaign_action(run)
        )
        # **beta.31: the category comes from the attribution, not from the label.**
        # ``safety_buy_runs`` is a set of indices, so it can only ever say yes or
        # no; the attribution says *how much* of the run was compulsory, which is
        # what separates a Safety Buy from a Mixed Buy. Both come from the same
        # reserve-relaxed counterfactual, so the word a user reads and the figures
        # a diagnostic reader audits cannot drift apart.
        category = category_of(
            action,
            outcome.safety_buy_attribution.get(run.start_index),
            # One category per campaign, decided by whether it sells. Derived from
            # the label, this flipped mid-campaign and split one lifecycle in two.
            sells=objective > 0.0,
        )
        # The campaign's own action rather than the display label: a safety buy
        # *is* a charge, and an actuator exists for it. Testing the label would
        # mark every reserve-driven buy as impossible.
        executable = _campaign_action(run) in IMPLEMENTED_ACTIONS
        runs.append(
            PlannedRun(
                identity=RunIdentity(
                    direction=direction_of(action),
                    start_utc=dt_util.as_utc(start),
                ),
                content=RunContent(
                    category=category,
                    # **The campaign's objective, at the boundary it is paid at**:
                    # the meter for a sale, the battery for a purchase. beta.31
                    # announced a battery figure and then tracked a meter one.
                    energy_kwh=objective,
                    end_utc=dt_util.as_utc(end),
                    window=f"{start:%H:%M}-{end:%H:%M}",
                    executable=executable,
                ),
            )
        )
    return tuple(runs)


def _campaign_action(campaign: Any) -> str:
    """Return the action label one campaign's direction implies.

    A campaign is a *direction*, which is the DP's own unit; the action labels are
    the slices inside it. A discharge campaign is labelled ``export`` because that
    is the action an actuator would have to perform for the part of it that has an
    objective -- the ``serve_load`` part needs no actuator and emits nothing.
    """
    if campaign.direction == ECONOMIC_DIRECTION_CHARGE:
        return ECONOMIC_ACTION_CHARGE
    return ECONOMIC_ACTION_EXPORT


def _economic_activity(
    coordinator: AlphaEmsCoordinator, previous: ActivityState | None
) -> ActivityEntry | None:
    """Return the Activity line this refresh deserves, or ``None`` for silence.

    ``now`` is the instant the coordinator published, not a fresh clock reading.
    The lifecycle must describe the same moment the plan does, or a run could be
    judged imminent against one clock and rendered against another.

    ``shadow`` is true for both non-writing modes, and the reason it is one flag
    rather than the mode itself is that Activity has no business branching on
    three values when the only question it asks is "may anything be sent". Off and
    Shadow answer that identically; the difference between them is in
    diagnostics, where the pipeline's own report says which one refused.
    """
    issued = (coordinator.data or {}).get("issued_at")
    if issued is None:
        return None
    return next_activity(
        previous=previous,
        runs=_planned_runs(coordinator),
        now=dt_util.as_utc(issued),
        execution=_execution_view(coordinator),
        shadow=coordinator.control_mode != CONTROL_MODE_ACTIVE,
    )


def _execution_view(coordinator: AlphaEmsCoordinator) -> ExecutionView | None:
    """Return the few facts Activity needs about Stage B, or ``None``.

    Narrow on purpose. Activity is handed a summary rather than the controller's
    state, so it cannot start describing the rolling setpoint -- which is the one
    thing that must stay out of this surface, being arithmetic rather than a
    decision and the largest source of the old spam.
    """
    control = (coordinator.data or {}).get("control") or {}
    report = control.get("execution") or {}
    if not report:
        return None
    target = report.get("target") or {}
    progress = report.get("progress") or {}
    power = report.get("power") or {}
    # **The terminal is built before anything else, and the ordering is the fix.**
    # A campaign ends on the 60-second tick, which wipes the carriers and publishes
    # no plan; every beta.31 path returned ``None`` there for want of a live
    # ``intent``, so the ending was never described and the Planned line stayed
    # standing as though still true. That is R10, and this is why the incident's
    # 17:45 refresh could not speak.
    terminal = _terminal_view(report)
    intent = report.get("intent")
    if not isinstance(intent, str):
        # A terminal-only view: there is an ending to describe and nothing running.
        if terminal is None:
            return None
        return ExecutionView(executed=True, terminal=terminal)

    # **One objective pair, chosen where the objective is defined.** A charge aims
    # at the battery figure and an export at the meter figure; the other one is a
    # ceiling, and a ceiling in a sentence about an objective is what published
    # ``Tracking 0.25 kWh`` beside ``Planned ... 0.11 kWh``.
    # **The campaign figure is the public quantity, per the beta.32 lifecycle.**
    # Stage B necessarily sees a campaign as several separate windows, and quoting
    # whichever it currently holds is how one sale came to be announced at
    # 2.65 kWh and tracked at a segment's share of it. Frozen at Started, so it
    # cannot shrink under a replan either.
    campaign = report.get("open_campaign")
    if isinstance(campaign, dict) and campaign.get("frozen_target_kwh") is not None:
        objective_target = float(campaign["frozen_target_kwh"])
        objective_realized = float(campaign.get("campaign_realized_kwh") or 0.0)
    elif intent == EXECUTION_INTENT_NET_EXPORT:
        objective = target.get("grid_target_kwh")
        if objective is None:
            # **A fault, not a zero.** An export target with no meter figure cannot
            # be tracked, and announcing 0.00 kWh for it would invite a reader to
            # look for a shortfall where there is a missing field.
            return (
                None
                if terminal is None
                else ExecutionView(executed=True, terminal=terminal)
            )
        objective_target = float(objective)
        # **The run's own objective, at its own boundary. beta.35.**
        #
        # This read ``grid_export_realized_kwh``, and nothing in the package has
        # ever written that key -- one hit in the whole tree, and it was this
        # reader. ``.get(...) or 0.0`` therefore hard-coded 0.00 for every export
        # run whose campaign target had not frozen, which is how a sale that moved
        # 1.92 kWh was published as ``0.00 / 5.05``. The coordinator now publishes
        # ``objective_realized_kwh`` beside the boundary it was measured at, from
        # the same accumulator the campaign uses.
        objective_realized = float(progress.get("objective_realized_kwh") or 0.0)
    else:
        objective_target = float(target.get("battery_target_kwh") or 0.0)
        objective_realized = float(progress.get("battery_realized_kwh") or 0.0)
    opens = report.get("window_start")
    closes = report.get("window_end")
    identity = None
    if isinstance(opens, str):
        start = dt_util.parse_datetime(opens)
        if start is not None:
            identity = RunIdentity(
                direction=direction_of(report.get("purpose") or intent),
                start_utc=dt_util.as_utc(start),
            )
    end_utc = None
    if isinstance(closes, str):
        closing = dt_util.parse_datetime(closes)
        if closing is not None:
            end_utc = dt_util.as_utc(closing)
    return ExecutionView(
        identity=identity,
        # **The lifecycle anchor.** Stage A's planned run and Stage B's admitted
        # run are the same run, so they agree on when the window closes -- which
        # is what lets a dispatch attach itself to the plan that was announced
        # for it without either side inventing a shared key.
        end_utc=end_utc,
        # "Under way, or would be": in shadow nothing is ever armed, so the
        # lifecycle is driven by the controller having an actionable target. The
        # wording is decided by ``executed``, not by this.
        running=report.get("state") in (EXECUTION_STATE_ARMED, EXECUTION_STATE_RUNNING),
        executed=bool(power.get("executed")),
        objective_target_kwh=objective_target,
        objective_realized_kwh=objective_realized,
        intent=intent,
        stop_reason=(report.get("result") or {}).get("stop_reason"),
        inhibit_reason=(report.get("result") or {}).get("inhibit_reason"),
        # The run identity, which is what the lifecycle is deduplicated on. Taken
        # from the carried run rather than from the publication: ``plan_id`` churns
        # every refresh as the horizon rolls, and keying three once-per-run events
        # on a churning id would produce three lines per quarter of an hour.
        run_id=((report.get("carried") or {}).get("run") or {}).get("run_id"),
        # A run admitted and waiting for its window. One line, then silence.
        prepared=report.get("state") == EXECUTION_STATE_PREPARED,
        # Set for exactly one refresh, and only after a write carrying an
        # activation actually succeeded.
        activation_confirmed=bool(coordinator.activation_confirmed),
        terminal=terminal,
        campaign_open=isinstance(campaign, dict) and bool(campaign.get("started")),
        # beta.34: so an Activity line can name the campaign the coordinator
        # measured. ``plan_id`` cannot -- it is derived from the window.
        campaign_id=(
            campaign.get("campaign_id") if isinstance(campaign, dict) else None
        ),
    )


def _terminal_view(report: dict[str, Any]) -> TerminalView | None:
    """Return the latched campaign outcome, or ``None``.

    A thin reading of ``control.execution.completed_campaign``, which the
    coordinator writes when a campaign closes -- outcome already decided, from the
    measurements taken where the energy moved. Nothing is computed here: this
    function contains no tolerance and no reason-to-outcome mapping, because both
    belong to the layer that measured the energy and neither belongs to a sensor.
    """
    latched = report.get("completed_campaign")
    if not isinstance(latched, dict):
        return None
    campaign_id = latched.get("campaign_id")
    outcome = latched.get("outcome")
    if not isinstance(campaign_id, str) or outcome not in CAMPAIGN_OUTCOMES:
        return None
    target = latched.get("objective_target_kwh")
    return TerminalView(
        campaign_id=campaign_id,
        outcome=outcome,
        objective_target_kwh=None if target is None else float(target),
        objective_realized_kwh=float(latched.get("objective_realized_kwh") or 0.0),
        objective_boundary=latched.get("objective_boundary"),
        reason=latched.get("reason"),
        measurable=bool(latched.get("objective_measurable", True)),
        started=bool(latched.get("started")),
    )


def _economic_blocked_reason(coordinator: AlphaEmsCoordinator) -> str:
    """Return why nothing is sent, most fundamental reason first.

    **One implementation, on the coordinator, since beta.33.** The logic lived here
    while diagnostics and the stored evidence snapshot each hardcoded
    ``execution_unavailable`` instead, so the three surfaces disagreed about the
    same fact. They now read
    :attr:`~custom_components.alpha_ems_manager.coordinator.AlphaEmsCoordinator.economic_blocked_reason`,
    which is where the ordering and its argument live.
    """
    return coordinator.economic_blocked_reason


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
    "what the controller is doing now. 'executing' means a command that moves the "
    "battery is on the wire; 'inhibited' means the safety gate refused; 'eligible' "
    "means it did not and only the execution barrier stopped a command; 'idle' "
    "means nothing is running and nothing was sent; 'off' means control is "
    "disabled. Whether the last helper write itself succeeded is a separate "
    "question, answered by control.execution.result.command_result"
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


#: What the Economic Value sensor's state is, in one sentence a person can read.
#:
#: **Corrected in beta.39, and the old wording contradicted itself in one
#: sentence.** It said "on the exact basis the optimiser minimised" and, four
#: words later, "both sides are metered cash". Those cannot both be true: the
#: scalar the dynamic programme minimises is ``objective_eur``, which carries the
#: minimum trade gain, the grid-charge margin, the throughput cost and the
#: terminal credit; the state is ``hold_cost_eur - cost_eur``, which carries none
#: of them. ``test_beta37_economic_value.py`` has asserted ``state !=
#: objective_eur`` since beta.37, so the code was already contradicting its own
#: prose. The cash half was the true half and is what is kept.
_ECONOMIC_VALUE_BASIS = (
    "expected CASH advantage of the selected plan over the passive ambient-walk "
    "comparator, over the horizon currently known. both sides are metered cash: "
    "the four model terms and the terminal credit live in objective_eur, which is "
    "the scalar the plan was chosen to minimise, and are NOT in this figure. a "
    "forecast, not money earned -- see realised_today_eur for what has actually "
    "happened. unknown means no valid comparison could be formed, and 0.00 means "
    "a valid comparison that came out equal"
)

#: What the four day-accounting attributes are, and how they add up.
#:
#: **beta.39.** The sensor could not answer "what has today earned, what is still
#: coming, and what is the honest total" -- and the figures that looked closest
#: were the ones it would have been most wrong to add. ``decision_advantage_eur``
#: is a from-now comparison against a counterfactual and is not a realised
#: quantity; ``net_cash_flow_eur`` is import less export, so a negative value
#: means money *arrived*. Neither is a profit and neither may be summed with the
#: other.
_ECONOMIC_VALUE_ACCOUNTING_BASIS = (
    "realised_today_eur + in_progress_interval_eur + "
    "remaining_expected_today_eur + forecast_revaluation_eur = "
    "total_economic_value_today_eur, and the total telescopes to the civil day's "
    "cash plus what the pack is worth now less what it was worth when the day "
    "opened. one counterfactual throughout: every avoidance term is measured "
    "against a household with no battery. an economic POSITION, not money in the "
    "bank -- the remaining term is a forecast and two terms are planner "
    "valuations. any addend unknown makes the total unknown, and "
    "accounting_unavailable_reason says which. no residual is absorbed into an "
    "addend to make it balance"
)

#: The flat attributes a dashboard card reads, in the order a person would.
#: Everything else is nested, and the nesting is where the audit trail lives.
_ECONOMIC_VALUE_FLAT = (
    "current_action",
    "reason_code",
    # **The day accounting first, because it is the question people actually
    # ask.** Dutch labels belong to the dashboard: Home Assistant does not
    # translate attribute *names*, and this entity has no translation entry, so
    # "Gerealiseerd vandaag / Lopend kwartier / Nog verwacht / Herwaardering /
    # Totaal" are Lovelace card labels over these five keys. Adding Dutch keys
    # here would put presentation in the payload.
    "realised_today_eur",
    "in_progress_interval_eur",
    "in_progress_interval_index",
    "remaining_expected_today_eur",
    "forecast_revaluation_eur",
    "total_economic_value_today_eur",
    "accounting_basis",
    "accounting_reconciliation_error_eur",
    "accounting_unavailable_reason",
    "decision_advantage_eur",
    "advantage_cash_eur",
    "advantage_basis",
    "comparator_model",
    "today_interval_value_eur",
    "tomorrow_interval_value_eur",
    "day_split_basis",
    "stored_energy_marginal_value_eur_kwh",
    "marginal_value_basis",
    "marginal_value_unavailable_reason",
    "terminal_edge_value_eur_kwh",
    "next_planned_charge_price_eur_kwh",
    "current_import_price_eur_kwh",
    "current_export_price_eur_kwh",
    "stored_energy_kwh",
    "horizon_from",
    "horizon_to",
    "horizon_intervals",
    "actionable_intervals",
    "tomorrow_prices_known",
    "unavailable_reason",
)


def _economic_value_payload(coordinator: AlphaEmsCoordinator) -> dict[str, Any]:
    """Return this refresh's Economic Value payload, or an empty dict."""
    try:
        return coordinator.economic_value()
    except Exception:  # pragma: no cover - the entity must never take the refresh
        _LOGGER.debug("Economic value could not be summarised", exc_info=True)
        return {}


def _economic_value_value(coordinator: AlphaEmsCoordinator) -> float | None:
    """Return the expected advantage of the plan over doing nothing, in EUR.

    **``None`` and ``0.00`` are different answers here.** ``None`` means no valid
    comparison could be formed -- no plan, no horizon, no actionable interval, or a
    reserve violation, which under the lexicographic objective means no monetary
    alternative was ever ranked at all. ``0.00`` means a *valid* comparison whose
    plan and passive counterfactual came out economically equal, which is a real
    result. Suppressing it into ``unknown`` would be as dishonest as rendering
    missing data as zero, and this module already forbids the second.
    """
    payload = _economic_value_payload(coordinator)
    if not payload.get("available"):
        return None
    state = payload.get("state")
    return state if isinstance(state, (int, float)) else None


def _economic_value_attributes(coordinator: AlphaEmsCoordinator) -> dict[str, Any]:
    """Return the audit trail for the state above.

    Flat where a Mushroom card reads it, nested where a person auditing the number
    does. Gated on the same predicate as the state, so the two can never describe
    different refreshes -- and on an unavailable refresh the reason is still
    published, because "why is this unknown" is the question a reader has.
    """
    payload = _economic_value_payload(coordinator)
    if not payload:
        return {}
    attributes: dict[str, Any] = {"basis": _ECONOMIC_VALUE_BASIS}
    if not payload.get("available"):
        attributes["unavailable_reason"] = payload.get("unavailable_reason")
        return attributes
    accounting = payload.get("today_accounting")
    if isinstance(accounting, dict):
        # **Flattened by projection, not by a second derivation.** The nested block
        # stays alongside, carrying the interval partition, the two ends of the
        # position and the provenance of the opening valuation -- so a reader who
        # wants to check the identity can, and a card that only wants five numbers
        # does not have to walk into it.
        for name in (
            "realised_today_eur",
            "in_progress_interval_eur",
            "remaining_expected_today_eur",
            "forecast_revaluation_eur",
            "total_economic_value_today_eur",
        ):
            payload[name] = accounting.get(name)
        partition = accounting.get("partition")
        if isinstance(partition, dict):
            payload["in_progress_interval_index"] = partition.get("in_progress_index")
        payload["accounting_basis"] = accounting.get("accounting_basis")
        payload["accounting_reconciliation_error_eur"] = accounting.get(
            "reconciliation_error_eur"
        )
        payload["accounting_unavailable_reason"] = accounting.get("unavailable_reason")

    for name in _ECONOMIC_VALUE_FLAT:
        if name in payload:
            attributes[name] = payload[name]
    for block in (
        "stored_value",
        "plan",
        "energy",
        "today",
        "tomorrow",
        "today_accounting",
    ):
        if isinstance(payload.get(block), dict):
            attributes[block] = payload[block]
    attributes["day_split_rule"] = payload.get("day_split_rule")
    attributes["accounting_rule"] = _ECONOMIC_VALUE_ACCOUNTING_BASIS
    attributes["figure_basis"] = _figure_basis(attributes)
    return attributes


def _figure_basis(attributes: dict[str, Any]) -> dict[str, str]:
    """Return the basis word for every euro figure published *at this entity*.

    **The basis map existed and the entity could not see it.** It lives in the
    ledger block, which reaches the diagnostics download and nothing else -- so an
    operator looking at this sensor saw a dozen adjacent euro attributes spanning
    cash, attribution, a planner valuation and a forecast, distinguished only by
    their names, on an entity Home Assistant labels ``MONETARY``. Adding a term to
    a cash total is then a matter of reading two attribute names and assuming.

    Projected from ``_basis_map`` rather than restated, so the entity and the
    download cannot disagree; the ``today_accounting.`` prefix is stripped because
    those five are flattened up to the top level here. A euro attribute with no
    entry is reported as such, which is a question a reader can act on -- unlike a
    silent omission, which reads as no caveat.
    """
    published = _basis_map()
    basis: dict[str, str] = {}
    for name in attributes:
        if not name.endswith("_eur") and not name.endswith("_eur_kwh"):
            continue
        word = published.get(name) or published.get(f"today_accounting.{name}")
        basis[name] = word or LEDGER_BASIS_UNCLASSIFIED
    return basis


def _battery_roi_value(coordinator: AlphaEmsCoordinator) -> float | None:
    """Return the percentage of the net investment recovered so far.

    ``None`` while no investment is configured, or before any day has been sealed.
    Both are named in ``unavailable_reason``: a recovery of zero and *not knowing*
    are different answers, and only one of them is a measurement.
    """
    payload = coordinator.battery_return(dt_util.now().date())
    if not payload.get("available"):
        return None
    value = payload.get("recovered_percent")
    return None if value is None else float(value)


def _battery_roi_attributes(coordinator: AlphaEmsCoordinator) -> dict[str, Any]:
    """Return the return figure with its coverage and its price basis.

    **The basis travels with the number.** The import leg is all-in cash and the
    export leg is, on a stock configuration, a wholesale reconstruction -- so
    ``export_leg_is_cash`` and ``calculation_basis`` sit here beside the euro
    figures rather than in the diagnostics download, and the period the cumulative
    total actually covers is published rather than implied.
    """
    return coordinator.battery_return(dt_util.now().date())


def _current_campaign_value(coordinator: AlphaEmsCoordinator) -> str | None:
    """Return ``created``, ``started`` or ``idle``.

    **``stopped`` is deliberately not a state, and that is a design decision rather
    than an omission.** On the normal path ``_async_stop_dispatch`` with campaign
    scope calls ``_close_campaign`` in the same call, so a ``stopped`` state would
    exist for less than one coordinator tick and no Home Assistant consumer could
    observe it. A state almost nobody can ever see is a misleading state: an
    automation written against it would look correct and never fire.

    The two moments genuinely separate only on the orphan-grace path and the
    "nothing names it any more" path -- which is exactly where the *event* earns its
    place. So the ``stopped`` event stays, exactly once, with its reason;
    ``stopped_at`` and ``stop_reason`` are published on the final result; and this
    entity moves ``started -> idle`` when the terminal is filed.
    """
    mark = coordinator.store.campaign_lifecycle
    if not mark:
        return CAMPAIGN_STATE_IDLE
    marks = mark.get("marks") or []
    if LIFECYCLE_KIND_STARTED in marks:
        return CAMPAIGN_STATE_STARTED
    if LIFECYCLE_KIND_CREATED in marks:
        return CAMPAIGN_STATE_CREATED
    return CAMPAIGN_STATE_IDLE


def _current_campaign_attributes(coordinator: AlphaEmsCoordinator) -> dict[str, Any]:
    """Return the open campaign's identity, promise and progress.

    **Both classifications, because one of them is allowed to move.** Nothing freezes
    the purchase split: ``_note_campaign_started`` freezes the start instant and the
    target and nothing else, ``Target`` has no field for the attribution at all, and
    ``execution_revision`` compares energy and window rather than attribution -- so a
    campaign spanning two admitted plans can legitimately read one category and then
    another under one unchanged instance id. Publishing only the live word would let
    a reader watch it change with nothing saying it had; publishing only the frozen
    one would go stale beside a live figure. Both, side by side, and the rule beside
    them.
    """
    mark = coordinator.store.campaign_lifecycle or {}
    live = coordinator._campaign_classification(mark.get("campaign_id"))
    return {
        "campaign_id": mark.get("campaign_id"),
        "campaign_instance_id": mark.get("instance_id"),
        "purpose": mark.get("purpose"),
        "classification": live.get("classification"),
        "classification_at_creation": mark.get("classification_at_creation"),
        "planned_kwh": mark.get("planned_kwh"),
        "realised_kwh": (
            None if not mark else round(coordinator._campaign_realized_now(), 3)
        ),
        "window_start": mark.get("window_start"),
        "window_end": mark.get("window_end"),
        "first_executable_at": mark.get("started_at"),
        "started_at": mark.get("started_at"),
        "revision": mark.get("revision"),
        "objective_boundary": mark.get("objective_boundary"),
        **{key: value for key, value in live.items() if key != "classification"},
        "rule": (
            "created is public but not yet physical; started means execution was "
            "confirmed. there is no stopped state: on the ordinary path the stop "
            "and the terminal land in the same tick, so a stopped state would be "
            "unobservable -- the stopped event carries that moment instead, and "
            "stopped_at and stop_reason are published on the final result. the "
            "classification may move under one instance because nothing freezes "
            "the purchase split, so what it was at creation is published beside it"
        ),
    }


def _last_campaign_value(coordinator: AlphaEmsCoordinator) -> str | None:
    """Return the last published campaign result, or ``None``."""
    result = coordinator._last_campaign_result
    if not result:
        return None
    value = result.get("result")
    return value if isinstance(value, str) else None


def _last_campaign_attributes(coordinator: AlphaEmsCoordinator) -> dict[str, Any]:
    """Return the terminal exactly as it was published.

    **Read, never re-derived.** The coordinator keeps the payload it fired, so the
    event and the entity cannot disagree -- which is the failure mode two derivations
    of one number always eventually produce.
    """
    result = coordinator._last_campaign_result
    if not result:
        return {"available": False, "unavailable_reason": "no_campaign_closed_yet"}
    published = {key: value for key, value in result.items() if key != "kind"}
    return {
        "available": True,
        **published,
        "rule": (
            "a non-zero realised figure never implies success: only the frozen "
            "objective within success_tolerance_kwh does, and an unmeasurable total "
            "outranks even a met objective. not_executed means the campaign was "
            "publicly created and never started, so nothing under-delivered; "
            "superseded means a started campaign was replaced by a newer plan"
        ),
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
        key=SENSOR_NEXT_PLANNED_ACTION,
        name="Next Planned Action",
        icon="mdi:calendar-clock",
        # The same enum as the action beside it. Deliberately: a user comparing
        # "now" against "next" is comparing two answers to the same question, and
        # two vocabularies would make that comparison harder than it is.
        device_class=SensorDeviceClass.ENUM,
        options=list(ECONOMIC_ACTION_OPTIONS),
        value_fn=_next_planned_action_value,
        attributes_fn=_next_planned_action_attributes,
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
    AlphaEmsSensorDescription(
        key=SENSOR_ECONOMIC_VALUE,
        name="Economic Value",
        icon="mdi:cash-clock",
        # **The integration's first monetary entity.** ``MONETARY`` with no state
        # class: Home Assistant pairs that device class with ``TOTAL``, and this is
        # neither a total nor a measurement -- it is a forecast over a rolling
        # horizon, which is exactly the case the forecast sensors above decline a
        # state class for. A long-term statistic over it would average a number
        # whose horizon shrinks through the day.
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY_EURO,
        value_fn=_economic_value_value,
        attributes_fn=_economic_value_attributes,
    ),
    AlphaEmsSensorDescription(
        key=SENSOR_BATTERY_ROI,
        name="Battery Return",
        icon="mdi:cash-refund",
        native_unit_of_measurement=PERCENTAGE,
        # **A percentage, so Economic Value stays the only EUR-valued state.** No
        # state class: this rises monotonically only while the battery is earning,
        # falls on a day it did not, and its denominator changes the moment an
        # operator corrects a subsidy -- none of which a long-term statistic
        # describes usefully.
        value_fn=_battery_roi_value,
        attributes_fn=_battery_roi_attributes,
    ),
    AlphaEmsSensorDescription(
        key=SENSOR_CURRENT_CAMPAIGN,
        name="Current Campaign",
        icon="mdi:play-circle-outline",
        device_class=SensorDeviceClass.ENUM,
        options=list(CAMPAIGN_STATE_OPTIONS),
        value_fn=_current_campaign_value,
        attributes_fn=_current_campaign_attributes,
    ),
    AlphaEmsSensorDescription(
        key=SENSOR_LAST_CAMPAIGN_RESULT,
        name="Last Campaign Result",
        icon="mdi:flag-checkered",
        device_class=SensorDeviceClass.ENUM,
        # The full outcome vocabulary, including the two beta.42 added. An ``ENUM``
        # whose options omit a reachable value publishes ``unknown`` for it, which
        # would hide exactly the two results this release exists to distinguish.
        options=list(CAMPAIGN_OUTCOMES),
        value_fn=_last_campaign_value,
        attributes_fn=_last_campaign_attributes,
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
