"""Runtime orchestration for one Alpha EMS Manager config entry.

The coordinator owns the whole data path: it listens to the configured source
entities, integrates house load into quarter-hour buckets, persists finalised
quarters, and derives the forecasts and confidence the four sensors display.

It never contacts an external service. Frank, Solcast, AlphaESS and the grid
meter are read purely through the Home Assistant state machine and config-entry
registry, so this integration adds no API traffic of its own.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EVENT_CORE_CONFIG_UPDATE,
    SUN_EVENT_SUNRISE,
    SUN_EVENT_SUNSET,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .alphaess_adapter import (
    ControlActionNotPermitted,
    ControlExecutionUnavailable,
    async_execute,
    discover,
    dispatch_readback_compatible,
    read_snapshot,
)
from .alphaess_device import (
    BOOLEAN_EXECUTION_OWNER,
    DISPATCH_DEADMAN_MINUTES,
    DISPATCH_DURATION,
    DISPATCH_ENABLE,
    DISPATCH_MODE_SOC_CONTROL,
    DISPATCH_POWER,
    FAMILIES,
    SENSOR_DISPATCH_START,
    action_refusal,
    build_command,
    device_power_kw,
    dispatch_refusal,
    limit_command,
    plan_arm_parameters,
    plan_dispatch_arm,
    plan_dispatch_cleanup,
    plan_dispatch_cutoff,
    plan_dispatch_power,
    plan_dispatch_rearm,
    plan_dispatch_stop,
    plan_marker_claim,
    plan_release_marker,
)
from .api import load_forecast_from
from .battery import INTERVAL_HOURS, BatteryLimits, sanitize_soc_percent
from .confidence import ConfidenceBreakdown, compute_confidence
from .const import (
    ACCOUNTING_OPENING_ENERGY_TOLERANCE_KWH,
    ACCOUNTING_UNAVAILABLE_AVOIDANCE_BASIS,
    ACCOUNTING_UNAVAILABLE_HORIZON_SHORT_OF_MIDNIGHT,
    ACCOUNTING_UNAVAILABLE_NO_DAY_RECORD,
    ACCOUNTING_UNAVAILABLE_NO_OPEN_QUARTER_MEASUREMENT,
    ACCOUNTING_UNAVAILABLE_NO_OPENING_VALUATION,
    ACCOUNTING_UNAVAILABLE_NO_PLAN,
    ACCOUNTING_UNAVAILABLE_NO_POSITION_VALUE,
    ACCOUNTING_UNAVAILABLE_NO_STORED_PRICES,
    ACCOUNTING_UNAVAILABLE_OPENING_ENERGY_MISMATCH,
    ACCOUNTING_UNAVAILABLE_VALUATION_REFERENCE_MOVED,
    ACTION_DISCHARGE,
    AMBIENT_SELF_CONSUMPTION_NO_SUPPRESSING_FEATURE,
    AMBIENT_SELF_CONSUMPTION_PEAK_SHAVING,
    AMBIENT_SELF_CONSUMPTION_SELF_CONSUMPTION,
    AMBIENT_SELF_CONSUMPTION_STATE_UNREADABLE,
    ARM_EVIDENCE_INCOHERENT,
    ARM_EVIDENCE_INCOMPLETE,
    ARM_EVIDENCE_NO_TRANSITION,
    ARM_EVIDENCE_STALE_REGISTER,
    ARM_EVIDENCE_UNATTRIBUTABLE,
    AUTHORITY_BASIS_ADMITTED_PLAN,
    AUTHORITY_BASIS_CARRIED_RUN,
    AUTHORITY_BASIS_NONE,
    AVOIDANCE_BASIS_NO_BATTERY,
    BATTERY_MAX_SOC_PERCENT,
    CADENCE_PHYSICAL_TICK,
    CADENCE_QUARTER_REFRESH,
    CALCULATION_BASIS_IMPORT_CASH_EXPORT_CASH,
    CALCULATION_BASIS_IMPORT_CASH_EXPORT_RECONSTRUCTED,
    CAMPAIGN_BOUNDARY_BATTERY,
    CAMPAIGN_BOUNDARY_METER,
    CAMPAIGN_CLASSIFICATION_EPSILON_KWH,
    CAMPAIGN_MEASUREMENT_RESOLUTION_PERCENT,
    CAMPAIGN_ORPHAN_GRACE_MINUTES,
    CAMPAIGN_SUCCESS_TOLERANCE_FRACTION,
    CAMPAIGN_SUCCESS_TOLERANCE_PER_QUARTER_KWH,
    CAMPAIGN_TARGET_UNAVAILABLE,
    CAP_NONE,
    CLAIM_SCHEMA_VERSION,
    COHERENCE_OK,
    COMPARATOR_MODEL_AMBIENT_ABSORB_ONLY,
    COMPARATOR_MODEL_AMBIENT_WALK,
    CONF_ALLOW_BATTERY_EXPORT,
    CONF_ALLOW_GRID_CHARGING,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_INVESTMENT_DATE,
    CONF_BATTERY_INVESTMENT_EUR,
    CONF_BATTERY_MAX_CHARGE_KW,
    CONF_BATTERY_MAX_DISCHARGE_KW,
    CONF_BATTERY_MIN_SOC_PERCENT,
    CONF_BATTERY_POWER_ENTITY,
    CONF_BATTERY_POWER_SIGN,
    CONF_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
    CONF_BATTERY_SOC_ENTITY,
    CONF_BATTERY_SUBSIDY_EUR,
    CONF_BATTERY_THROUGHPUT_COST_EUR_PER_KWH,
    CONF_CONTROL_EXECUTION_ENABLED,
    CONF_CONTROL_EXPORT_MARGIN_PERCENT,
    CONF_DAILY_HOUSE_LOAD_ENTITY,
    CONF_EV_POWER_ENTITY,
    CONF_FRANK_ENTRY_ID,
    CONF_GRID_CHARGE_BUDGET_KWH,
    CONF_GRID_CHARGE_MARGIN_EUR_PER_KWH,
    CONF_GRID_POWER_ENTITY,
    CONF_GRID_POWER_SIGN,
    CONF_HAS_PV,
    CONF_HOUSE_LOAD_ENTITY,
    CONF_MINIMUM_TRADE_GAIN_EUR,
    CONF_OTHER_ONE_TIME_CREDIT_EUR,
    CONF_PV_POWER_ENTITY,
    CONF_SELECTED_SOLCAST_SITE_IDS,
    CONF_SOLCAST_ENTRY_ID,
    CONF_USE_PV_FORECAST,
    CONTROL_EXECUTABLE_ACTIONS,
    CONTROL_EXECUTION_AVAILABLE,
    CONTROL_HORIZON_MINUTES,
    CONTROL_LIVE_DISPATCH_INTENTS,
    CONTROL_MAX_SOURCE_AGE_SECONDS,
    CONTROL_MIN_POWER_KW,
    CONTROL_MODE_ACTIVE,
    CONTROL_MODE_OFF,
    CONTROL_MODE_OPTIONS,
    CONTROL_MODE_SHADOW,
    CONTROL_REFUSE_MARKER_NOT_VERIFIED,
    CONTROL_REFUSE_NO_CLAIMABLE_RUN,
    CONTROL_REFUSE_STOP_NOT_VERIFIED,
    CONTROL_STATE_ELIGIBLE,
    CONTROL_STATE_ERROR,
    CONTROL_STATE_EXECUTED,
    CONTROL_STATE_EXECUTING,
    CONTROL_STATE_IDLE,
    CONTROL_STATE_INHIBITED,
    CONTROL_STATE_OFF,
    CONTROL_TICK_ENERGY_HORIZON_SECONDS,
    DEFAULT_ALLOW_BATTERY_EXPORT,
    DEFAULT_ALLOW_GRID_CHARGING,
    DEFAULT_BATTERY_MIN_SOC_PERCENT,
    DEFAULT_BATTERY_POWER_SIGN,
    DEFAULT_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
    DEFAULT_BATTERY_THROUGHPUT_COST_EUR_PER_KWH,
    DEFAULT_CONTROL_EXECUTION_ENABLED,
    DEFAULT_CONTROL_EXPORT_MARGIN_PERCENT,
    DEFAULT_GRID_CHARGE_BUDGET_KWH,
    DEFAULT_GRID_CHARGE_MARGIN_EUR_PER_KWH,
    DEFAULT_GRID_POWER_SIGN,
    DEFAULT_MINIMUM_TRADE_GAIN_EUR,
    DISPATCH_LIMIT_NONE,
    DISPATCH_POWER_DEADBAND_KW,
    ECONOMIC_ACTION_CURTAIL,
    ECONOMIC_ACTION_EXPORT,
    ECONOMIC_BLOCKED_ACTION_NOT_EXECUTABLE,
    ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE,
    ECONOMIC_BLOCKED_EXPORT_NOT_PERMITTED,
    ECONOMIC_BLOCKED_MODE_NOT_ACTIVE,
    ECONOMIC_BLOCKED_NO_PRIMITIVE_CURTAIL,
    ECONOMIC_BLOCKED_NONE,
    ECONOMIC_BLOCKED_NOT_ENABLED,
    EV_ABSENCE_GRACE_REFRESHES,
    EVENT_CAMPAIGN_LIFECYCLE,
    EXECUTION_ABORT_STOP_REASONS,
    EXECUTION_COMPLETION_STOP_REASONS,
    EXECUTION_FAILED_STOP_REASONS,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_NET_EXPORT,
    EXECUTION_STOP_CAMPAIGN_COMPLETE,
    EXECUTION_STOP_COHERENCE_LOST,
    EXECUTION_STOP_EXECUTION_ERROR,
    EXECUTION_STOP_MARKER_LOST,
    EXECUTION_STOP_NO_BATTERY_PLAN,
    EXECUTION_STOP_PLAN_REPLACED,
    EXECUTION_STOP_QUARTER_EXPIRED,
    EXECUTION_STOP_QUARTER_PROGRESS_UNKNOWN,
    EXECUTION_STOP_QUARTER_TARGET_REACHED,
    EXECUTION_STOP_SAFETY,
    EXECUTION_STOP_SWITCHED_OFF,
    EXECUTION_STOP_TIMER_NOT_REFRESHED,
    EXECUTION_STOP_WINDOW_ENDED,
    EXECUTION_TARGET_STALE_MINUTES,
    EXECUTION_VERIFY_DISPATCH_INACTIVE,
    EXECUTION_VERIFY_DISPATCH_SETPOINT,
    EXECUTION_VERIFY_MARKER_ON,
    EXECUTION_VERIFY_NO_FAMILY_ACTIVE,
    EXECUTION_WITHDRAWAL_STOP_REASONS,
    HOLD_REASON_QUARTER_SATISFIED,
    HOLD_REASON_RATE_BELOW_RESOLUTION,
    HOLD_WRITE_FAILURE_LIMIT,
    INHIBIT_NO_COMMAND_REASONS,
    INHIBIT_NO_DECISION,
    INHIBIT_NO_PLAN,
    INHIBIT_PLAN_UNAVAILABLE,
    INHIBIT_WITHDRAWAL_REASONS,
    LIFECYCLE_ADMITTED,
    LIFECYCLE_CLASS_COVERAGE_BUY,
    LIFECYCLE_CLASS_ECONOMIC_BUY,
    LIFECYCLE_CLASS_ECONOMIC_EXPORT,
    LIFECYCLE_CLASS_MIXED_BUY,
    LIFECYCLE_CLASS_SAFETY_BUY,
    LIFECYCLE_CLASS_SERVE_LOAD,
    LIFECYCLE_CLASS_UNKNOWN,
    LIFECYCLE_CLEANUP_COMPLETE,
    LIFECYCLE_DEADMAN_EXPIRED,
    LIFECYCLE_DEGRADED,
    LIFECYCLE_EXECUTING,
    LIFECYCLE_FOREIGN,
    LIFECYCLE_IDLE,
    LIFECYCLE_KIND_CREATED,
    LIFECYCLE_KIND_REMOVED,
    LIFECYCLE_KIND_STARTED,
    LIFECYCLE_KIND_STOPPED,
    LIFECYCLE_RELEASING,
    LIFECYCLE_STARTING,
    LIFECYCLE_STATES,
    LIFECYCLE_STOPPED,
    LIFECYCLE_STOPPING,
    LIFECYCLE_TRAIL_LIMIT,
    LIFECYCLE_UNPROVEN,
    LOG_THROTTLE_SECONDS,
    MAX_ABORTED_CAMPAIGNS_REMEMBERED,
    MAX_ARM_MEASUREMENTS_REPORTED,
    MAX_ARM_PLAN_ENTRIES_PUBLISHED,
    MAX_COMPLETED_QUARTERS_REPORTED,
    MAX_CONTROL_EVENTS_REPORTED,
    MAX_DISPATCH_START_ACTIVE_SAMPLES,
    MAX_DISPATCH_START_SAMPLES_REPORTED,
    MAX_PHYSICAL_DECISIONS_REPORTED,
    MAX_QUARTER_REFUSALS_RECORDED,
    MAX_SAMPLE_GAP_SECONDS,
    MIN_CONTROLLABLE_QUARTER_KWH,
    MIN_EXECUTABLE_QUARTER_KWH,
    MIN_QUARTER_COVERAGE,
    OUTCOME_CANCELED,
    OUTCOME_FAILED,
    OUTCOME_NOT_EXECUTED,
    OUTCOME_PARTIAL,
    OUTCOME_SUCCESS,
    OUTCOME_SUPERSEDED,
    OWNERSHIP_DEGRADED,
    OWNERSHIP_FOREIGN,
    OWNERSHIP_NONE,
    OWNERSHIP_OWNED,
    OWNERSHIP_RELEASING,
    OWNERSHIP_UNPROVEN,
    PRICE_BASIS_LIVE_FORECAST,
    PRICE_BASIS_STORED_SNAPSHOT,
    PRICE_CROSS_CHECK_DISAGREES,
    PRICE_EXPORT_BASIS_ADJUSTMENT_VAT,
    PRICE_EXPORT_BASIS_API_FIELD,
    PRICE_EXPORT_BASIS_UNKNOWN,
    PRICE_FLAG_EXPORT_CROSS_CHECK_FAILED,
    PRICE_FLAG_IMPORT_CROSS_CHECK_FAILED,
    PRICE_LEG_ALL_IN_CASH,
    PRICE_UNAVAILABLE_NOT_CONFIGURED,
    PRICE_UNAVAILABLE_OPTIONS_UNREADABLE,
    PRICE_UNAVAILABLE_SOURCE_UNAVAILABLE,
    PROBE_PHASE_AFTER_REARM,
    PROBE_PHASE_AFTER_START,
    PROBE_PHASE_AFTER_STOP,
    PROBE_PHASE_BEFORE_START,
    PROBE_PHASE_IDLE,
    PROBE_PHASE_STEADY,
    PV_ABSORPTION_DISPATCH_ACTIVE,
    PV_ABSORPTION_EXCESS_EXPORT,
    PV_ABSORPTION_NO_SUPPRESSING_FEATURE,
    PV_ABSORPTION_PEAK_SHAVING,
    PV_ABSORPTION_SELF_CONSUMPTION,
    PV_ABSORPTION_STATE_UNREADABLE,
    PV_AGGREGATE_SITE,
    PV_FLAG_AVAILABLE_SITES_CHANGED,
    PV_FLAG_SELECTED_MODEL_CHANGED,
    PV_FLAG_SELECTED_SITES_CHANGED,
    PV_FLAG_SOURCE_CORRECTION_CHANGED,
    PV_SELECTION_ORIGIN_AUTO,
    PV_SELECTION_ORIGIN_STORED,
    PV_UNAVAILABLE_EMPTY_SELECTION,
    PV_UNAVAILABLE_NO_SITES_DISCOVERED,
    PV_UNAVAILABLE_NOT_CONFIGURED,
    PV_UNAVAILABLE_SERVICE_FAILED,
    PV_UNAVAILABLE_SERVICE_MISSING,
    QUARTER_END_EXPIRED,
    QUARTER_END_SAFETY,
    QUARTER_END_TARGET_REACHED,
    QUARTER_MINUTES,
    QUARTER_SECONDS,
    QUARTER_TARGET_TOLERANCE_KWH,
    REALIZED_BENEFIT_BASIS_VERSION,
    REASON_VOCABULARY_CAMPAIGN_END,
    REASON_VOCABULARY_QUARTER_COMPLETION,
    REASON_VOCABULARY_RUN_STOP,
    REFUSE_MODE_NOT_ACTIVE,
    REFUSED_RUN_VALUE_BASIS,
    RETENTION_GATE_NO_PV,
    ROI_MIN_SAMPLE_DAYS,
    ROI_PAYBACK_UNAVAILABLE_INSUFFICIENT_HISTORY,
    ROI_PAYBACK_UNAVAILABLE_NO_BENEFIT,
    ROI_TRAILING_LONG_DAYS,
    ROI_TRAILING_SHORT_DAYS,
    ROI_UNAVAILABLE_NO_HISTORY,
    ROI_UNAVAILABLE_NO_INVESTMENT,
    SAFETY_SAMPLE_SECONDS,
    SELECT_INVERTER_AC_LIMIT,
    SHORTFALL_ABSORBING_FREE_PV,
    SHORTFALL_NONE,
    SHORTFALL_QUARTER_EXPIRED,
    SHORTFALL_SENSOR_INCOHERENCE,
    SHORTFALL_TARGET_REACHED,
    STOP_SCOPE_ABORT,
    STOP_SCOPE_CAMPAIGN,
    STOP_SCOPE_ROW,
    STOP_SCOPES,
    TICK_APPLIED,
    TICK_ERROR,
    TICK_HELD_QUARTER_SATISFIED,
    TICK_HELD_RATE_BELOW_RESOLUTION,
    TICK_HOLD_WRITE_FAILED,
    TICK_SKIPPED_DISPATCH_INACTIVE,
    TICK_SKIPPED_INCOHERENT,
    TICK_SKIPPED_LOCK_HELD,
    TICK_SKIPPED_NO_QUARTER,
    TICK_SKIPPED_NOT_LIVE,
    TICK_SKIPPED_OWNERSHIP,
    TICK_SKIPPED_STALE_TARGET,
    TICK_SKIPPED_SUB_RESOLUTION,
    TICK_STOPPED_ORPHAN_DISPATCH,
    TICK_STOPPED_QUARTER_EXPIRED,
    TICK_STOPPED_TARGET_REACHED,
)
from .control import translate
from .dispatch import (
    ChargeLimits,
    QuarterProgress,
    deadman_minutes,
    decide_for_intent,
    permitted_sign,
    sign_matches_intent,
    tick_energy_cap_kw,
)
from .dispatch import decide as decide_setpoint
from .economic import (
    RUN_STATE_IDLE,
    EconomicOutcome,
    ForecastRisk,
    IntervalPrice,
    RetentionGate,
    TerminalValue,
    actionable_intervals,
    bucket_at_or_below_kwh,
    build_economic_snapshot,
    build_horizon,
    build_outcome,
    build_physics_table,
    campaign_identity,
    campaign_instance_identity,
    day_block_for,
    desired_grid_kw_at,
    economic_value_summary,
    edge_creditable_energy_kwh,
    edge_value_eur_per_kwh,
    execution_revision,
    execution_target,
    fingerprint_settings,
    post_horizon_window,
    run_state_for_intent,
    select_bucket_kwh,
)
from .energy_balance import (
    COHERENCE_UNKNOWN,
    OUTCOME_SKIPPED_INCOHERENT,
    BalanceMonitor,
    BalanceSample,
    ControlCoherence,
    SourceCoherence,
    control_coherence,
    evaluate_balance,
    measure_coherence,
)
from .execution import (
    ADMISSION_REFUSED_ABANDONED,
    TARGET_TOLERANCE_KWH,
    AdmittedPlan,
    CarriedQuarter,
    CarriedRun,
    ForwardAuthorisation,
    OwnershipEvidence,
    action_for_intent,
    actionable_target,
    affirms,
    carried_from_record,
    carry_forward,
    carry_plan_verbose,
    control_intent_for,
    decide,
    forward_authorisation,
    instant_of,
    measure_progress,
    ownership_of,
    parse_target,
    quarter_intent_for,
    remaining_authorised_kwh,
    stale_marker,
    target_as_published,
    withdrawal_basis,
)
from .execution import as_dict as execution_as_dict
from .forecast import (
    DayForecast,
    TodayForecast,
    adapt_today,
    build_forecast,
    collect_forecast_inputs,
)
from .forecast_recorder import ForecastRecorder, RecorderResult
from .frank_source import (
    DayRead,
    FrankCapability,
    FrankOptions,
    read_current_prices,
    read_options,
    read_today,
    read_tomorrow,
)
from .frank_source import discover as discover_frank
from .history_store import ForecastHistoryStore
from .normalization import (
    PowerFlows,
    describe_power_problem,
    normalize_energy_kwh,
    normalize_percentage,
    normalize_power_w,
    split_battery_power,
    split_grid_power,
)
from .plan import BatteryPlan, build_plan
from .price_forecast import (
    PriceForecast,
    PriceProvenance,
    build_price_forecast,
    build_price_snapshot,
    cross_check,
    unavailable_price_forecast,
)
from .pv_forecast import (
    PvForecast,
    PvProvenance,
    PvSnapshot,
    build_pv_snapshot,
    score_pv_day,
    sites_identity,
    sites_model,
)
from .pv_forecast import (
    build_forecast as build_pv_forecast,
)
from .quarter import (
    QuarterAccumulator,
    QuarterResult,
    interpretable_pv_w,
    sanitize_ev_w,
    sanitize_load_w,
    sanitize_pv_w,
)
from .realized import (
    closing_inventory_kwh,
    day_accounting,
    day_partition,
    open_quarter_value_eur,
    opening_inventory_kwh,
    realized_window,
    soc_series_to_energy,
)
from .reserve import (
    ReserveProjection,
    build_reserve_reachable,
    build_reserve_snapshot,
    fingerprint_battery_config,
    uncertainty_margin,
)
from .safety import (
    ControlContext,
    ExecutionDecision,
    ExportRequest,
    SafetyVerdict,
    absorbing_capacity_kw,
    authorize_emergency_self_stop,
    authorize_export,
    authorize_marker_release,
    authorize_reset,
    authorize_start,
    emergency_self_stop_authorized,
    evaluate,
    safe_discharge_power_kw,
)
from .simulation import IntervalDemand
from .soc_coherence import SocCoherenceMonitor
from .solcast_source import SolcastCapability, SolcastFacts, read_facts, read_forecast
from .solcast_source import discover as discover_solcast
from .storage import (
    DayRecord,
    LearningStore,
    expected_quarters_for,
    index_for_start_utc,
    interval_start_utc,
    utc_midnight,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _PlanAuthority:
    """An admitted plan, in the few terms the ownership claim needs.

    **beta.34.** Not a ``CarriedRun`` and deliberately not pretending to be one:
    it answers only the questions :meth:`AlphaEmsCoordinator._write_execution_record`
    asks, so nothing else in the controller can start treating an admitted plan as
    an economic run. Every field is copied from the plan or from the open row --
    none is derived, defaulted or invented.
    """

    run_id: str
    plan_id: str
    revision: int
    intent: str
    target: Any
    admitted_at: datetime
    affirmed_at: datetime
    stale_after: datetime


#: How many horizon intervals of price and gate evidence a decision record keeps.
#:
#: **beta.34.** Eight quarters is two hours, which is where every gate decision
#: that matters is made: an export is vetoed at the interval it would have
#: happened in, and the veto that mattered on 2026-08-29 was at the head. The
#: whole-horizon digest sits beside it, so a replay can still prove it holds the
#: same series the decision saw. Bounded because these records are written every
#: refresh and kept for a year.
_RECORD_HEAD_INTERVALS = 8


def _price_text(value: float | None) -> str:
    """Return a price formatted for the fingerprint, ``none`` when absent."""
    return "none" if value is None else f"{value:.6f}"


def _rounded_price(value: float | None) -> float | None:
    """Return a price rounded for the record, preserving ``None``."""
    return None if value is None else round(value, 5)


#: Seconds after each quarter boundary at which the bucket is closed. The small
#: delay lets sources that publish exactly on the boundary land first.
_QUARTER_TRIGGER_SECOND = 5

#: Why a finalised quarter was not learned.
#:
#: A bare ``rejected_quarters`` counter was not enough to act on. Every route to
#: a rejected quarter ends in the same place -- the interval failed to reach
#: MIN_QUARTER_COVERAGE -- but the *causes* are unrelated to each other, and a
#: user whose learning has stalled needs to be told which one applies. The
#: source-level reasons below are attributed in preference to the coverage one
#: whenever a source problem was actually seen during the interval, because
#: "your house-load entity has no unit" is a fixable statement and "coverage was
#: insufficient" is not.

#: The configured entity does not exist in the state machine at all.
REJECT_SOURCE_MISSING: str = "source_entity_missing"
#: The reading parsed, but fell outside the plausible band for its quantity --
#: a house load above MAX_PLAUSIBLE_LOAD_W, or an EV power below the negative
#: noise floor. A glitch, not a measurement.
REJECT_VALUE_IMPLAUSIBLE: str = "value_implausible"
#: No source problem was seen; the interval simply was not observed for enough
#: of its length. Normal at startup and after a restart.
REJECT_INSUFFICIENT_COVERAGE: str = "insufficient_sample_coverage"
#: The interval's chronological index fell outside the stored day. Only
#: reachable when a day's recorded shape and the instant being filed disagree,
#: which in practice means Home Assistant's timezone changed under a running
#: history.
REJECT_INTERVAL_OUT_OF_RANGE: str = "interval_outside_stored_day"

#: Throttle key for the rejected-quarter warning. One key per distinct reason,
#: so a newly appearing cause is never rate-limited by an older one, and each
#: still speaks at most once per LOG_THROTTLE_SECONDS.
_REJECTED_QUARTER_LOG = "rejected_quarter"

#: Throttle keys for the two energy-balance wordings. They are deliberately
#: distinct: the two messages describe different situations and call for
#: different action, so neither may rate-limit the other.
_BALANCE_UNAVAILABLE_LOG = "energy_balance_unavailable"
_BALANCE_LOG_MODERATE = "energy_balance_moderate"
_BALANCE_LOG_GROSS = "energy_balance_gross"

#: Throttle key for a failure inside the Phase-2 evidence layer. Throttled like
#: everything else on this path: a persistently unwritable document would
#: otherwise warn every fifteen minutes forever.
_FORECAST_HISTORY_LOG = "forecast_history"
#: Throttle key for a battery-planning failure. Separate from the forecast one so
#: a fault in one layer cannot silence the other.
_BATTERY_PLAN_LOG = "battery_plan"
#: Throttle key for a control-layer failure. Separate again, for the same reason:
#: three additive layers must be able to fail independently and say so.
_CONTROL_LOG = "control"
#: Throttle key for the PV forecast layer. Its own, so a Solcast fault cannot
#: silence a control warning or the other way round.
_PV_LOG = "pv_forecast"
_PRICE_LOG = "price_forecast"
#: Throttle key for the reserve evidence layer, for the same reason as the four
#: above it: a storage fault while recording a requirement must not silence a
#: Solcast, price or control warning, and must not cost the refresh.
_RESERVE_LOG = "reserve"
#: Throttle key for the economic layer. Its own, for the same reason as the
#: five above: the optimizer is the newest thing in the refresh and a fault in
#: it must not silence a Solcast, price, reserve or control warning.
_ECONOMIC_LOG = "economic"

#: The statement every control surface in this release repeats, because it is the
#: single most important fact about it.
#: What this release will and will not send, in one sentence a reader can check.
#:
#: It said "no command reaches the inverter in this release" up to beta.23, which
#: was true and is now not. Renaming the key would have broken every reader of the
#: payload; leaving the sentence would have been worse than a broken key, because a
#: stale reassurance is read as a current one.
#: Distinguishes "use the record's own run" from "this run, which may be None".
#: A bare ``None`` default cannot express both, and conflating them would let the
#: Stage-B path silently fall back to the record when no run is carried.
_UNSET_RUN = "<unset>"


def carried_run_id_of(carried: Any) -> str | None:
    """Return the carried run's id, or ``None`` when nothing is carried."""
    return None if carried is None else carried.run_id


@dataclass(frozen=True, slots=True)
class TickOutcome:
    """What one control evaluation actually did, recorded once at its end.

    **The beta.26 diagnostics fault, fixed by shape rather than by wording.** A
    single mutable reason string was written by the sixty-second tick and then
    published beside figures computed during the quarter refresh, so a stale
    "no owned run" sat next to a freshly successful write with nothing saying the
    two described different events. A record that carries its own cadence cannot do
    that, and recording it once at the end means an early reason can no longer
    survive a later write.
    """

    cadence: str
    reason: str
    wrote: bool
    at: datetime
    #: Which stage of the evaluation produced the reason, for a reader who needs to
    #: know whether the controller got as far as calculating anything.
    phase: str = "evaluate"

    def as_dict(self) -> dict[str, Any]:
        """Return the bounded diagnostics form."""
        return {
            "cadence": self.cadence,
            "phase": self.phase,
            "reason": self.reason,
            "wrote": self.wrote,
            "at": self.at.isoformat(),
        }


_EXECUTION_SCOPE = (
    "the control pipeline is fully evaluated and two stage-b intents may execute: "
    "grid_charge, and net_export inside an admitted quarter. both use the dispatch "
    "mode 2 surface only -- negative power charges, positive exports -- and the "
    "force charging and force discharging helper families are never written for "
    "either. everything else is refused at the authorization stage and again at "
    "the send site, and executes nothing: serve_load, the phase-3 reserve guard's "
    "discharge (which still cannot export, and is still refused by would_export), "
    "pv curtailment, panel shutdown and dispatch modes 6 and 7"
)


def _optional_number(raw: Any) -> float | None:
    """Return a stored numeric option as a float, or ``None``.

    A config entry is JSON, so a number written by the options form comes back
    as an ``int`` or a ``float`` -- but a hand-edited entry, or one carried over
    from a future release, can hold anything. Anything unusable becomes ``None``,
    which the battery layer reports as a missing field rather than substituting a
    value for. Booleans are excluded because ``True`` is an ``int``.

    Not ``normalization.parse_numeric``, which owns this question elsewhere,
    because that reads a *sensor state* -- it parses strings and knows about
    ``unavailable``. This reads a stored configuration value, where a string is a
    damaged document rather than a number to be recovered. The small overlap is
    deliberate: the two have different inputs and different correct answers.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    number = float(raw)
    return number if math.isfinite(number) else None


def _number(raw: Any, default: float) -> float:
    """Return a stored numeric option, falling back to a documented default."""
    value = _optional_number(raw)
    return default if value is None else value


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """Resolved source selection for one config entry."""

    house_load_entity: str
    battery_soc_entity: str | None
    battery_power_entity: str | None
    battery_power_sign: str
    daily_house_load_entity: str | None
    #: Optional flexible-load source. When set, baseline load is measured load
    #: minus this; when unset, baseline equals measured.
    ev_power_entity: str | None
    has_pv: bool
    pv_power_entity: str | None
    grid_power_entity: str | None
    grid_power_sign: str
    frank_entry_id: str | None
    use_pv_forecast: bool
    solcast_entry_id: str | None
    #: Which Solcast rooftop sites belong to this installation. Stable
    #: ``resource_id`` values, never display names.
    #:
    #: An empty tuple means the question has not been answered yet, which is
    #: distinct from "none of them": on first successful discovery the resolved
    #: set is persisted once, after which a site newly added to Solcast is
    #: reported as available but unselected rather than silently joining the plan.
    #: An explicitly stored empty list is a different thing again, and produces a
    #: named unavailability rather than a silent fall back to everything.
    selected_solcast_site_ids: tuple[str, ...]
    #: Whether a selection has actually been stored. Without this, "no key" and
    #: "an empty list" would be the same fact, and they are not.
    solcast_selection_stored: bool
    #: Phase-3 battery planning facts. ``None`` where the user has not supplied
    #: one: capacity and the two power limits have no default, because a
    #: capacity cannot be derived from a percentage sensor and a power limit
    #: cannot be inferred from a capacity without assuming a C-rate. Absent
    #: means the battery layer declines to decide and says which field is
    #: missing -- it never means a guessed value.
    battery_capacity_kwh: float | None
    battery_max_charge_kw: float | None
    battery_max_discharge_kw: float | None
    #: These two do have defaults, which are choices rather than measurements of
    #: this particular battery. See ``const.py``.
    battery_min_soc_percent: float
    battery_round_trip_efficiency_percent: float
    #: Phase-4 control settings. All three have defaults: unlike the battery
    #: hardware facts, none of them describes the installation, so a sensible
    #: value is a choice rather than a guess about someone's equipment.
    control_export_margin_percent: float
    #: Read, and deliberately absent from the options form while the release
    #: barrier makes it unable to change anything.
    control_execution_enabled: bool
    #: A ceiling, in kWh, on grid energy one Live charge run may buy. Zero means
    #: the commissioning tightener is off, never that charging is forbidden.
    grid_charge_budget_kwh: float
    #: Phase-8 economic settings. A threshold, a per-kWh margin, a throughput cost
    #: and two opt-ins, all in the form because every one of them changes the
    #: *published plan* -- so a user can see what a setting did before any command
    #: is sent. The execution flag above is the separate question of whether one
    #: is, and since beta.24 it decides exactly that.
    minimum_trade_gain_eur: float
    grid_charge_margin_eur_per_kwh: float
    #: Wear per kWh of AC throughput in both directions. Off by default; see
    #: :data:`CONF_BATTERY_THROUGHPUT_COST_EUR_PER_KWH` for why the honest default
    #: is zero rather than a plausible-looking couple of cents.
    battery_throughput_cost_eur_per_kwh: float
    allow_grid_charging: bool
    allow_battery_export: bool
    #: What the battery cost, what came back, and when it was bought. beta.42.
    #:
    #: **``None`` where the three levers above carry defaults**, and the difference
    #: is deliberate: an installation that has not entered a purchase price is not
    #: one whose battery was free. So the return sensor is unavailable with a named
    #: reason rather than publishing a recovery against zero -- the same distinction
    #: the three absent hardware facts already draw.
    #:
    #: **These reach nothing that decides.** Capital cost is not a marginal cost and
    #: most of an installed battery is sunk, which is the reasoning the three
    #: economic levers are built on; a purchase price that could move a dispatch
    #: would be a category error, not a feature.
    battery_investment_eur: float | None
    battery_subsidy_eur: float | None
    other_one_time_credit_eur: float | None
    battery_investment_date: str | None

    @classmethod
    def from_entry(cls, entry: ConfigEntry) -> SourceConfig:
        """Build the effective configuration.

        Options shadow data, so an Options Flow change takes effect without the
        user having to delete and re-add the integration.
        """

        def value(key: str, default: Any = None) -> Any:
            return entry.options.get(key, entry.data.get(key, default))

        return cls(
            house_load_entity=value(CONF_HOUSE_LOAD_ENTITY, ""),
            battery_soc_entity=value(CONF_BATTERY_SOC_ENTITY),
            battery_power_entity=value(CONF_BATTERY_POWER_ENTITY),
            battery_power_sign=value(
                CONF_BATTERY_POWER_SIGN, DEFAULT_BATTERY_POWER_SIGN
            ),
            daily_house_load_entity=value(CONF_DAILY_HOUSE_LOAD_ENTITY),
            ev_power_entity=value(CONF_EV_POWER_ENTITY),
            has_pv=bool(value(CONF_HAS_PV, False)),
            pv_power_entity=value(CONF_PV_POWER_ENTITY),
            grid_power_entity=value(CONF_GRID_POWER_ENTITY),
            grid_power_sign=value(CONF_GRID_POWER_SIGN, DEFAULT_GRID_POWER_SIGN),
            frank_entry_id=value(CONF_FRANK_ENTRY_ID),
            use_pv_forecast=bool(value(CONF_USE_PV_FORECAST, False)),
            solcast_entry_id=value(CONF_SOLCAST_ENTRY_ID),
            selected_solcast_site_ids=_site_ids(value(CONF_SELECTED_SOLCAST_SITE_IDS)),
            solcast_selection_stored=(
                CONF_SELECTED_SOLCAST_SITE_IDS in entry.options
                or CONF_SELECTED_SOLCAST_SITE_IDS in entry.data
            ),
            battery_capacity_kwh=_optional_number(value(CONF_BATTERY_CAPACITY_KWH)),
            battery_max_charge_kw=_optional_number(value(CONF_BATTERY_MAX_CHARGE_KW)),
            battery_max_discharge_kw=_optional_number(
                value(CONF_BATTERY_MAX_DISCHARGE_KW)
            ),
            battery_min_soc_percent=_number(
                value(CONF_BATTERY_MIN_SOC_PERCENT),
                DEFAULT_BATTERY_MIN_SOC_PERCENT,
            ),
            battery_round_trip_efficiency_percent=_number(
                value(CONF_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT),
                DEFAULT_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
            ),
            control_export_margin_percent=_number(
                value(CONF_CONTROL_EXPORT_MARGIN_PERCENT),
                DEFAULT_CONTROL_EXPORT_MARGIN_PERCENT,
            ),
            grid_charge_budget_kwh=_number(
                value(CONF_GRID_CHARGE_BUDGET_KWH),
                DEFAULT_GRID_CHARGE_BUDGET_KWH,
            ),
            control_execution_enabled=bool(
                value(
                    CONF_CONTROL_EXECUTION_ENABLED,
                    DEFAULT_CONTROL_EXECUTION_ENABLED,
                )
            ),
            minimum_trade_gain_eur=_number(
                value(CONF_MINIMUM_TRADE_GAIN_EUR),
                DEFAULT_MINIMUM_TRADE_GAIN_EUR,
            ),
            grid_charge_margin_eur_per_kwh=_number(
                value(CONF_GRID_CHARGE_MARGIN_EUR_PER_KWH),
                DEFAULT_GRID_CHARGE_MARGIN_EUR_PER_KWH,
            ),
            # **Read here or it silently does nothing.** The beta.21 lesson: the
            # margin above was accepted by ``solve`` and present in the config for
            # a whole release while this reader was the missing link, and stock
            # installs never noticed because the default is zero.
            battery_throughput_cost_eur_per_kwh=_number(
                value(CONF_BATTERY_THROUGHPUT_COST_EUR_PER_KWH),
                DEFAULT_BATTERY_THROUGHPUT_COST_EUR_PER_KWH,
            ),
            allow_grid_charging=bool(
                value(CONF_ALLOW_GRID_CHARGING, DEFAULT_ALLOW_GRID_CHARGING)
            ),
            allow_battery_export=bool(
                value(CONF_ALLOW_BATTERY_EXPORT, DEFAULT_ALLOW_BATTERY_EXPORT)
            ),
            # ``_optional_number`` rather than ``_number``: absent has to survive as
            # absent all the way to the sensor, or the reason it publishes would be
            # a lie about a figure that had already been turned into a zero.
            battery_investment_eur=_optional_number(value(CONF_BATTERY_INVESTMENT_EUR)),
            battery_subsidy_eur=_optional_number(value(CONF_BATTERY_SUBSIDY_EUR)),
            other_one_time_credit_eur=_optional_number(
                value(CONF_OTHER_ONE_TIME_CREDIT_EUR)
            ),
            battery_investment_date=_optional_date(value(CONF_BATTERY_INVESTMENT_DATE)),
        )


def _optional_number(raw: Any) -> float | None:
    """Return a finite float, or ``None`` -- and never a default.

    The distinction this preserves is the whole of beta.42's investment semantics:
    *not entered* and *entered as zero* are different facts, and only the second is
    a measurement. Collapsing them would let an installation that has said nothing
    publish a recovery percentage against a battery that cost nothing.
    """
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or abs(value) == float("inf"):
        return None
    return value


def _optional_date(raw: Any) -> str | None:
    """Return an ISO date string, or ``None``.

    Validated as a real date rather than trusted as a string, because it becomes
    the declared start of a lifetime accounting period. A malformed entry reads as
    no investment date -- which the provenance block then reports -- rather than as
    a date nothing can compare against.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def _site_ids(raw: Any) -> tuple[str, ...]:
    """Return stored Solcast site identifiers, sorted and de-duplicated.

    Sorted so the stored order cannot affect a fingerprint, and filtered so a
    hand-edited document containing a number or a null degrades to the entries
    that are usable rather than to a crash on the next refresh.
    """
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(sorted({item for item in raw if isinstance(item, str) and item}))


def _capacity_total(values: Iterable[float | None]) -> float | None:
    """Return the sum of the capacities that were reported, or ``None``.

    ``None`` when the source reported none of them, which is not the same as a
    total of zero: one is "it did not say" and the other is "there is no array".
    """
    present = [value for value in values if value is not None]
    if not present:
        return None
    return round(sum(present), 4)


#: Chosen lattice per battery configuration. The search builds up to eighty
#: candidate transition tables and costs about 195 ms, which is fine once and
#: absurd every quarter-hour -- so it is memoised on the only two inputs that can
#: change it. Keyed rather than cached on the coordinator because the solve runs
#: in an executor and must not read coordinator state from another thread. A
#: benign race writes the same answer twice.
_BUCKET_CHOICE: dict[tuple[BatteryLimits, int], tuple[float, str]] = {}


def _bucket_for(limits: BatteryLimits, floor_energy_kwh: float) -> tuple[float, str]:
    """Return the lattice for this configuration, choosing it at most once."""
    key = (limits, round(floor_energy_kwh * 1000.0))
    chosen = _BUCKET_CHOICE.get(key)
    if chosen is None:
        chosen = select_bucket_kwh(limits, floor_energy_kwh=floor_energy_kwh)
        _BUCKET_CHOICE[key] = chosen
    return chosen


@dataclass(frozen=True, slots=True)
class PlanningInputs:
    """Everything beta.31 added to the planning decision, computed in one place.

    **A bundle rather than six more positional arguments**, because
    ``async_add_executor_job`` passes positionally and a list that long is a
    silently-shifted argument waiting to happen -- which is exactly the fault
    ``test_the_executor_call_passes_every_parameter_it_declares`` exists to catch.

    It also keeps the seam. ``_solve_economic`` stays a pure function of its
    inputs: a caller may hand it any reserve curve and any edge value, which is
    what lets a suite vary one economic term at a time. ``None`` at the call site
    means *the pre-beta.31 question* -- enforce the curve you were given, value
    the horizon edge at nothing -- so every existing test still describes the
    behaviour it was written for, and the production switch is one argument.
    """

    #: The curve the solver must obey. Reachability in production; whatever the
    #: caller supplies in a test.
    enforced_reserve: tuple[float | None, ...]
    #: The autonomy curve, carried for diagnostics and consumed by no solve.
    autonomy_reserve: tuple[float | None, ...]
    reachability: Any
    uncertainty: Any
    actionable_intervals: int
    edge_value_eur_per_kwh: float
    edge_creditable_kwh: float
    #: Whether to also solve the problem under beta.30's economics, for reading.
    #:
    #: **Shadow only, and temporary.** It doubles the solve to publish something no
    #: decision consumes, which is worth it exactly once: while the change of
    #: architecture is being watched against live inputs before it is trusted with
    #: money. Never in Live, where the plan is executing and the payload is not
    #: being read.
    compare_legacy: bool = False
    #: The measured forecast-quality evidence the export permission may use, or
    #: ``None`` to leave the permission off -- which is what every pre-beta.32 test
    #: gets, so each still describes the behaviour it was written for.
    forecast_risk: Any = None
    #: Whether the inverter covers residual house load from the battery when
    #: nothing is dispatched. **Unknown means not modelled**, so the default is the
    #: pre-beta.32 counterfactual: an idle interval imports at full price.
    ambient_self_consumption: bool = False
    #: What energy left at the horizon's end is worth. ``None`` keeps the beta.34
    #: flat credit, so every pre-beta.35 caller solves the problem it always did.
    terminal_value: Any = None
    #: The run state the head is already in, reported by Stage B as a fact. See
    #: ``solve``'s own parameter for why continuing must not pay a second fee.
    head_run_state: int = RUN_STATE_IDLE


def _solve_economic(
    limits: BatteryLimits,
    floor_energy_kwh: float,
    start_energy_kwh: float,
    terminal_floor_kwh: float,
    demands: tuple[IntervalDemand, ...],
    prices: tuple[IntervalPrice, ...],
    raw_reserve: tuple[float | None, ...],
    reserve_above_capacity_kwh: float,
    minimum_trade_gain_eur: float,
    grid_charge_margin_eur_per_kwh: float,
    battery_throughput_cost_eur_per_kwh: float,
    planning: PlanningInputs | None,
    allow_grid_charging: bool,
    allow_battery_export: bool,
) -> EconomicOutcome | None:
    """Build the physics table and run the four solves. Executor-side.

    Positional rather than keyword because ``async_add_executor_job`` passes
    positionally, and a module-level function rather than a method so nothing
    about the coordinator's state can be read from another thread.

    **Both** economic settings are parameters, and that is the whole of the
    beta.21 fix. ``grid_charge_margin_eur_per_kwh`` was read into the config and
    accepted by ``solve``, and this signature was the gap between them: the
    executor call passes positionally, so a parameter that is not here is a
    setting that silently does nothing. Stock installs were unaffected because
    the default is zero, which is exactly why it went unnoticed.
    """
    started = time.perf_counter()
    bucket_kwh, bucket_rule = _bucket_for(limits, floor_energy_kwh)
    table = build_physics_table(
        limits, floor_energy_kwh=floor_energy_kwh, bucket_kwh=bucket_kwh
    )
    if table is None:  # pragma: no cover - build_limits precludes it
        return None
    table_ms = (time.perf_counter() - started) * 1000.0

    # **What the solver obeys, and it is decided by the caller.**
    #
    # ``raw_reserve`` used to be handed straight in, and it is the **autonomy**
    # curve: the minimum stored energy if the grid is never used again, over the
    # whole forecast. As a hard lexicographic floor that demanded 73 % state of
    # charge against a 20 % physical floor on the reference installation --
    # immobilising 96.9 % of the usable pack and making purchases compulsory at
    # any price.
    #
    # Since beta.31 production passes a ``PlanningInputs`` carrying the
    # **reachability** curve instead: can the pack hold the floor given
    # replenishment that is physically possible and actionable? No bundle means
    # the older question, which is what keeps every pre-beta.31 suite meaningful.
    enforced_reserve = raw_reserve if planning is None else planning.enforced_reserve
    compare_legacy = planning is not None and planning.compare_legacy
    edge_value = 0.0 if planning is None else planning.edge_value_eur_per_kwh
    edge_creditable = float("inf") if planning is None else planning.edge_creditable_kwh
    terminal_value = None if planning is None else planning.terminal_value
    head_run_state = RUN_STATE_IDLE if planning is None else planning.head_run_state

    horizon = build_horizon(
        demands=demands,
        prices=prices,
        required_reserve_kwh=enforced_reserve,
        table=table,
    )
    if not horizon.intervals:
        return None

    return build_outcome(
        table=table,
        horizon=horizon,
        start_energy_kwh=start_energy_kwh,
        terminal_floor_kwh=terminal_floor_kwh,
        floor_energy_kwh=floor_energy_kwh,
        minimum_trade_gain_eur=minimum_trade_gain_eur,
        grid_charge_margin_eur_per_kwh=grid_charge_margin_eur_per_kwh,
        battery_throughput_cost_eur_per_kwh=battery_throughput_cost_eur_per_kwh,
        allow_grid_charging=allow_grid_charging,
        allow_battery_export=allow_battery_export,
        terminal_value=terminal_value,
        head_run_state=head_run_state,
        reserve_above_capacity_kwh=reserve_above_capacity_kwh,
        table_ms=table_ms,
        bucket_rule=bucket_rule,
        edge_value_eur_per_kwh=edge_value,
        edge_creditable_kwh=edge_creditable,
        compare_legacy=compare_legacy,
        autonomy=raw_reserve if planning is None else planning.autonomy_reserve,
        reachability=None if planning is None else planning.reachability,
        uncertainty=None if planning is None else planning.uncertainty,
        actionable_interval_count=(
            0 if planning is None else planning.actionable_intervals
        ),
        forecast_risk=None if planning is None else planning.forecast_risk,
        ambient_self_consumption=(
            False if planning is None else planning.ambient_self_consumption
        ),
    )


def _execution_block(report: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the execution block, or ``None`` when there is nothing usable.

    Every reader goes through here. beta.19 dereferenced
    ``report["execution"]["power"]["requested_kw"]`` directly, and ``power`` is
    ``None`` for every Stage-B state reachable today -- so the first authorized
    send would have raised inside a call sitting *outside* the safe wrapper and
    taken the whole refresh down with it.
    """
    if not isinstance(report, dict):
        return None
    block = report.get("execution")
    return block if isinstance(block, dict) else None


def _mark_execution_error(
    report: dict[str, Any] | None, reason: str, *, failed: bool = True
) -> None:
    """Record an execution error without assuming the report is well-formed.

    ``failed`` separates the two things that reach here, and beta.31 needed the
    distinction because it now shows on an entity. A *refusal* -- the execution
    barrier declining before the first service call -- is the expected outcome of a
    non-writing release and says nothing went wrong. A *failure* -- a rejected
    write, or one that could not be read back -- is something a user should look
    at.

    Until beta.31 both left the report's own ``state`` at whatever eligibility had
    computed *before* the write was attempted, so a failed command published as
    ``eligible`` or ``inhibited``. A reader watching the Control State entity could
    not tell a refresh that sent nothing from one whose command failed, which is
    the single most important distinction the entity can carry.
    """
    if failed and isinstance(report, dict):
        # Set on the report rather than at the call sites, so a future error path
        # cannot forget it. Before the block lookup, because an error is an error
        # whether or not the execution block came out well-formed.
        report["state"] = CONTROL_STATE_ERROR
    block = _execution_block(report)
    if block is None:
        return
    result = block.get("result")
    if not isinstance(result, dict):
        result = {}
        block["result"] = result
    result["execution_error"] = reason


def _mark_command_result(report: dict[str, Any] | None, result: str) -> None:
    """Record whether the last staged write succeeded, as a command result.

    Deliberately not the public state. "The write returned" and "the battery is
    moving" are different facts, and beta.33 published the first under the name of
    the second.
    """
    block = _execution_block(report)
    if block is None:
        return
    outcome = block.get("result")
    if not isinstance(outcome, dict):
        outcome = {}
        block["result"] = outcome
    outcome["command_result"] = result


def _mark_arm_refused(report: dict[str, Any] | None, reason: str) -> None:
    """Record why an arming sequence was refused before anything was written.

    Separate from ``execution_error`` because the two answer different questions:
    that one says the refresh did not succeed, this one says *what the controller
    declined to do and why*. A reader looking at a live dispatch that never
    started needs the second.
    """
    block = _execution_block(report)
    if block is None:
        return
    result = block.get("result")
    if not isinstance(result, dict):
        result = {}
        block["result"] = result
    result["arm_refused_reason"] = reason


def _mark_execution_applied(
    report: dict[str, Any] | None, applied_kw: float | None
) -> None:
    """Record what was actually written, defensively.

    A malformed or partial report degrades to doing nothing rather than raising:
    the figures are diagnostics, and losing one is a cost worth paying to keep the
    coordinator loop alive.
    """
    block = _execution_block(report)
    if block is None:
        return
    power = block.get("power")
    if not isinstance(power, dict):
        power = {}
        block["power"] = power
    power["applied_kw"] = 0.0 if applied_kw is None else float(applied_kw)
    power["executed"] = True


def _claim_id(run_id: str, now: datetime) -> str:
    """Return a stable identity for one physical claim.

    **One arm, one identity.** A run id names an economic run and legitimately
    outlives several arms -- a run stopped and restarted inside its window keeps it.
    Progress and ownership need to distinguish the *arms*, because delivered energy
    belongs to the arm that delivered it.

    Derived from the run and the instant of the write, so it is reproducible from the
    record and needs no counter to persist.
    """
    seed = f"{run_id}:{now.isoformat()}".encode()
    return hashlib.blake2s(seed, digest_size=8).hexdigest()


def _dispatch_start_instant(snapshot: Any, now: datetime) -> datetime | None:
    """Return the device's dispatch start as an instant, or ``None``.

    The AlphaESS surface publishes a numeric register, not a timestamp, while the
    ownership comparison works in instants -- passing the raw float through would
    have raised on the subtraction. There is no calendar in that register to
    recover, so what is compared is *the reading itself*: the record stores
    whatever the register said when the command was written, and ownership requires
    the live register to still say the same thing.

    Represented as an instant offset from the day's start so the existing tolerance
    comparison works unchanged, and so a register that resets cannot silently
    match. A missing or zero reading yields ``None``, which the ownership rule
    treats as "no evidence" rather than as agreement.
    """
    if snapshot is None:
        return None
    raw = getattr(snapshot, "dispatch_start", None)
    if raw is None or not isinstance(raw, (int, float)) or raw != raw:
        return None
    if raw <= 0:
        return None
    return now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        seconds=float(raw)
    )


def _export_check(
    context: ControlContext,
    *,
    requested: Any,
    command: Any,
    safe_power_kw: float,
    inhibit_reason: str | None,
) -> dict[str, Any]:
    """Return the export bound as the gate saw it, and what it did to the command.

    Reported at the instant the gate ran rather than at download time. Without
    that a ``would_export`` verdict could not be reconstructed from a diagnostics
    download, because the flow block elsewhere in the payload is read later and
    describes a different instant -- which is exactly how a correct inhibit came
    to look arithmetically wrong beside the readings printed next to it.

    Every field is a bounded scalar, and the three powers are reported separately
    on purpose: what was asked for, what was safe, and what would be sent. A
    single ``commanded_power_kw`` could not distinguish a command that was never
    limited from one that was reduced to exactly the bound.
    """
    capacity_kw = absorbing_capacity_kw(context)
    requested_kw = 0.0 if requested is None else requested.power_kw
    final_kw = 0.0 if command is None else command.power_kw
    limited = bool(command is not None and command.safety_limited)
    return {
        # The three powers verbatim: ``device_power_kw`` already rounds to the
        # helper's own decimals, so these are the exact figures that would be
        # written. Rounding again here could only make the report disagree with
        # the command.
        "requested_power_kw": requested_kw,
        "absorbing_capacity_kw": round(capacity_kw, 4),
        "safety_margin_percent": context.export_margin_percent,
        "safe_capacity_kw": round(safe_power_kw, 4),
        "safety_limited": limited,
        "limited_power_kw": final_kw if limited else None,
        "final_command_power_kw": final_kw,
        "grid_import_w": context.grid_import_w,
        "grid_export_w": context.grid_export_w,
        "battery_power_w": context.battery_power_w,
        "inhibit_reason": inhibit_reason,
        "basis": (
            "capacity = max(0, grid_import - grid_export + battery discharge), "
            "measured at the meter; the margin reduces the capacity, never the "
            "command. non-export discharge is clamped down to the maximum safely "
            "absorbable measured household/grid demand; if no representable safe "
            "command remains, the command is refused"
        ),
        "ordering": (
            "measure capacity, apply the margin to the capacity, clamp the "
            "requested command to what remains, quantise downwards to a helper "
            "step, then recompute the commanded energy"
        ),
    }


def _reserve_horizon_edges(
    projection: ReserveProjection,
    *,
    today: date,
    tomorrow: date,
    today_interval_count: int,
    tz: tzinfo,
) -> tuple[datetime | None, datetime | None]:
    """Return the absolute instants a reserve horizon spans.

    The requirement is indexed by the plan's continuous chronological index,
    which runs through today and straight on into tomorrow, so an index at or
    beyond today's real length names an interval of tomorrow. Resolving that
    needs the civil day and its real length -- 92, 96 or 100 -- which is exactly
    the calendar knowledge the reserve module deliberately does not have, so it is
    done here and handed in.

    Absolute UTC throughout, and the end is the *start of the interval after* the
    last one, so a horizon reports the instant it stops covering rather than the
    beginning of its final quarter.
    """
    if not projection.intervals:
        return None, None

    def moment(index: int) -> datetime:
        if index < today_interval_count:
            return interval_start_utc(today, index, tz)
        return interval_start_utc(tomorrow, index - today_interval_count, tz)

    first = projection.intervals[0].index
    last = projection.intervals[-1].index
    return moment(first), moment(last) + timedelta(minutes=QUARTER_MINUTES)


def _tally(counts: dict[str, int], key: str) -> None:
    """Increment ``key`` in a bounded counter mapping."""
    counts[key] = counts.get(key, 0) + 1


class _ThrottledLogger:
    """Emits each distinct warning at most once per throttle window.

    An entity that stays unavailable for a day would otherwise produce a warning
    every minute, which buries anything genuinely new in the log.
    """

    def __init__(self) -> None:
        self._last: dict[str, datetime] = {}
        self._suppressed: dict[str, int] = {}

    def warning(self, key: str, message: str, *args: Any) -> bool:
        """Log ``message`` unless the same ``key`` fired recently.

        Returns whether the line was actually emitted. Callers that record a
        user-visible "last warning" timestamp must consult it: reporting a
        warning that the throttle swallowed makes diagnostics contradict the log
        it is meant to explain, and sends the reader looking for an entry that
        was never written.
        """
        now = dt_util.utcnow()
        previous = self._last.get(key)
        if previous is not None and (now - previous) < timedelta(
            seconds=LOG_THROTTLE_SECONDS
        ):
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            return False
        skipped = self._suppressed.pop(key, 0)
        self._last[key] = now
        if skipped:
            # Formatted defensively: a caller whose message carries a literal
            # ``%`` and no arguments would otherwise raise from inside the
            # logging call, turning a suppressed-warning tally into an exception
            # on the sampling path.
            try:
                rendered = message % args
            except (TypeError, ValueError):
                rendered = message
            _LOGGER.warning("%s (%d further occurrences suppressed)", rendered, skipped)
        else:
            _LOGGER.warning(message, *args)
        return True

    def clear(self, key: str) -> None:
        """Note that ``key``'s condition has resolved.

        The throttle window is deliberately **not** reset here. Clearing it let a
        flapping source defeat the throttle entirely: a charger that alternates
        between a value and ``unavailable`` resolves and re-fails between
        successive reads, and since these run on every state change of a
        fast-updating house-load sensor, that produced a warning per read --
        indefinitely, and at whatever rate the fastest source publishes. That is
        precisely the log burial this class exists to prevent.

        Only the suppression tally is dropped, so the next genuine warning after
        the throttle window expires does not claim occurrences that belonged to an
        earlier, already-resolved episode.
        """
        self._suppressed.pop(key, None)


class AlphaEmsCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Learns household load and publishes derived forecasts."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialise the coordinator for ``entry``."""
        super().__init__(
            hass,
            _LOGGER,
            name=entry.title,
            config_entry=entry,
            # Event driven: refreshes are triggered by the quarter-hour tick,
            # never by a polling interval.
            update_interval=None,
        )
        self.entry = entry
        self.config = SourceConfig.from_entry(entry)
        self.store = LearningStore(hass, entry.entry_id)
        #: Phase-2 forecast evidence. Deliberately a separate document set from
        #: the learning history: the two have different retention horizons, and
        #: a schema migration that discards one must not take the other with it.
        self.history = ForecastHistoryStore(hass, entry.entry_id)
        self.recorder = ForecastRecorder(self.history, self.store)
        #: Result of the most recent recording pass, for sensors and diagnostics.
        self.last_record: RecorderResult = RecorderResult()
        self._accumulator = QuarterAccumulator(dt_util.get_default_time_zone())
        self._ev_accumulator: QuarterAccumulator | None = None
        #: Measured PV generation, integrated on the same machinery as the other
        #: two. Present only when a PV source is configured. Its output is
        #: additive evidence: it can never reject an interval or change a learned
        #: baseline, which is what keeps ``test_pv_independence.py`` true.
        self._pv_accumulator: QuarterAccumulator | None = None
        #: Grid import and export, integrated separately. Two accumulators rather
        #: than one signed series, because the canonical convention resolves the
        #: source's sign once at the edge and nothing downstream should have to
        #: reason about it again -- the same rule ``PowerFlows`` exists to enforce.
        self._grid_import_accumulator: QuarterAccumulator | None = None
        self._grid_export_accumulator: QuarterAccumulator | None = None
        #: Measured battery power, integrated so Stage B can read delivered energy
        #: *within* a quarter rather than waiting for one to close.
        #:
        #: A separate accumulator rather than a reading of the existing ones,
        #: because none of them measures the battery and because
        #: ``setpoint x elapsed`` is not the same quantity: a clamp, a limit, a
        #: cloud or a full pack all make what arrived differ from what was asked
        #: for, and a controller trusting the request would compound its own error
        #: every refresh. Sign-normalised charge only -- discharge is a separate
        #: question and is not what a charge target is measured against.
        self._battery_charge_accumulator: QuarterAccumulator | None = None
        self._log = _ThrottledLogger()
        self.last_balance: BalanceSample | None = None
        #: The Stage-A execution targets this refresh published. Read by the
        #: Stage B controller, which computes the command a Live run would send
        #: and sends nothing: the execution barrier is closed and no service call
        #: is reachable from that path. Held on the coordinator rather than
        #: recomputed in diagnostics because a revision number has to be a
        #: property of the refresh that produced it, not of whoever happened to
        #: download a report.
        self.execution_targets: tuple[dict[str, Any], ...] = ()
        #: The same, as remembered across a restart. Only the fields
        #: ``execution_revision`` compares, so a reboot does not announce every
        #: target it has been tracking for hours as brand new.
        self._execution_revisions: dict[str, dict[str, Any]] = {}
        #: What Stage B concluded this refresh, for diagnostics. A dict rather
        #: than the dataclass so the payload builder cannot reach the controller's
        #: internals and start deciding things with them.
        self.execution_report: dict[str, Any] = {}
        #: Delivered battery energy inside the current execution window, and the
        #: window it belongs to. Session-scoped: after a restart progress is
        #: reconstructed from the persisted state-of-charge series instead, which
        #: is the only basis that survives one.
        #: Which plan the delivered-energy total belongs to.
        #:
        #: The **run id**, which is minted by Stage B and stable for the life of
        #: the run. Two earlier keys were both wrong, and in opposite directions:
        #: beta.19 used ``(plan_id, revision)``, so a revision bump discarded
        #: delivery already made and the controller re-demanded energy already in
        #: the pack; ``plan_id`` alone then looked right but churns every refresh
        #: as the horizon rolls, which would have reset these figures every quarter
        #: and brought the sawtooth back by a different route.
        #:
        #: A revision moves the target; it does not un-deliver the kilowatt-hours.
        #: Neither does the horizon advancing.
        self._execution_run: str | None = None
        self._execution_window_start_kwh: float | None = None
        #: Delivered battery energy in *closed* quarters of this window.
        #:
        #: Kept here because ``QuarterAccumulator`` zeroes at every boundary --
        #: ``open_energy_kwh`` is one quarter, never a run. beta.19 read it as
        #: cumulative progress, which sawtoothed and near a window's end asked for
        #: about 35 kW.
        self._execution_closed_kwh: float = 0.0
        #: Grid energy attributed to charging this pack, this window. An estimate
        #: from the measured balance, monotonic by construction.
        self._execution_grid_kwh: float = 0.0
        #: Instant of the last attribution sample, so gaps can be detected.
        self._grid_attribution_at: datetime | None = None
        #: The most recent Stage B decision, so the command and the published
        #: report describe the same refresh.
        self._stage_b_decision: Any = None
        #: The last carried run that ended, and why. **Session-local and never
        #: persisted**: it records what this session observed, and a restart
        #: legitimately forgets it rather than restating a stale claim as a fact.
        #:
        #: It exists because ``carried.ended_reason`` is truthful for exactly one
        #: refresh. A real diagnostics download taken three refreshes after a
        #: withdrawal carried nothing at all about it, so answering "why did the
        #: run stop?" meant reconstructing it from two snapshots and the event ring.
        self._last_ended: dict[str, Any] | None = None
        #: The Stage-A target Stage B has accepted and is carrying, if any.
        #:
        #: Deliberately **not** persisted. A carried run is a prediction admitted
        #: before the restart and Stage A republishes within one refresh, so
        #: resuming one would buy at most a single interval and inherit a whole
        #: class of stale-resume risk. The ownership *record* is persisted, because
        #: not abandoning a live dispatch is a different question.
        self._carried: CarriedRun | None = None
        #: Session-scoped balance tally and debounce state. Not persisted:
        #: a restart must not inherit a failure run that may already be over.
        self.balance = BalanceMonitor()
        self.last_finalized_quarter: datetime | None = None
        self.rejected_quarters = 0
        #: Rejected quarters by reason. Bounded by the reason constants above,
        #: so it cannot grow with runtime.
        self.rejected_quarters_by_reason: dict[str, int] = {}
        #: The most recently rejected interval and why, for diagnostics.
        self.last_rejected_quarter: datetime | None = None
        self.last_rejected_reason: str | None = None
        #: Intervals whose measured load was accepted but whose flexible-load
        #: reading was not, so their baseline is unusable.
        self.invalid_ev_quarters = 0
        #: Flexible-load invalidations by reason, same key space as above.
        self.invalid_ev_quarters_by_reason: dict[str, int] = {}
        #: Source problems observed since the last interval was attributed.
        #: Held across the interval rather than read at the boundary, so an
        #: outage in the middle of a quarter is still named as its cause
        #: instead of being reported as bare insufficient coverage.
        self._house_problem: str | None = None
        self._ev_problem: str | None = None
        #: Whether the configured flexible-load entity has ever been readable,
        #: and how many refreshes in a row it has been absent. Home Assistant
        #: brings integrations up in an arbitrary order, so an entity this one
        #: depends on is routinely missing for the first refresh or two after a
        #: restart -- and a warning then describes the startup sequence rather
        #: than the configuration. **Learning is paused from the first absence
        #: regardless:** these two fields govern the log line only.
        self._ev_seen = False
        self._ev_absences = 0
        #: The zone both accumulators label their buckets in. Compared against
        #: the live zone so a change can be acted on rather than silently
        #: splitting the write path across two calendars.
        self._tz_key = str(dt_util.get_default_time_zone())
        #: The selected control mode. Held here rather than on the entity so the
        #: pipeline and the thing the user sees cannot disagree about it. Starts
        #: off, and the entity restores the stored value once it is added.
        self.control_mode = CONTROL_MODE_OFF
        #: Session-scoped comparison of measured state of charge against measured
        #: battery power. Instrumentation: it gates nothing.
        self.soc_coherence = SocCoherenceMonitor()
        #: Recent control outcomes, newest first, bounded for diagnostics.
        self._control_events: list[dict[str, Any]] = []
        #: When a command was last sent, and at what power. Both stay ``None``
        #: while the execution barrier is closed, because nothing is ever sent --
        #: but they are now *assigned* on the one path that would send something,
        #: so beta.20 flips a barrier rather than also discovering that the
        #: cooldown gate had never had anything to read.
        self._last_control_write: datetime | None = None
        self._last_control_power_kw: float | None = None
        #: What this refresh would send, held only for the single send site.
        self._pending_commands: tuple[Any, ...] = ()
        self._pending_power_kw: float | None = None
        #: The command and the device reading the pending steps were built from,
        #: held for the same reason the steps are: the causal record has to be
        #: written from what was *decided*, at the instant of writing, and the send
        #: site is the only place that ordering can be honoured.
        self._pending_command: Any = None
        self._pending_snapshot: Any = None
        #: Whether the pending steps stop a run rather than start one. A reset must
        #: never write a claim of ownership.
        self._pending_is_reset: bool = False
        #: Whether the pending write is the degraded emergency stop, which ends the
        #: execution as surely as a reset does. See ``_pending_is_emergency`` where
        #: it is set for the argument.
        self._pending_is_emergency: bool = False
        #: Why this refresh's staged write is a reset, for the teardown.
        self._pending_stop_reason: str | None = None
        # **The two stages of the pending sequence, and what is checked between
        # them.** Held apart rather than as one list because the whole point of
        # beta.25 is that stage two is *conditional*: an arm may not activate
        # until the claim reads back, and a stop may not disturb a running
        # dispatch's fields until the deactivation reads back. Their concatenation
        # is always exactly the list the report published.
        # **One lock, over every actuator sequence.** Until beta.25 there was a
        # single write path -- the quarter refresh -- so nothing needed
        # serialising. The sixty-second physical controller is a second one, and a
        # Home Assistant timer callback is not serialised against a coordinator
        # refresh, so without this the two can interleave mid-sequence. The arm is
        # the dangerous case: mode, power, cutoff and duration must all be settled
        # before the enable, and a correction landing between them would arm a
        # dispatch against half-written values.
        self._execution_lock = asyncio.Lock()
        # --- beta.27: the per-quarter execution envelope ---------------------
        #: The quarter currently authorised for execution. Carried in its own
        #: right, because a run that ends at a boundary used to leave the quarter
        #: after it with no carrier at all -- which is the whole of R1.
        #: The frozen quarter schedule of the admitted run. **The carried object
        #: since beta.30**; the executing quarter is derived from it rather than held
        #: in a slot of its own, which is what makes a skipped boundary impossible.
        self._plan: AdmittedPlan | None = None
        #: The row covering this instant, derived from ``_plan`` at the top of every
        #: tick and every refresh. A cache, never an authority.
        self._quarter: CarriedQuarter | None = None
        #: The arm being measured, keyed on the claim id -- which the execution
        #: record's own comment says "names one" physical claim. beta.44.
        self._arm_open: dict[str, Any] | None = None
        #: Finished arm measurements, bounded. The calibration set a later release
        #: needs to price an arm cycle, and a log of nothing else. beta.44.
        self._arm_measurements: deque[dict[str, Any]] = deque(
            maxlen=MAX_ARM_MEASUREMENTS_REPORTED
        )
        #: Whether the previous tick saw a dispatch running, so an activation can be
        #: recognised as a *transition* rather than as pre-existing state. beta.44.
        self._arm_saw_dispatch: bool = False
        #: How many physical arms the latest published plan asks for, and what each
        #: buys. A finished dict, so no reference to a solve is retained. beta.44.
        self._arm_plan: dict[str, Any] = {}
        #: Which solved run each published target came from, so the arm plan can
        #: price an arm against the idle counterfactual without publishing a
        #: horizon index. Rebuilt with the targets every refresh. beta.44.
        self._runs_by_plan_id: dict[str, Any] = {}
        #: Append order for the lifecycle trail, so a reader can order transitions
        #: three cadences wrote without comparing their clocks. beta.43.
        self._lifecycle_seq: int = 0
        #: Which cadence is currently running, stamped onto each transition. beta.43.
        self._lifecycle_cadence: str = CADENCE_QUARTER_REFRESH
        #: What Alpha EMS observed at the instant it released its own dispatch, so
        #: the vendor's dead-man tail can be told from somebody else's run. beta.43.
        #:
        #: **Session-local on purpose.** After a restart there is no run to
        #: recognise and no claim to honour, so the conservative answer -- a running
        #: dispatch we cannot prove is ours is ``foreign`` -- is the correct one.
        #: Persisting it would let a reboot inherit a claim it cannot support.
        self._release_receipt: dict[str, Any] | None = None
        #: Measured progress inside the current quarter, keyed on its start so a
        #: new quarter can never inherit the last one's accumulators.
        self._quarter_key: datetime | None = None
        self._quarter_battery_kwh: float = 0.0
        self._quarter_grid_import_kwh: float = 0.0
        self._quarter_grid_export_kwh: float = 0.0
        self._quarter_sampled_at: datetime | None = None
        self._quarter_peak_kw: float = 0.0
        self._quarter_power_sum: float = 0.0
        self._quarter_power_samples: int = 0
        self._quarter_pv_helped: bool = False
        self._quarter_target_reached_at: datetime | None = None
        self._quarter_clamps: set[str] = set()
        #: What this row actually attempted, and why it did not.
        #:
        #: **beta.36, and the 0.56 kWh row of 2026-08-30 is why.** That row was
        #: admitted, derived, ticked against fifteen times and moved nothing, and the
        #: only thing the record said was ``quarter_expired`` -- which is also
        #: exactly what a mid-row teardown looks like. No tick reason, no
        #: authorisation refusal and no write-boundary refusal could reach the
        #: completed-quarter record, so the payload could not distinguish "never
        #: armed" from "ran its course". These fields make the next occurrence name
        #: itself.
        #:
        #: Kept **apart from** ``binding_clamps``: a clamp reduced a command that was
        #: sent, a refusal means nothing was. Merging them would put
        #: ``reason_vocabulary`` in the position of naming two families at once.
        #: What each row attempted, keyed on the row's own start instant.
        #:
        #: **Keyed on the row rather than held beside the accumulators. beta.36.**
        #: The measured totals are claim-scoped and are reset the instant a claim
        #: appears or a boundary passes, and they are captured and restored around
        #: recording for exactly that reason. Provenance cannot live in that regime:
        #: an arm is what *creates* the claim, so a counter cleared on a claim change
        #: erases the event it exists to record -- measured, and every completed row
        #: reported ``armed: false`` while the inverter was demonstrably armed. Nor
        #: can it be captured, because the reset that clobbers it happens on a
        #: refresh where the ending row is not the one being measured any more.
        #:
        #: One dict, one key, bounded exactly like the completed-row ring it feeds.
        self._quarter_provenance: dict[datetime, dict[str, Any]] = {}
        #: Consecutive unverified zero-kilowatt hold writes. Escalates at
        #: ``HOLD_WRITE_FAILURE_LIMIT``: a dispatch we cannot command down is a
        #: dispatch we do not control.
        #: Consecutive unverified zero writes for the open row. Not provenance: it
        #: is a live escalation counter, and it resets with the row like the totals.
        self._quarter_hold_failures: int = 0
        #: Which row's objective has already been added to the campaign total.
        #:
        #: **The exactly-once guard.** Three call sites can record a completed row --
        #: the tick's end-of-row, the tick's end-of-quarter and the refresh's
        #: between-ticks catch-up -- and nothing said they were mutually exclusive.
        #: One is a lost quarter, two is a double count, and beta.35 published
        #: ``quarters_admitted: 2`` against three completed rows, which is the first.
        self._campaign_accrued_row: datetime | None = None
        # ---------------------------------------------------------------- campaign
        #
        # **One economic campaign, one lifecycle, and the state lives here.**
        #
        # It has to live here for a structural reason rather than a tidiness one:
        # every stop funnel wipes ``_carried``, ``_quarter`` and ``_plan``, and the
        # 60-second tick that ends a campaign publishes no coordinator data at all.
        # Through beta.31 the Activity terminal was derived from those carriers, so
        # a campaign that ended on a tick had nothing left to speak from -- and the
        # measured 17:30-17:45 export terminated in silence while its Planned line
        # stayed standing as though still true. Held apart from the carriers, the
        # campaign's own record survives every one of those paths.
        #
        # **Immutable once started** (beta.32 invariant B12): the objective is
        # frozen at the first confirmed activation, the realised figure only ever
        # accumulates, and a later Stage-A replan can add information but can
        # neither shrink the target nor reset the progress.
        self._campaign_id: str | None = None
        #: This *physical attempt* at the campaign, minted once when it opens.
        #:
        #: **beta.36, and it is a stored field rather than a derived one on
        #: purpose.** The semantic key is ``(campaign_id, opened_at)``, but deriving
        #: it on each refresh would risk silently regenerating it -- the same class
        #: of bug as the beta.29/30 plan-id churn, and the same class as the
        #: ``revision`` trap in :func:`~.execution.admission_key_of`. So it is
        #: created in exactly one place, never recomputed, and immutable for the
        #: life of the attempt.
        #:
        #: An economic campaign may legitimately be attempted twice in one day: once
        #: aborted for a genuine hazard, once afresh. Those are two instances, with
        #: two frozen objectives and two terminals, because they are two different
        #: things that happened.
        self._campaign_instance_id: str | None = None
        #: When this instance opened. Half of the semantic key, published so the
        #: instance id can be checked rather than trusted.
        self._campaign_opened_at: datetime | None = None
        self._campaign_run_id: str | None = None
        self._campaign_end_utc: datetime | None = None
        #: The end Stage A *planned* for this campaign, frozen when it opened.
        #:
        #: Distinct from ``_campaign_end_utc``, which is the furthest row actually
        #: *observed* -- a high-water mark of the past that can only understate the
        #: campaign. Conflating the two is how a thirty-three-row campaign came to
        #: report ``window_end`` three rows in. Both are published, separately.
        self._campaign_planned_end_utc: datetime | None = None
        self._campaign_boundary: str | None = None
        self._campaign_started_at: datetime | None = None
        self._campaign_frozen_target_kwh: float | None = None
        #: The objective read while the campaign was still published, kept so the
        #: freeze has something to freeze. **The freeze is structurally one refresh
        #: late** -- ``_note_campaign_progress`` runs inside the control report,
        #: which is built before ``_async_dispatch`` sets ``activation_confirmed``
        #: -- so by the time the freeze fires, ``execution_targets`` has been
        #: rebuilt from a solve whose head is ``elapsed + 1`` and which therefore
        #: cannot contain the quarter just executed. A campaign of one quarter lost
        #: its target to that every time. Updated on every refresh the campaign is
        #: still named, so it tracks a growing objective right up to activation.
        self._campaign_opening_target_kwh: float | None = None
        #: The *admissions* whose authority was aborted. **beta.36.**
        #:
        #: This replaces ``_abandoned_campaigns`` as the thing the execution path
        #: consults, and the re-key is the whole release. A campaign identity is a
        #: digest of the campaign's **end instant**, so every republication of a live
        #: campaign carries the same one -- and latching it forbade re-admitting the
        #: very campaign whose single row had just finished. On 2026-08-30 that was
        #: five and a half hours of charge running with no row, no ceiling and no
        #: records; on 2026-08-31 it happened again through a different door.
        #:
        #: Keyed on :func:`~.execution.admission_key_of`, so it names one physical
        #: attempt. Strictly *stronger* than the campaign key for the job it was
        #: actually meant to do -- an admission key always exists, whereas
        #: ``campaign_id`` is ``None`` on a pre-beta.32 publication and latched
        #: nothing at all.
        self._abandoned_admissions: list[str] = []
        #: Campaign *instances* that have filed their terminal.
        #:
        #: One Started instance files exactly one terminal, and this is what makes
        #: that true rather than hoped for. Keyed on the instance, not the identity:
        #: keying it on the identity would bar a genuinely new attempt from having
        #: any lifecycle at all, which is the forbidden third state wearing a
        #: different hat.
        self._closed_instances: list[str] = []
        #: Economic campaigns that genuinely *finished*.
        #:
        #: **The asymmetry the approved semantics turn on.** After an **abort**, a
        #: later admission of the same campaign may open a new instance -- it is a
        #: new attempt and deserves its own accounting. After a genuine
        #: **completion** or **window end** the campaign is done, and Stage A
        #: continuing to publish it (because its horizon still contains it) is not a
        #: reason to execute it a second time. Without this a finished campaign
        #: would loop through fresh instances for the rest of the day.
        self._final_campaigns: list[str] = []
        #: Why no plan was admitted this refresh, when one was declined. Diagnostics.
        self._admission_refusal: str | None = None
        #: Why no run is carried, when a publication was declined. Diagnostics.
        self._carry_refusal: str | None = None
        #: Why an armed dispatch is commanding nothing this refresh, if it is.
        self._hold_reason: str | None = None
        #: Whether this refresh took up a persisted claim. Diagnostics only.
        self._adopted_this_refresh: bool = False
        self._campaign_realized_kwh: float = 0.0
        self._campaign_quarters_admitted: int = 0
        #: How many executable rows the frozen objective was summed over.
        #:
        #: Published so ``16.74 kWh`` is readable as *thirty-three rows* rather than
        #: an unexplained scalar. On 2026-08-30 that figure stood beside three
        #: completed rows and nobody could tell whether the number or the close was
        #: wrong without reading the source.
        self._campaign_objective_rows: int = 0
        self._campaign_measurable: bool = True
        #: Every campaign this refresh's solve published, and how the purchase
        #: attribution classifies it. Rebuilt each refresh, because attribution is
        #: allowed to move under a surviving instance and freezing it here would
        #: publish a stale word beside a live figure.
        self._campaign_classifications: dict[str, dict[str, Any]] = {}
        #: The last public terminal, as published. Read by the result sensor, which
        #: therefore never re-derives it -- one derivation, one figure.
        self._last_campaign_result: dict[str, Any] | None = None
        #: The last computed export-price basis, with the sealed set it was
        #: computed for. See :meth:`_roi_price_basis` for why an entity read must
        #: not walk a year of stored issuances.
        self._roi_basis_cache: tuple[tuple[Any, ...], dict[str, Any]] | None = None
        #: The finished campaign, latched. **Not consumed on read**: the surfaces
        #: make it fire once through their own ``closed`` set, and a latch a reader
        #: could exhaust would be a terminal that depends on who looked first.
        self._closed_campaign: dict[str, Any] | None = None
        #: The claim the current quarter's progress belongs to. Progress keys on
        #: ``(claim_id, quarter_start)``, so neither a new arm nor a new row can
        #: inherit measurements taken under the other.
        self._quarter_claim: str | None = None
        #: The read-only dispatch-start probe. Diagnostics only; no decision reads it.
        self._dispatch_start_samples: deque[dict[str, Any]] = deque(
            maxlen=MAX_DISPATCH_START_SAMPLES_REPORTED
        )
        #: The same samples, but **only those taken while a dispatch was running.**
        #:
        #: The ring above is ordered and evicted by time, so a run that ends is
        #: followed by hours of idle ``raw=0`` samples that push the only
        #: informative entries out. Measured: beta.30's probe captured a real Live
        #: charge at 03:15 and by 12:00 every one of its thirty-two entries read
        #: ``0`` with ``phase: before_start``. The register's meaning is still
        #: unmeasured for exactly that reason -- a retention fault, not a probe
        #: fault. Nothing idle is ever appended here.
        self._dispatch_start_active: deque[dict[str, Any]] = deque(
            maxlen=MAX_DISPATCH_START_ACTIVE_SAMPLES
        )
        #: Where the execution lifecycle is, and when it got there.
        self._lifecycle: str = LIFECYCLE_IDLE
        self._lifecycle_at: datetime | None = None
        self._lifecycle_previous: str | None = None
        #: Every transition, in order, bounded. **beta.39.** The lifecycle advances
        #: on three cadences and only one of them publishes a control report, so a
        #: single state field structurally cannot answer "did it reach
        #: ``executing``?" -- see ``LIFECYCLE_TRAIL_LIMIT``.
        self._lifecycle_trail: deque[dict[str, Any]] = deque(
            maxlen=LIFECYCLE_TRAIL_LIMIT
        )
        #: When tomorrow's prices were first observed to be available, this session.
        #:
        #: **A measurement nobody has taken.** The unknown-price policy turns on how
        #: long the unpriced tail actually lasts, and the integration refuses to
        #: predict the publication time -- correctly, since day-ahead can publish
        #: early or late. So it is recorded when it happens instead. Session-local:
        #: a restart forgets it rather than restating a stale claim.
        self._tomorrow_prices_available_at: str | None = None
        #: The bounded history of finished execution quarters.
        self._completed_quarters: deque[dict[str, Any]] = deque(
            maxlen=MAX_COMPLETED_QUARTERS_REPORTED
        )
        #: What each cadence last did, kept apart so a tick reason can never be
        #: published beside quarter-refresh figures as though they were one event.
        self._tick_outcome: TickOutcome | None = None
        self._refresh_outcome: TickOutcome | None = None
        #: Set when a run is adopted from the persisted record after a restart.
        #: ``CarriedQuarter`` is not persisted and quarter progress cannot be
        #: reconstructed without guessing, so an adopted dispatch is **stopped**
        #: rather than continued -- see :meth:`_adopt_persisted_run`.
        self._quarter_progress_unknown: bool = False
        #: The last setpoint actually written, for the deadband comparison. Never
        #: the last *calculated* one: the deadband exists to compare against what
        #: is on the wire.
        self._applied_setpoint_kw: float | None = None
        # **When the setpoint last moved, and by how much.** beta.34, for balance
        # regime attribution only -- nothing in the control path reads either.
        # Kept here rather than derived from ``_last_control_write`` because that
        # timestamp advances on every successful write, including ones that send
        # no power at all, and a marker release is not a command transition.
        self._last_setpoint_write: datetime | None = None
        self._setpoint_delta_kw: float | None = None
        #: Control-grade coherence, carried across physical ticks.
        self._coherence: ControlCoherence | None = None
        #: The forward authorisation cap, replaced on every affirmation.
        self._forward: ForwardAuthorisation | None = None
        #: Narrow emergency-stop attempts made against the current dispatch.
        self._emergency_attempts = 0
        #: The bounded ring of recent physical decisions.
        self._physical_decisions: deque[dict[str, Any]] = deque(
            maxlen=MAX_PHYSICAL_DECISIONS_REPORTED
        )
        #: Why the most recent physical tick did nothing, when it did nothing.
        self._last_tick_reason: str | None = None
        self._pending_stage_one: tuple[Any, ...] = ()
        self._pending_stage_two: tuple[Any, ...] = ()
        self._pending_verify: str | None = None
        #: Whether the pending step list ends in an activation, so a *confirmed*
        #: physical start can be distinguished from a computed one. Activity says
        #: "started" only when a write carrying this actually succeeded.
        self._pending_activates: bool = False
        #: Set for exactly one refresh after an activation write lands.
        self._activation_confirmed: bool = False
        #: The dead-man deadline observed when the run was last re-armed, and the
        #: run it belonged to. A sustain must move the first of these forward; if it
        #: does not, the timer is not being refreshed and the run is stopped.
        self._sustained_deadline: datetime | None = None
        self._sustained_run_id: str | None = None
        #: What the pending write would do, held between building and sending.
        self._pending_deadline: datetime | None = None
        self._pending_run_id: str | None = None
        #: What of the Solcast boundary was found on the last refresh, and what it
        #: said. Held for diagnostics so a user can see which sites exist, which
        #: are selected and which selected one has gone missing -- without a
        #: per-site entity, which would multiply entities for information that is
        #: read once and then acted on by nobody.
        self.pv_capability: SolcastCapability = SolcastCapability()
        #: Whether the one-time site-membership write has already been scheduled
        #: this session. Without it a refresh that overlaps the pending write
        #: would schedule a second one.
        self._pv_selection_write_scheduled = False
        #: When the last refresh ran. Everything in the published data is a
        #: snapshot from this instant, and saying so is what stops a reader
        #: comparing it against a live figure and concluding the two contradict.
        self.last_refresh_at: datetime | None = None
        self.pv_facts: SolcastFacts | None = None
        #: The most recent forecast per target day. Rebuilt every refresh.
        self.pv_forecasts: dict[date, PvForecast] = {}

        #: The price layer. Published for diagnostics and evidence, and read by
        #: nothing in the decision layer -- which is what makes "prices change no
        #: decision" a structural property rather than a promise.
        self.price_forecasts: dict[date, PriceForecast] = {}
        self.price_capability = FrankCapability()
        self.price_options = FrankOptions(readable=False)

    # -- lifecycle -------------------------------------------------------

    async def async_prepare(self) -> None:
        """Load persisted history before entities are added.

        Both documents are read here so the first refresh already has the
        forecast evidence in hand: without it, that refresh would look like a
        fresh installation and re-issue snapshots that are already on disk.
        """
        await self.store.async_load(str(dt_util.get_default_time_zone()))
        await self.history.async_load()
        # Revisions survive a restart; progress deliberately does not. A revision
        # is a statement about what Stage A published and must be continuous,
        # whereas progress must be re-measured from evidence rather than trusted
        # from a snapshot taken before the lights went out.
        self._execution_revisions = dict(self.store.execution_revisions)

    @callback
    def async_start(self) -> None:
        """Register listeners and timers.

        Every subscription is handed to ``entry.async_on_unload`` so a reload
        tears them down exactly once and never leaves a duplicate behind.
        """
        tz = dt_util.get_default_time_zone()
        self._tz_key = str(tz)
        self._accumulator = QuarterAccumulator(tz)
        self._ev_accumulator = (
            QuarterAccumulator(tz, sanitizer=sanitize_ev_w)
            if self.config.ev_power_entity
            else None
        )
        self._pv_accumulator = (
            QuarterAccumulator(tz, sanitizer=sanitize_pv_w)
            if self.config.has_pv and self.config.pv_power_entity
            else None
        )
        # Both sides carry the load sanitiser: after the sign is resolved each is
        # a plain non-negative power with the same plausibility band, and a
        # separate one would be a second opinion about the same question.
        self._grid_import_accumulator = (
            QuarterAccumulator(tz, sanitizer=sanitize_load_w)
            if self.config.grid_power_entity
            else None
        )
        self._grid_export_accumulator = (
            QuarterAccumulator(tz, sanitizer=sanitize_load_w)
            if self.config.grid_power_entity
            else None
        )
        # Charging only, and the load sanitiser for the same reason the grid pair
        # uses it: once the configured sign is resolved this is a plain
        # non-negative power with the same plausibility band.
        self._battery_charge_accumulator = (
            QuarterAccumulator(tz, sanitizer=sanitize_load_w)
            if self.config.battery_power_entity
            else None
        )

        # Home Assistant has not necessarily finished starting when this entry is
        # set up, and the first refresh happens during that setup. Anything read
        # then is provisional: the AlphaESS Modbus sensors may not have published
        # a value yet, and an integration this one consumes may still be loading
        # its own config entry. Because refreshes are driven by the quarter-hour
        # tick rather than an interval, a provisional reading would otherwise
        # stand for up to fifteen minutes -- which is exactly the beta.9 defect,
        # where a Solcast entry still setting up left the PV layer unusable and a
        # battery state of charge that had not yet published left the plan
        # reporting a missing input, both of them beside live values in
        # diagnostics that plainly contradicted them.
        #
        # ``async_at_started`` fires immediately when Home Assistant is already
        # running, so a reload behaves the same way as a cold boot.
        self.entry.async_on_unload(
            async_at_started(self.hass, self._handle_hass_started)
        )

        watched = [
            entity_id
            for entity_id in (
                self.config.house_load_entity,
                self.config.ev_power_entity,
                # Watched so generation is integrated at the rate the sensor
                # actually publishes. The 60-second safety sample alone would
                # left-hold each reading for a whole minute, which on a partly
                # cloudy day is where the energy is.
                self.config.pv_power_entity if self.config.has_pv else None,
            )
            if entity_id
        ]
        if watched:
            self.entry.async_on_unload(
                async_track_state_change_event(
                    self.hass, watched, self._handle_source_change
                )
            )

        self.entry.async_on_unload(
            async_track_time_interval(
                self.hass,
                self._handle_safety_sample,
                timedelta(seconds=SAFETY_SAMPLE_SECONDS),
            )
        )
        self.entry.async_on_unload(
            async_track_time_change(
                self.hass,
                self._handle_quarter_boundary,
                minute=list(range(0, 60, QUARTER_MINUTES)),
                second=_QUARTER_TRIGGER_SECOND,
            )
        )
        self.entry.async_on_unload(
            self.hass.bus.async_listen(
                EVENT_CORE_CONFIG_UPDATE, self._handle_core_config_update
            )
        )

        # Seed the accumulator so integration starts from the current reading
        # rather than from the first future state change.
        self._sample(dt_util.now())

    @callback
    def _handle_core_config_update(self, event: Event) -> None:
        """Reload the entry when Home Assistant's timezone changes.

        Both accumulators capture the zone once, at :meth:`async_start`, and
        label every finalised bucket with the civil date and slot it implies.
        The storage layer meanwhile stamps each day with the zone it was written
        in. Home Assistant does not reload config entries when its timezone
        changes, so without this the two halves of the write path ran on
        different calendars until the next restart -- long enough to file whole
        afternoons of energy into morning intervals of a day that still looked
        complete.

        Rebuilding is the honest response: a reload discards the in-flight
        quarter, which cannot reach the coverage threshold anyway, and starts
        both accumulators in the zone the user has just chosen.
        """
        if "time_zone" not in event.data:
            return
        current = str(dt_util.get_default_time_zone())
        if current == self._tz_key:
            return
        _LOGGER.info(
            "Home Assistant's timezone changed from %s to %s; reloading Alpha "
            "EMS Manager so measurement restarts on the new calendar. Days "
            "already learned keep the zone they were recorded in",
            self._tz_key,
            current,
        )
        self.hass.config_entries.async_schedule_reload(self.entry.entry_id)

    async def async_shutdown_store(self) -> None:
        """Flush pending learning and forecast data to disk.

        The forecast flush is guarded on its own so a failure there cannot stop
        the learning history -- the irreplaceable half -- from being written.
        """
        await self.store.async_save_now()
        try:
            await self.history.async_save_now()
        except Exception:
            _LOGGER.exception(
                "Forecast history could not be flushed to disk. Learning "
                "history was written normally and is unaffected"
            )

    # -- source reading --------------------------------------------------

    def _read_power(self, entity_id: str | None) -> float | None:
        """Return a source power in watts, or ``None`` when unusable."""
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        return normalize_power_w(
            state.state, state.attributes.get("unit_of_measurement")
        )

    def read_flows(self) -> PowerFlows:
        """Return the current snapshot in the canonical internal convention."""
        battery_charge, battery_discharge = split_battery_power(
            self._read_power(self.config.battery_power_entity),
            self.config.battery_power_sign,
        )
        grid_import, grid_export = split_grid_power(
            self._read_power(self.config.grid_power_entity),
            self.config.grid_power_sign,
        )
        # With no PV array the generation term is a known zero, not missing
        # data, so the balance check stays usable for PV-less installations.
        pv = self._read_power(self.config.pv_power_entity)
        if not self.config.has_pv:
            pv = 0.0

        # ``PowerFlows`` documents every field as ``None`` or ``>= 0``, and the
        # balance allowance multiplies these by positive fractions. The battery
        # and grid splitters guarantee it for their four outputs, but house load
        # and PV arrive straight from the state machine. An inverted PV sensor, or
        # a template that dips below zero, therefore produced a *negative*
        # allowance -- and a warning reading "residual 0 W against an allowance of
        # -120 W" for a snapshot whose identity closed exactly. A negative
        # generation or consumption figure is not a small-signal artefact to be
        # clamped away; it means the entity cannot be interpreted, so it is
        # treated as missing and the balance check simply returns no verdict.
        # Sanitised with the same rule the learning path uses, so the two agree
        # on what a usable house-load reading is. They did not: a reading above
        # MAX_PLAUSIBLE_LOAD_W was rejected for learning but accepted here,
        # where it inflates ``ac_power`` and therefore the allowance -- making
        # the balance check most permissive exactly when the house-load entity
        # is most obviously wrong.
        house = sanitize_load_w(self._read_power(self.config.house_load_entity))
        # The plausibility ceiling PV never had. Deliberately the *strict* rule
        # rather than the accumulation sanitizer: this path's freshness exemption
        # applies to a PV reading of exactly zero, so clamping a small negative up
        # to zero here would hand that exemption to a reading nobody published as
        # zero. See ``interpretable_pv_w``.
        pv = interpretable_pv_w(pv)

        return PowerFlows(
            house_load_w=house,
            pv_w=pv,
            battery_charge_w=battery_charge,
            battery_discharge_w=battery_discharge,
            grid_import_w=grid_import,
            grid_export_w=grid_export,
        )

    def read_daily_house_load_kwh(self) -> float | None:
        """Return the optional cumulative daily validation reading."""
        entity_id = self.config.daily_house_load_entity
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        return normalize_energy_kwh(
            state.state, state.attributes.get("unit_of_measurement")
        )

    # -- measurement -----------------------------------------------------

    @callback
    def _handle_source_change(self, event: Event) -> None:
        """Integrate on every published change of the house-load entity."""
        self._sample(dt_util.now())

    @callback
    def _handle_hass_started(self, _hass: HomeAssistant) -> None:
        """Replace the setup-time snapshot once everything else is up.

        A refresh rather than a partial re-read, because every layer took its
        provisional values from the same instant: the sources, the consumed
        integrations and the derived plan. Re-running one of them would leave the
        others stale and the payload internally inconsistent, which is what made
        the beta.9 symptom so confusing to read.

        ``async_refresh`` rather than ``async_request_refresh``, deliberately.
        The requesting form is debounced, so a startup refresh and a user action
        arriving within the cooldown collapse into one -- and the survivor can be
        the one taken *before* the user acted, leaving their change unreflected
        until the next quarter-hour tick. This fires once at startup and is not
        something to rate-limit against.
        """
        self.hass.async_create_task(self.async_refresh())

    @callback
    def _handle_safety_sample(self, now: datetime) -> None:
        """Advance integration even while the source is quiet, then correct.

        **Two jobs on one cadence, and no new timer.** The sampling half is
        unchanged. The second half is the beta.25 physical controller: it follows
        the *frozen* Stage-A grid target for the current quarter using live
        measurements, which is what a fifteen-minute setpoint could not do.

        Scheduled rather than awaited, because this callback is synchronous and a
        write is not.
        """
        moment = dt_util.as_local(now)
        self._sample(moment)
        self._sample_balance()
        self.hass.async_create_task(self._async_physical_tick(moment))

    async def _async_physical_tick(self, now: datetime) -> None:
        """Correct the commanded power against live measurements. Never plans.

        **The narrowest write path in the integration.** It reads the frozen grid
        target for the quarter it is in and moves the setpoint toward it. It does
        not re-run prices, admit a run, re-rank a window, alter an economic target
        or re-arm the dead-man -- the last of those matters most, because a re-arm
        on a power cadence would extend a run the economics never extended.

        Every refusal is recorded rather than silent: a controller that did
        nothing and said nothing is indistinguishable from one that is not running.
        """
        if self.control_mode != CONTROL_MODE_ACTIVE:
            self._note_tick(now, TICK_SKIPPED_NOT_LIVE)
            return
        if not self.config.control_execution_enabled:
            self._note_tick(now, TICK_SKIPPED_NOT_LIVE)
            return
        # **Non-blocking, and a skip rather than a queue.** A correction computed
        # while an arm was in progress describes a world that no longer exists by
        # the time the lock frees, and the next tick is only sixty seconds away.
        if self._execution_lock.locked():
            self._note_tick(now, TICK_SKIPPED_LOCK_HELD)
            return
        try:
            async with self._execution_lock:
                await self._async_correct_setpoint(now)
        except Exception:
            # **A fault costs the correction, never the coordinator.** This runs
            # as a detached task, so an escaping exception would surface only as
            # an unretrieved-task error while the sampling half carried on and
            # nobody knew the controller had stopped. The lock is released by the
            # context manager either way, which is the property that matters most.
            _LOGGER.exception(
                "The physical controller failed this tick; the setpoint is "
                "unchanged and the next tick will retry"
            )
            self._note_tick(now, TICK_ERROR)

    async def _async_correct_setpoint(self, now: datetime) -> None:
        """Run one correction, with the execution lock already held.

        **Progress is measured before authority is questioned.** What the plant did
        in the last sixty seconds is a physical fact and stays true whether or not
        this tick is entitled to write; accruing it only on the paths that write
        would lose energy from the quarter's totals on exactly the ticks a reader
        most wants explained.
        """
        # **Captured before anything advances, and beta.43 is what it is for.**
        #
        # The two statements below rebase the accumulators onto whatever row now
        # covers this instant: ``_refresh_executing_quarter`` moves the slot to the
        # successor, and ``_accrue_quarter_progress`` then sees a changed row key and
        # calls ``_reset_quarter_progress`` -- zeroing the totals of the row that just
        # ended, *before* ``_async_end_row`` below is ever reached. Its own
        # ``_capture_quarter_progress`` was written to protect those totals across
        # the physical stop, which happens two statements later than the loss.
        #
        # So a row with a successor recorded ``0.0`` and a row that ended with
        # nothing after it recorded the truth, because that path returns early from
        # ``_accrue_quarter_progress`` and never resets. On 2026-09-05 the
        # 20:15-20:30 export row filed ``realized_grid_kwh: 0.0`` and a 100 %
        # shortfall while the physical ring, twenty-three seconds before that row
        # ended, held ``grid_realized_kwh: 0.494``. All nine completed rows of that
        # capture split exactly along "has a successor".
        #
        # Additive on purpose: nothing is reordered, so the derivation still happens
        # before the measurement and no physical action moves.
        self._lifecycle_cadence = CADENCE_PHYSICAL_TICK
        pending = self._capture_quarter_progress()
        pending_clamps = set(self._quarter_clamps)
        # **Derive before measuring.** The row covering this instant decides which
        # accumulators the sample belongs to, so it must be resolved first -- and the
        # derivation is also what notices a row ending, because a derived quarter
        # cannot be found expired: it stops being returned.
        ended = self._refresh_executing_quarter(now)
        self._accrue_quarter_progress(now)
        quarter = self._quarter
        run = self._carried
        snapshot = read_snapshot(self.hass)
        self._record_dispatch_start_sample(snapshot, now, cadence=CADENCE_PHYSICAL_TICK)
        self._observe_arm(snapshot, now)

        if ended is not None:
            # The row that was being executed has finished. Close it whatever
            # follows: the shortfall belongs to the row it happened in.
            await self._async_end_row(
                ended,
                now,
                snapshot,
                stop=self._quarter is None,
                measured=pending,
                clamps=pending_clamps,
            )
            if self._quarter is None:
                self._note_tick(now, TICK_STOPPED_QUARTER_EXPIRED, wrote=True)
                return

        # **Three reasons where beta.26 published one.** ``no_owned_run`` covered
        # "nothing is admitted", "nothing is armed" and "we cannot prove this is
        # ours" -- three different situations needing three different responses,
        # reported as one string that told a reader none of them apart.
        #
        # **Either carrier is authority enough.** A quarter is the beta.27 envelope
        # and is preferred, but a publication written before beta.27 carries no
        # ``quarter_schedule`` and admits no quarter -- and refusing to correct a
        # run for that reason would take the hardware-proven beta.26 charge path
        # away. So the tick degrades to the run rather than to nothing.
        # **Activity and ownership are asked first. beta.38, and the order is the
        # fix.**
        #
        # Through beta.37 "nothing to execute" returned before either question, so
        # the one state that must never be left alone -- *we own a dispatch that is
        # physically moving the battery and nothing authorises it* -- was published
        # as ``no_admitted_quarter`` and left running. The economic cadence would
        # eventually reset it fifteen minutes later, and failing that the vendor
        # dead-man twenty minutes after the arm. Neither is cleanup; both are a
        # timeout being used as one.
        if not snapshot.dispatch_active:
            self._note_tick(now, TICK_SKIPPED_DISPATCH_INACTIVE)
            return
        owned_now = self._ownership_now(snapshot, now) == OWNERSHIP_OWNED
        # **The cadence that can see a run happen. beta.39.**
        #
        # Placed here, immediately after activity and ownership are read, and
        # deliberately *ahead* of the guards below rather than behind them.
        # ``snapshot.dispatch_active`` has already been proven true above, so
        # ``ownership_of`` cannot answer ``none`` and this projection can never
        # produce ``idle`` or ``admitted`` -- it can only report what is running
        # and under whose authority.
        #
        # Behind the guards it would have been silent on exactly the cases a
        # reader most needs: a marker deleted mid-run leaves the tick returning
        # early, and the lifecycle would have gone on reading ``executing`` from
        # what it believed a minute earlier until the next quarter refresh came
        # round. Reporting a hazard is not the same as acting on one, and the
        # actions below are unchanged.
        #
        # Projected through the same function the report uses, so there is one
        # criterion and not two. ``stop_reason`` is ``None`` because every stop
        # this tick can decide is taken below and notes its own transition, and
        # ``arming`` is ``False`` because a tick never arms -- it corrects a
        # dispatch that is already running.
        self._note_lifecycle(
            self._lifecycle_state_from(
                ownership_state=self._ownership_now(snapshot, now),
                stop_reason=None,
                resetting=False,
                releasing=False,
                arming=False,
                now=now,
            ),
            now,
        )
        if quarter is None and run is None:
            if owned_now:
                # **The orphan, stopped on the cadence that found it.** Routed
                # through the one teardown helper, at abort scope, because an owned
                # dispatch with no authority is something that happened *to* the
                # run rather than an ending it earned -- and the admission that
                # produced it must not re-arm. ``_async_stop_dispatch`` verifies the
                # stop before it tears anything down, so an unverified attempt keeps
                # every piece of evidence and the next tick tries again.
                # **``quarter_progress_unknown``, and it is the exact words.** We
                # own a dispatch and have no quarter to measure it against, which
                # is what that reason names -- and it is what the *refresh* files
                # for the same situation after a restart, so the two cadences
                # cannot disagree about a terminal. An abort reason, so it is
                # never suppressed by the opened-row authority.
                await self._async_stop_dispatch(
                    now,
                    snapshot,
                    EXECUTION_STOP_QUARTER_PROGRESS_UNKNOWN,
                    scope=STOP_SCOPE_ABORT,
                )
                self._note_tick(now, TICK_STOPPED_ORPHAN_DISPATCH, wrote=True)
                return
            self._note_tick(now, TICK_SKIPPED_NO_QUARTER)
            return
        if not owned_now:
            self._note_tick(now, TICK_SKIPPED_OWNERSHIP)
            return

        # **The open quarter's own bounds are the authority, not the run's.** This
        # is the R1 fix at the tick: a run that ended at the boundary leaves
        # ``self._carried`` empty while the quarter it opened is still executing,
        # and asking the run whether the tick may proceed is what made a whole
        # quarter unexecutable.
        if quarter is not None:
            # **No expiry test here any more.** A derived row cannot be found
            # expired: the derivation above stops returning it, and that is where
            # the end is noticed. A test here would be unreachable code pretending
            # to be a safety check.
            if quarter.objective_kwh() < self._min_armable_kwh(quarter):
                # Stage A marks such a row non-executable and never publishes it as
                # armable; this is Stage B's own backstop, because arming it would
                # overshoot the objective by construction.
                #
                # **The same rule as Stage A's, asked through one helper. beta.43.**
                # Two copies of a floor is one too many, and the copy that drifted
                # would be this one -- it already had to be kept in step with the
                # resolution constant by hand.
                self._note_tick(now, TICK_SKIPPED_SUB_RESOLUTION)
                return
        elif run.stale_at(now) or not run.actionable_at(now):
            # The beta.26 window test, reached only on the run-only fallback.
            self._note_tick(now, TICK_SKIPPED_STALE_TARGET)
            return

        coherence = self._update_coherence(now)
        if not coherence.usable:
            # **Hold the last applied setpoint, and calculate nothing.** No
            # fallback production, house or grid figure is invented: an unusable
            # reading produces a hold, never a guess. The economic target is
            # untouched.
            self._note_tick(now, TICK_SKIPPED_INCOHERENT)
            if coherence.expired:
                await self._async_end_quarter(
                    now,
                    snapshot,
                    QUARTER_END_SAFETY,
                    SHORTFALL_SENSOR_INCOHERENCE,
                    stop_reason=EXECUTION_STOP_COHERENCE_LOST,
                )
            return

        # **Target reached stops commanding now, whatever the lease says.** The
        # 20/25-minute duration is a dead-man, not an entitlement: a dispatch left
        # armed because a countdown has not expired is how a target is exceeded.
        #
        # **What it does *not* do, since beta.36, is end the campaign.** A row
        # meeting its own objective is a success, and routing that success through
        # the teardown helper is what destroyed a five-and-a-half-hour charge on
        # 2026-08-30. If a further executable row of this campaign follows, the row
        # rests: zero is commanded, ownership and the frozen schedule are kept, and
        # the next boundary picks up the next row. Only genuine campaign finality
        # ends anything.
        progress = self._quarter_progress(now)
        if progress is not None and self._quarter_target_reached(progress):
            if self._quarter_target_reached_at is None:
                self._quarter_target_reached_at = now
                self._note_quarter_clamp(SHORTFALL_TARGET_REACHED)
            # **The third outcome, and beta.40 exists because of it.**
            #
            # A satisfied row is exactly where free production is guaranteed to
            # leak: both outcomes below stop the battery, and beta.36 measured
            # what Mode 2 at 0 kW actually does -- it is a *total* hold that
            # suppresses charging as well as discharging, so 1.3 kW of
            # production went to the meter while the pack had room. That is the
            # right command for a row with nothing left to do and an
            # indefensible one for a row still authorised to store free energy.
            #
            # So a satisfied objective with envelope left and surplus measured
            # falls through to the ordinary setpoint path, where the absorption
            # branch commands the surplus. Nothing else changes: the latch stays
            # set, so the moment the surplus goes the next tick takes the
            # beta.39 path and the row is satisfied and held exactly as before.
            # No new lifecycle state, and no recovery path to invent.
            if self._absorption_live(progress):
                self._note_quarter_clamp(SHORTFALL_ABSORBING_FREE_PV)
            else:
                await self._async_finish_satisfied_row(now, snapshot)
                return

        decision = self._dispatch_setpoint(now)
        if decision is None:
            self._note_tick(now, TICK_SKIPPED_STALE_TARGET)
            return
        # **A rate the actuator cannot express is a rest, not a fault. beta.36.**
        #
        # On 2026-08-31 production covered the row and the grid ceiling was nearly
        # spent, so the authorised rate collapsed. ``quarter_intent_for`` returned
        # ``None``, ``evaluate`` reported unsafe with ``nothing_to_command``, and an
        # unsafe verdict on an owned live dispatch was promoted to ``safety`` -- an
        # unsuppressable total abort that blacklisted the campaign for the session.
        # Nothing was wrong with the plant. It recovers on its own the moment
        # production drops or the budget frees up, inside this same row.
        if (
            self._quarter is not None
            and abs(decision.applied_kw) < CONTROL_MIN_POWER_KW
            and self._quarter_target_reached_at is None
        ):
            await self._async_hold_at_zero(
                now, snapshot, HOLD_REASON_RATE_BELOW_RESOLUTION
            )
            return
        # **The absorbing row's own floor, and it is the safety net beta.40
        # needs rather than a second policy.** The clause above is guarded on
        # the latch, so once a row is satisfied it can no longer route a
        # sub-resolution figure to a rest -- and a satisfied row that fell
        # through to absorb can still arrive here at nearly zero, because a
        # physical clamp it could not see took the command after
        # ``_absorption_live`` said yes: headroom collapsing, the inverter
        # limit, or the surplus going between the two reads.
        #
        # Whatever the cause, there is nothing left to absorb, so the row is
        # finished on exactly the beta.39 path. Writing the trickle instead
        # would command a figure the device cannot express.
        if (
            self._quarter is not None
            and abs(decision.applied_kw) < CONTROL_MIN_POWER_KW
            and self._quarter_target_reached_at is not None
        ):
            await self._async_finish_satisfied_row(now, snapshot)
            return
        self._record_physical_decision(now, decision, coherence)
        if not decision.update_needed:
            self._note_tick(now, decision.update_reason)
            return
        await self._async_send_locked(
            plan_dispatch_power(decision.applied_kw),
            now=now,
            verify=EXECUTION_VERIFY_DISPATCH_SETPOINT,
        )
        self._applied_setpoint_kw = decision.applied_kw
        self._note_tick(now, TICK_APPLIED, wrote=True)

    async def _async_finish_satisfied_row(self, now: datetime, snapshot: Any) -> None:
        """Hold or end a row whose own objective is met. **Unchanged beta.39.**

        Extracted so beta.40's third outcome is a branch *around* this rather
        than an edit *inside* it: the satisfied-row behaviour a reader has been
        able to rely on since beta.36 is this function, byte for byte, and the
        only new thing is that it is now reached one condition later.
        """
        scope = self._completion_scope(now)
        if scope is None:
            await self._async_hold_at_zero(now, snapshot, HOLD_REASON_QUARTER_SATISFIED)
            return
        await self._async_end_quarter(
            now,
            snapshot,
            QUARTER_END_TARGET_REACHED,
            SHORTFALL_TARGET_REACHED,
            # **Name what is ending, not what scope it has.** With no campaign
            # open -- an ordinary single-run charge -- "campaign objective
            # reached" would be a claim about a thing that does not exist, and
            # the surfaces would render a campaign success for a run.
            stop_reason=self._campaign_stop_reason(
                EXECUTION_STOP_QUARTER_TARGET_REACHED
            ),
            scope=scope,
        )
        self._note_tick(now, TICK_STOPPED_TARGET_REACHED, wrote=True)

    async def _async_end_row(
        self,
        row: CarriedQuarter,
        now: datetime,
        snapshot: Any,
        *,
        stop: bool,
        measured: tuple[Any, ...] | None = None,
        clamps: set[str] | None = None,
    ) -> None:
        """Close a finished row, stopping the dispatch only when nothing follows.

        **Two different situations, and conflating them is how a boundary loses a
        quarter.** A row that ends inside a multi-quarter plan hands over to the next
        row: the dispatch keeps running, the claim stays, and only the measurements
        reset. A row that ends with nothing after it is the end of the plan, and the
        dispatch must stop -- immediately, whatever the dead-man lease still says.

        The shortfall is recorded against the row it happened in either way, and is
        **never** carried forward.
        """
        # **Put the caller's snapshot back first. beta.43.**
        #
        # ``measured`` is what the accumulators held before the slot advanced, and
        # on the tick path they have since been rebased onto the successor row --
        # see ``_async_correct_setpoint``. Restoring here rather than reading the
        # live fields is what makes every figure below describe ``row``: the
        # totals, ``_quarter_target_reached_at``, and the clamps that bound it.
        # The restore lasts exactly as long as the bookkeeping does; the
        # ``_reset_quarter_progress`` at the end of this method rebases onto the
        # successor again, as it always did.
        if measured is not None:
            self._restore_quarter_progress(measured)
            self._quarter_clamps = set(() if clamps is None else clamps)
        measured = self._capture_quarter_progress()
        reached = self._quarter_target_reached_at is not None
        # A row that met its objective did not fall short of it.
        self._note_quarter_clamp(
            SHORTFALL_TARGET_REACHED if reached else SHORTFALL_QUARTER_EXPIRED
        )
        clamps = set(self._quarter_clamps)
        # **Accrued before the stop, and beta.36's reasoning applied to its
        # sibling. beta.43.**
        #
        # ``_async_end_quarter`` has done this since beta.36 and says why: the
        # accrual is in-memory arithmetic over energy the plant has already moved,
        # it has no reason to be sequenced after a physical write, and sequencing it
        # there let ``_close_campaign`` set ``_campaign_id = None`` in between -- so
        # the guard in ``_accrue_campaign_progress`` early-returned and the row's
        # energy reached the history and no campaign figure. That fix was applied to
        # one of the two row-ending paths. This is the other one.
        #
        # Exactly once is a property of ``_campaign_accrued_row``, not of ordering,
        # so the ``accrue=False`` below is intent made visible rather than the
        # mechanism: a double call is already harmless.
        self._accrue_campaign_progress(row, self._row_objective_kwh(row))
        if stop:
            # **Which scope, and beta.36 is the first release that asks. **
            #
            # ``stop`` means "no row covers *this instant*", which is not the same
            # as "the campaign is over" -- a ``serve_load`` gap in the middle of a
            # plan satisfies the first and not the second, and beta.35 lost every
            # row after such a gap because it tore the schedule down. Only genuine
            # finality clears the plan.
            scope = self._completion_scope(now, ending=row)
            await self._async_stop_dispatch(
                now,
                snapshot,
                (
                    self._campaign_stop_reason(EXECUTION_STOP_QUARTER_EXPIRED)
                    if scope == STOP_SCOPE_CAMPAIGN
                    else EXECUTION_STOP_QUARTER_EXPIRED
                ),
                scope=scope or STOP_SCOPE_ROW,
            )
        self._restore_quarter_progress(measured)
        self._quarter_clamps = clamps
        self._record_completed_quarter(row, QUARTER_END_EXPIRED, accrue=False)
        self._reset_quarter_progress(self._quarter)

    @callback
    def _capture_quarter_progress(self) -> tuple[Any, ...]:
        """Return the measured totals, so a stop cannot erase them before use."""
        return (
            self._quarter_battery_kwh,
            self._quarter_grid_import_kwh,
            self._quarter_grid_export_kwh,
            self._quarter_peak_kw,
            self._quarter_power_sum,
            self._quarter_power_samples,
            self._quarter_pv_helped,
            self._quarter_target_reached_at,
        )

    @callback
    def _restore_quarter_progress(self, measured: tuple[Any, ...]) -> None:
        """Put the captured totals back, for exactly as long as recording takes."""
        (
            self._quarter_battery_kwh,
            self._quarter_grid_import_kwh,
            self._quarter_grid_export_kwh,
            self._quarter_peak_kw,
            self._quarter_power_sum,
            self._quarter_power_samples,
            self._quarter_pv_helped,
            self._quarter_target_reached_at,
        ) = measured

    async def _async_end_quarter(
        self,
        now: datetime,
        snapshot: Any,
        completion: str,
        shortfall: str,
        *,
        stop_reason: str | None = None,
        scope: str = STOP_SCOPE_ABORT,
    ) -> None:
        """End the open quarter: stop the dispatch, then record what happened.

        **The physical stop comes first and the bookkeeping second.** If the record
        were written first and the stop then failed, the history would claim a
        finished quarter while the inverter was still moving energy.

        **No deficit is carried.** The shortfall is recorded against this quarter
        and Stage A decides the next one independently -- carrying a deficit forward
        would let Stage B accumulate an entitlement no economic layer authorised.
        """
        if stop_reason is None:
            stop_reason = (
                EXECUTION_STOP_QUARTER_EXPIRED
                if completion == QUARTER_END_EXPIRED
                else EXECUTION_STOP_QUARTER_TARGET_REACHED
            )
        # Held across the stop, which clears the slot: the physical write must come
        # first, and the record must still know what it is recording afterwards.
        # **Held across the stop, which resets the accumulators.** The physical write
        # must come first -- a record written before a stop that then failed would
        # claim a finished quarter while the inverter was still moving energy -- so
        # everything the record needs is captured before the stop and restored after
        # it, for exactly as long as it takes to write the row.
        finished = self._quarter
        measured = (
            self._quarter_battery_kwh,
            self._quarter_grid_import_kwh,
            self._quarter_grid_export_kwh,
            self._quarter_peak_kw,
            self._quarter_power_sum,
            self._quarter_power_samples,
            self._quarter_pv_helped,
            self._quarter_target_reached_at,
            set(self._quarter_clamps),
        )
        self._note_quarter_clamp(shortfall)
        clamps = set(self._quarter_clamps)
        # **Accrued before the stop, and only here. beta.36.**
        #
        # beta.35's stop-before-record rule is correct and is kept: a row record
        # written before a stop that then failed would claim a finished quarter while
        # the inverter was still moving energy. But ``_record_completed_quarter`` did
        # *two* things with different failure semantics, and coupling them is what
        # lost a quarter. The accrual is in-memory arithmetic over energy the plant
        # has already moved -- it has no reason to be sequenced after a physical
        # write, and sequencing it there let ``_close_campaign`` set
        # ``_campaign_id = None`` in between. The 2026-08-30 terminal reported
        # 0.27 kWh where its own three rows had realised 0.548.
        self._accrue_campaign_progress(finished, self._row_objective_kwh(finished))
        await self._async_stop_dispatch(now, snapshot, stop_reason, scope=scope)
        (
            self._quarter_battery_kwh,
            self._quarter_grid_import_kwh,
            self._quarter_grid_export_kwh,
            self._quarter_peak_kw,
            self._quarter_power_sum,
            self._quarter_power_samples,
            self._quarter_pv_helped,
            self._quarter_target_reached_at,
            _clamps,
        ) = measured
        self._quarter_clamps = clamps
        self._record_completed_quarter(finished, completion, accrue=False)
        # **The row ends; the plan does not.** A finished row inside a multi-quarter
        # plan must leave the rest of the schedule admitted, or the next boundary
        # would have nothing to derive from -- which is the defect this release
        # removes. The plan is dropped only by ``carry_plan``, when it is spent.
        self._refresh_executing_quarter(now)
        self._reset_quarter_progress(self._quarter)

    @callback
    def _controller_block(self, setpoint: Any, now: datetime) -> dict[str, Any]:
        """Return the physical controller's own diagnostics for this refresh.

        Everything a reader needs to reconstruct one decision without rerunning
        it, and the intermediate figures are kept apart on purpose: ``calculated``
        before the clamps and ``applied`` after them is what makes a clamp
        visible at all.
        """
        live = self._live_kw()
        coherence = self._coherence or COHERENCE_UNKNOWN
        payload: dict[str, Any] = {
            "controller_refresh_at": now.isoformat(),
            "house_load_kw": None if live is None else round(live[0], 3),
            "pv_kw": None if live is None else round(live[1], 3),
            "actual_grid_kw": None if live is None else round(live[2], 3),
            "dispatch_power_deadband_kw": DISPATCH_POWER_DEADBAND_KW,
            # **Two cadences, two records, and they are never merged.** The tick
            # reason describes the sixty-second correction; ``refresh_decision``
            # describes the quarter refresh whose figures surround it here. beta.26
            # published one mutable string beside the other cadence's figures, so a
            # stale refusal read as an explanation of a fresh write.
            "last_tick": (
                None if self._tick_outcome is None else self._tick_outcome.as_dict()
            ),
            "refresh_decision": (
                None
                if self._refresh_outcome is None
                else self._refresh_outcome.as_dict()
            ),
            # Kept for one release so an existing dashboard does not break. The
            # typed records above are what a reader should use.
            "last_tick_reason": self._last_tick_reason,
            "applied_setpoint_kw": self._applied_setpoint_kw,
            "lock_held": self._execution_lock.locked(),
            "cadence_rule": (
                "the economic target is refreshed every quarter; the physical "
                "setpoint is corrected every sixty seconds against it. one "
                "power write per tick at most, and the duration is re-armed "
                "only on the economic cadence"
            ),
        }
        if setpoint is None:
            payload["desired_grid_kw"] = None
            payload["dispatch_limited_by"] = DISPATCH_LIMIT_NONE
            payload["update_needed"] = False
        else:
            payload.update(setpoint.as_dict())
        payload.update(coherence.as_dict())
        payload.update(self._authorisation_block())
        return payload

    @callback
    def _authorisation_block(self) -> dict[str, Any]:
        """Return the two-cap authorisation state, from :mod:`.execution`."""
        forward = self._forward
        block: dict[str, Any] = {
            "forward_authorised_kwh": None,
            "forward_from": None,
            "delivered_since_forward_kwh": None,
            "binding_cap": CAP_NONE,
            "authorisation_rule": (
                "two caps on separate domains, never one comparison across "
                "origins: the frozen remainder runs from the admitted window "
                "start, the forward allowance from the latest publication's own "
                "boundary. it may only reduce an admitted run, never grow one"
            ),
        }
        if forward is not None:
            block.update(forward.as_dict())
        decision = self._stage_b_decision
        demand = None if decision is None else decision.demand
        if demand is not None and demand.grid_cap_kwh is not None:
            remaining = max(0.0, demand.grid_cap_kwh - demand.grid_charged_kwh)
            revised, cap = remaining_authorised_kwh(
                now=dt_util.now(),
                frozen_remaining_kwh=remaining,
                forward=forward,
            )
            block["binding_cap"] = cap
            block["authorisation_reduced_by_replan"] = round(
                max(0.0, remaining - revised), 3
            )
        return block

    @callback
    def _frozen_remaining_kwh(self, now: datetime) -> float | None:
        """Return the grid energy this run may still buy, or ``None`` if uncapped.

        The same two-cap composition the authorisation block publishes -- the
        frozen remainder from the admitted window start, reduced by the forward
        allowance from the latest publication's own boundary -- read here so a
        quarter can snapshot it **at admission**.

        Snapshotted, because once a quarter is open the run-level caps must not
        reach backwards into it. That is consistent with the forward cap rather than
        an exception to it: ``remaining_authorised_kwh`` returns the frozen cap
        whenever ``now < forward.forward_from``, and ``forward_from`` is by
        construction the *next* boundary -- so the forward cap has always been a
        "next quarter onward" instrument.
        """
        decision = self._stage_b_decision
        demand = None if decision is None else decision.demand
        if demand is None or demand.grid_cap_kwh is None:
            return None
        remaining = max(0.0, demand.grid_cap_kwh - demand.grid_charged_kwh)
        revised, _cap = remaining_authorised_kwh(
            now=now, frozen_remaining_kwh=remaining, forward=self._forward
        )
        return revised

    @callback
    def _affirming_target(self, carried: Any) -> Any:
        """Return the freshest published target for the carried run, or ``None``.

        Matched on **intent and overlap**, exactly as ``affirms`` does, so the cap
        is set from the publication that affirmed the run rather than from whatever
        happens to be first in the list. Purely temporal: no price is read here,
        because Stage B never chooses a window.
        """
        if carried is None:
            return None
        for raw in self.execution_targets or ():
            target = parse_target(raw)
            if target is None:
                continue
            if affirms(carried, target):
                return target
        return None

    @callback
    def _quarter_block(self, now: datetime) -> dict[str, Any]:
        """Return the open quarter's state and the finished ones behind it.

        Published every tick, because the question a reader has during a quarter is
        "is it on course?", and that cannot be answered from a target and a setpoint
        alone -- it needs elapsed time, delivered energy and the remainder together.
        """
        quarter = self._quarter
        progress = self._quarter_progress(now)
        export = quarter is not None and quarter.intent == EXECUTION_INTENT_NET_EXPORT
        block: dict[str, Any] = {
            "quarter_start": (
                None if quarter is None else quarter.quarter_start.isoformat()
            ),
            "quarter_end": None if quarter is None else quarter.quarter_end.isoformat(),
            "intent": None if quarter is None else quarter.intent,
            "quarter_seconds_elapsed": (
                None
                if quarter is None
                else round((now - quarter.quarter_start).total_seconds(), 1)
            ),
            "quarter_seconds_remaining": (
                None if progress is None else round(progress.seconds_remaining, 1)
            ),
            "battery_target_this_quarter_kwh": (
                None if quarter is None else round(quarter.battery_allowance_kwh(), 3)
            ),
            "battery_realized_this_quarter_kwh": round(self._quarter_battery_kwh, 3),
            "battery_remaining_this_quarter_kwh": (
                None if progress is None else round(progress.battery_remaining_kwh, 3)
            ),
            # **The split, beta.40.** The realised figure above is every kWh
            # the pack took; these two say how much of it the row promised and
            # how much was free production stored under its envelope. They sum
            # to it exactly, which is the invariant a reader can check.
            "battery_objective_realized_this_quarter_kwh": round(
                self._quarter_objective_kwh, 3
            ),
            "battery_absorbed_extra_this_quarter_kwh": round(
                self._quarter_absorbed_kwh, 3
            ),
            "retention_authorised_this_quarter": (
                None if quarter is None else quarter.absorption_authorised()
            ),
            "absorption_gate": self._absorption_gate_now(quarter),
            "retention_until_dc_kwh": (
                None if quarter is None else quarter.retention_until_dc_kwh
            ),
            "retainable_now_kwh": (
                None
                if progress is None or progress.retention_remaining_kwh is None
                else round(progress.retention_remaining_kwh, 3)
            ),
            "absorption_rule": (
                "objective and absorbed sum to realised. the objective is "
                "what the row promised and is the only figure a campaign is "
                "judged on; absorbed is free production kept under Stage A's "
                "retention verdict, which is real energy and is not progress "
                "against a promise the row never made. the verdict is economic "
                "only -- eta_rt * marginal_value > export_price -- and the "
                "controller bounds it by the measured surplus, so it can never "
                "cause grid import"
            ),
            "grid_target_this_quarter_kwh": (
                None
                if quarter is None
                else round(
                    quarter.grid_export_target_kwh
                    if export
                    else quarter.grid_authorised_kwh,
                    3,
                )
            ),
            "grid_realized_this_quarter_kwh": round(
                self._quarter_grid_export_kwh
                if export
                else self._quarter_grid_import_kwh,
                3,
            ),
            "grid_remaining_this_quarter_kwh": (
                None if progress is None else round(progress.grid_remaining_kwh, 3)
            ),
            "required_average_battery_kw": (
                None if progress is None else round(progress.battery_rate_kw, 3)
            ),
            "required_grid_correction_kw": (
                None if progress is None else round(progress.grid_rate_kw, 3)
            ),
            "binding_clamps": sorted(self._quarter_clamps),
            "target_reached_at": (
                None
                if self._quarter_target_reached_at is None
                else self._quarter_target_reached_at.isoformat()
            ),
            "completed_quarters": list(self._completed_quarters),
            "objective_rule": (
                "the objective is the battery figure for a grid charge and the "
                "actual meter export for a net export; the other figure is a "
                "ceiling. a ceiling is never a completion test, so unspent grid "
                "authorisation on a charge is production having paid for it, not "
                "a deficit"
            ),
            "carry_over_rule": (
                "a shortfall is recorded against the quarter it happened in and "
                "never carried forward. stage a decides each quarter independently"
            ),
        }
        return block

    @callback
    def _export_quarter_open(self, now: datetime) -> bool:
        """Return whether an admitted ``net_export`` quarter is open right now."""
        quarter = self._quarter
        return (
            quarter is not None
            and quarter.intent == EXECUTION_INTENT_NET_EXPORT
            and quarter.open_at(now)
        )

    @callback
    def _export_verdict(self, intent: Any, context: Any, now: datetime) -> Any:
        """Return the dedicated Live export authorisation for this refresh.

        **Built from the ``ControlContext`` that already exists at the call site**,
        not from a second reading of the same entities. The capability, the readings
        and the conflicting-feature flags are assembled once per refresh precisely so
        the gate cannot see a different world halfway through evaluating itself, and
        a parallel assembly here would be a second world to keep in step.

        What this adds beyond the context is the part the context has no field for:
        the admitted quarter, its two remaining authorisations, and the reserve
        headroom the export must stay above.
        """
        quarter = self._quarter
        progress = None if quarter is None else self._quarter_progress(now)
        plan = self.battery_plan
        headroom_kwh = None
        max_discharge_kw = None
        min_soc = None
        if plan is not None and plan.state is not None:
            max_discharge_kw = plan.state.limits.max_discharge_kw
            min_soc = plan.reserve.configured_min_soc_percent
            headroom_kwh = max(
                0.0,
                plan.state.energy_kwh - plan.state.limits.energy_for_soc(min_soc),
            )
        return authorize_export(
            ExportRequest(
                intent=None if quarter is None else quarter.intent,
                quarter_admitted=quarter is not None,
                quarter_open=quarter is not None and quarter.open_at(now),
                dispatch_active=bool(context.dispatch_active),
                owned=bool(context.dispatch_owned),
                # The causal record must name the run the quarter was admitted
                # under. ``_execution_identity`` resolves that quarter-first, which
                # is what lets a quarter stay provable while the run slot is empty.
                causation_proven=self._execution_identity() is not None,
                foreign_dispatch=bool(
                    context.dispatch_active and not context.dispatch_owned
                ),
                # **Absence of a coherence verdict is not evidence of
                # incoherence.** The control-grade coherence state is produced by
                # the sixty-second tick and set to ``None`` by every stop -- and the
                # tick cannot run before a START, because it requires an active
                # dispatch. So requiring the verdict to *exist* made an export
                # unstartable on the first refresh after any stop, including the
                # previous quarter's own expiry: refused ``sensor_incoherence``,
                # with the next opportunity a full quarter away.
                #
                # When no verdict exists yet, the honest question is the one
                # ``control_coherence`` seeds itself from -- ``sources_available``,
                # which is exactly ``_live_kw() is not None``. A verdict that does
                # exist and says unusable still refuses, unchanged.
                #
                # Deliberately **not** fixed by advancing the coherence state on the
                # refresh cadence as well: its grace is counted in ticks
                # (``bad_ticks >= CONTROL_COHERENCE_GRACE_TICKS``), so a second
                # cadence feeding it would shorten a documented 180-second safety
                # bound. The machine is untouched here.
                coherent=(
                    self._coherence.usable
                    if self._coherence is not None
                    else self._live_kw() is not None
                ),
                conflicting_feature=bool(
                    context.excess_export_active or context.peak_shaving_active
                ),
                missing_entities=tuple(context.missing_entities)
                + tuple(context.unavailable_entities),
                failsafe_available=bool(context.failsafe_available),
                soc_percent=context.soc_percent,
                configured_min_soc_percent=min_soc,
                reserve_headroom_kwh=headroom_kwh,
                battery_remaining_kwh=(
                    0.0 if progress is None else progress.battery_remaining_kwh
                ),
                grid_export_remaining_kwh=(
                    0.0 if progress is None else progress.grid_remaining_kwh
                ),
                requested_kw=(
                    0.0 if intent is None else max(0.0, float(intent.average_power_kw))
                ),
                inverter_max_discharge_kw=max_discharge_kw,
                # No configured site export limit exists in this integration, so
                # this bound is genuinely unconstrained rather than defaulted.
                site_export_limit_kw=None,
                tick_cap_kw=(
                    None
                    if progress is None
                    else tick_energy_cap_kw(progress.battery_remaining_kwh)
                ),
            )
        )

    @callback
    def _record_dispatch_start_sample(
        self, snapshot: Any, now: datetime, *, cadence: str
    ) -> None:
        """Record one read-only sample of the vendor dispatch-start register.

        **P0: a measurement, not a decision.** The ownership model this release ships
        does not read this register at all -- see
        :data:`OWNERSHIP_PROVENANCE_PARAMETERS`. It is sampled because its meaning
        has never been established on the hardware, and because every ownership
        model up to beta.29 rested on an *assumption* about it that the test double
        then asserted rather than tested:

            # tests/test_beta24_live_charge.py
            # "The same reconstruction the ownership layer performs, from the same
            #  instant: seconds since the refresh day's midnight."

        A double defined as the inverse of the function under test cannot fail, which
        is why roughly two hundred ownership assertions passed while Live execution
        was impossible on the real inverter.

        **Nothing decides on this.** The samples are appended to a bounded ring and
        read by diagnostics alone. No service is called, no entity is written, and no
        new cadence is introduced: the snapshot is the one the caller already read.

        Candidate interpretations are published **side by side and unlabelled as to
        which is correct**. Deciding here would repeat the original mistake.
        """
        state = self.hass.states.get(SENSOR_DISPATCH_START)
        raw = None if snapshot is None else snapshot.dispatch_start
        record = (
            self.store.execution_record
            if isinstance(self.store.execution_record, dict)
            else {}
        )
        written = instant_of(record.get("written_at"))
        previous = (
            self._dispatch_start_samples[-1] if self._dispatch_start_samples else None
        )
        active = bool(snapshot is not None and snapshot.dispatch_active)
        duration = None if snapshot is None else snapshot.dispatch_duration_minutes
        # One read, so the commanded figures and the measured ones in the same sample
        # describe the same instant.
        flows = self.read_flows()

        # --- phase, from observable transitions only ---------------------------
        was_active = bool(previous.get("dispatch_active")) if previous else False
        previous_duration = (
            previous.get("dispatch_duration_minutes") if previous else None
        )
        if active and not was_active:
            phase = PROBE_PHASE_AFTER_START
        elif not active and was_active:
            phase = PROBE_PHASE_AFTER_STOP
        elif active and previous is not None and previous_duration != duration:
            phase = PROBE_PHASE_AFTER_REARM
        elif active:
            phase = PROBE_PHASE_STEADY
        elif record:
            phase = PROBE_PHASE_IDLE
        else:
            phase = PROBE_PHASE_BEFORE_START

        # --- candidate interpretations, none preferred -------------------------
        candidates: dict[str, str | None] = {}
        numeric = raw if isinstance(raw, (int, float)) and raw == raw else None
        if numeric is not None and numeric > 0:
            local_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            utc_now = dt_util.as_utc(now)
            utc_midnight = utc_now.replace(hour=0, minute=0, second=0, microsecond=0)
            candidates["local_midnight_seconds"] = (
                local_midnight + timedelta(seconds=numeric)
            ).isoformat()
            candidates["utc_midnight_seconds"] = (
                utc_midnight + timedelta(seconds=numeric)
            ).isoformat()
            if 1e9 <= numeric <= 4e9:
                candidates["unix_epoch_seconds"] = datetime.fromtimestamp(
                    numeric, tz=UTC
                ).isoformat()
            if 1e12 <= numeric <= 4e12:
                candidates["unix_epoch_millis"] = datetime.fromtimestamp(
                    numeric / 1000.0, tz=UTC
                ).isoformat()
            if numeric <= 86400:
                candidates["elapsed_seconds_before_now"] = (
                    now - timedelta(seconds=numeric)
                ).isoformat()
                candidates["countdown_seconds_after_now"] = (
                    now + timedelta(seconds=numeric)
                ).isoformat()

        deltas = {
            name: (
                None
                if written is None or value is None
                else round((instant_of(value) - written).total_seconds(), 1)
            )
            for name, value in candidates.items()
        }

        previous_raw = previous.get("raw_numeric") if previous else None
        self._dispatch_start_samples.append(
            {
                "at_local": now.isoformat(),
                "at_utc": dt_util.as_utc(now).isoformat(),
                "cadence": cadence,
                "phase": phase,
                # The string exactly as the state machine holds it: ``parse_numeric``
                # discards it, and a timestamp device class or a unit suffix would
                # answer the semantics question outright.
                "raw_state": None if state is None else state.state,
                "raw_numeric": numeric,
                "raw_device_class": (
                    None if state is None else state.attributes.get("device_class")
                ),
                "raw_unit": (
                    None
                    if state is None
                    else state.attributes.get("unit_of_measurement")
                ),
                "raw_state_class": (
                    None if state is None else state.attributes.get("state_class")
                ),
                "raw_last_changed": (
                    None if state is None else state.last_changed.isoformat()
                ),
                "raw_last_updated": (
                    None if state is None else state.last_updated.isoformat()
                ),
                # What the production reconstruction makes of it. Reported so the
                # measurement can be compared against the belief it replaces.
                "reconstructed_local_midnight": (
                    None
                    if (reconstructed := _dispatch_start_instant(snapshot, now)) is None
                    else reconstructed.isoformat()
                ),
                "dispatch_active": active,
                "dispatch_selected_mode": (
                    None if snapshot is None else snapshot.dispatch_selected_mode
                ),
                "register_mode": None if snapshot is None else snapshot.dispatch_mode,
                "helper_setpoint_kw": (
                    None if snapshot is None else snapshot.dispatch_setpoint_kw
                ),
                "register_power_w": (
                    None if snapshot is None else snapshot.dispatch_power_w
                ),
                "dispatch_cutoff_percent": (
                    None if snapshot is None else snapshot.dispatch_cutoff_percent
                ),
                "dispatch_duration_minutes": duration,
                "dispatch_timer_active": (
                    None if snapshot is None else snapshot.dispatch_timer_active
                ),
                "dispatch_timer_finishes_at": (
                    None
                    if snapshot is None or snapshot.dispatch_timer_finishes_at is None
                    else snapshot.dispatch_timer_finishes_at.isoformat()
                ),
                "owner_marker": None if snapshot is None else snapshot.owner_marker,
                "claim_written_at": record.get("written_at"),
                "claim_dispatch_start": record.get("dispatch_start"),
                "claim_id": record.get("claim_id"),
                "ownership_state": self._ownership_now(snapshot, now),
                "seconds_since_claim_written": (
                    None
                    if written is None
                    else round((now - written).total_seconds(), 1)
                ),
                "raw_changed_since_previous": (
                    None if previous is None else previous_raw != numeric
                ),
                "raw_delta_since_previous": (
                    None
                    if previous is None or previous_raw is None or numeric is None
                    else round(numeric - previous_raw, 3)
                ),
                "raw_vs_register_time_s": (
                    None
                    if numeric is None
                    or snapshot is None
                    or snapshot.dispatch_time_s is None
                    else round(numeric - snapshot.dispatch_time_s, 3)
                ),
                "raw_vs_duration_seconds": (
                    None
                    if numeric is None or duration is None
                    else round(numeric - duration * 60.0, 3)
                ),
                # **What the plant did, beside what it was told. beta.36.**
                #
                # This ring recorded only *commanded* values -- the helper setpoint
                # and the raw register -- and never the pack, so no capture in the
                # repository can say what AlphaESS mode 2 at **0 kW** actually does
                # to the battery. That is the one physical unknown the 0 kW hold
                # rests on: ``plan_dispatch_cleanup``'s own docstring says a dispatch
                # left armed at zero still holds a duration, a cutoff and a timer,
                # but not whether the pack holds still or falls back to
                # self-consumption. Interval aggregates are far too coarse to isolate
                # a sixty-second window.
                #
                # Read-only, like everything else here. The first supervised hold now
                # answers the question from the payload instead of from a stopwatch.
                "battery_charge_w": flows.battery_charge_w,
                "battery_discharge_w": flows.battery_discharge_w,
                "pv_w": flows.pv_w,
                "house_load_w": flows.house_load_w,
                "grid_import_w": flows.grid_import_w,
                "grid_export_w": flows.grid_export_w,
                "hold_reason": self._hold_reason,
                "candidates": candidates,
                "deltas_to_claim_written_s": deltas,
                "rule": (
                    "read-only. no decision path reads this ring. candidate "
                    "interpretations are published side by side and none is "
                    "selected: the correct one is whichever delta to "
                    "claim_written_at stays small across a whole run. "
                    "raw_delta_since_previous alone separates a fixed start instant "
                    "from elapsed seconds from a countdown, and a jump at "
                    "after_rearm would show the re-arm re-anchoring it"
                ),
            }
        )
        # **The same sample, kept where nothing idle can displace it.** An idle
        # sample carries no information about the register's meaning -- it reads
        # zero -- so letting one evict a sample taken during a run is how beta.30
        # lost the only capture it had.
        if active:
            self._dispatch_start_active.append(self._dispatch_start_samples[-1])

    @callback
    def _refresh_executing_quarter(self, now: datetime) -> CarriedQuarter | None:
        """Derive the executing quarter from the frozen plan. **The boundary fix.**

        Called at the top of every tick and every refresh. Until beta.30 the
        executing quarter was *carried* in a slot and admitted one refresh ahead --
        two rules that cannot both hold with one slot, because while a quarter is
        open the slot is occupied and nothing can be admitted for the boundary after
        it. On the real installation the executing quarter jumped from ``22:15Z`` to
        ``22:45Z`` and the quarter between was never executed.

        Deriving it removes the slot, and with it the race: the row covering this
        instant either exists in the frozen schedule or does not.

        Returns the row that **just ended**, when this call is the one that noticed,
        so the caller can close it. ``None`` when nothing ended.
        """
        plan = self._plan
        previous = self._quarter
        # **A torn-down schedule may not execute again.** The row lookup is pure and
        # knows nothing about lifecycle, which is right -- so the refusal lives here,
        # at the one place a row becomes a quarter. Without it the zombie of
        # 2026-08-29 is one derivation away at every boundary.
        #
        # **beta.36 asks about the admission, not the campaign.** Asking about the
        # campaign destroyed this plan on every refresh for five and a half hours on
        # 2026-08-30, and again on 2026-08-31: the identity is a digest of the
        # campaign's end, so a legitimately re-admitted continuation of the same
        # campaign carried the same latched id. The attempt that was aborted is the
        # thing that may not execute again.
        if plan is not None and self._admission_abandoned(plan):
            plan = None
            self._plan = None
            self._admission_refusal = ADMISSION_REFUSED_ABANDONED
        self._quarter = None if plan is None else plan.executing_quarter(now)
        if previous is None:
            return None
        if (
            self._quarter is not None
            and self._quarter.quarter_start == previous.quarter_start
        ):
            return None
        # **The row that was current no longer is, so it has ended.** With a derived
        # quarter, expiry is not observable as ``now >= quarter_end`` any more -- the
        # row simply stops being returned. Handing the finished row back is what lets
        # the caller close it: record the shortfall, and stop the dispatch if nothing
        # follows it. Without this a multi-quarter plan would roll silently and a
        # single-row plan would leave a dispatch running with nothing aiming it.
        return previous

    @callback
    def _quarter_progress_key(self) -> tuple[str | None, datetime | None]:
        """Return the identity this quarter's measurements belong to.

        ``(claim_id, quarter_start)``. Both halves are needed: a new row under the
        same claim is new progress, and a new claim for the same row -- a stop and a
        restart inside one quarter -- is new progress too.
        """
        record = self.store.execution_record
        claim = None
        if isinstance(record, dict):
            raw = record.get("claim_id")
            claim = raw if isinstance(raw, str) and raw else None
        return (
            claim,
            None if self._quarter is None else self._quarter.quarter_start,
        )

    @callback
    def _note_lifecycle(self, state: str, now: datetime) -> None:
        """Record a lifecycle transition. One field, one question.

        **The vocabulary is checked, since beta.39.** The argument was a bare
        string with nothing to compare it against, so a typo at a call site would
        have published a state no reader can interpret -- and the field's whole
        purpose is that a reader never has to guess. A wrong word fails loudly
        here rather than quietly in a payload.

        **And every transition is appended to a bounded trail.** The lifecycle
        advances on three cadences -- the quarter refresh, the write boundary and
        the sixty-second physical tick -- and only the first of them publishes a
        control report. So on 2026-09-02 a Sell that ran for fifteen minutes at
        7.4 kW published ``admitted -> starting -> stopping``: the state field
        showed the latest answer at each publication and ``executing`` fell
        between two of them. A sequence answers the question a single field
        cannot, and it costs one bounded list.
        """
        if state not in LIFECYCLE_STATES:  # pragma: no cover - programming error
            raise ValueError(f"unknown lifecycle state: {state}")
        if state == self._lifecycle:
            return
        self._lifecycle_previous = self._lifecycle
        self._lifecycle = state
        self._lifecycle_at = now
        # **``at`` is the cadence's own instant, and the trail is append-ordered.
        # Those are two different orderings, and beta.43 stops them being read as
        # one.** A quarter refresh threads a single ``now`` captured at its first
        # line through a body that takes 31-35 s on the reference hardware, so it
        # stamps transitions with an instant that is by then half a minute old --
        # while the sixty-second tick, running freely inside that window, stamps
        # fresh ones. On 2026-09-05 the trail holds ``stopped`` and
        # ``cleanup_complete`` at 17:30:20 followed by ``foreign`` at 17:30:05.
        #
        # Neither number is wrong and neither is changed here: ``now`` stays the
        # refresh's one consistent instant, which is deliberate. What is added is a
        # monotonic ``seq``, so a reader can reconstruct the true order without
        # inventing a clock, and the ``cadence`` that stamped it, so the staleness
        # is attributable rather than mysterious.
        self._lifecycle_seq += 1
        self._lifecycle_trail.append(
            {
                "state": state,
                "at": now.isoformat(),
                "seq": self._lifecycle_seq,
                "cadence": self._lifecycle_cadence,
            }
        )

    @callback
    def _note_campaign_started(self, now: datetime) -> None:
        """Freeze what this campaign promised, at the instant execution began.

        **Idempotent, and called from the transition rather than from the report.
        beta.38.**

        Through beta.37 this lived inside ``_note_campaign_progress``, gated on
        ``self.activation_confirmed`` -- a flag set in ``_async_dispatch``, which runs
        *after* the control report is built. So on the refresh that actually armed the
        hardware the campaign had not started yet, and the payload said so: the
        2026-09-01 capture shows ``open_campaign.started: false`` and
        ``frozen_target_kwh: null`` beside a quarter that was executing. A reader
        checking "is this campaign under way?" got the wrong answer for exactly the
        refresh where it began.

        It is now called from the send site the moment an activation write lands, so
        the freeze and the physical start are one transition. The report-side call
        remains for every path that reaches a started campaign without passing
        through that write, and the ``is None`` guard is what makes two callers safe:
        **the freeze happens once and the frozen target never moves afterwards**,
        which is what makes a verdict against it meaningful.
        """
        if self._campaign_id is None or self._campaign_started_at is not None:
            return
        # **Frozen here and nowhere else.** Success is judged against what was
        # promised when execution began: a campaign that promised 2.65 kWh and
        # delivered 1.80 because Stage A changed its mind is Partial, not a
        # retroactively successful 1.80 / 1.80.
        self._campaign_started_at = now
        # The live figure where the campaign is still published, and the last
        # one read while it was, where it is not. Never ``None`` because a
        # target existed: that is the whole of the beta.34 correction.
        # **``is not None``, not truthiness. beta.35, and it is a latch
        # rather than a fix.**
        #
        # ``_campaign_objective_kwh`` goes to some trouble to distinguish "this
        # campaign sells nothing" (``0.0``) from "nobody published it"
        # (``None``), and ``live or ...`` conflates the two. Today it happens
        # to be harmless: ``_note_campaign_progress`` has already assigned
        # ``opening = live`` for every non-``None`` reading, so both spellings
        # return the same number in every reachable state -- the null target on
        # 2026-08-29 came from ``_campaign_objective_kwh`` returning ``None``,
        # not from this line.
        #
        # It is written the careful way anyway, because the property is one
        # move away from mattering: separate the two blocks, or capture the
        # opening figure anywhere else, and the ``or`` begins discarding
        # legitimate zeros with nothing to notice it.
        live = self._campaign_objective_kwh(self._campaign_id)
        self._campaign_frozen_target_kwh = (
            live if live is not None else self._campaign_opening_target_kwh
        )
        # The public transition rides the frozen one, so the two can never
        # disagree about when a campaign began. beta.42.
        self._lifecycle_started(now)

    @callback
    def _lifecycle_state_from(
        self,
        *,
        ownership_state: str,
        stop_reason: str | None,
        resetting: bool,
        releasing: bool,
        arming: bool,
        now: datetime,
    ) -> str:
        """Return where the lifecycle is, projected from facts already decided.

        **beta.38, and it exists because the field was a lie.** ``_note_lifecycle``
        had no callers anywhere in the package, so ``execution.lifecycle.state`` read
        ``idle`` for the life of the process and the other eleven states were
        unreachable. On 2026-09-01 a reader looking at a 10 kW export saw
        ``lifecycle.state: "idle"`` -- and "is the lifecycle terminal while hardware
        moves?" is precisely the question this release has to answer, from a field
        that could not answer anything.

        **A projection, not a second state machine.** Every input is a boolean the
        write boundary has already settled this refresh; nothing new is derived and
        no branch here can change a command. Order is hazard-first, exactly as the
        write boundary orders its own branches, so the published state names the
        thing that actually decided the refresh.

        **A hold reports ``executing``, deliberately.** It keeps ownership, so it
        reaches the one criterion by the same door a moving run does. beta.36
        settled that "a hold is a state of a run that is still going" -- it keeps
        ownership, the claim, the frozen schedule and the campaign, and it keeps
        re-arming the dead-man.
        The vocabulary has no ``holding`` member and inventing one would split a
        single question across two fields, which is the defect this field exists to
        prevent; ``hold_reason`` is published in the same block and is the
        discriminator.
        """
        if ownership_state == OWNERSHIP_DEGRADED:
            return LIFECYCLE_DEGRADED
        if ownership_state == OWNERSHIP_RELEASING:
            # beta.43. Projected, never decided on -- like every other state here.
            return LIFECYCLE_RELEASING
        if ownership_state == OWNERSHIP_FOREIGN:
            return LIFECYCLE_FOREIGN
        if ownership_state == OWNERSHIP_UNPROVEN:
            return LIFECYCLE_UNPROVEN
        if stop_reason == EXECUTION_STOP_TIMER_NOT_REFRESHED:
            return LIFECYCLE_DEADMAN_EXPIRED
        # **Stopping covers the whole of an unfinished stop.** A reset or a marker
        # release is in flight; a stop reason with no writes planned is a stop this
        # refresh could not complete. Either way the answer is not ``idle``, which is
        # the distinction the no-zombie invariant turns on.
        if resetting or releasing:
            return LIFECYCLE_STOPPING
        if stop_reason is not None and ownership_state == OWNERSHIP_OWNED:
            return LIFECYCLE_STOPPING
        # **Confirmed execution outranks a start in progress. beta.39, and the
        # order is the whole of the fix.**
        #
        # ``arming`` used to be tested first, and it is the weaker fact: it says a
        # write carrying an activation is *planned this refresh*, which is a
        # statement about our own intention. ``OWNERSHIP_OWNED`` is a statement
        # about the plant -- the vendor register reports ``dispatch_active``, our
        # persisted claim matches this run, and the owner marker is on -- and
        # ``ownership_of`` returns ``NONE`` the instant activity is false, so it
        # cannot be reached before execution really began.
        #
        # With ``arming`` first, a refresh that re-armed a run already confirmed
        # running published ``starting``; and because the projection runs once, in
        # a pure report built one frame *before* the write, a run whose only arm
        # was its own first refresh could never be seen owned by it at all.
        # ``executing`` needed a second quarter refresh inside the same run, which
        # a single-row campaign does not have. That is exactly what 2026-09-02
        # produced.
        #
        # Nothing here manufactures the state from the command just sent: this
        # branch fires only on evidence that the dispatch is already active and
        # provably ours. An arm with no such evidence yet stays ``starting``,
        # truthfully, until a later observation proves otherwise.
        #
        # **And it is the *only* route to ``executing``. beta.39.** beta.38 had
        # three: ``holding``, ``sustaining``, or ownership. The first two are
        # subsumed by the third -- both are computed with ``owned`` conjoined, so
        # neither can be true while ownership is anything else -- so they added no
        # reachable state and cost the field its single criterion. Written as a
        # disjunction the projection would answer ``executing`` for
        # ``holding=True, ownership=none``: unreachable in production, and a
        # predicate that will answer a question wrongly when asked directly is a
        # predicate nobody can reason about. ``hold_reason`` remains the
        # discriminator between a run that is resting and one that is moving, in
        # the same block, which is where beta.36 put it.
        if ownership_state == OWNERSHIP_OWNED:
            return LIFECYCLE_EXECUTING
        if arming:
            return LIFECYCLE_STARTING
        # Admitted but not yet physical: a frozen schedule exists and either has not
        # opened or has opened without anything being armed under it yet.
        plan = self._plan
        if plan is not None and not self._admission_abandoned(plan):
            return LIFECYCLE_ADMITTED
        if self._carried is not None:
            return LIFECYCLE_ADMITTED
        return LIFECYCLE_IDLE

    @callback
    def _admission_block(self, now: datetime) -> dict[str, Any]:
        """Return why there is, or is not, an admitted plan and a carried run.

        **The field whose absence made 2026-08-30 five hours of guesswork.** For an
        incident whose whole shape is "no admitted plan for five and a half hours",
        ``admitted_plan: null`` carried no reason at all: ``carry_plan`` had eight
        refusal clauses and reported none of them, and the abandonment latch that
        actually nulled the plan was downstream of all eight. Reconstructing which
        one had fired took reading every clause against a capture taken hours later.

        The two layers are reported side by side because they can disagree, and their
        disagreement is a defect class of its own: through beta.35 the run layer
        minted a fresh run from a target naming a torn-down campaign while the plan
        layer destroyed that run's plan on the same refresh, once every fifteen
        minutes, for the rest of the session.
        """
        plan = self._plan
        return {
            "admitted": plan is not None,
            "refused": self._admission_refusal,
            "admission_key": None if plan is None else plan.admission_key,
            "campaign_instance_id": self._campaign_instance_id,
            "run_carried": self._carried is not None,
            "run_refused": self._carry_refusal,
            "abandoned_admissions": len(self._abandoned_admissions),
            "closed_instances": len(self._closed_instances),
            "final_campaigns": len(self._final_campaigns),
            "hold_reason": self._hold_reason,
            "authority_holds": self._plan_authority_holds(now),
            "rule": (
                "refused names which clause declined a publication, and it is never "
                "null while admitted is false: a plan admitted and then destroyed "
                "by the abandonment latch reports admission_abandoned, not silence. "
                "run_refused is the same question one layer down, and the two "
                "layers read the same latches so they cannot disagree about whether "
                "an attempt is dead"
            ),
        }

    @callback
    def _lifecycle_block(self) -> dict[str, Any]:
        """Return where the execution lifecycle is, when it got there, and how.

        **``transitions`` is the beta.39 addition and it is the load-bearing
        one.** ``state`` can only ever report the latest answer, and two of the
        three cadences that advance the lifecycle publish no report -- so a run
        that started and finished between two publications left no trace of having
        executed at all. The trail is bounded, append-only within a session, and
        nothing decides on it.
        """
        return {
            "state": self._lifecycle,
            "entered_at": (
                None if self._lifecycle_at is None else self._lifecycle_at.isoformat()
            ),
            "previous_state": self._lifecycle_previous,
            "transitions": list(self._lifecycle_trail),
            "rule": (
                "one field for one question. the state is projected from facts the "
                "write boundary has already settled, and executing means the "
                "vendor register reports an active dispatch that our own claim and "
                "marker prove is ours -- never merely that a command was sent. "
                "transitions carries every change this session, bounded, because "
                "the lifecycle also advances at the write boundary and on the "
                "sixty-second tick, neither of which publishes a report"
            ),
        }

    @callback
    def _open_campaign_block(self) -> dict[str, Any] | None:
        """Return the open campaign as the payload states it, or ``None``.

        **Extracted in beta.39 for one reason: it has to be renderable twice.**
        ``started`` and ``frozen_target_kwh`` are settled at the write boundary,
        which runs a frame *after* the report is built, so the 2026-09-01 capture
        published ``started: false`` and ``frozen_target_kwh: null`` beside a
        ``completed_campaign`` whose ``started_at`` was that very refresh. beta.38
        moved the freeze to the correct instant and the payload still lied, because
        the payload was already written. See ``_settle_execution_payload``.
        """
        if self._campaign_id is None:
            return None
        return {
            "campaign_id": self._campaign_id,
            # **The join keys, since beta.43.** Without the instance id an open
            # campaign could not be tied to the lifecycle events that describe it or
            # to the terminal that eventually closes it, and ``campaign_id`` alone
            # cannot: it is stable across attempts by design.
            "campaign_instance_id": self._campaign_instance_id,
            "campaign_end": (
                None
                if self._campaign_end_utc is None
                else self._campaign_end_utc.isoformat()
            ),
            "objective_boundary": self._campaign_boundary,
            "started": self._campaign_started_at is not None,
            "frozen_target_kwh": (
                None
                if self._campaign_frozen_target_kwh is None
                else round(self._campaign_frozen_target_kwh, 3)
            ),
            # **Committed plus the quarter in flight.** The committed sum
            # advances only on a completed quarter, so quoting it alone would
            # show a campaign frozen at its last boundary while energy was
            # visibly moving.
            "campaign_realized_kwh": round(self._campaign_realized_now(), 3),
            "campaign_committed_kwh": round(self._campaign_realized_kwh, 3),
            "quarters_admitted": self._campaign_quarters_admitted,
            "rule": (
                "the realised figure accumulates across segments and holds "
                "across serve_load gaps; it is reset only when the campaign "
                "closes. the target is frozen at the first confirmed "
                "activation and may never shrink"
            ),
        }

    @callback
    def _settle_execution_payload(self, report: dict[str, Any] | None) -> None:
        """Re-publish the two blocks the write boundary settles after the report.

        **beta.39, and it is a publish-ordering fix rather than a control change.**
        ``_build_control_report`` is pure and runs one call frame before
        ``_async_dispatch``; three facts are therefore decided *after* the payload
        describing them has been assembled:

        * whether the campaign started, and what target was frozen at that instant;
        * whether a stop and its cleanup landed;
        * whether fresh evidence now proves the dispatch is ours and active.

        The dict is still mutable here -- ``control_report`` is
        ``self.data["control"]``, and ``self.data`` is assigned from the mapping
        this refresh returns -- so the two blocks are simply re-rendered from state
        that has since advanced. **Nothing is recomputed and no command is
        reachable from here.**

        **beta.43 adds the third block, and it was the one that needed it most.**
        ``completed_campaign`` is a latch deliberately *not* consumed on read, so it
        can be hours old, and it was rendered once in the pure frame and never
        again. A terminal filed by the sixty-second tick while the solve was in
        flight therefore appeared only on the *following* report, beside two blocks
        that had since moved on; on 2026-09-05 it held a campaign from three hours
        earlier with nothing marking it historical. ``published_at`` and
        ``completed_campaign_is_current`` are published beside it so a reader can
        date the payload and the latch independently -- 31 to 35 seconds separate
        them on the reference hardware.

        **What it does not do is promote ``starting`` to ``executing``.** A write
        that landed is not evidence that a dispatch is running, and reading the
        vendor register microseconds after our own write reads an echo rather than
        a confirmation -- the live probe of 2026-09-02 dates the register going
        active at 20:45:49, 44.7 seconds after the claim was written. So an arming
        refresh publishes ``starting``, truthfully, and the first *observation*
        that satisfies ``ownership_of`` -- the physical tick, or a later refresh --
        is what records ``executing``. This helper only re-renders; it decides
        nothing and reads no register -- which is also why it takes no instant:
        both blocks it re-renders read the coordinator's own state, and inventing
        a second timestamp for a re-render would invite exactly the confusion the
        original ordering caused.
        """
        if report is None:
            return
        execution = report.get("execution")
        if not isinstance(execution, dict):  # pragma: no cover - defensive
            return
        execution["lifecycle"] = self._lifecycle_block()
        execution["open_campaign"] = self._open_campaign_block()
        # The third block, since beta.43. See the docstring.
        execution["completed_campaign"] = self._closed_campaign
        ended_at = (
            None
            if self._closed_campaign is None
            else instant_of(self._closed_campaign.get("ended_at"))
        )
        # True when the terminal beside it was filed during *this* refresh cycle,
        # which is the question a reader of a never-consumed latch actually has.
        execution["completed_campaign_is_current"] = bool(
            ended_at is not None
            and self.last_refresh_at is not None
            and ended_at >= self.last_refresh_at
        )
        # **What the payload's own instant is.** ``issued_at`` is captured at the
        # refresh's first line and the body takes 31-35 s on the reference hardware,
        # so a reader comparing ``issued_at`` against the wall clock was measuring
        # the solve without being told. Published rather than corrected: the single
        # threaded instant is deliberate.
        execution["published_at"] = dt_util.now().isoformat()

    @callback
    def _record_decision(
        self,
        outcome: Any,
        *,
        now: datetime,
        plan: Any,
        planning: Any,
    ) -> None:
        """Persist one compact, replayable record of this Stage-A decision.

        **Scalars and fingerprints, never the series.** The forecast and price
        evidence layers already retain their own inputs for a year, so duplicating
        them here would cost megabytes to store what is already on disk. What was
        genuinely unrecoverable is the pack energy at the instant of the decision
        and the settings it was made under -- and both are here.

        **beta.34 adds the export-permission head, because the rule above turned
        out to have a hole in it.** On 2026-08-29 the plan collapsed at 13:00 --
        five runs to none, cost_eur -0.546 to +0.544, 9.3 kWh of export gone -- and
        the record of that refresh could not say why. The evidence layers do hold
        the prices, but the *gate* is computed from them by a two-pass solve whose
        intermediate quantities existed only for the length of one refresh: the
        survival window, the protection price it implies, and which intervals were
        free to export under it. Two adjacent records were therefore
        indistinguishable across the largest single-refresh economic change the
        installation has produced.

        Bounded to the first ``_RECORD_HEAD_INTERVALS`` intervals and a digest.
        The head is where every gate decision that matters is made -- an export is
        vetoed at the interval it would happen in -- and the digest is what lets an
        offline replay prove it is holding the same prices the decision saw.

        Nothing reads this except diagnostics and the offline replay harness. It is
        written after the plan exists, so a record can never describe a decision
        that was not made.
        """
        state = None if plan is None else plan.state
        desired = None if outcome is None else outcome.desired
        relaxed = None if outcome is None else outcome.relaxed
        config = self.config
        uncertainty = None if outcome is None else outcome.uncertainty
        self.store.record_decision(
            {
                "decision_at": now.isoformat(),
                # **What this refresh cost, persisted. beta.42.**
                #
                # ``solve_ms`` existed only as an instantaneous reading in the live
                # diagnostics payload and was never stored, so "what is the typical
                # solve cost, and has it moved?" was structurally unanswerable from
                # inside the codebase -- which is why it had to be measured from
                # outside. Two floats on a two-day ring, and the ring already exists.
                #
                # There is no timing *guard* here and this does not become one. The
                # measured duty cycle is 2.85 s against a 900 s period, so a warning
                # threshold would be a number invented well before any evidence
                # justified it. What this buys is the evidence.
                "solve_ms": None if outcome is None else round(outcome.solve_ms, 1),
                "solves": None if outcome is None else outcome.solve_count,
                "start_energy_kwh": None if state is None else state.energy_kwh,
                "start_soc_percent": None if state is None else state.soc_percent,
                # The gates in force. Published *and* fingerprinted since beta.31:
                # until then the digest omitted both per-kWh terms, so two
                # installations differing by a whole margin looked identical and no
                # recorded decision could be reproduced.
                "minimum_trade_gain_eur": config.minimum_trade_gain_eur,
                "grid_charge_margin_eur_per_kwh": (
                    config.grid_charge_margin_eur_per_kwh
                ),
                "battery_throughput_cost_eur_per_kwh": (
                    config.battery_throughput_cost_eur_per_kwh
                ),
                "allow_grid_charging": config.allow_grid_charging,
                "allow_battery_export": config.allow_battery_export,
                "floor_energy_kwh": (
                    None if planning is None else self._floor_energy_for_record(plan)
                ),
                "actionable_intervals": (
                    None if planning is None else planning.actionable_intervals
                ),
                "reachability_now_dc_kwh": (
                    None
                    if planning is None or planning.reachability is None
                    else planning.reachability.required_now_dc_kwh
                ),
                "autonomy_now_dc_kwh": (
                    None
                    if plan is None or plan.reserve_projection is None
                    else plan.reserve_projection.required_now_dc_kwh
                ),
                "bridge_kwh_now": (None if outcome is None else outcome.bridge_kwh_now),
                "uncertainty_total_dc_kwh": (
                    None if uncertainty is None else uncertainty.total_dc_kwh
                ),
                "uncertainty_blind_dc_kwh": (
                    None if uncertainty is None else uncertainty.blind_dc_kwh
                ),
                "uncertainty_statistical_dc_kwh": (
                    None if uncertainty is None else uncertainty.statistical_dc_kwh
                ),
                "uncertainty_binding": (
                    None if uncertainty is None else uncertainty.binding
                ),
                "edge_value_eur_kwh": (
                    None if outcome is None else outcome.edge_value_eur_per_kwh
                ),
                "edge_energy_kwh": None if desired is None else desired.edge_energy_kwh,
                "edge_value_eur": None if desired is None else desired.edge_value_eur,
                "action": None if desired is None else desired.action,
                "cost_eur": None if desired is None else desired.cost_eur,
                "hold_cost_eur": None if desired is None else desired.hold_cost_eur,
                # **The counterfactual, and it is the number that matters.** The
                # same solver, the same horizon, the same prices, with the reserve
                # relaxed to the hard floor. Its difference from ``cost_eur`` is
                # what the reserve is costing -- and it was already being computed
                # every refresh before beta.31 published it.
                "relaxed_cost_eur": None if relaxed is None else relaxed.cost_eur,
                "grid_import_kwh": (
                    None if desired is None else desired.planned_grid_import_kwh
                ),
                "grid_export_kwh": (
                    None if desired is None else desired.planned_grid_export_kwh
                ),
                "battery_throughput_kwh": (
                    None if desired is None else desired.battery_throughput_kwh
                ),
                "violation_kwh": None if desired is None else desired.violation_kwh,
                "run_count": 0 if desired is None else len(desired.runs),
                # Fingerprints, so the series behind the decision can be found in
                # the evidence layers that already keep them for a year.
                "tomorrow_prices_available": self._tomorrow_prices_available(),
                "tomorrow_prices_available_at": self._tomorrow_prices_available_at,
                **self._price_evidence(outcome),
                **self._gate_evidence(outcome),
                # **What the optimiser believed it was worth, at the moment it
                # chose. beta.37.** The decision-time half of a comparison a later
                # release can complete against measured outcomes, and the half that
                # is irrecoverable afterwards: prices are revised, forecasts are
                # replaced, and the state of charge moves. Scalars and an existing
                # fingerprint only -- the series themselves are already retained for
                # a year by the evidence layers, and duplicating them here would
                # make this a database.
                **self._economic_value_evidence(outcome, plan, now),
            }
        )

    @callback
    def _economic_value_evidence(
        self, outcome: Any, plan: Any, now: datetime
    ) -> dict[str, Any]:
        """Return the Economic Value scalars for one persisted record.

        A flat projection of :meth:`_economic_value_for`, because a record is read by
        a replay harness rather than a dashboard and nested blocks would make every
        field a two-step lookup. Prefixed ``ev_`` so a reader can tell at a glance
        which figures arrived in beta.37 and which were already there -- and so a
        name can never collide with one of the twenty-odd keys already in the record.
        """
        summary = self._economic_value_for(outcome, plan, now)
        if not summary.get("available"):
            return {
                "ev_available": False,
                "ev_state_unavailable_reason": summary.get("unavailable_reason"),
            }
        stored = summary.get("stored_value") or {}
        energy = summary.get("energy") or {}
        return {
            "ev_available": True,
            "ev_state_unavailable_reason": None,
            "ev_selected_plan_cost_eur": (summary.get("plan") or {}).get(
                "selected_plan_cost_eur"
            ),
            "ev_counterfactual_cost_eur": (summary.get("plan") or {}).get(
                "counterfactual_cost_eur"
            ),
            "ev_decision_advantage_eur": summary.get("decision_advantage_eur"),
            "ev_advantage_cash_eur": summary.get("advantage_cash_eur"),
            "ev_stored_energy_marginal_value_eur_kwh": summary.get(
                "stored_energy_marginal_value_eur_kwh"
            ),
            "ev_marginal_value_unavailable_reason": summary.get(
                "marginal_value_unavailable_reason"
            ),
            "ev_terminal_edge_value_eur_kwh": summary.get(
                "terminal_edge_value_eur_kwh"
            ),
            "ev_stored_energy_dc_kwh": summary.get("stored_energy_kwh"),
            "ev_head_run_state": stored.get("head_run_state"),
            "ev_selected_action": summary.get("current_action"),
            "ev_reason_code": summary.get("reason_code"),
            "ev_expected_grid_import_kwh": energy.get("expected_grid_import_kwh"),
            "ev_expected_grid_export_kwh": energy.get("expected_grid_export_kwh"),
            "ev_expected_battery_throughput_kwh": energy.get(
                "expected_battery_throughput_kwh"
            ),
            "ev_today_interval_value_eur": summary.get("today_interval_value_eur"),
            "ev_tomorrow_interval_value_eur": summary.get(
                "tomorrow_interval_value_eur"
            ),
            "ev_tomorrow_prices_known": summary.get("tomorrow_prices_known"),
            "ev_actionable_intervals": summary.get("actionable_intervals"),
            "ev_comparator_model": summary.get("comparator_model"),
        }

    @callback
    def _terminal_value(
        self,
        *,
        demands: tuple[IntervalDemand, ...],
        prices: tuple[IntervalPrice, ...],
        horizon_intervals: int,
        limits: Any,
        edge_value_eur_per_kwh: float,
        edge_creditable_kwh: float,
        today_interval_count: int,
    ) -> Any:
        """Return what energy left at the end of the horizon is worth.

        **Every input already existed. Nothing new is forecast here.**

        Three quantities:

        * **how much** the household will take before the pack refills for free,
          and **at what price**. Both come from :func:`post_horizon_window`, which
          owns the bound and its provenance.

          This method used to compute them from ``demands[horizon_intervals:]``
          directly, on the stated assumption that the demand and production
          forecasts "already run a full day further" than the prices. They do
          not. The price series is built one entry per demand, so the moment
          tomorrow day-ahead publishes, the priced horizon and the forecast series
          end at the same instant, that slice is empty, and the demand-bounded
          segment of the terminal value collapses to zero width. On the reference
          installation that fired every afternoon from roughly 13:00 and pinned
          the worth of stored energy at ``eta_discharge * export_price``, which put
          the break-even import price below any quarter the market has offered.
        * **what the rest could fetch**, which is the export price at the horizon
          edge, or nothing at all where the user has not permitted export.
        """
        eta = getattr(limits, "discharge_efficiency", 1.0) or 1.0
        window = post_horizon_window(
            demands,
            prices,
            horizon_intervals=horizon_intervals,
            today_interval_count=today_interval_count,
        )
        export_edge = 0.0
        if self.config.allow_battery_export:
            for price in reversed(prices[:horizon_intervals]):
                if price.export_eur_kwh is not None:
                    export_edge = max(0.0, price.export_eur_kwh)
                    break
        return TerminalValue(
            demand_ac_kwh=window.demand_ac_kwh,
            displaced_price_eur_kwh=window.displaced_price_eur_kwh,
            export_price_eur_kwh=export_edge,
            discharge_efficiency=eta,
            edge_value_eur_per_kwh=edge_value_eur_per_kwh,
            edge_creditable_kwh=edge_creditable_kwh,
            window_basis=window.basis,
            window_intervals=window.intervals,
            window_stopped_by=window.stopped_by,
        )

    @callback
    def _position_value_eur(
        self, outcome: Any, energy_kwh: float | None
    ) -> float | None:
        """Return what holding ``energy_kwh`` is worth, on *this refresh's* curve.

        ``V(floor) - V(energy)`` from the head layer of the current solve.
        Planner-derived, an opportunity value from now, never a purchase cost.

        **Extracted in beta.38 so the two ends of the ledger share one curve.** The
        realised position identity is ``realised + closing - opening``, and that
        subtraction only means anything if both terms come from the same value
        function. Differencing two *different* curves would fold a revaluation --
        prices moved, the forecast moved, the horizon shortened -- into a figure
        labelled as what operating the battery achieved. Valuing both ends here, on
        the curve this refresh has already computed, makes the difference purely
        operational and costs no solve at all.

        ``None`` where the optimiser could not state it, never zero.
        """
        if not isinstance(outcome, EconomicOutcome) or not outcome.available:
            return None
        if energy_kwh is None:
            return None
        plan = outcome.desired
        bucket_kwh = outcome.bucket_kwh
        if not bucket_kwh:
            return None
        # Against the floor the plan is actually held to, which is the only
        # reference point that makes "above the floor" mean anything.
        #
        # **``bucket_at_or_below_kwh``, not ``int(e / bucket_kwh)``. beta.37.** The
        # bare division mis-floors an exact multiple to ``n - 1``, because
        # ``start_energy_dc_kwh`` is the float product ``n * bucket_kwh`` and need not
        # divide cleanly -- roughly four per cent of bucket sizes in the live
        # 0.15--0.40 band. When it happens this figure is short by one bucket's worth.
        floor_bucket = bucket_at_or_below_kwh(
            plan.terminal_floor_kwh, bucket_kwh=bucket_kwh
        )
        bucket = bucket_at_or_below_kwh(energy_kwh, bucket_kwh=bucket_kwh)
        value, _reason = plan.stored_value_eur(
            floor_bucket=floor_bucket, current_bucket=bucket
        )
        return value

    @callback
    def current_prices(self, now: datetime) -> tuple[float | None, float | None]:
        """Return the import and export price of the interval **in progress**.

        ``(import, export)``, either of which may be ``None``.

        **Not the horizon head, and the difference has caused two published
        defects.** Stage A's head is ``elapsed_intervals + 1`` -- the *next* whole
        interval -- so ``desired.intervals[0]`` is not now. A reader asking "what is
        electricity costing me at this moment" wants the interval containing this
        instant, which is what ``PriceForecast.interval_at`` answers, half-open and
        with no fallback to a neighbour: an instant nobody priced has no price.

        Read-only, and no decision path calls it. Prices deliberately reach no
        decision layer at all -- see the note above ``_price_forecasts_safely`` -- and
        this exists so one *entity* can show a person the context their plan was made
        in, not so the plan can consult it.
        """
        for forecast in self.price_forecasts.values():
            interval = forecast.interval_at(now)
            if interval is not None:
                return (
                    interval.import_price_eur_kwh,
                    interval.export_price_eur_kwh,
                )
        return None, None

    @callback
    def economic_value(self, now: datetime | None = None) -> dict[str, Any]:
        """Return the Economic Value payload for this refresh. Publish-only.

        The entity's reader. It takes the outcome from the published refresh, which
        is what an entity is allowed to see.
        """
        outcome = (self.data or {}).get("economic")
        if not isinstance(outcome, EconomicOutcome):
            outcome = None
        return self._economic_value_for(outcome, self.battery_plan, now)

    @callback
    def _economic_value_for(
        self, outcome: Any, plan: Any, now: datetime | None = None
    ) -> dict[str, Any]:
        """Return the Economic Value payload for one outcome. **One derivation.**

        Assembled here because this is where the calendar and the prices are, and
        computed in :func:`economic.economic_value_summary` because that is where the
        plan is -- so the entity, the diagnostics payload and the persisted evidence
        are three readers of one derivation rather than three derivations that have
        to agree.

        Takes the outcome as an argument rather than reading ``self.data``, because
        the decision record is written *before* the refresh publishes: a persisted
        figure and the entity's figure must describe the same solve, and the only way
        to guarantee that is to hand both the same object.
        """
        moment = dt_util.now() if now is None else now
        import_price, export_price = self.current_prices(moment)
        today_count = 0
        if plan is not None:
            today = plan.forecast.get("today") or {}
            count = today.get("interval_count")
            today_count = count if isinstance(count, int) else 0
        # **Known, not merely requested.** A forecast that exists but carries no
        # usable series is not knowledge, and publishing ``true`` for it would tell a
        # reader the horizon reaches tomorrow when it does not.
        tomorrow_forecast = self.price_forecasts.get(moment.date() + timedelta(days=1))
        tomorrow_known = bool(
            tomorrow_forecast is not None and tomorrow_forecast.available
        )
        summary = economic_value_summary(
            outcome,
            today_interval_count=today_count,
            import_price_eur_kwh=import_price,
            export_price_eur_kwh=export_price,
            comparator_model=(
                COMPARATOR_MODEL_AMBIENT_WALK
                if self._ambient_self_consumption()[0]
                else COMPARATOR_MODEL_AMBIENT_ABSORB_ONLY
            ),
            tomorrow_prices_known=tomorrow_known,
        )
        if not summary.get("available"):
            return summary
        # The calendar half, added here rather than in the pure layer: an interval
        # index becomes an instant only against a civil day and its real length.
        target_day = None if plan is None else plan.target_day
        tz = dt_util.get_default_time_zone()

        def instant(index: int) -> str | None:
            """Return the local instant a chronological index begins at."""
            if target_day is None or today_count <= 0:
                return None
            if index < today_count:
                start = interval_start_utc(target_day, index, tz)
            else:
                start = interval_start_utc(
                    target_day + timedelta(days=1), index - today_count, tz
                )
            return dt_util.as_local(start).isoformat()

        summary["horizon_from"] = instant(summary["first_interval_index"])
        # The instant the horizon stops covering, not the start of its last quarter.
        summary["horizon_to"] = instant(summary["last_interval_index"] + 1)
        # **The civil day's position, on the same entity. beta.39.** Assembled here
        # rather than in the pure layer for the same reason the instants are: it
        # needs the calendar, the persisted history and the stored prices, and none
        # of those belong to the solver.
        summary["today_accounting"] = self._today_accounting(outcome, plan, moment)
        return summary

    @callback
    def _head_run_state(self, now: datetime | None = None) -> int:
        """Return the run state Stage B is physically in, as a fact.

        **Not Stage B inventing economics.** It expresses no preference and asks
        for nothing; it reports which direction the inverter is being driven in, so
        that Stage A stops charging a fresh run-start fee to continue a campaign it
        is already executing and has already paid for. Starting anything new still
        pays, because only the head state is seeded.

        Read from the admitted open row -- the authority that actually produces the
        command -- and only while it is owned. An unowned state is idle, which is
        what every solve did before beta.35.

        **beta.36 stops the bookkeeping from lying about the physics.** The row was
        the *only* source, so whenever ``self._plan`` went missing -- which on
        2026-08-30 was every refresh for five and a half hours -- this reported
        ``IDLE`` while the inverter was demonstrably charging under a live claim, and
        every Stage-A solve paid a fresh run-start fee to continue a campaign it was
        already running. That silently reverted beta.35's own correction.

        The carried run is now the fallback, and it is a fact of the same kind: a run
        Stage B is carrying, whose window covers this instant, under an ownership
        record Alpha EMS wrote itself. It is **not** a licence to invent a direction
        -- a genuinely idle or torn-down execution still seeds ``IDLE``, because
        without a record there is nothing to read.
        """
        # Bound before it is compared: Phase 4 forbids a comparison whose text
        # contains "owned", since that is the shape an ownership derivation takes.
        # ``ownership_of`` is still the only thing that decides ownership; this is
        # a read of the identity in a record Alpha EMS wrote itself.
        recorded = self._owned_run_id()
        if recorded is None:
            return RUN_STATE_IDLE
        if self._quarter is not None and self._plan is not None:
            return run_state_for_intent(self._quarter.intent)
        carried = self._carried
        # ``now`` is optional because the solve site does not carry one -- it runs in
        # the executor, several layers below the refresh that has the instant. It
        # defaults to the wall clock, which is what every other unparameterised
        # derivation here does, and exists so a caller that *does* hold the refresh's
        # own instant can ask about that one instead of a second reading of the clock.
        moment = dt_util.now() if now is None else now
        if carried is not None and carried.actionable_at(moment):
            return run_state_for_intent(carried.target.intent)
        return RUN_STATE_IDLE

    @callback
    def _price_evidence(self, outcome: Any) -> dict[str, Any]:
        """Return the price head and its digest, for offline replay.

        The digest covers the **whole** horizon while the head is truncated, so a
        replay can prove it reconstructed the right series even though the record
        only shows the start of it.
        """
        if outcome is None or not outcome.desired.available:
            return {}
        intervals = outcome.desired.intervals
        if not intervals:
            return {}
        digest = hashlib.sha256(
            "|".join(
                f"{_price_text(row.import_price_eur_kwh)},"
                f"{_price_text(row.export_price_eur_kwh)}"
                for row in intervals
            ).encode()
        ).hexdigest()[:16]
        head = intervals[:_RECORD_HEAD_INTERVALS]
        return {
            "price_fingerprint": digest,
            "horizon_intervals": len(intervals),
            "first_interval_index": intervals[0].index,
            "import_price_head_eur_kwh": [
                _rounded_price(row.import_price_eur_kwh) for row in head
            ],
            "export_price_head_eur_kwh": [
                _rounded_price(row.export_price_eur_kwh) for row in head
            ],
        }

    @callback
    def _gate_evidence(self, outcome: Any) -> dict[str, Any]:
        """Return the export-permission head, so a veto can be replayed.

        Every field here is already computed each refresh and was already
        published live; none of it was ever *recorded*, which is why the 13:00
        collapse could be seen and not explained.
        """
        if outcome is None:
            return {}
        floor = outcome.export_floor_kwh
        raw = outcome.export_floor_raw_kwh
        protect = outcome.protect_price_eur_per_kwh
        free = outcome.export_free
        return {
            "survival_window_quarters": outcome.survival_window_end,
            "survival_window_basis": outcome.survival_window_basis,
            "export_floor_head_dc_kwh": [
                round(value, 3) for value in floor[:_RECORD_HEAD_INTERVALS]
            ],
            "export_floor_raw_head_dc_kwh": [
                round(value, 3) for value in raw[:_RECORD_HEAD_INTERVALS]
            ],
            "protect_price_head_eur_kwh": [
                None if value is None else round(value, 5)
                for value in protect[:_RECORD_HEAD_INTERVALS]
            ],
            "export_free_head": list(free[:_RECORD_HEAD_INTERVALS]),
            "export_gate_cost_eur": outcome.export_gate_cost_eur,
            "export_gate_withheld_kwh": outcome.export_gate_withheld_kwh,
            "export_gate_retained_kwh": outcome.export_gate_retained_kwh,
        }

    @callback
    def _floor_energy_for_record(self, plan: Any) -> float | None:
        """Return the configured hard floor as energy, for the decision record."""
        projection = None if plan is None else plan.reserve_projection
        return None if projection is None else projection.floor_energy_kwh

    @callback
    def _tomorrow_prices_available(self) -> bool | None:
        """Return whether tomorrow is priceable, from the source's own signal.

        Read, never predicted. The publication *time* is deliberately not modelled
        anywhere in this integration -- day-ahead can publish early or late -- so
        the transition instant is **observed** and recorded instead. That is the
        measurement the unknown-price policy needs and nobody has ever taken.
        """
        forecast = self.price_forecasts.get(self._tomorrow_date())
        return None if forecast is None else forecast.available

    @callback
    def _tomorrow_date(self) -> Any:
        """Return tomorrow's civil date in the configured zone."""
        return (dt_util.now() + timedelta(days=1)).date()

    @callback
    def _forecast_risk(
        self, horizon_intervals: int, today_interval_count: int
    ) -> ForecastRisk | None:
        """Return the measured forecast-quality evidence, or ``None``.

        **Every field is a quantity the learning stack already computed and, before
        beta.32, published to nobody who could act on it**: the signed bias, the
        absolute error split by provenance, the newly-measured error persistence,
        and today's adaptation ratio. No distribution is invented and no price is
        forecast.

        ``None`` when there is no rolling window at all, which leaves the export
        permission off and the plan exactly as beta.31 would have made it. That is
        the same doctrine ``_forecast_mae_kwh_per_interval`` already follows: a
        figure that cannot be established honestly is absent, never zero.
        """
        record = self.last_record
        window = None if record is None else getattr(record, "window", None)
        if window is None:
            return None
        # Adaptation is meaningless beyond the day it measured, and
        # ``today_interval_count`` is already the count of leading horizon
        # intervals belonging to today -- the same split ``_economic_prices`` uses
        # to decide which calendar day an interval's price comes from.
        adaptation = None
        today = (self.data or {}).get("today")
        if today is not None:
            adaptation = getattr(today, "adaptation_ratio", None)
        return ForecastRisk(
            bias_kwh=getattr(window, "bias_kwh", None),
            mae_kwh=getattr(window, "mae_kwh", None),
            mae_modelled_kwh=getattr(window, "mae_modelled_kwh", None),
            mae_filled_kwh=getattr(window, "mae_filled_kwh", None),
            mae_by_band=getattr(window, "mae_by_band", None),
            error_persistence=getattr(window, "error_persistence", None),
            adaptation_ratio=adaptation,
            today_interval_count=min(max(0, today_interval_count), horizon_intervals),
        )

    @callback
    def _ambient_self_consumption(self) -> tuple[bool, str]:
        """Return whether the inverter serves house load from the battery unbidden.

        Wrapped for the same reason every layer added since Phase 2 is: this reads
        the vendor control surface, and a fault there must cost one modelling
        refinement rather than the whole refresh.

        **Unknown means not modelled**, exactly as surplus absorption already
        treats an unreadable entity. Guessing optimistically here would give the
        plan a house fed for free, so the default is the pessimistic one -- an idle
        interval imports, which is what every release before beta.32 assumed
        unconditionally.
        """
        try:
            return self._ambient_self_consumption_from_device()
        except Exception:
            self._log.warning(
                _PV_LOG,
                (
                    "Whether the inverter serves house load from the battery could "
                    "not be determined; the plan treats an idle interval as "
                    "importing at full price, which is the conservative reading "
                    "and is what earlier releases did unconditionally"
                ),
            )
            _LOGGER.debug("Ambient self-consumption state unreadable", exc_info=True)
            return False, AMBIENT_SELF_CONSUMPTION_STATE_UNREADABLE

    @callback
    def _ambient_self_consumption_from_device(self) -> tuple[bool, str]:
        """Return the ambient-discharge verdict and how it was established.

        **The surplus-absorption detector, one direction over**, and the parallel
        is the argument for it: that layer stopped asserting "the inverter absorbs
        surplus autonomously" and started reading the control surface, because the
        vendor's own features contradict the assertion. The discharge direction has
        carried the same unexamined assertion ever since --
        ``docs/ARCHITECTURE.md`` says baseline self-consumption is real in the
        default configuration, and until now nothing checked.

        Two gates, and the two omissions matter more than the gates:

        * **Peak Shaving** arms the vendor's own dispatch, which may hold the pack
          against house load. Not modelled.
        * An inverter whose feature flags are **present but unreadable**, or whose
          required entities are unavailable, could be doing anything. Not modelled.

        **Not gated on ``excess_export_active``**: that feature directs *production*
        to load and feed-in, which is a statement about the charge direction. It
        says nothing about whether stored energy answers a residual load.

        **Not gated on ``dispatch_active``** -- and this is the one real departure.
        Surplus absorption asks what the pack is doing *now*, so a live dispatch
        makes the answer unknowable. This asks a **counterfactual**: what would
        happen in an interval where nothing is dispatched. A command running right
        now is no evidence about that, and gating on it would make the idle
        counterfactual -- and therefore every published marginal euro figure --
        flicker between two bases every time the optimiser dispatched anything.
        """
        capability = discover(self.hass)
        if capability.peak_shaving_active:
            return False, AMBIENT_SELF_CONSUMPTION_PEAK_SHAVING
        # Before the rest, because a feature boolean that cannot be read could be
        # hiding a feature that is on.
        if capability.feature_flags_present and not capability.feature_flags_readable:
            return False, AMBIENT_SELF_CONSUMPTION_STATE_UNREADABLE
        if capability.unavailable:
            return False, AMBIENT_SELF_CONSUMPTION_STATE_UNREADABLE
        if capability.missing or not capability.feature_flags_present:
            # Nothing on this installation could suppress it, which is different
            # evidence from "we checked and nothing is" -- named separately for the
            # same reason the absorption vocabulary names both.
            return True, AMBIENT_SELF_CONSUMPTION_NO_SUPPRESSING_FEATURE
        return True, AMBIENT_SELF_CONSUMPTION_SELF_CONSUMPTION

    @callback
    def _forecast_mae_kwh_per_interval(self) -> float | None:
        """Return the load forecast's mean absolute error, or ``None``.

        The **only** forecast-quality figure that reaches a planning decision, and
        it reaches exactly one: the statistical half of the uncertainty margin.
        ``None`` when there is not yet enough history to compare, which the margin
        reads as "no statistical claim" rather than as "no error".
        """
        record = self.last_record
        window = None if record is None else getattr(record, "window", None)
        return None if window is None else getattr(window, "mae_kwh", None)

    @callback
    def _executing_intent(self) -> str | None:
        """Return the intent currently being executed, or ``None``.

        **The quarter first**, because it is the narrower and more current
        authority and it freezes its intent at admission; then the carried run; then
        **the claim**, which is what makes a stop provable after both have gone.

        That last fallback is not a convenience. Since beta.30 the readback is an
        ownership factor, and the readback checks the *sign this intent permits* --
        so an unknown intent means an unprovable dispatch. On the Off path and after
        a restart neither the plan nor the run survives, and without the claim's own
        intent the EMS would be unable to prove ownership of the very dispatch it is
        trying to stop. The claim recorded what it armed; that is the authoritative
        answer to "what is running", and it outlives everything else by design.

        ``None`` is still a real answer and fails closed everywhere it is consumed:
        the sign gate refuses an unknown intent rather than guessing a direction.
        """
        if self._quarter is not None:
            return self._quarter.intent
        if self._carried is not None:
            return self._carried.intent
        record = self.store.execution_record
        if isinstance(record, dict):
            intent = record.get("intent")
            if isinstance(intent, str) and intent:
                return intent
        return None

    @callback
    def _execution_identity(self) -> str | None:
        """Return the run id ownership must be matched against.

        **The fallback that keeps a quarter executable while the run slot is
        empty**, which is beta.27's authority split:

            open quarter's run_id  ->  else carried run's run_id  ->  else None

        The causal record names a run, and a quarter admitted under run X still
        names run X -- so ``record_matches`` keeps working with nothing relaxed. The
        quarter is consulted *first* precisely so a newly admitted future run
        cannot take over the execution identity mid-quarter and rebase the
        run-keyed progress reset underneath it.
        """
        if self._quarter is not None:
            return self._quarter.run_id
        if self._carried is not None:
            return self._carried.run_id
        return None

    @callback
    def _reset_quarter_progress(self, quarter: CarriedQuarter | None) -> None:
        """Begin measuring ``quarter``, discarding any previous one's totals."""
        # **Two scopes, and beta.36 needs them apart.** The measured totals are
        # *claim*-scoped: a stop and a restart inside one row is a new arm, and energy
        # the previous arm delivered is not progress against this one. The provenance
        # below is *row*-scoped, and it has to be -- an arm is exactly what creates
        # the new claim, so clearing the arm record on a claim change erases the very
        # event it exists to record. Measured: the arming refresh counted its arm and
        # the next tick, seeing a claim where there had been none, wiped it, so every
        # completed row reported ``armed: false``.
        previous_row = self._quarter_key
        row_start = None if quarter is None else quarter.quarter_start
        self._quarter_claim = self._quarter_progress_key()[0]
        self._quarter_key = row_start
        self._quarter_battery_kwh = 0.0
        self._quarter_grid_import_kwh = 0.0
        self._quarter_grid_export_kwh = 0.0
        self._quarter_sampled_at = None
        self._quarter_peak_kw = 0.0
        self._quarter_power_sum = 0.0
        self._quarter_power_samples = 0
        self._quarter_pv_helped = False
        self._quarter_target_reached_at = None
        self._quarter_clamps = set()
        if previous_row != row_start:
            self._quarter_hold_failures = 0
        # **A new row is unaccrued.** Without this the exactly-once guard would
        # suppress the *next* row's accrual whenever two rows happened to share a
        # start instant across a restart, which is not reachable today and is one
        # refactor away from being.
        self._campaign_accrued_row = None

    @callback
    def _accrue_quarter_progress(self, now: datetime) -> None:
        """Integrate measured flows into the open quarter's totals.

        **Measured, never ``setpoint x elapsed``.** The setpoint is what the
        inverter was *asked* for; a clamp, a limit, a cloud or a full pack each make
        the delivered figure a different number.

        The two grid quantities follow the domains they will be compared against,
        and they are not symmetric because the plant is not:

        * **import** is attributed exactly as :meth:`_accrue_grid_attribution`
          does it -- production surplus first, grid second, incoherent readings
          attributed wholly to the grid -- because no metered grid-to-battery
          channel exists and the ceiling was published in those terms;
        * **export** is taken **straight off the meter**, because unlike the import
          share this genuinely is a metered channel, and it is the quantity Stage
          A's export objective is expressed in.

        Same defensive properties as the run-scoped accumulator: monotonic, a gap
        longer than :data:`MAX_SAMPLE_GAP_SECONDS` accrues nothing rather than
        extrapolating across a silence, and reset keyed on ``quarter_start`` alone.
        """
        quarter = self._quarter
        if quarter is None:
            self._quarter_sampled_at = None
            return
        # **Keyed on the claim as well as the row.** A stop and a restart inside one
        # quarter is a new arm, and energy delivered by the previous arm is not
        # progress against this one.
        claim, row_start = self._quarter_progress_key()
        if self._quarter_key != row_start or self._quarter_claim != claim:
            self._reset_quarter_progress(quarter)

        previous = self._quarter_sampled_at
        self._quarter_sampled_at = now
        if previous is None:
            return
        seconds = (now - previous).total_seconds()
        if seconds <= 0.0 or seconds > MAX_SAMPLE_GAP_SECONDS:
            # Out of order, or a silence too long to integrate across.
            return
        hours = seconds / 3600.0

        flows = self.read_flows()
        if quarter.intent == EXECUTION_INTENT_NET_EXPORT:
            discharge_kw = max(0.0, (flows.battery_discharge_w or 0.0) / 1000.0)
            self._quarter_battery_kwh += discharge_kw * hours
            if flows.grid_export_w is not None:
                self._quarter_grid_export_kwh += (
                    max(0.0, flows.grid_export_w) / 1000.0 * hours
                )
            power_kw = discharge_kw
        else:
            charge_kw = max(0.0, (flows.battery_charge_w or 0.0) / 1000.0)
            self._quarter_battery_kwh += charge_kw * hours
            measured = self._budget_surplus_kw()
            if measured is None:
                # Conservative: attribute the whole charge to the grid, so the
                # ceiling binds earlier. A budget exists to bound buying.
                #
                # **beta.40 leans on this rather than special-casing it.** With
                # no readable surplus the grid share is the whole charge and the
                # absorption branch has nothing to earn, so a site without a
                # usable production entity gets no opportunistic absorption --
                # by arithmetic rather than by a special case. Since beta.42 an
                # *implausible* reading reaches this branch too, rather than
                # inflating the surplus and emptying the ceiling.
                surplus_kw = 0.0
            else:
                surplus_kw = measured
            self._quarter_grid_import_kwh += max(0.0, charge_kw - surplus_kw) * hours
            if surplus_kw > 0.0:
                self._quarter_pv_helped = True
            power_kw = charge_kw

        self._quarter_peak_kw = max(self._quarter_peak_kw, power_kw)
        self._quarter_power_sum += power_kw
        self._quarter_power_samples += 1

    @callback
    def _quarter_progress(self, now: datetime) -> QuarterProgress | None:
        """Return what remains of the open quarter, in both energy domains."""
        quarter = self._quarter
        if quarter is None:
            return None
        if quarter.intent == EXECUTION_INTENT_NET_EXPORT:
            grid_remaining = quarter.grid_export_target_kwh - (
                self._quarter_grid_export_kwh
            )
        else:
            grid_remaining = quarter.grid_authorised_kwh - self._quarter_grid_import_kwh
        return QuarterProgress(
            seconds_remaining=quarter.seconds_remaining(now),
            # **Objective-attributed, beta.40.** Free production stored above
            # the objective must not reduce what the objective still owes, or a
            # row would report itself finished on energy it never promised.
            battery_remaining_kwh=max(
                0.0, quarter.battery_allowance_kwh() - self._quarter_objective_kwh
            ),
            grid_remaining_kwh=max(0.0, grid_remaining),
            # **Stage A's frozen verdict, beta.40.** Whether the tariff prefers
            # keeping free production to selling it.
            retention_authorised=quarter.absorption_authorised(),
            # **And how much is still worth keeping, measured now.** Two bounds in
            # one figure, because they answer the same question: the economic level
            # above which keeping stops paying, and the room the pack physically
            # has. Both are compared against a *live* state of charge -- the plan's
            # own state is rebuilt on the economic cadence and would be a quarter of
            # an hour stale on the cadence that commands.
            retention_remaining_kwh=self._retainable_kwh(quarter),
        )

    @callback
    def _quarter_ring_fields(self, now: datetime) -> dict[str, Any]:
        """Return the quarter context every flight-recorder entry carries.

        **Enough to reconstruct the quarter, not just the instant.** Diagnostics are
        almost never captured at the moment production moved, so a download taken an
        hour later has to be able to answer "what was this tick aiming at, under
        whose authority, and how much was left" without the reader having to
        correlate three other blocks by timestamp.

        Shared by the decision entry and the refusal entry, so the two cannot drift
        into carrying different context for the same tick.
        """
        quarter = self._quarter
        progress = self._quarter_progress(now)
        export = quarter is not None and quarter.intent == EXECUTION_INTENT_NET_EXPORT
        return {
            "plan_id": None if quarter is None else quarter.plan_id,
            "run_id": self._execution_identity(),
            "intent": None if quarter is None else quarter.intent,
            "quarter_start": (
                None if quarter is None else quarter.quarter_start.isoformat()
            ),
            "quarter_end": None if quarter is None else quarter.quarter_end.isoformat(),
            "battery_target_kwh": (
                None if quarter is None else round(quarter.battery_allowance_kwh(), 3)
            ),
            "battery_realized_kwh": round(self._quarter_battery_kwh, 3),
            "battery_remaining_kwh": (
                None if progress is None else round(progress.battery_remaining_kwh, 3)
            ),
            "grid_target_kwh": (
                None
                if quarter is None
                else round(
                    quarter.grid_export_target_kwh
                    if export
                    else quarter.grid_authorised_kwh,
                    3,
                )
            ),
            "grid_realized_kwh": round(
                self._quarter_grid_export_kwh
                if export
                else self._quarter_grid_import_kwh,
                3,
            ),
            "grid_remaining_kwh": (
                None if progress is None else round(progress.grid_remaining_kwh, 3)
            ),
            "seconds_remaining": (
                None if progress is None else round(progress.seconds_remaining, 1)
            ),
            "stop_reason": None,
        }

    @callback
    def _quarter_target_reached(self, progress: QuarterProgress) -> bool:
        """Return whether the open quarter's own objective has been met.

        **The objective decides, and which figure is the objective depends on the
        intent** -- the asymmetry the publication contract already states: a charge
        aims at the battery figure, an export at the meter figure.

        So a charge whose grid ceiling is spent has *not* finished: free production
        may still fill the pack, which is beta.26's F2. A ceiling is never a
        completion test.
        """
        quarter = self._quarter
        if quarter is None:
            return False
        if quarter.intent == EXECUTION_INTENT_NET_EXPORT:
            return progress.grid_remaining_kwh <= QUARTER_TARGET_TOLERANCE_KWH
        return progress.battery_remaining_kwh <= QUARTER_TARGET_TOLERANCE_KWH

    @callback
    def _note_quarter_clamp(self, clamp: str | None) -> None:
        """Remember a clamp that bound this quarter, for the completion record."""
        if clamp and clamp != SHORTFALL_NONE:
            self._quarter_clamps.add(clamp)

    @callback
    def _row_objective_kwh(self, row: CarriedQuarter | None) -> float:
        """Return one row's realised objective, at its own boundary.

        Extracted so the accrual and the completed-row record cannot disagree about
        which boundary a row is judged at -- the meter for an export, the battery for
        a charge. They used to compute it independently, eight lines apart.
        """
        if row is None:
            return 0.0
        if row.intent == EXECUTION_INTENT_NET_EXPORT:
            return self._quarter_grid_export_kwh
        # **The objective's share, not the whole charge. beta.40.** Absorbed
        # free production is real energy in the pack and is reported as such,
        # but it is not progress against this row's promise -- and this figure
        # is what a campaign's realised total is summed from.
        #
        # **Against ``row``'s own allowance since beta.43**, not against whatever
        # the slot has moved on to. See :meth:`_objective_kwh_for`.
        return self._objective_kwh_for(row)

    @callback
    def _campaign_row_is_final(
        self, now: datetime, *, ending: CarriedQuarter | None = None
    ) -> bool:
        """Return whether anything is known to follow this row.

        **Three clauses, each looking for positive evidence of continuation**, and
        the absence of all three is finality. beta.35 had one implicit test -- "no
        row covers this instant" -- and it closed a thirty-three-row campaign after
        three rows, because a ``serve_load`` gap satisfies it.

        1. *The frozen schedule.* The universal clause: a later executable row in
           the admitted plan is continuation whatever else is true. Blind past one
           plan, because a campaign may legitimately span several -- which is why
           the other two exist.
        2. *The frozen ``campaign_end``.* Stage A's own statement of scope, and safe
           to trust because ``campaign_identity`` is a digest of it, so an end that
           moves is a different campaign. Blind to Stage A having stopped publishing
           the campaign at all.
        3. *The current publications.* Sees a continuation written after this plan
           was frozen. Blind to a row frozen before it was written.

        Clauses 2 and 3 are asked **only of a row that belongs to the open
        campaign**. They are campaign facts, and applying them to a row outside it
        would answer "something follows" using another schedule's scope -- so a
        satisfied row with no campaign, or one whose campaign has already been handed
        over, would rest at zero for ever, re-arming a dead-man over a plan that no
        longer exists. That is the zombie shape this release removes, not one to add.

        Which way this must fail is therefore settled: **the absence of evidence is
        finality, never a hold.** A hold has no timeout of its own; it is bounded
        only by the row that owns it.
        """
        row = ending if ending is not None else self._quarter
        if row is None:
            return False
        plan = self._plan
        if plan is not None and any(
            other.executable and other.start >= row.quarter_end for other in plan.rows
        ):
            return False
        in_campaign = (
            self._campaign_id is not None and row.campaign_id == self._campaign_id
        )
        if in_campaign:
            planned_end = self._campaign_planned_end_utc
            if planned_end is not None and planned_end > row.quarter_end:
                return False
            for target in self.execution_targets:
                if target.get("campaign_id") != self._campaign_id:
                    continue
                closes = instant_of(target.get("window_end"))
                if closes is not None and closes > row.quarter_end:
                    return False
        return True

    @callback
    def _campaign_stop_reason(self, row_reason: str) -> str:
        """Return the truthful reason a campaign-scoped stop should carry. beta.40.

        Three outcomes, and each names what actually happened:

        * no campaign open -- the row's own reason. "Campaign objective reached"
          would be a claim about a thing that does not exist, and the surfaces
          would render a campaign success for a single-run charge;
        * campaign open and the objective satisfied within tolerance --
          ``campaign_objective_reached``, which is then true;
        * campaign open and the window ended short -- ``window_ended``, which
          already exists, already means exactly this, and is already in
          :data:`EXECUTION_COMPLETION_STOP_REASONS`, so the outcome mapping in
          :meth:`_close_campaign` is unchanged and a short campaign still files
          ``partial`` rather than ``canceled``.

        No new vocabulary: a synonym for a reason the codebase already has would
        be a sixth alias for a reader to disambiguate.
        """
        if self._campaign_id is None:
            return row_reason
        if self._campaign_objective_met():
            return EXECUTION_STOP_CAMPAIGN_COMPLETE
        return EXECUTION_STOP_WINDOW_ENDED

    @callback
    def _campaign_objective_met(self) -> bool:
        """Return whether the frozen campaign objective is actually satisfied.

        **The question ``_completion_scope`` answers with a shrug. beta.40.**

        A campaign-scoped stop has two entirely different causes: the objective was
        delivered, or the last planned row closed without it. Both are legitimate
        endings and both stop the same dispatch, so the *scope* is rightly the same
        -- but the published *reason* is a claim about the objective, and until
        beta.40 both causes published ``campaign_objective_reached``.

        The 2026-09-03 campaign is what that looks like: 23 of 23 rows completed,
        so the schedule was final and the scope was correct, while the objective
        stood at **5.939 of 13.100 kWh**. ``outcome`` said ``partial`` -- it is
        computed here from the energy and was always right -- and ``reason`` said
        the objective had been reached. A reader trusting the reason would have
        recorded a 45 %-delivered campaign as a success.

        Same tolerance as the terminal verdict and the scope test, deliberately:
        three places asking one question must not each pick their own answer.
        """
        frozen = self._campaign_frozen_target_kwh
        if frozen is None or frozen <= 0.0:
            # No objective was ever published, so none can have been reached.
            return False
        tolerance = self._completion_tolerance_kwh(
            frozen, self._campaign_quarters_admitted
        )
        return self._campaign_realized_now() >= frozen - tolerance

    def _completion_scope(
        self, now: datetime, *, ending: CarriedQuarter | None = None
    ) -> str | None:
        """Return the scope a finished row's stop should have, or ``None`` to hold.

        ``None`` is the ordinary answer, and it is the whole product decision: a row
        finishing inside a campaign stops nothing.
        """
        if self._campaign_row_is_final(now, ending=ending):
            return STOP_SCOPE_CAMPAIGN
        frozen = self._campaign_frozen_target_kwh
        if frozen is not None and frozen > 0.0:
            tolerance = self._completion_tolerance_kwh(
                frozen, self._campaign_quarters_admitted
            )
            if self._campaign_realized_now() >= frozen - tolerance:
                # The campaign delivered what it promised. Reuses the existing
                # reporting tolerance rather than introducing a control one.
                return STOP_SCOPE_CAMPAIGN
        return None

    @callback
    def _min_armable_kwh(self, row: CarriedQuarter) -> float:
        """Return the smallest objective this row may be armed for. beta.43.

        The actuator-resolution floor for every intent, and for ``net_export`` the
        controllability floor as well -- the same asymmetry Stage A applies when it
        stamps ``not_executable``, expressed once so the two cannot drift apart.
        Stage B's backstop is a backstop: it never admits a row Stage A refused.
        """
        if row.intent == EXECUTION_INTENT_NET_EXPORT:
            return MIN_CONTROLLABLE_QUARTER_KWH
        return MIN_EXECUTABLE_QUARTER_KWH

    @callback
    def _objective_kwh_for(self, row: CarriedQuarter | None) -> float:
        """Return the realised objective **of ``row``**, at ``row``'s own boundary.

        **The subject is the argument, and beta.43 is where that stopped being a
        comment.** :meth:`_record_completed_quarter` has stated the rule since
        beta.35 -- *"the subject is a parameter, not ``self._quarter``"* -- and then
        read three figures off properties that resolve against the field. Both
        recording callers reach it after the slot has advanced, so a finished charge
        row was judged against the **successor** row's allowance: a successor with a
        zero allowance zeroes it, while ``_campaign_quarters_admitted`` still counts
        the accrual.

        Export is unaffected either way, because its objective is a plain metered
        field with no envelope. The expression is otherwise beta.40's exactly.
        """
        if row is None:
            return self._quarter_battery_kwh
        if row.intent == EXECUTION_INTENT_NET_EXPORT:
            # An export has no absorption envelope -- there is no such thing as
            # free production to discharge -- so its whole movement is objective.
            return self._quarter_battery_kwh
        return min(self._quarter_battery_kwh, row.battery_allowance_kwh())

    @callback
    def _absorbed_kwh_for(self, row: CarriedQuarter | None) -> float:
        """Return free production stored above ``row``'s objective. beta.43."""
        return max(0.0, self._quarter_battery_kwh - self._objective_kwh_for(row))

    @property
    def _quarter_objective_kwh(self) -> float:
        """Return the open row's realised **objective**. beta.40.

        ``_quarter_battery_kwh`` is every kWh the pack took. This is the part of
        it the row promised, and :attr:`_quarter_absorbed_kwh` is the rest --
        free production stored under the row's envelope, which is real energy
        and is *not* progress against a promise the row never made. The two sum
        to the total exactly, by construction.

        **Derived rather than integrated, and that is a proof rather than a
        shortcut.** Crediting the objective first and capping it hard gives, for
        a total ``T`` and a frozen allowance ``A``:

            obj(0) = 0,  obj(n+1) = obj(n) + min(step, A - obj(n))
                    =>  obj = min(T, A)

        so a running sum would hold nothing this expression does not, and the
        equality needs only that ``A`` not move while the row is open. It cannot:
        an opened quarter's allowance is frozen at admission and no later
        publication can enlarge or reduce it -- the beta.27 invariant, held by
        ``test_beta27_quarter_authority``.

        Deriving it therefore buys the same semantics with no second accumulator
        to reset, capture, restore or lose across a stop -- and the capture
        tuples this class holds are positional.
        """
        # The open row is the subject here, and this is the one place that is
        # true: every *recording* caller names its row instead. beta.43.
        return self._objective_kwh_for(self._quarter)

    @property
    def _quarter_absorbed_kwh(self) -> float:
        """Return free production stored above the row's objective. beta.40."""
        return self._absorbed_kwh_for(self._quarter)

    @callback
    def _retainable_kwh(self, quarter: CarriedQuarter) -> float | None:
        """Return the AC energy this row may still keep, or ``None``.

        **Two bounds, one figure, because they answer the same question**: how
        much more free production is worth keeping, and how much the pack can
        still take. Whichever is lower governs.

        The economic half is Stage A's ``retention_until_dc_kwh`` -- the level at
        which the optimiser's own dual stops clearing the export price. The
        physical half is the pack's own ceiling. Both are differenced against a
        **live** state of charge, and that is the point: ``battery_plan.state`` is
        rebuilt on the economic cadence, so on the sixty-second cadence that
        commands it can be a quarter of an hour old -- and a quarter of an hour at
        the power this branch will command is long enough to fill the room the
        stale figure still reports.

        ``None`` means unbounded, and is what a site with no readable pack state
        and no published ceiling gets: the physical clamps still apply, exactly as
        they do to every other charge.
        """
        plan = self.battery_plan
        if plan is None or plan.state is None:
            return None
        limits = plan.state.limits
        soc_percent = self._read_soc_percent()
        if soc_percent is None:
            # No reading, no bound to compute -- and ``_absorption_live`` refuses
            # on the same absence, so nothing is granted on an unknown pack.
            return None
        stored_dc = limits.energy_for_soc(soc_percent)
        ceiling_dc = limits.energy_for_soc(BATTERY_MAX_SOC_PERCENT)
        until_dc = quarter.retention_until_dc_kwh
        cap_dc = ceiling_dc if until_dc is None else min(until_dc, ceiling_dc)
        room_dc = max(0.0, cap_dc - stored_dc)
        if limits.charge_efficiency <= 0.0:  # pragma: no cover - defensive
            return None
        return room_dc / limits.charge_efficiency

    @callback
    def _measured_pv_surplus_kw(self) -> float | None:
        """Return measured production above the house, or ``None`` if unknown.

        **The authoritative reading for beta.40, and it is deliberately the
        entity one.** ``_live_kw`` treats an absent production sensor as zero,
        which is right for a controller correcting a *commanded* figure and
        wrong for granting authority: a zero-filled surplus would look like a
        measurement. Here absent is ``None`` and ``None`` earns nothing, on the
        same terms the accrual already attributes an unreadable charge wholly to
        the grid.

        Open-loop by construction: production less house load, never the meter.
        The meter would be the tighter signal and is unusable -- absorbing
        reduces the export that authorised it, so the authority would chase its
        own effect.
        """
        pv_w = self._read_pv_power_w()
        load_w = self._read_house_load_w()
        if pv_w is None or load_w is None:
            return None
        return max(0.0, (pv_w - load_w) / 1000.0)

    @callback
    def _absorption_gate_now(self, quarter: CarriedQuarter | None) -> str | None:
        """Return the gate word for the open row, live reading included.

        Stage A's frozen verdict, except where the live side has already overruled
        it: with no readable production there is no measured surplus to earn
        anything, whatever the plan authorised. Reported rather than inferred, so
        a reader never has to work out from a zero whether the tariff refused or
        the sensor did.
        """
        if quarter is None:
            return None
        if self._measured_pv_surplus_kw() is None:
            return RETENTION_GATE_NO_PV
        return quarter.retention_gate

    @callback
    def _absorption_live(self, progress: QuarterProgress) -> bool:
        """Return whether free production may still be stored under this row.

        The condition that keeps a satisfied row charging instead of holding at
        zero. Every clause is necessary and one of them is subtle:

        * Stage A must have authorised keeping it, on this row, before it opened;
        * production must be *measured*, not assumed. ``None`` is a refusal;
        * the surplus must clear the actuator's own minimum. Without this clause
          the target-reached latch would suppress the below-resolution hold and
          leave the tick writing a command the device cannot express -- a trickle
          instead of a rest;
        * the pack must have somewhere to put it, **read live**.

        The last clause is measured rather than taken from ``battery_plan``, and
        that is deliberate. The plan's state is rebuilt on the economic cadence, so
        on the physical one it can be a quarter of an hour old -- and a quarter of
        an hour at the surplus this branch is willing to command is exactly long
        enough to fill the room the stale figure is reporting. The state of charge
        is read every tick anyway, and it is the pack's own physical state.
        """
        quarter = self._quarter
        if quarter is None or quarter.intent == EXECUTION_INTENT_NET_EXPORT:
            return False
        if not progress.retention_authorised:
            return False
        surplus_kw = self._measured_pv_surplus_kw()
        if surplus_kw is None or surplus_kw < CONTROL_MIN_POWER_KW:
            return False
        soc_percent = self._read_soc_percent()
        if soc_percent is None:
            # An unknown pack is not an empty one. No reading, no absorption.
            return False
        if soc_percent >= BATTERY_MAX_SOC_PERCENT:
            return False
        # **And there must be something left that is worth keeping.** A row whose
        # economic ceiling has been reached is finished absorbing even though the
        # sun is still shining and the pack is not full, which is the whole point
        # of the ceiling: past it, exporting pays better.
        retainable = progress.retention_remaining_kwh
        return retainable is None or retainable > QUARTER_TARGET_TOLERANCE_KWH

    @callback
    def _quarter_is_satisfied(self, now: datetime) -> bool:
        """Return whether the open row is done and has nothing left to do.

        **Satisfied *and* not absorbing, since beta.40.** The refresh path reads
        this to command zero, and a row whose objective is met while it is still
        storing free production is not finished -- answering ``True`` there would
        write a zero straight over a live absorption on the economic cadence,
        undoing on the quarter boundary exactly what the tick had just done.
        """
        if self._quarter is None or self._quarter_target_reached_at is None:
            return False
        progress = self._quarter_progress(now)
        return progress is None or not self._absorption_live(progress)

    @callback
    def _row_provenance(self, row_start: datetime | None) -> dict[str, Any] | None:
        """Return the provenance record for one row, creating it on first use.

        ``None`` when there is no row: provenance belongs to a row and there is
        nowhere honest to put it otherwise. Bounded on insertion by the same figure
        that bounds the completed-row ring, oldest first, so a long day cannot grow
        it without limit.
        """
        if row_start is None:
            return None
        record = self._quarter_provenance.get(row_start)
        if record is None:
            record = {
                "arm_attempts": 0,
                "write_count": 0,
                "hold_writes": 0,
                "refusals": [],
            }
            self._quarter_provenance[row_start] = record
            while len(self._quarter_provenance) > MAX_COMPLETED_QUARTERS_REPORTED:
                self._quarter_provenance.pop(next(iter(self._quarter_provenance)))
        return record

    @callback
    def _open_row_provenance(self) -> dict[str, Any] | None:
        """Return the open row's provenance record, or ``None`` if no row is open."""
        row = self._quarter
        return self._row_provenance(None if row is None else row.quarter_start)

    @callback
    def _note_quarter_write(self) -> None:
        """Count one power write that landed inside this row."""
        record = self._open_row_provenance()
        if record is not None:
            record["write_count"] += 1

    @callback
    def _note_quarter_arm_attempt(self) -> None:
        """Count one authorised arm sequence for this row.

        Authorised, not merely planned, so ``armed`` is exactly true. A refused arm
        is recorded as a refusal instead -- see :meth:`_note_quarter_refusal`.
        """
        record = self._open_row_provenance()
        if record is not None:
            record["arm_attempts"] += 1

    @callback
    def _note_quarter_refusal(self, reason: str | None) -> None:
        """Remember why a write for this row did not happen.

        **Kept apart from ``binding_clamps`` deliberately.** That field is the clamp
        vocabulary, and a refusal is not a clamp: a clamp reduced a command that was
        *sent*, a refusal means nothing was. Merging them would put
        ``reason_vocabulary`` in the position of naming two families at once -- and
        would have made the 0.56 kWh row of 2026-08-30 no more legible, because its
        one recorded clamp was ``quarter_expired``, which is also exactly what a
        mid-row teardown writes.
        """
        record = self._open_row_provenance()
        if record is None or not reason:
            return
        refusals = record["refusals"]
        if reason in refusals or len(refusals) >= MAX_QUARTER_REFUSALS_RECORDED:
            return
        refusals.append(reason)

    async def _async_hold_at_zero(
        self, now: datetime, snapshot: Any, hold_reason: str
    ) -> None:
        """Command zero, keep everything else, and stay in the row.

        **The beta.36 state that did not exist, and whose absence destroyed two
        campaigns.** The row has nothing to ask for at this instant -- either its
        objective is met, or the authorised rate is below what the actuator can
        express. Ownership, the claim, the frozen schedule and the campaign instance
        all stay; the commanded power goes to zero, once, explicitly.

        **Deliberately not through ``_dispatch_setpoint``.** ``_finish`` substitutes
        the *held* value for any move smaller than ``DISPATCH_POWER_DEADBAND_KW``
        (0.2 kW) -- which is two actuator steps -- so a row satisfied while sitting
        at 0.1 kW would keep drawing 0.1 kW for the rest of the quarter with
        ``within_deadband`` printed beside it. A commanded rest must be exempt from
        the hysteresis that exists to suppress *noise*.

        If the write does not read back, the previous setpoint is still on the device
        and still delivering into a met objective. That is retried, and escalated to
        a real abort after :data:`HOLD_WRITE_FAILURE_LIMIT` consecutive failures: a
        dispatch we cannot command down is a dispatch we do not control.
        """
        tick_reason = (
            TICK_HELD_QUARTER_SATISFIED
            if hold_reason == HOLD_REASON_QUARTER_SATISFIED
            else TICK_HELD_RATE_BELOW_RESOLUTION
        )
        self._hold_reason = hold_reason
        if self._applied_setpoint_kw == 0.0:
            self._quarter_hold_failures = 0
            self._note_tick(now, tick_reason)
            return
        landed = await self._async_send_locked(
            plan_dispatch_power(0.0),
            now=now,
            verify=EXECUTION_VERIFY_DISPATCH_SETPOINT,
        )
        if not landed:
            self._quarter_hold_failures += 1
            self._note_quarter_refusal(TICK_HOLD_WRITE_FAILED)
            if self._quarter_hold_failures >= HOLD_WRITE_FAILURE_LIMIT:
                await self._async_end_quarter(
                    now,
                    snapshot,
                    QUARTER_END_SAFETY,
                    SHORTFALL_TARGET_REACHED,
                    stop_reason=EXECUTION_STOP_EXECUTION_ERROR,
                    scope=STOP_SCOPE_ABORT,
                )
                self._note_tick(now, TICK_STOPPED_TARGET_REACHED, wrote=True)
                return
            self._note_tick(now, TICK_HOLD_WRITE_FAILED)
            return
        self._applied_setpoint_kw = 0.0
        record = self._open_row_provenance()
        if record is not None:
            record["hold_writes"] += 1
        self._quarter_hold_failures = 0
        self._note_quarter_write()
        self._note_tick(now, tick_reason, wrote=True)

    @callback
    def _record_completed_quarter(
        self, quarter: CarriedQuarter | None, reason: str, *, accrue: bool = True
    ) -> None:
        """Append ``quarter`` to the bounded history, with its measured progress.

        Recorded whatever the outcome, because the quarter that fell short is the
        one a reader most needs -- and the shortfall is **stated**, not left to be
        derived by subtracting two other published figures.

        **The subject is a parameter, not ``self._quarter``.** Both callers reach
        this after the slot has already moved on: the stop path clears it before the
        bookkeeping runs, and the carry path has replaced it with the *next* quarter.
        Reading the field would have recorded nothing in the first case and the new
        quarter's targets against the old one's measured progress in the second.
        """
        if quarter is None:
            return
        # Read once, and for the row being recorded rather than for whatever row the
        # accumulators have moved on to.
        provenance = self._row_provenance(quarter.quarter_start)
        assert provenance is not None
        planned_battery = quarter.battery_allowance_kwh()
        export = quarter.intent == EXECUTION_INTENT_NET_EXPORT
        planned_grid = (
            quarter.grid_export_target_kwh if export else quarter.grid_authorised_kwh
        )
        realised_grid = (
            self._quarter_grid_export_kwh if export else self._quarter_grid_import_kwh
        )
        # Against the objective, never the ceiling: unspent grid authorisation on a
        # charge is not a shortfall, it is production having paid for the charge.
        planned = planned_grid if export else planned_battery
        # **The objective's share, beta.40.** Judging a row's shortfall against
        # every kWh the pack took would let absorbed free production paper over
        # an objective the row genuinely missed -- and this same figure is what
        # the campaign accumulator below is advanced by.
        # **``quarter``, never the field. beta.43.** Both callers arrive after the
        # slot has advanced; reading the property here judged this row against the
        # successor's allowance. See :meth:`_objective_kwh_for`.
        objective = self._objective_kwh_for(quarter)
        realised = realised_grid if export else objective
        shortfall = max(0.0, planned - realised)
        mean_kw = (
            0.0
            if not self._quarter_power_samples
            else self._quarter_power_sum / self._quarter_power_samples
        )
        # **The campaign's own accumulator, advanced from the same measurement.**
        # One source, so the campaign total and the quarter rows can never
        # disagree -- and it accrues across ``serve_load`` gaps and segment
        # boundaries, because a campaign that pauses to feed the house has not
        # stopped selling.
        #
        # ``accrue=False`` is for the one caller that must accrue *before* the
        # physical stop -- see ``_async_end_quarter``. The guard in
        # ``_accrue_campaign_progress`` makes a double call harmless anyway; the
        # switch keeps the intent visible rather than relying on it.
        if accrue:
            self._accrue_campaign_progress(quarter, realised)
        self._completed_quarters.append(
            {
                "quarter_start": quarter.quarter_start.isoformat(),
                "quarter_end": quarter.quarter_end.isoformat(),
                "intent": quarter.intent,
                "planned_battery_kwh": round(planned_battery, 3),
                "realized_battery_kwh": round(self._quarter_battery_kwh, 3),
                # beta.40: the split of the figure above. They sum to it.
                "objective_battery_kwh": round(objective, 3),
                "absorbed_extra_kwh": round(
                    max(0.0, self._quarter_battery_kwh - objective), 3
                ),
                "retention_authorised": quarter.retention_authorised,
                "absorption_gate": quarter.retention_gate,
                "planned_grid_kwh": round(planned_grid, 3),
                "realized_grid_kwh": round(realised_grid, 3),
                "shortfall_kwh": round(shortfall, 3),
                # **Null below one actuator step, since beta.32.** The observed row
                # published ``140 %`` against a 0.01 kWh objective -- arithmetically
                # correct and useless: a percentage of a figure smaller than
                # anything a command could move is noise wearing a decimal point.
                # Withheld rather than clipped, because "no meaningful percentage"
                # and "0 %" are different statements.
                "shortfall_percent": (
                    None
                    if planned < MIN_EXECUTABLE_QUARTER_KWH
                    else round(100.0 * shortfall / planned, 1)
                ),
                # **Signed, and at the objective's boundary.** ``shortfall_kwh`` is
                # clamped at zero, so a quarter that *over*-delivered looked
                # identical to one that landed exactly. The sign is what separates
                # meter-side tracking lag -- which scales with the target and would
                # defeat a fixed tolerance at larger objectives -- from noise, and a
                # week of real export quarters settles which the 13 % was.
                "objective_tracking_error_kwh": round(realised - planned, 4),
                "objective_tracking_error_fraction": (
                    None
                    if planned < MIN_EXECUTABLE_QUARTER_KWH
                    else round((realised - planned) / planned, 4)
                ),
                "objective_boundary": (
                    CAMPAIGN_BOUNDARY_METER if export else CAMPAIGN_BOUNDARY_BATTERY
                ),
                "campaign_id": quarter.campaign_id,
                "completion_reason": reason,
                "reason_vocabulary": REASON_VOCABULARY_QUARTER_COMPLETION,
                "max_dispatch_kw": round(self._quarter_peak_kw, 3),
                "mean_dispatch_kw": round(mean_kw, 3),
                "pv_helped": self._quarter_pv_helped,
                "binding_clamps": sorted(self._quarter_clamps),
                # **What this row actually attempted. beta.36.**
                #
                # The 0.56 kWh row of 2026-08-30 was admitted, derived, ticked
                # against fifteen times and moved nothing, and the only thing the
                # record said was ``quarter_expired`` -- which is also exactly what a
                # mid-row teardown writes. No tick reason, no authorisation refusal
                # and no write-boundary refusal could reach this record, so the
                # payload could not distinguish "never armed" from "ran its course".
                "armed": provenance["arm_attempts"] > 0,
                "arm_attempts": provenance["arm_attempts"],
                "write_count": provenance["write_count"],
                "hold_writes": provenance["hold_writes"],
                "refusals": list(provenance["refusals"]),
                "write_or_refuse_rule": (
                    "an executable row either armed, or refused and said why. a row "
                    "that did neither is a lost quarter, and these fields are what "
                    "say so. kept apart from binding_clamps on purpose: a clamp "
                    "reduced a command that was sent, a refusal means none was"
                ),
                "target_reached_at": (
                    None
                    if self._quarter_target_reached_at is None
                    else self._quarter_target_reached_at.isoformat()
                ),
                "carry_over_rule": (
                    "a shortfall is recorded and never carried into another "
                    "quarter. Stage A decides each quarter independently"
                ),
            }
        )

    @callback
    def _accrue_campaign_progress(
        self, quarter: CarriedQuarter | None, realised_kwh: float
    ) -> None:
        """Add one completed quarter's realised objective to its campaign.

        Only for the campaign currently open, and only from the objective boundary
        the quarter was judged at -- the meter for an export, the battery for a
        charge. A quarter belonging to some other campaign contributes nothing,
        which is what stops a replan from crediting one campaign with another's
        energy.
        """
        if quarter is None or quarter.campaign_id is None:
            return
        if quarter.campaign_id != self._campaign_id:
            return
        # **Exactly once, made a property of a field rather than of call ordering.**
        #
        # Three sites can record a completed row -- the tick's end-of-row, the tick's
        # end-of-quarter and the refresh's between-ticks catch-up -- and nothing said
        # they were mutually exclusive. beta.35 published ``quarters_admitted: 2``
        # against three completed rows, which is the *losing* failure; the same
        # ambiguity makes double-counting representable, and no trace has happened to
        # exhibit it yet.
        if (
            self._campaign_accrued_row is not None
            and quarter.quarter_start == self._campaign_accrued_row
        ):
            return
        self._campaign_accrued_row = quarter.quarter_start
        self._campaign_realized_kwh += max(0.0, realised_kwh)
        self._campaign_quarters_admitted += 1
        if self._quarter_progress_unknown:
            # A restart lost a quarter of this campaign. The total is no longer a
            # measurement, and saying so is the honesty guard that outranks even a
            # met objective in the outcome precedence.
            self._campaign_measurable = False

    @callback
    def _open_quarter_objective_kwh(self) -> float:
        """Return the open quarter's realised objective, at its own boundary.

        Zero once the row has been accrued: a row counted as committed and again as
        open would be double counted, which is the other half of the exactly-once
        guard in :meth:`_accrue_campaign_progress`.
        """
        quarter = self._quarter
        if quarter is None or quarter.campaign_id != self._campaign_id:
            return 0.0
        if (
            self._campaign_accrued_row is not None
            and quarter.quarter_start == self._campaign_accrued_row
        ):
            return 0.0
        if quarter.intent == EXECUTION_INTENT_NET_EXPORT:
            return self._quarter_grid_export_kwh
        # Objective-attributed, beta.40: see ``_row_objective_kwh``. A campaign
        # counting absorbed production towards its frozen target would read
        # itself complete while it still had energy to buy, and terminate.
        return self._quarter_objective_kwh

    @callback
    def _campaign_realized_now(self) -> float:
        """Return the campaign's realised objective including the open quarter."""
        return self._campaign_realized_kwh + self._open_quarter_objective_kwh()

    @callback
    def _campaign_objective_kwh(self, campaign_id: str) -> float | None:
        """Return what the published plan means this campaign to achieve.

        Summed over the campaign's **executable** segments, at each one's own
        objective boundary -- the meter for an export, the battery for a charge.

        **A non-executable segment contributes nothing, and that is the whole
        subtlety.** A ``serve_load`` segment is published with a battery figure,
        because the plan really does move that energy; but it is a *ceiling* on
        ambient inverter behaviour, not an objective anybody commanded, and no
        actuator is asked for it. Adding it would inflate a sale's promise by the
        energy the house happened to take during the gap -- measured on the
        reference multi-segment shape, 2.64 kWh of genuine meter objective became
        5.39. A discharge campaign whose segments are *all* ``serve_load``
        therefore sums to zero, which is correct: it sells nothing, so it is not a
        sell.

        This mirrors :attr:`EconomicCampaign.objective_kwh` exactly, and it has to:
        the frozen target and the announced figure are the same promise, and a
        campaign that announced one number and was judged against another would be
        the boundary confusion beta.32 set out to end.

        ``None`` when no published target names this campaign, which is how a
        pre-beta.32 record and an unplaceable target both degrade to run-level
        behaviour rather than to a zero.
        """
        # **The admitted schedule first, since beta.35, and the reason is that the
        # publication cannot answer.** This read ``execution_targets`` alone -- the
        # solve from *this* refresh, whose head is ``elapsed + 1``. A campaign whose
        # remaining rows are all behind that head appears in no published target at
        # all, so every read returned ``None`` and the freeze had nothing to freeze:
        # on 2026-08-29 an export that had already moved 1.92 kWh was published with
        # ``frozen_target_kwh: null`` and closed as *target unavailable*.
        #
        # The frozen schedule has no such problem. It was admitted before any of it
        # happened, it is immutable afterwards, and it carries the campaign's own
        # objective at the campaign's own boundary. ``execution_targets`` stays as
        # the fallback, for a campaign spanning more than one admitted plan.
        plan = self._plan
        if plan is not None and plan.campaign_id == campaign_id:
            frozen = 0.0
            rows = 0
            # **Bounded by the campaign's own end, since beta.36.** ``QuarterRow``
            # carries no campaign field, so a per-row membership test is not
            # expressible -- but the frozen ``campaign_end`` bounds the same thing
            # from outside. A no-op on every plan seen so far, because ``admit_plan``
            # builds from one target and one target names one campaign; the *type*
            # permits a heterogeneous plan, and an unbounded sum over it would
            # silently promise another campaign's energy.
            horizon = plan.campaign_end
            for row in plan.rows:
                if not row.executable:
                    continue
                if horizon is not None and row.start >= horizon:
                    continue
                if plan.intent == EXECUTION_INTENT_NET_EXPORT:
                    frozen += float(row.grid_export_target_kwh or 0.0)
                elif plan.intent == EXECUTION_INTENT_GRID_CHARGE:
                    frozen += float(row.battery_kwh or 0.0)
                rows += 1
            self._campaign_objective_rows = rows
            return frozen

        total = 0.0
        seen = False
        # **Counted on this branch too, since beta.43.** Only the frozen-plan branch
        # above assigned ``_campaign_objective_rows``, so a campaign read through the
        # fallback published whatever count the *previous* campaign had left behind:
        # the 2026-09-05 terminal reported ``objective_row_count: 1`` beside three
        # recorded rows and two accruals, three figures disagreeing at once.
        rows = 0
        for target in self.execution_targets:
            if target.get("campaign_id") != campaign_id:
                continue
            # Membership is what ``seen`` records, so a campaign made entirely of
            # gaps still reports 0.0 rather than ``None`` -- "it sells nothing" and
            # "nobody published it" are different answers.
            seen = True
            intent = target.get("intent")
            if intent == EXECUTION_INTENT_NET_EXPORT:
                total += float(target.get("grid_target_kwh") or 0.0)
            elif intent == EXECUTION_INTENT_GRID_CHARGE:
                total += float(target.get("battery_target_kwh") or 0.0)
            rows += 1
        if seen:
            self._campaign_objective_rows = rows
        return total if seen else None

    @callback
    def _note_campaign_progress(self, now: datetime, stop_reason: str | None) -> None:
        """Open, advance and close the campaign lifecycle. Once per refresh.

        **This is the layer beta.31 did not have, and its absence is R10/R11.**
        Since beta.29 the hardware is armed from the admitted quarter and stopped
        from the 60-second tick; ``Decision`` stopped being the executor two
        releases ago, and the Activity terminal was still wired to it. So the
        terminal is derived here instead -- from the carrier that actually admits
        the energy, in the layer that measured it.

        Three transitions, and nothing else:

        * a quarter appears whose campaign is not the open one -- **open** it, and
          close whatever was open first, because one campaign at a time is the
          whole point of the unit;
        * a write carrying an activation succeeds while a campaign is open --
          **freeze** its objective, once, for ever;
        * no quarter and no admissible target names the open campaign any more --
          **close** it, latching the outcome computed below.
        """
        quarter = self._quarter
        current = None if quarter is None else quarter.campaign_id
        if self._campaign_is_final(current) and current != self._campaign_id:
            # **A campaign that genuinely finished never runs again. beta.36.**
            #
            # The beta.35 guard asked whether the campaign had been *abandoned*,
            # which conflated two different endings and barred the one case that
            # must be allowed: a fresh attempt after a hazard abort. The rule that
            # actually needs enforcing is the other one -- a campaign that reached
            # its objective or ran out of schedule is done, and Stage A continuing
            # to publish it (its horizon still contains it) is not a reason to
            # execute it twice.
            #
            # Realised history stays immutable either way: a new *instance* never
            # touches the closed one's totals, because it has its own.
            return
        if current is not None and current != self._campaign_id:
            self._close_campaign(now, stop_reason)
            self._campaign_id = current
            # **The instance identity is minted here and nowhere else. beta.36.**
            #
            # One economic campaign may be attempted more than once in a day -- once
            # aborted for a genuine hazard, once afresh -- and those are two
            # different things that happened, with two frozen objectives and two
            # terminals. Everything downstream keys on this rather than on
            # ``campaign_id`` so that "exactly one terminal" and "realised history is
            # immutable" are true of the *attempt*, which is the thing they can
            # actually be true of.
            #
            # Stored, never derived. ``(campaign_id, opened_at)`` is the semantic
            # key, but recomputing it per refresh is how the beta.29 plan ids churned
            # and how the ``revision`` trap in ``admission_key_of`` would have bitten.
            self._campaign_opened_at = now
            self._campaign_instance_id = campaign_instance_identity(current, now)
            self._campaign_run_id = quarter.run_id
            self._campaign_end_utc = quarter.quarter_end
            # **The end Stage A planned, frozen, beside the end we have observed.**
            # Safe to freeze because ``campaign_identity`` is a digest of this very
            # instant: a campaign whose end moves is a different campaign.
            plan = self._plan
            self._campaign_planned_end_utc = (
                None
                if plan is None or plan.campaign_id != current
                else plan.campaign_end
            )
            self._campaign_boundary = (
                CAMPAIGN_BOUNDARY_METER
                if quarter.intent == EXECUTION_INTENT_NET_EXPORT
                else CAMPAIGN_BOUNDARY_BATTERY
            )
            self._campaign_started_at = None
            self._campaign_frozen_target_kwh = None
            self._campaign_opening_target_kwh = self._campaign_objective_kwh(current)
            self._campaign_realized_kwh = 0.0
            self._campaign_quarters_admitted = 0
            self._campaign_measurable = True
            self._campaign_accrued_row = None
            # **The one moment the codebase did not record. beta.42.** The campaign
            # is public from here, so this is where its public lifecycle opens --
            # after the opening target is captured, so ``planned_kwh`` is the figure
            # the campaign actually announced rather than a null.
            self._lifecycle_created(now, quarter)

        if self._campaign_id is None:
            return

        # **The end instant tracks the furthest quarter this campaign reaches**, so
        # a campaign whose later segments are still ahead of us does not look
        # finished the moment its first one ends.
        if (
            quarter is not None
            and quarter.campaign_id == self._campaign_id
            and (
                self._campaign_end_utc is None
                or quarter.quarter_end > self._campaign_end_utc
            )
        ):
            self._campaign_end_utc = quarter.quarter_end

        # **The planned end is filled in late when it has to be, and never moved.**
        # A campaign can open on the refresh a plan is admitted, in which case the
        # plan was already there; it can also open from a row derived before the
        # publication carrying ``campaign_end`` arrived. Filling a ``None`` is new
        # information; overwriting a value would be re-deriving frozen scope.
        if self._campaign_planned_end_utc is None:
            plan = self._plan
            if plan is not None and plan.campaign_id == self._campaign_id:
                self._campaign_planned_end_utc = plan.campaign_end

        # **Read while it is still there.** The objective is re-read on every
        # refresh that still publishes this campaign, and only until the freeze --
        # after which it is never consulted again, so a later publication cannot
        # reach the frozen figure through this door.
        if self._campaign_started_at is None:
            live = self._campaign_objective_kwh(self._campaign_id)
            if live is not None:
                self._campaign_opening_target_kwh = live
        else:
            self._grow_campaign_target()

        if self.activation_confirmed:
            self._note_campaign_started(now)

        still_planned = self._campaign_still_published()
        if quarter is None and not still_planned:
            self._close_campaign(now, stop_reason)
            return

        # **The orphan bound, beta.34.** The pair above is necessary and is not
        # sufficient: a quarter carried from a frozen plan that Stage A has stopped
        # affirming keeps ``quarter`` non-``None`` indefinitely, and the campaign
        # then never reaches a terminal at all -- no verdict, no energy reported,
        # and the next campaign's terminal is the first thing a reader sees. The
        # admitted schedule is rebuilt every quarter, so an hour past the furthest
        # instant this campaign ever claimed is far outside any legitimate carry.
        if self._campaign_end_utc is None:
            return
        grace = timedelta(minutes=CAMPAIGN_ORPHAN_GRACE_MINUTES)
        if now > self._campaign_end_utc + grace:
            self._close_campaign(now, stop_reason or EXECUTION_STOP_WINDOW_ENDED)

    @callback
    def _grow_campaign_target(self) -> None:
        """Raise the frozen target to the campaign's full known objective. beta.43.

        **Monotonic, and one-directional on purpose.** The freeze exists so a
        campaign that promised 2.65 kWh and delivered 1.80 because Stage A changed
        its mind is Partial rather than a retroactively successful 1.80 / 1.80 --
        that property is what ``max`` preserves. What the freeze was never meant to
        do is *cap* a campaign at whatever fraction of itself happened to be
        published at the instant it started.

        The 2026-09-05 capture is the shape it gets wrong. A campaign opened with a
        single published row, froze a 0.25 kWh target, and went on to run three rows
        whose meter objectives summed to 1.50 kWh. It filed ``success`` at
        ``0.233 of 0.25`` -- a true statement about a promise that had stopped being
        the promise, beside 1.206 kWh of recorded export.

        Identity-safe by construction rather than by convention:
        :meth:`_campaign_objective_kwh` matches ``campaign_id`` on both of its
        branches, so another campaign's energy is not reachable from here, and the
        instance guard keeps a fresh attempt from inheriting the last one's growth.
        Growth is read from the live publication only; nothing already recorded is
        rewritten, and the terminal is judged against the target as finally grown.
        """
        if self._campaign_id is None or self._campaign_instance_id is None:
            return
        if self._campaign_started_at is None:
            return
        live = self._campaign_objective_kwh(self._campaign_id)
        if live is None:
            return
        frozen = self._campaign_frozen_target_kwh
        if frozen is None or live > frozen:
            self._campaign_frozen_target_kwh = live

    @callback
    def _campaign_row_records(self, campaign_id: str | None) -> list[dict[str, Any]]:
        """Return the completed-row records naming this campaign. beta.43."""
        if campaign_id is None:
            return []
        return [
            row
            for row in self._completed_quarters
            if row.get("campaign_id") == campaign_id
        ]

    @callback
    def _completion_tolerance_kwh(
        self, target_kwh: float | None, quarters: int
    ) -> float:
        """Return how far short a campaign may land and still be a success.

        **Derived from the plant, then bounded by the promise.** Three quantities,
        each measured rather than chosen:

        * **Actuator quantisation.** Stage B floors the commanded power to the
          0.1 kW helper step, so every quarter can under-deliver by up to
          ``0.1 kW * 0.25 h`` -- and the flooring makes it *systematic*, always
          downward. Twenty-two quarters accumulate 0.55 kWh of it.
        * **Measurement resolution.** The objective is a state-of-charge delta and
          the sensor reports a level. One percent of usable capacity is the worst
          case a pack may report, 0.216 kWh here.
        * **The promise.** The two above scale with duration and would, on a small
          objective, excuse most of a miss. So they are capped at a fraction of
          what was promised -- and that cap is floored at one actuator step, or a
          0.3 kWh campaign would be failed by a single quantisation it could not
          have avoided.

        Worked, on figures from the live installation: a one-quarter Safety Buy
        promising 1.11 kWh tolerates ``min(0.025 + 0.216, max(0.025, 0.056))`` =
        **0.056**, and the observed 0.047 kWh shortfall is a success. A
        twenty-two-quarter campaign promising 18.33 kWh tolerates
        ``min(0.55 + 0.216, max(0.025, 0.917))`` = **0.766**, which is 4.2 % -- and
        the same campaign delivering 12 kWh is Partial, as it should be.

        The flat 0.025 kWh per quarter this replaces is the actuator resolution
        alone. It is a real term and it is still here; it was never the whole of
        the error, and used by itself it called a 4.3 % miss a failure.
        """
        quantisation = CAMPAIGN_SUCCESS_TOLERANCE_PER_QUARTER_KWH * max(1, quarters)
        capacity = self.config.battery_capacity_kwh or 0.0
        resolution = capacity * CAMPAIGN_MEASUREMENT_RESOLUTION_PERCENT / 100.0
        physical = quantisation + resolution
        if target_kwh is None or target_kwh <= 0.0:
            return min(physical, TARGET_TOLERANCE_KWH)
        proportional = max(
            CAMPAIGN_SUCCESS_TOLERANCE_PER_QUARTER_KWH,
            CAMPAIGN_SUCCESS_TOLERANCE_FRACTION * target_kwh,
        )
        return min(physical, proportional)

    @callback
    def _close_campaign(self, now: datetime, stop_reason: str | None) -> None:
        """Latch the open campaign's outcome, from the measurements taken here.

        **The precedence is computed once, in this order, and the order is
        argued:**

        1. an untrustworthy measurement is **Failed** -- the honesty guard, because
           publishing a verdict on a figure we do not believe is worse than
           publishing no verdict;
        2. the objective met within the reporting tolerance is **Success** -- above
           Cancelled deliberately: *the money made outranks the reason the plan
           then changed*, and beta.31 filed a real 0.10 / 0.11 kWh export as
           "Canceled -- Plan Replaced";
        3. a reason in the failed set is **Failed**;
        4. a reason in the cancelled set is **Canceled**;
        5. anything else is **Partial**.

        A campaign that never started produces no terminal at all: nothing
        physical happened, so there is nothing to have finished.
        """
        campaign_id = self._campaign_id
        instance_id = self._campaign_instance_id
        # **The identity is cleared at the *end* of this method, not here. beta.36.**
        #
        # It used to be the second statement, seven lines above the read of
        # ``_campaign_realized_now()`` -- and ``_open_quarter_objective_kwh``
        # compares ``quarter.campaign_id != self._campaign_id``, so with the identity
        # already ``None`` that comparison was always true and the open quarter's
        # term was **structurally always 0.0**, on every terminal ever filed. The
        # comment below promised the opposite. The live ``open_campaign`` figure uses
        # the same helper with the campaign still open and therefore *did* include
        # it, so the two published figures disagreed by exactly the closing quarter.
        if campaign_id is None or self._campaign_started_at is None:
            # Nothing physical happened, so there is nothing to have finished -- and
            # nothing to latch either, which is what leaves a never-started campaign
            # free to be attempted properly later.
            #
            # **The public lifecycle still closes, and through a different latch.
            # beta.42.** Every ``created`` gets exactly one ``removed`` or the
            # guarantee this surface is built on is not a guarantee. Routing that
            # through the execution finality above would block the legitimate later
            # attempt this early return exists to preserve, so the telemetry latch is
            # separate, keyed on the instance, and touches neither
            # ``_final_campaigns`` nor ``_closed_instances``. ``created -> removed``
            # with no ``stopped`` is a legal sequence: nothing had begun to stop.
            if campaign_id is not None:
                self._lifecycle_removed(
                    now,
                    result=OUTCOME_NOT_EXECUTED,
                    completion_reason=stop_reason,
                    terminal=None,
                )
            self._campaign_id = None
            self._campaign_instance_id = None
            self._campaign_opened_at = None
            self._campaign_planned_end_utc = None
            return
        if self._instance_closed(instance_id):
            # **Exactly one terminal per attempt, enforced rather than hoped for.**
            self._campaign_id = None
            self._campaign_instance_id = None
            return
        # Read once, and before anything below can clear the identity. beta.43.
        campaign_rows = self._campaign_row_records(campaign_id)
        # **Plus the row that caused this close, which is deliberately not recorded
        # yet.** beta.35's stop-before-record rule is preserved -- a record written
        # before a stop that then failed would claim a finished quarter while the
        # inverter was still moving energy -- so on the ``_async_end_quarter`` path
        # the closing row reaches ``_completed_quarters`` *after* this terminal. It
        # has, however, already been accrued, and ``_campaign_accrued_row`` names it.
        # Counting recorded rows alone would make every such terminal short by one,
        # which is a new defect rather than a fix for the old one.
        recorded_starts = {row.get("quarter_start") for row in campaign_rows}
        pending_row = (
            self._campaign_accrued_row is not None
            and self._campaign_accrued_row.isoformat() not in recorded_starts
        )
        target_kwh = self._campaign_frozen_target_kwh
        # **Including whatever the open quarter had moved.** A campaign cut short
        # mid-quarter delivered that energy, and dropping it would report a
        # shortfall the plant did not have. True since beta.36; see above.
        realized = self._campaign_realized_now()
        quarters = self._campaign_quarters_admitted
        tolerance = self._completion_tolerance_kwh(target_kwh, quarters)
        # **Two questions, and beta.33 answered them with one word.** Whether the
        # measurement can be trusted is about the plant; whether an objective was
        # ever published is about the plan. A campaign that delivered 1.063 kWh
        # against a target nobody recorded was filed *Failed -- Measurement
        # Unavailable* on the live installation, and the measurement was fine.
        measurable = self._campaign_measurable
        target_known = target_kwh is not None
        shortfall = None if target_kwh is None else target_kwh - realized
        if not measurable:
            outcome = OUTCOME_FAILED
        elif not target_known:
            # Energy moved and was measured; there is simply nothing to judge it
            # against. Partial states exactly that, and the reason names why.
            outcome = OUTCOME_PARTIAL
            stop_reason = stop_reason or CAMPAIGN_TARGET_UNAVAILABLE
        elif shortfall is not None and shortfall <= tolerance:
            outcome = OUTCOME_SUCCESS
        elif stop_reason in EXECUTION_FAILED_STOP_REASONS:
            outcome = OUTCOME_FAILED
        elif stop_reason in EXECUTION_COMPLETION_STOP_REASONS:
            # **The ordinary way a campaign ends.** ``window_ended`` fires when the
            # last planned quarter closes, on every campaign that runs to
            # completion -- so reading it as a cancellation called every campaign
            # that missed its tolerance *Canceled*, including a 4.3 % miss on a
            # charge that physically worked.
            outcome = OUTCOME_PARTIAL
        elif stop_reason:
            outcome = OUTCOME_CANCELED
        else:
            outcome = OUTCOME_PARTIAL
        self._closed_campaign = {
            "campaign_id": campaign_id,
            # **Which attempt this was.** Two terminals for one economic campaign is
            # legitimate when a hazard aborted the first; two for one *attempt* never
            # is, and these two fields are how a reader tells those apart.
            "campaign_instance_id": instance_id,
            "opened_at": (
                None
                if self._campaign_opened_at is None
                else self._campaign_opened_at.isoformat()
            ),
            "run_id": self._campaign_run_id,
            "window_end": (
                None
                if self._campaign_end_utc is None
                else self._campaign_end_utc.isoformat()
            ),
            # The end Stage A *planned*, beside the furthest row actually observed.
            # Conflating them is how a thirty-three-row campaign reported a
            # ``window_end`` three rows in.
            "planned_end": (
                None
                if self._campaign_planned_end_utc is None
                else self._campaign_planned_end_utc.isoformat()
            ),
            "started": True,
            "started_at": self._campaign_started_at.isoformat(),
            "ended_at": now.isoformat(),
            "objective_boundary": self._campaign_boundary,
            "objective_target_kwh": (
                None if target_kwh is None else round(target_kwh, 3)
            ),
            "objective_realized_kwh": round(realized, 3),
            "objective_measurable": measurable,
            # **Signed, and null below the actuator quantum.** A 13 % shortfall on
            # a 0.11 kWh objective and a 13 % shortfall on a 5 kWh one are
            # different problems, and a percentage of a figure smaller than one
            # actuator step is noise wearing a decimal point. beta.31 published
            # 140 % on a 0.01 kWh target.
            "objective_tracking_error_kwh": (
                None if shortfall is None else round(-shortfall, 4)
            ),
            "objective_tracking_error_fraction": (
                None
                if target_kwh is None or target_kwh < MIN_EXECUTABLE_QUARTER_KWH
                else round(-shortfall / target_kwh, 4)
            ),
            "quarters_admitted": quarters,
            # **A real count since beta.43, and no longer the same local.**
            # ``quarters_admitted`` counts *accruals*; this counts the rows that
            # actually exist -- recorded, plus the one accrued and awaiting its
            # record. Publishing one number under two names read as corroboration
            # and was not: on 2026-09-05 both said 2 while three completed rows
            # carried the campaign id. They are different questions, and their
            # disagreement is now the diagnostic rather than something a reader
            # has to already know.
            "rows_completed": len(campaign_rows) + (1 if pending_row else 0),
            "objective_row_count": self._campaign_objective_rows,
            # **What the total was summed from. beta.43.** A terminal that
            # disagrees with its own rows is now self-evidently wrong from one
            # payload, instead of needing the physical ring to catch it.
            "objective_rows_realised": [
                {
                    "quarter_start": row.get("quarter_start"),
                    "objective_kwh": (
                        row.get("realized_grid_kwh")
                        if row.get("objective_boundary") == CAMPAIGN_BOUNDARY_METER
                        else row.get("objective_battery_kwh")
                    ),
                }
                for row in campaign_rows
            ],
            "accrued_row": (
                None
                if self._campaign_accrued_row is None
                else self._campaign_accrued_row.isoformat()
            ),
            "success_tolerance_kwh": round(tolerance, 4),
            "outcome": outcome,
            "reason": stop_reason,
            # **A campaign terminal speaks the campaign vocabulary. beta.36.**
            # ``REASON_VOCABULARY_CAMPAIGN_END`` has existed since beta.32 with no
            # producer, while this record was tagged ``run_stop`` -- so the
            # 2026-08-30 terminal published a *quarter* reason under the *run*
            # vocabulary on a *campaign* record, three layers disagreeing at once.
            "reason_vocabulary": REASON_VOCABULARY_CAMPAIGN_END,
            "rule": (
                "the outcome is decided here, where the energy was measured, and "
                "the reason is published beside it rather than standing in for it. "
                "the target is frozen at the first confirmed activation and may "
                "never shrink, and since beta.43 it may grow to the campaign's own "
                "full published objective -- a campaign is judged on what it "
                "promised, not on the fraction of itself that happened to be "
                "published when it started; the realised figure accumulates across "
                "segments and across serve_load gaps and is reset only when the "
                "campaign closes"
            ),
        }
        # **``superseded`` rather than ``partial`` for a started campaign the plan
        # displaced. beta.42.** The shortfall is not the plant's: the campaign was
        # overtaken by a newer authoritative plan, not missed. Applied only where the
        # judgement above did not already reach something stronger, so an
        # unmeasurable campaign is still ``failed`` and a met objective is still
        # ``success`` -- the precedence is unchanged, this adds one leaf to it.
        public_result = outcome
        if outcome == OUTCOME_PARTIAL and stop_reason == EXECUTION_STOP_PLAN_REPLACED:
            public_result = OUTCOME_SUPERSEDED
        self._lifecycle_removed(
            now,
            result=public_result,
            completion_reason=stop_reason,
            terminal=self._closed_campaign,
        )
        if instance_id is not None:
            self._closed_instances.append(instance_id)
            del self._closed_instances[:-MAX_ABORTED_CAMPAIGNS_REMEMBERED]
        # **The asymmetry, in one place.** A campaign that *finished* is done, and
        # Stage A continuing to publish it is not a reason to run it again. A
        # campaign that was *aborted* may be attempted afresh, so it is deliberately
        # not latched here -- what is latched for an abort is the admission, in
        # ``_abandon_execution``, which kills the attempt and not the intention.
        if stop_reason in EXECUTION_COMPLETION_STOP_REASONS:
            self._remember_final_campaign(campaign_id)
        self._campaign_id = None
        self._campaign_instance_id = None
        self._campaign_opened_at = None
        self._campaign_run_id = None
        self._campaign_end_utc = None
        self._campaign_planned_end_utc = None
        self._campaign_boundary = None
        self._campaign_started_at = None
        self._campaign_frozen_target_kwh = None
        self._campaign_opening_target_kwh = None
        self._campaign_realized_kwh = 0.0
        self._campaign_quarters_admitted = 0
        self._campaign_objective_rows = 0
        self._campaign_accrued_row = None
        self._campaign_measurable = True

    @callback
    def _duration_to_command(self, command: Any, snapshot: Any) -> int:
        """Return the dead-man duration this refresh will actually write.

        **The gate has to guard a real value, and the two paths write different
        ones.** A live Dispatch arm or re-arm writes ``deadman_minutes()`` -- the
        alternating twenty/twenty-five that makes the vendor automation fire at all
        -- while the advisory helper-family command carries its own figure. Feeding
        the safety gate the second while the first is what reaches the inverter
        would leave ``INHIBIT_DURATION_OUT_OF_RANGE`` guarding a number nobody
        sends.

        That mattered more than it looks: beta.32's audit read the advisory figure
        as though it were the Dispatch dead-man and concluded, wrongly, that a
        configured horizon could break ownership. The two are separate, and this is
        where the separation becomes explicit.
        """
        if self._executing_intent() in CONTROL_LIVE_DISPATCH_INTENTS:
            return deadman_minutes(
                None if snapshot is None else snapshot.dispatch_duration_minutes
            )
        return 0 if command is None else command.duration_minutes

    @callback
    def _live_kw(self) -> tuple[float, float, float] | None:
        """Return ``(house_kw, pv_kw, grid_kw)`` now, or ``None`` if unusable.

        **The unit and sign boundary, crossed exactly once.** ``PowerFlows`` is in
        watts with import and export as separate non-negative fields, because the
        sources disagree about signs and that disagreement is resolved before a
        ``PowerFlows`` exists. The controller works in signed kilowatts, where
        positive grid means import -- so the conversion happens here and nowhere
        else.

        ``None`` when house load or the meter cannot be read. **No fallback figure
        is invented**: production is allowed to be absent and treated as zero,
        because a missing photovoltaic forecast is a real configuration rather
        than a fault, but an unknown house load or meter reading means the
        controller does not know what it is correcting.
        """
        flows = self.read_flows()
        if flows.house_load_w is None:
            return None
        if flows.grid_import_w is None and flows.grid_export_w is None:
            return None
        grid_w = (flows.grid_import_w or 0.0) - (flows.grid_export_w or 0.0)
        return (
            flows.house_load_w / 1000.0,
            (flows.pv_w or 0.0) / 1000.0,
            grid_w / 1000.0,
        )

    @callback
    def _blocking_conflicts(self, snapshot: Any) -> tuple[str, ...]:
        """Return the conflicting families that must prevent an arm.

        **All six the vendor automation would silently switch off**, minus the one
        that is ours. ``AlphaESS Dispatch`` disables force charging, force
        discharging, force import, force export, excess export and peak shaving
        before arming and cancels their timers -- so arming over a feature the user
        selected destroys it without asking.

        The charge family is excluded while a dispatch of ours is running, because
        then it is not somebody else's feature; the distinction is ownership, never
        the entity.
        """
        if snapshot is None:
            return ()
        return tuple(snapshot.conflicting_active)

    @callback
    def _dispatch_setpoint(self, now: datetime) -> Any:
        """Return this instant's signed Dispatch setpoint, or ``None``.

        **The quarter is the execution envelope, and it is asked first.** This is
        R3: a run publishes ``desired_grid_kw`` as its *first* interval's rate while
        its window spans every interval it covers, so a multi-quarter run executed
        against the run-level figure follows quarter one's target for the whole run.
        The quarter rows carry each interval's own figures, so asking the quarter
        removes the defect rather than compensating for it.

        The run-level path is kept as the fallback and is **unchanged**, which is
        what makes this safe to ship: a publication or a persisted record written
        before beta.27 carries no ``quarter_schedule``, admits no quarter, and
        executes exactly the beta.26 arithmetic that was proven on the hardware.
        """
        quarter = self._quarter
        if quarter is not None:
            progress = self._quarter_progress(now)
            live = self._live_kw()
            if progress is not None and live is not None:
                house_kw, pv_kw, _grid_kw = live
                decision = decide_for_intent(
                    intent=quarter.intent,
                    progress=progress,
                    house_load_kw=house_kw,
                    pv_kw=pv_kw,
                    limits=self._charge_limits(
                        None if self._carried is None else self._carried.target, now
                    ),
                    last_applied_kw=self._applied_setpoint_kw,
                    **self._export_limits(quarter, now),
                )
                if decision is not None:
                    self._note_quarter_clamp(decision.limited_by)
                    return decision
        return self._setpoint_for(
            None if self._carried is None else self._carried.target, now
        )

    @callback
    def _export_limits(self, quarter: CarriedQuarter, now: datetime) -> dict[str, Any]:
        """Return the export-only bounds, empty for a charge.

        Separate from :meth:`_charge_limits` because the two directions are bounded
        by different physical quantities, and folding them into one structure would
        mean every slot was ``None`` for one of them. Three of the four are
        structurally absent for a charge: a charge never approaches the floor, never
        breaches the reserve and never exports.
        """
        if quarter.intent != EXECUTION_INTENT_NET_EXPORT:
            return {}
        plan = self.battery_plan
        max_discharge_kw = None
        reserve_headroom_kwh = None
        if plan is not None and plan.state is not None:
            max_discharge_kw = plan.state.limits.max_discharge_kw
            # **The reserve is absolute: no export price unlocks a violation.** The
            # bound is the energy above the configured floor, expressed as energy so
            # the conversion to a rate happens once, in :mod:`.dispatch`, against
            # the quarter's own remaining time.
            floor_kwh = plan.state.limits.energy_for_soc(
                plan.reserve.configured_min_soc_percent
            )
            reserve_headroom_kwh = max(0.0, plan.state.energy_kwh - floor_kwh)
        return {
            "max_discharge_kw": max_discharge_kw,
            "reserve_headroom_kwh": reserve_headroom_kwh,
            # **No configured site export limit exists in this integration**, so
            # this clamp is genuinely unconstrained rather than defaulted to a
            # number nobody chose. Stated here so a reader does not assume the
            # slot is enforcing something.
            "grid_export_limit_kw": None,
        }

    @callback
    def _setpoint_for(self, target: Any, now: datetime) -> Any:
        """Return the signed Dispatch setpoint for this instant, or ``None``.

        **The one place the physical setpoint is decided**, shared by the quarter
        refresh and the sixty-second tick so the two cannot disagree about what
        the same world means.

        ``desired_grid_kw`` is Stage A's, frozen for the quarter. Nothing here
        reads a price, ranks a window or moves a target: the arithmetic lives in
        :mod:`.dispatch` and this only assembles its inputs.
        """
        if target is None or target.desired_grid_kw is None:
            return None
        live = self._live_kw()
        if live is None:
            return None
        house_kw, pv_kw, _grid_kw = live
        return decide_setpoint(
            desired_grid_kw=target.desired_grid_kw,
            house_load_kw=house_kw,
            pv_kw=pv_kw,
            limits=self._charge_limits(target, now),
            last_applied_kw=self._applied_setpoint_kw,
            charge_only=True,
        )

    @callback
    def _charge_limits(self, target: Any, now: datetime) -> ChargeLimits:
        """Return the bounds on a charge, as typed components.

        **Assembled from the typed parts, never from ``demand_for``'s composite
        ``required_kw``.** That figure already has the headroom ceiling and the
        grid cap folded into it, so feeding it in would apply both a second time.

        Three of the eight slots are structurally non-binding for a charge and are
        left ``None`` rather than filled with a number that means nothing: a charge
        never approaches the minimum state of charge, never breaches the dynamic
        reserve, and never exports. Saying ``None`` is saying "unconstrained",
        which is true; inventing a bound would be a clamp nobody could interpret.
        """
        decision = self._stage_b_decision
        demand = None if decision is None else decision.demand
        plan = self.battery_plan

        inverter_kw = None
        if plan is not None and plan.state is not None:
            inverter_kw = plan.state.limits.max_charge_kw

        # **Clamp four: the grid authorisation, not the battery remainder.**
        # ``battery_target_kwh`` is production plus grid, a forecast composite, so
        # bounding a grid-power controller with it stops absorption the moment
        # production runs ahead of forecast and pushes free energy out to the meter.
        grid_kw = None
        if demand is not None and demand.grid_cap_kwh is not None:
            remaining_kwh = max(0.0, demand.grid_cap_kwh - demand.grid_charged_kwh)
            # And bounded again by the downward revision, whichever binds harder.
            revised, _cap = remaining_authorised_kwh(
                now=now,
                frozen_remaining_kwh=remaining_kwh,
                forward=self._forward,
            )
            # **An energy ceiling, expressed at the row's own clock -- not a pace
            # across the whole run. beta.40, and this one cost 5.076 kWh.**
            #
            # Dividing the run's remaining budget by the *run's* remaining time
            # makes it a flat average rate, and that rate then caps the battery
            # through ``battery_cap_kw`` in every individual row. On the
            # 2026-09-03 campaign it throttled exactly the three rows Stage A had
            # deliberately sized at full inverter power:
            #
            #     row 11:45  needed 10.00 kW, row authorised 9.08, pace 2.73
            #                -> observed mean 3.40 kW, delivered 0.795 of 2.50
            #     row 12:15  needed 10.00 kW, row authorised 9.28, pace 2.99
            #                -> observed mean 3.46 kW, delivered 0.808 of 2.50
            #     row 13:00  needed 10.00 kW, row authorised 9.84, pace 3.78
            #                -> observed mean 4.38 kW, delivered 1.021 of 2.50
            #
            # each predicted from ``pace + measured surplus`` to within 0.5-2.8 %.
            # The campaign realised 5.939 of 13.100 kWh and left **5.076 kWh of
            # authorised grid purchase unspent** -- the budget was never the
            # binding constraint, its pace was.
            #
            # **It is the beta.36 defect one level up.** beta.36 stopped the
            # *row's* grid authorisation capping battery power; the *run's*
            # remaining budget was still doing it, as an average. A budget is an
            # energy, and the honest rate it permits is the rate that would spend
            # it inside the row now executing -- which is exactly the shape the
            # other two remainders already have, and Stage A's own per-row
            # ``grid_authorised_kwh`` still paces each row through
            # ``progress.grid_rate_kw``.
            #
            # The total stays bounded exactly: across the open row this permits at
            # most ``revised`` kWh, and ``revised`` is recomputed every tick from
            # measured ``grid_charged_kwh``. It is emphatically **not** catch-up --
            # energy missed in an expired quarter never becomes available in a
            # later one, because each row is bounded by its own frozen
            # authorisation first and that figure never moves.
            minutes = max(1.0, demand.remaining_minutes)
            row = self._quarter
            if row is not None:
                minutes = min(
                    minutes,
                    max(
                        CONTROL_TICK_ENERGY_HORIZON_SECONDS / 60.0,
                        row.seconds_remaining(now) / 60.0,
                    ),
                )
            grid_kw = revised / (minutes / 60.0)

        return ChargeLimits(
            inverter_kw=inverter_kw,
            remaining_grid_kw=grid_kw,
            headroom_kw=None if demand is None else demand.ceiling_kw,
        )

    async def _async_send_locked(
        self, steps: tuple[Any, ...], *, now: datetime, verify: str | None
    ) -> bool:
        """Send steps with the lock already held, and verify the readback.

        Returns whether the write both landed and read back. **The verification
        stays inside the locked section**: a check that ran after the lock was
        released would be reading a world another sequence may already have moved.
        """
        if not steps:
            return True
        try:
            await async_execute(self.hass, steps, intent=self._executing_intent())
        except (ControlExecutionUnavailable, ControlActionNotPermitted):
            _LOGGER.warning("A Dispatch write was refused at the send site")
            return False
        except Exception:
            _LOGGER.exception("A Dispatch write could not be sent")
            return False
        self._last_control_write = now
        if verify is None:
            return True
        return self._staged_write_landed(verify)

    @callback
    def _abandon_execution(self, now: datetime, reason: str | None) -> None:
        """Tear the whole authority state down at once. **The only place.**

        **beta.35, and the 20:00-20:24 hardware trace is the argument.** beta.34
        had three teardowns -- the refresh reset, the tick stop and the emergency
        stop -- each maintaining its own hand-written list of fields to clear. They
        disagreed, and the refresh one was the shortest: it released the ownership
        record, the row and the dead-man observation, but left ``self._plan``
        standing. Its own comment argued the rule correctly for the row --

            And the quarter goes with it: its authority came from a dispatch that
            is no longer running.

        -- and that reasoning was simply never applied to the schedule the row came
        from. So after the 20:00 reset the frozen plan stayed authoritative: the
        controller went on narrating quarter 2 while the dispatch was off, roughly
        2.28 kWh of planned export was lost in silence, and at 20:15 the surviving
        schedule advanced to quarter 3 and **re-armed the inverter**. The lifecycle
        said terminated; the schedule said otherwise; the hardware believed the
        schedule.

        There are exactly two legal states in beta.35 -- authoritative, or gone --
        and this method is what makes the second one true. Everything named in
        :data:`EXECUTION_ABORT_IS_TOTAL` is cleared here, together, and nowhere
        else, so the three paths cannot drift apart again.

        The campaign is closed **here** rather than left to the next refresh, and
        the *admission* is remembered: an abandoned schedule may not re-arm.
        Realised history stays exactly as measured -- ``_close_campaign`` reads it
        before anything is reset, so a campaign that moved energy reports the energy
        it moved.

        **beta.36 changes what is remembered, and only that.** beta.35 latched the
        *campaign identity*, which is a digest of the campaign's end instant and is
        therefore byte-identical on every republication of a live campaign. So an
        abort -- or, worse, a completion routed here by mistake -- forbade the
        campaign from ever being admitted again in that session. The thing an abort
        must kill is the physical attempt that went wrong, not the economic
        intention; those are different objects and now have different keys.
        """
        plan = self._plan
        carried = self._carried
        # **A campaign Stage A is still publishing is not over. beta.36.**
        #
        # The refresh's own reset is the third caller here, and it fires for endings
        # that are not aborts at all: a run reaching its ``window_end``, a withdrawal
        # standing once the plan's authority is genuinely spent. Both leave the
        # *dispatch* finished and the *campaign* running -- which is the ordinary
        # shape of a campaign split by a ``serve_load`` interval into two published
        # runs, and the shape the 2026-08-30 campaign actually had.
        #
        # Closing it here anyway files a terminal mid-campaign, and the next refresh
        # then opens a second instance of the same economic campaign for the second
        # run: two terminals, two frozen objectives, and the realised energy of the
        # first attributed to neither. ``_note_campaign_progress`` already owns the
        # question -- ``still_planned`` over the current publications -- and it is
        # the single source of truth for it, so this defers to it rather than
        # answering it a second way.
        #
        # An abort still closes immediately and unconditionally. Something happened
        # *to* the dispatch; the campaign has to be told, and a terminal filed late
        # after a hazard is a terminal that may never be filed at all.
        aborting = reason is None or reason in EXECUTION_ABORT_STOP_REASONS
        if aborting or not self._campaign_still_published():
            # First, because it reads the realised accumulators the rest of this
            # method is about to clear, and because a terminal filed twice is worse
            # than a terminal filed late.
            self._close_campaign(now, reason)
        # **The latch is written only for a genuine abort, and ``None`` counts.** An
        # abort with no reason is the ``quarter_progress_unknown`` shape and is the
        # most dangerous case, so it fails closed. With the scoped stop in place this
        # method is reached only by aborts anyway -- the test is written down so a
        # future caller that reuses it for a completion cannot silently latch.
        if aborting:
            for key in (
                None if plan is None else plan.admission_key,
                None if carried is None else carried.admission_key,
            ):
                if key is not None:
                    self._remember_abandoned_admission(key)
        self._clear_execution_record()
        self._sustained_deadline = None
        self._sustained_run_id = None
        self._carried = None
        self._quarter = None
        self._plan = None
        self._reset_quarter_progress(None)
        self._quarter_progress_unknown = False
        self._forward = None

    @callback
    def _campaign_still_published(self) -> bool:
        """Return whether Stage A is still publishing the open campaign.

        The one question, asked in one place. ``_note_campaign_progress`` reads the
        same predicate to decide when to close a campaign nothing names any more, so
        the teardown and the lifecycle cannot answer it differently -- which they did
        through beta.35, and the teardown's answer was always "it is over".
        """
        if self._campaign_id is None:
            return False
        return any(
            target.get("campaign_id") == self._campaign_id
            for target in self.execution_targets
        )

    @callback
    def _remember_abandoned_admission(self, key: str) -> None:
        """Record that this *admission* may never execute again.

        Session-local and bounded, like every other latch here. A restart
        legitimately re-evaluates from the persisted claim and the physical state
        rather than from a memory of a decision taken before the reboot -- and a
        restart that finds a live dispatch it cannot attribute still writes
        nothing, which is the older and stronger guarantee.
        """
        if key in self._abandoned_admissions:
            return
        self._abandoned_admissions.append(key)
        del self._abandoned_admissions[:-MAX_ABORTED_CAMPAIGNS_REMEMBERED]

    @callback
    def _admission_abandoned(self, plan: Any) -> bool:
        """Return whether this admitted plan's attempt has been torn down."""
        if plan is None:
            return False
        return plan.admission_key in self._abandoned_admissions

    @callback
    def _remember_final_campaign(self, campaign_id: str) -> None:
        """Record that this economic campaign genuinely finished.

        The counterpart of :meth:`_remember_abandoned_admission`, and the asymmetry
        between them is the approved semantics: an aborted *attempt* may be retried,
        a *finished* campaign may not be re-run because Stage A keeps publishing it.
        """
        if campaign_id in self._final_campaigns:
            return
        self._final_campaigns.append(campaign_id)
        del self._final_campaigns[:-MAX_ABORTED_CAMPAIGNS_REMEMBERED]

    @callback
    def _campaign_is_final(self, campaign_id: str | None) -> bool:
        """Return whether this economic campaign has already run to its end."""
        return campaign_id is not None and campaign_id in self._final_campaigns

    @callback
    def _instance_closed(self, instance_id: str | None) -> bool:
        """Return whether this campaign instance has already filed its terminal."""
        return instance_id is not None and instance_id in self._closed_instances

    async def _async_stop_owned_run(
        self, now: datetime, snapshot: Any, reason: str
    ) -> None:
        """Stop a dispatch we own and tear everything down. **Compatibility name.**

        Kept so no existing caller changes meaning by accident, and it defaults to
        the widest scope for the same reason -- the ``authorize``/``authorize_start``
        precedent. New callers should say which scope they mean.
        """
        await self._async_stop_dispatch(now, snapshot, reason, scope=STOP_SCOPE_ABORT)

    async def _async_stop_dispatch(
        self, now: datetime, snapshot: Any, reason: str | None, *, scope: str
    ) -> None:
        """Stop a dispatch we own, at the named scope, with the lock held.

        Enable **off first**, then verified inactive, and only then the resting
        values and the marker. The cleanup is withheld on an unverified stop for a
        concrete reason: writing the duration restarts the vendor timer, so tidying
        up a dispatch that did not actually stop would extend the run being ended.
        **The physical sequence is byte-identical in every scope**; what differs is
        what survives it.

        **beta.36, and this method existing is the release.** Through beta.35 there
        was one teardown, and every stop reached it: a row meeting its own target, a
        row running out of time, a run hitting an authorised ceiling, and a genuine
        hazard all cleared the same nine fields and latched the same campaign. On
        2026-08-30 a quarter *succeeding* therefore destroyed a five-and-a-half-hour
        charge campaign and blacklisted it for the session. A row ending is not a run
        ending and neither is a campaign ending.

        * ``STOP_SCOPE_ROW`` -- this row is done and a later executable row remains.
          The frozen plan and the campaign instance survive; the dispatch stops and
          the next boundary arms again. This is also what a ``serve_load`` gap in the
          middle of a plan needs, which beta.35 lost every row after.
        * ``STOP_SCOPE_CAMPAIGN`` -- the campaign genuinely finished. The lifecycle
          closes honestly and the plan is cleared, but **nothing is latched as
          abandoned**: it succeeded.
        * ``STOP_SCOPE_ABORT`` -- something happened *to* the dispatch. Total
          teardown, this admission permanently dead. See :meth:`_abandon_execution`.
        """
        if scope not in STOP_SCOPES:  # pragma: no cover - programming error
            raise ValueError(f"unknown stop scope: {scope}")
        landed = await self._async_send_locked(
            plan_dispatch_stop(), now=now, verify=EXECUTION_VERIFY_DISPATCH_INACTIVE
        )
        if not landed:
            # Every piece of evidence is kept, so the next refresh reads the same
            # state and tries again. Clearing it would drop to ``unproven``, and an
            # unproven dispatch is never touched -- the run would latch on.
            _LOGGER.warning(
                "A Dispatch stop for %s could not be verified; ownership evidence "
                "has been kept so the next refresh can retry",
                reason,
            )
            return
        # **Two transitions, because they are two verified events. beta.39.**
        # The stop is verified inactive by the send above; the cleanup is what
        # returns the resting values and releases the marker. This method already
        # made the distinction -- it withholds the cleanup on an unverified stop
        # precisely because the two are not the same thing -- and then published
        # one word for both. ``cleanup_complete`` was a constant with no writer.
        self._note_lifecycle(LIFECYCLE_STOPPED, now)
        await self._async_send_locked(plan_dispatch_cleanup(), now=now, verify=None)
        self._note_lifecycle(LIFECYCLE_CLEANUP_COMPLETE, now)

        if scope == STOP_SCOPE_ABORT:
            # One teardown, and this is the whole of it.
            self._abandon_execution(now, reason)
        else:
            # **Dispatch-scoped, and true of both surviving scopes: the dispatch has
            # stopped, so everything that was evidence *of that dispatch* goes.** The
            # frozen schedule is not evidence of a dispatch; it is Stage A's decision,
            # and it outlives any one arm of it.
            self._note_release_receipt(snapshot, now)
            self._clear_execution_record()
            self._sustained_deadline = None
            self._sustained_run_id = None
            self._carried = None
            if scope == STOP_SCOPE_CAMPAIGN:
                # **Published before the terminal, and only at campaign scope.**
                # ``STOP_SCOPE_ROW`` reaches the shared ``_note_lifecycle`` calls
                # above and deliberately not this one: that scope means the row is
                # done and a later executable row remains, so the instance survives
                # and re-arms at the next boundary. A public ``stopped`` there would
                # fire at every row boundary and every ``serve_load`` gap.
                self._lifecycle_stopped(now, reason)
                self._close_campaign(now, reason)
                self._plan = None
                self._forward = None
                self._quarter_progress_unknown = False
            # **After the terminal, not before it. beta.43.**
            #
            # These two lines used to sit above the campaign branch, and between them
            # they made two of ``_close_campaign``'s inputs unreadable:
            # ``self._quarter``
            # is what ``_open_quarter_objective_kwh`` needs to include the row in
            # flight, and ``_reset_quarter_progress`` clears ``_campaign_accrued_row``
            # -- the exactly-once latch that keeps the accrual above from being
            # counted twice through that same term. Moving them here is what makes
            # the comment at ``_close_campaign``'s ``realized =`` true for the first
            # time, and it is required by the accrual in ``_async_end_row``.
            self._quarter = None
            self._reset_quarter_progress(None)
        self._applied_setpoint_kw = None
        self._coherence = None
        self._emergency_attempts = 0
        if reason is not None:
            self._last_tick_reason = reason

    async def _async_emergency_self_stop(self, now: datetime, snapshot: Any) -> None:
        """Send the one write the emergency authority grants, and nothing else.

        **Not ownership, and the state it belongs to is never called owned.** The
        marker has gone, so ownership is ``degraded``; what survives is proof that
        Alpha EMS caused this dispatch, and that proof authorises exactly
        ``Dispatch enable -> OFF``.

        Everything else stops immediately: no power, cutoff, duration, mode or
        photovoltaic write, and no dead-man re-arm. Bounded to three attempts, one
        per physical tick, after which the device dead-man finishes the job.
        """
        steps = plan_dispatch_stop()
        decision = authorize_emergency_self_stop(
            authorized=emergency_self_stop_authorized(
                dispatch_active=bool(snapshot.dispatch_active),
                marker_present_and_on=bool(snapshot.owner_marker),
                record_matches_run=self._evidence_for(snapshot, now).record_matches,
                readback_compatible=self._evidence_for(
                    snapshot, now
                ).readback_compatible,
                contradicted=False,
            ),
            steps=tuple(step.entity_id for step in steps),
            attempts_made=self._emergency_attempts,
        )
        if not decision.authorized:
            return
        self._emergency_attempts += 1
        landed = await self._async_send_locked(
            steps, now=now, verify=EXECUTION_VERIFY_DISPATCH_INACTIVE
        )
        if not landed:
            return
        # The third abort path, converging on the same teardown as the other two.
        self._abandon_execution(now, EXECUTION_STOP_MARKER_LOST)
        self._applied_setpoint_kw = None
        self._last_tick_reason = EXECUTION_STOP_MARKER_LOST

    @callback
    def _note_tick(self, now: datetime, reason: str, *, wrote: bool = False) -> None:
        """Record what this physical tick did, and why. **Once, at its end.**

        The reason is usually a refusal, and then deliberately **not** a clamp
        reason: nothing was calculated, so naming a clamp would invent an
        explanation for a decision that was never made.

        The typed outcome carries its own cadence, which is the beta.26 diagnostics
        fault fixed by shape. A bare mutable string written here was published
        beside figures computed during the *quarter refresh*, so a stale
        ``no_owned_run`` sat next to a freshly successful write with nothing saying
        the two described different events. A record that names its own cadence
        cannot do that.
        """
        self._last_tick_reason = reason
        self._tick_outcome = TickOutcome(
            cadence=CADENCE_PHYSICAL_TICK, reason=reason, wrote=wrote, at=now
        )
        # **The refusal goes in the ring with its quarter context.** It is the entry
        # a reader most needs explained -- "nothing was written" is only useful
        # beside what was being aimed at and how much of it was left.
        entry = {"controller_refresh_at": now.isoformat(), "update_reason": reason}
        entry.update(self._quarter_ring_fields(now))
        self._physical_decisions.append(entry)

    @callback
    def _record_physical_decision(
        self, now: datetime, decision: Any, coherence: ControlCoherence
    ) -> None:
        """Append one physical decision to the bounded ring."""
        entry = {"controller_refresh_at": now.isoformat()}
        entry.update(decision.as_dict())
        live = self._live_kw()
        entry["actual_grid_kw"] = None if live is None else round(live[2], 3)
        entry["coherence_state"] = coherence.state
        entry["dispatch_power_deadband_kw"] = DISPATCH_POWER_DEADBAND_KW
        # **Enough to reconstruct the quarter, not just the instant.** Diagnostics
        # are almost never captured at the moment production moved, so a download
        # taken an hour later has to be able to answer "what was this tick aiming
        # at, under whose authority, and how much was left" without the reader
        # having to correlate three other blocks by timestamp.
        entry.update(self._quarter_ring_fields(now))
        self._physical_decisions.append(entry)

    @callback
    def _update_coherence(self, now: datetime) -> ControlCoherence:
        """Advance the control-grade coherence state by one physical tick."""
        available = self._live_kw() is not None
        self._coherence = control_coherence(
            previous=self._coherence,
            now=now,
            coherence=self._source_coherence(),
            sources_available=available,
        )
        return self._coherence

    @callback
    def _ownership_now(self, snapshot: Any, now: datetime) -> str:
        """Return the ownership state for a snapshot, on the Dispatch surface."""
        return ownership_of(self._evidence_for(snapshot, now))

    @callback
    def _evidence_for(
        self, snapshot: Any, now: datetime, *, run_id: str | None = _UNSET_RUN
    ) -> OwnershipEvidence:
        """Return the ownership evidence, including the signed readback.

        **The signed readback is the field that must never be forgotten.** On the
        Dispatch surface direction is a number, so evidence without it cannot tell
        an owned charge from somebody else's discharge -- and a second copy of this
        construction is how it came to be omitted once already.

        ``run_id`` defaults to the record's own run so the physical tick and the
        Off path agree; the Stage-B path passes the *carried* run, because there a
        record naming a different run is contradictory rather than merely old.
        """
        # **The expected sign comes from the executing intent**, because beta.27
        # executes two directions: a readback check with a hardcoded sign would call
        # an owned export somebody else's discharge. An unknown intent yields
        # ``None`` and the readback is treated as incompatible, which is the
        # conservative direction.
        expected_sign = permitted_sign(self._executing_intent())
        # **The readback of our own handwriting, which is what beta.30 rests
        # ownership on.** These four helpers were written by Alpha EMS itself, so
        # comparing them against the claim is a causal check rather than the
        # parameter matching this project rejects -- that was parameters *alone*, and
        # these are the third of three conjoined factors.
        #
        # The duration verdict is computed here because the permitted dead-man set
        # belongs to the device layer. It asks whether the live duration is one Alpha
        # EMS is willing to command -- not whether it equals the claim's, which the
        # 20/25-minute alternation makes false at every re-arm.
        duration = None if snapshot is None else snapshot.dispatch_duration_minutes
        duration_permitted = (
            None if duration is None else round(duration) in DISPATCH_DEADMAN_MINUTES
        )
        return OwnershipEvidence(
            dispatch_active=bool(snapshot is not None and snapshot.dispatch_active),
            marker_on=bool(snapshot is not None and snapshot.owner_marker),
            record=self.store.execution_record,
            dispatch_start=_dispatch_start_instant(snapshot, now),
            run_id=self._owned_run_id() if run_id is _UNSET_RUN else run_id,
            now=now,
            readback_compatible=bool(
                snapshot is not None
                and expected_sign is not None
                and dispatch_readback_compatible(
                    snapshot,
                    expected_mode=DISPATCH_MODE_SOC_CONTROL,
                    expected_sign=expected_sign,
                )
            ),
            executing_plan_id=None if self._plan is None else self._plan.plan_id,
            readback_power_kw=None
            if snapshot is None
            else snapshot.dispatch_setpoint_kw,
            readback_cutoff_percent=(
                None if snapshot is None else snapshot.dispatch_cutoff_percent
            ),
            readback_duration_minutes=duration,
            readback_duration_permitted=duration_permitted,
            # beta.43. Only ever *narrows* the answer from foreign to releasing,
            # and only against a deadline the register still agrees with.
            release_receipt=self._release_receipt,
            dispatch_timer_finishes_at=(
                None if snapshot is None else snapshot.dispatch_timer_finishes_at
            ),
        )

    @callback
    def _handle_quarter_boundary(self, now: datetime) -> None:
        """Close the finished quarter and refresh the derived values."""
        self._sample(dt_util.as_local(now))
        self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _sample(self, moment: datetime) -> None:
        """Feed the current readings into both accumulators.

        House load and the flexible load are integrated independently but
        advanced with the same instant, so their finalised intervals line up
        one-for-one even when the two sources update at completely different
        rates. Neither has to wait for the other.
        """
        house_w = self._read_house_load_w()
        house_results = self._accumulator.add_sample(moment, house_w)

        ev_results: list[QuarterResult] = []
        if self._ev_accumulator is not None:
            ev_results = self._ev_accumulator.add_sample(
                moment, self._read_ev_power_w()
            )

        pv_results: list[QuarterResult] = []
        if self._pv_accumulator is not None:
            pv_results = self._pv_accumulator.add_sample(
                moment, self._read_pv_power_w()
            )

        grid_import_results: list[QuarterResult] = []
        grid_export_results: list[QuarterResult] = []
        if (
            self._grid_import_accumulator is not None
            and self._grid_export_accumulator is not None
        ):
            flows = self.read_flows()
            grid_import_results = self._grid_import_accumulator.add_sample(
                moment, None if flows is None else flows.grid_import_w
            )
            grid_export_results = self._grid_export_accumulator.add_sample(
                moment, None if flows is None else flows.grid_export_w
            )

        if self._battery_charge_accumulator is not None:
            # Charging only: a charge target is measured against energy that went
            # *in*, and netting a discharge against it would report a pack that
            # cycled as one that never moved. The results are discarded because
            # nothing persists this series -- it exists to be read in flight, via
            # ``open_energy_kwh``, which is the only way to know progress inside a
            # quarter rather than after it.
            power = self._canonical_battery_power_w()
            charge_w = None if power is None else max(0.0, power)
            for closed in self._battery_charge_accumulator.add_sample(moment, charge_w):
                # Closed quarters are added to the window total rather than
                # discarded. This is the whole of the sawtooth fix: progress is
                # closed quarters plus the open one, for the same plan.
                self._execution_closed_kwh += max(0.0, closed.energy_kwh)
            self._accrue_grid_attribution(moment, charge_w)

        self._ingest(
            house_results,
            ev_results,
            pv_results,
            grid_import_results,
            grid_export_results,
        )

    @callback
    def _read_house_load_w(self) -> float | None:
        """Return the current house-load reading, logging why it is unusable."""
        entity_id = self.config.house_load_entity
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            self._house_problem = REJECT_SOURCE_MISSING
            self._log.warning(
                "missing_house_load",
                "House load entity %s does not exist; learning is paused",
                entity_id,
            )
            return None

        unit = state.attributes.get("unit_of_measurement")
        value_w = normalize_power_w(state.state, unit)
        if value_w is None:
            self._house_problem = (
                describe_power_problem(state.state, unit) or REJECT_SOURCE_MISSING
            )
            self._log.warning(
                "invalid_house_load",
                (
                    "House load entity %s reported %r, which is not a usable "
                    "power value; the affected time counts as missing coverage "
                    "rather than zero consumption"
                ),
                entity_id,
                state.state,
            )
        elif sanitize_load_w(value_w) is None:
            # Parsed and correctly united, but outside the plausible band. The
            # accumulator will discard it too; naming it here is what stops the
            # resulting rejection from being reported as mere thin coverage.
            self._house_problem = REJECT_VALUE_IMPLAUSIBLE
        else:
            self._log.clear("invalid_house_load")
            self._log.clear("missing_house_load")
        return value_w

    @callback
    def _canonical_battery_power_w(self) -> float | None:
        """Return battery power positive-for-charging, or ``None``.

        Normalised through :func:`normalization.split_battery_power` first, so the
        user's own sign convention is resolved away exactly once and this figure
        can be read without knowing it -- the rule ``PowerFlows`` exists to
        enforce. Reporting the raw sensor value instead would put a number in
        diagnostics whose meaning depended on a setting elsewhere in the same
        payload, which is the confusion this whole convention was built to end.

        Positive for charging, matching the plan's own published convention.
        """
        charge, discharge = split_battery_power(
            self._read_power(self.config.battery_power_entity),
            self.config.battery_power_sign,
        )
        if charge is None or discharge is None:
            return None
        return charge - discharge

    @callback
    def _state_age_seconds(self, entity_id: str | None) -> float | None:
        """Return how long ago an entity last published, in seconds.

        ``last_reported`` in preference to ``last_updated``, for the reason the
        balance coherence check already established: it advances on every
        publication, including one that repeats the previous value. A steady
        battery power that has read the same figure for ten minutes is perfectly
        current, and judging it by ``last_updated`` would call it stale.

        ``None`` means no age could be established, which the accompanying value
        check reports as a missing source rather than an old one.
        """
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        reported = getattr(state, "last_reported", None) or state.last_updated
        return max(0.0, (dt_util.utcnow() - reported).total_seconds())

    @callback
    def set_control_mode(self, mode: str) -> None:
        """Select a control mode, falling back to off for anything unknown."""
        self.control_mode = mode if mode in CONTROL_MODE_OPTIONS else CONTROL_MODE_OFF

    def _read_soc_percent(self) -> float | None:
        """Return the battery state of charge in percent, or ``None``.

        The first place in the project that reads this entity as a *value*: until
        Phase 3 it was validated when selected and reported in diagnostics, and
        nothing consumed it.

        Routed through :func:`normalization.normalize_percentage`, which insists
        the unit actually says percent. A house-load sensor selected by mistake
        would otherwise arrive as a 2000 % battery. Then through
        :func:`battery.sanitize_soc_percent`, which clamps a narrow noise band at
        either end and refuses anything further out -- a reading of -20 % would
        otherwise compute a charge headroom larger than the pack itself.
        """
        entity_id = self.config.battery_soc_entity
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        return sanitize_soc_percent(
            normalize_percentage(
                state.state, state.attributes.get("unit_of_measurement")
            )
        )

    def _read_ev_power_w(self) -> float | None:
        """Return the current flexible-load reading in watts, or ``None``."""
        entity_id = self.config.ev_power_entity
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            # The safety rule first, and unconditionally: a missing reading is a
            # missing reading, never a zero, and learning stops here whatever the
            # log decides to say about it.
            self._ev_problem = REJECT_SOURCE_MISSING
            self._ev_absences += 1
            if self._ev_seen or self._ev_absences > EV_ABSENCE_GRACE_REFRESHES:
                self._log.warning(
                    "missing_ev",
                    (
                        "EV charger entity %s does not exist; baseline learning "
                        "is paused while measured house load keeps being recorded"
                    ),
                    entity_id,
                )
            else:
                _LOGGER.debug(
                    (
                        "EV charger entity %s not present yet (refresh %d of the "
                        "startup grace); baseline learning is paused"
                    ),
                    entity_id,
                    self._ev_absences,
                )
            return None

        unit = state.attributes.get("unit_of_measurement")
        value_w = normalize_power_w(state.state, unit)
        if value_w is None:
            self._ev_problem = (
                describe_power_problem(state.state, unit) or REJECT_SOURCE_MISSING
            )
            self._log.warning(
                "invalid_ev",
                (
                    "EV charger entity %s reported %r, which is not a usable "
                    "power value; the baseline for the affected intervals is "
                    "marked invalid rather than assuming no charging"
                ),
                entity_id,
                state.state,
            )
        elif sanitize_ev_w(value_w) is None:
            self._ev_problem = REJECT_VALUE_IMPLAUSIBLE
        else:
            self._log.clear("invalid_ev")
            self._log.clear("missing_ev")
            # Readable once means readable ever: a later disappearance is a real
            # fault and warns immediately rather than spending the grace again.
            self._ev_seen = True
            self._ev_absences = 0
        return value_w

    @callback
    def _budget_surplus_kw(self) -> float | None:
        """Return measured production surplus for the grid budget, or ``None``.

        **The plausibility bands apply here too, and beta.42 exists because they
        did not.** ``_read_pv_power_w`` returns a bare ``normalize_power_w`` with no
        ceiling, and ``_read_house_load_w`` names an implausible reading and then
        returns it anyway -- both correct for their own callers, where the
        accumulator sanitises afterwards. This path had no accumulator behind it.

        So a production entity spiking to a million watts made
        ``max(0, charge - surplus)`` zero, and the grid-import ceiling accrued
        nothing for the whole quarter. That ceiling is the only bound on how much a
        run may buy, and the unguarded direction was the permissive one --
        precisely what ``MAX_PLAUSIBLE_PV_W`` was introduced to stop for the balance
        check, which is the reasoning this call site never received.

        ``None`` when either reading is unusable, which the callers already treat as
        "attribute the whole charge to the grid". The fail-safe direction is
        unchanged; an implausible reading now reaches it instead of bypassing it.
        """
        pv_w = sanitize_pv_w(self._read_pv_power_w())
        load_w = sanitize_load_w(self._read_house_load_w())
        if pv_w is None or load_w is None:
            return None
        return max(0.0, (pv_w - load_w) / 1000.0)

    @callback
    def _read_pv_power_w(self) -> float | None:
        """Return the current PV reading for accumulation, or ``None``.

        Deliberately sets **no** problem attribution and increments no counter.
        The other two readers do, because a bad house-load or flexible-load
        reading invalidates that interval's baseline and the user needs to know
        which source cost them the interval. PV cannot invalidate anything: it is
        recorded beside the baseline and read by nothing that computes it. A PV
        entity that vanishes costs PV evidence and nothing else, and reporting it
        as a learning problem would be false.
        """
        entity_id = self.config.pv_power_entity
        if not entity_id or not self.config.has_pv:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        return normalize_power_w(
            state.state, state.attributes.get("unit_of_measurement")
        )

    @callback
    def _ingest(
        self,
        house_results: list[QuarterResult],
        ev_results: list[QuarterResult],
        pv_results: list[QuarterResult] | None = None,
        grid_import_results: list[QuarterResult] | None = None,
        grid_export_results: list[QuarterResult] | None = None,
    ) -> None:
        """Persist finalised intervals that carry enough coverage.

        Intervals are stored by chronological index rather than by wall-clock
        slot, so a fall-back day keeps both occurrences of the repeated hour.
        """
        if not house_results:
            return

        tz = dt_util.get_default_time_zone()
        ev_expected = self._ev_accumulator is not None
        # A state of charge is a level, not a flow, so it is sampled at the
        # boundary rather than integrated across the interval. Read once here,
        # and attached only to the interval that has *just* closed: when several
        # quarters close together after a restart, the state of charge two hours
        # ago is genuinely unknown, and repeating today's reading across them
        # would be inventing history.
        soc_percent = self._read_soc_percent()
        latest_start = max(
            (result.start_utc for result in house_results if result.accepted),
            default=None,
        )
        # The two accumulators are advanced together, so equal-length result
        # lists are the normal case; pairing by start instant keeps the mapping
        # correct even if one of them was created later.
        ev_by_start = {result.start_utc: result for result in ev_results}
        # Same pairing idiom, and deliberately kept separate from the EV branch
        # below: a missing or under-covered PV interval must leave the baseline
        # completely alone. It records no rejection, increments no counter and
        # invalidates nothing -- it simply stores no PV value for that interval.
        pv_by_start = {
            result.start_utc: result for result in (pv_results or []) if result.accepted
        }
        # Same pairing idiom again, and the same isolation: a missing or
        # under-covered grid interval stores no grid value and touches nothing
        # else. It is evidence for a later phase, so it must never be able to
        # invalidate a baseline it has no opinion about.
        grid_import_by_start = {
            result.start_utc: result
            for result in (grid_import_results or [])
            if result.accepted
        }
        grid_export_by_start = {
            result.start_utc: result
            for result in (grid_export_results or [])
            if result.accepted
        }

        changed = False
        for result in house_results:
            if not result.accepted:
                self._record_rejected_quarter(result)
                continue

            record = self.store.get_or_create(result.day, tz)
            # Indexed in the zone the *record* was written in, not the zone that
            # happens to be current. The two differ only after Home Assistant's
            # timezone is changed, and then they differ by hours: an existing
            # day keeps its original ``tz_key`` and length, so indexing it under
            # the new zone wrote each afternoon quarter over a morning one while
            # the day still looked complete.
            index = index_for_start_utc(result.day, result.start_utc, record.tz)

            ev_kwh: float | None = None
            if ev_expected:
                ev_result = ev_by_start.get(result.start_utc)
                if ev_result is not None and ev_result.accepted:
                    ev_kwh = ev_result.energy_kwh
                else:
                    self.invalid_ev_quarters += 1
                    _tally(
                        self.invalid_ev_quarters_by_reason,
                        self._ev_problem or REJECT_INSUFFICIENT_COVERAGE,
                    )

            pv_result = pv_by_start.get(result.start_utc)
            grid_import_result = grid_import_by_start.get(result.start_utc)
            grid_export_result = grid_export_by_start.get(result.start_utc)

            if not record.record_interval(
                index,
                measured_kwh=result.energy_kwh,
                ev_kwh=ev_kwh,
                ev_expected=ev_expected,
                soc_percent=soc_percent if result.start_utc == latest_start else None,
                pv_kwh=None if pv_result is None else pv_result.energy_kwh,
                grid_import_kwh=(
                    None
                    if grid_import_result is None
                    else grid_import_result.energy_kwh
                ),
                grid_export_kwh=(
                    None
                    if grid_export_result is None
                    else grid_export_result.energy_kwh
                ),
            ):
                # The index fell outside the day. Unreachable under a stable
                # timezone, so reaching it means the stored day's shape and the
                # instant being filed disagree -- which must be counted and
                # named rather than leaving a quarter that reports itself as
                # finalised while having stored nothing.
                self._record_rejected_quarter(result, REJECT_INTERVAL_OUT_OF_RANGE)
                continue

            if result.start_utc == latest_start and soc_percent is not None:
                self._observe_soc_coherence(record, index, soc_percent)

            self.last_finalized_quarter = result.start_utc
            self.store.last_finalized = result.start_utc.isoformat()
            changed = True

        # Attribution is per interval, so the problem window is reopened once
        # every interval in this batch has been accounted for. Clearing it any
        # earlier would let a single bad reading explain only the first of
        # several quarters closed together after a restart.
        self._house_problem = None
        self._ev_problem = None

        if changed:
            self.store.schedule_save()

    @callback
    def _observe_soc_coherence(
        self, record: DayRecord, index: int, soc_percent: float
    ) -> None:
        """Compare this interval's state-of-charge movement against its power.

        Instrumentation only. It gates nothing, and it is here rather than in the
        control layer because the comparison needs the interval that has just
        closed -- the one moment both ends of the movement are known.

        The battery power used is the reading at the boundary, not an integral
        over the interval, because no integral exists: power is sampled, not
        accumulated. That makes the comparison coarse, which is precisely why it
        reports a direction and an order of magnitude rather than a verdict.
        """
        capacity = self.config.battery_capacity_kwh
        if capacity is None or index <= 0:
            return
        previous = record.soc_at(index - 1)
        if previous is None:
            return
        power = self._canonical_battery_power_w()
        if power is None:
            return
        self.soc_coherence.observe(
            index=index,
            soc_before_percent=previous,
            soc_after_percent=soc_percent,
            battery_power_w=power,
            capacity_kwh=capacity,
            interval_hours=INTERVAL_HOURS,
        )

    @callback
    def _record_rejected_quarter(
        self, result: QuarterResult, reason: str | None = None
    ) -> None:
        """Count a quarter that could not be learned, and say why once.

        The warning is throttled per reason rather than per occurrence. A source
        that stays broken rejects a quarter every fifteen minutes, which is four
        lines an hour of identical text; a source that breaks in a new way must
        still be able to speak immediately rather than waiting behind the
        older problem's throttle window.
        """
        reason = reason or self._house_problem or REJECT_INSUFFICIENT_COVERAGE
        self.rejected_quarters += 1
        _tally(self.rejected_quarters_by_reason, reason)
        self.last_rejected_quarter = result.start_utc
        self.last_rejected_reason = reason

        if reason == REJECT_INSUFFICIENT_COVERAGE:
            # Expected after a restart or a reload: the interval that was in
            # flight cannot reach the coverage threshold and never could. Saying
            # so every time would train the user to ignore the message.
            _LOGGER.debug(
                "Quarter starting %s covered only %.0f%% of its length and was "
                "not learned",
                result.start_utc.isoformat(),
                result.coverage * 100,
            )
            return

        self._log.warning(
            f"{_REJECTED_QUARTER_LOG}:{reason}",
            (
                "Quarter starting %s was not learned (%s); house load source "
                "%s covered only %.0f%% of the interval. Learning stays paused "
                "for as long as this persists"
            ),
            result.start_utc.isoformat(),
            reason,
            self.config.house_load_entity,
            result.coverage * 100,
        )

    @property
    def balance_source_entities(self) -> list[str]:
        """Return the entities participating in the balance identity."""
        return self._balance_source_entities()

    @callback
    def _balance_source_entities(self) -> list[str]:
        """Return the source entities that participate in the balance identity.

        PV is omitted when the system has none: its contribution is a known
        zero rather than an unread sensor, so it has no timestamp to compare.
        """
        candidates = [
            self.config.house_load_entity,
            self.config.battery_power_entity,
            self.config.grid_power_entity,
        ]
        if self.config.has_pv:
            candidates.append(self.config.pv_power_entity)
        return [entity_id for entity_id in candidates if entity_id]

    @callback
    def _unreadable_balance_sources(self) -> tuple[str, ...]:
        """Return the configured balance sources that currently read unusably."""
        return tuple(
            entity_id
            for entity_id in self._balance_source_entities()
            if self._read_power(entity_id) is None
        )

    @callback
    def _is_quiescent_zero_pv(self, entity_id: str) -> bool:
        """Return whether ``entity_id`` is the PV source and reads exactly zero.

        This is the *only* freshness exemption in the integration, and it is
        deliberately confined to PV rather than written as a general rule about
        zero-valued flows.

        The arithmetic argument -- a term of exactly zero cannot make its own
        timestamp matter -- is source-agnostic, and :func:`measure_coherence`
        states it in full. What is specific to PV is the *need*. Night is a
        predictable eight to twelve hours in which generation is genuinely zero
        and a change-driven source has no reason to republish, so PV alone
        accumulates a sustained skip rate: on the reference installation, 185 of
        189 skipped samples were a PV sensor sitting at 0 W since dusk, while
        the identity it was blocking closed to within 1 W.

        Battery idle, a null grid reading and a zero house load are transient
        states, not a nightly regime, so relaxing them would buy no measurable
        coverage while widening the surface on which a genuinely dead source
        could pass unnoticed. House load in particular is the learning target;
        its silence is information, not noise.

        The exemption is self-terminating. A PV sensor that starts generating
        publishes a new, non-zero value by definition -- that is what a
        change-driven sensor does -- so at sunrise the source is simultaneously
        fresh and non-zero, and the normal rules resume on the same sample.
        """
        if entity_id != self.config.pv_power_entity or not self.config.has_pv:
            return False
        # Read through the same normalisation the identity uses. An
        # ``unavailable`` or badly united PV entity yields ``None`` here, not a
        # zero, so it is never exempted -- and it would not reach a verdict at
        # all, because a snapshot missing a component is not judged.
        return self._read_power(entity_id) == 0.0

    @callback
    def _source_coherence(self) -> SourceCoherence:
        """Return how closely aligned in time the balance sources are.

        ``last_reported`` is preferred over ``last_updated`` because it advances
        on every publication, including one that repeats the previous value. A
        steady battery power that has read the same figure for ten minutes is
        perfectly current, but its ``last_updated`` is ten minutes old and would
        look stale.

        A PV source reading exactly zero contributes no timestamp at all, so the
        remaining sources are compared against each other rather than against a
        sensor that has correctly stopped reporting an unchanging nothing. See
        :meth:`_is_quiescent_zero_pv`.
        """
        reported_at: list[datetime] = []
        entity_ids: list[str] = []
        quiescent: list[str] = []
        for entity_id in self._balance_source_entities():
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            if self._is_quiescent_zero_pv(entity_id):
                quiescent.append(entity_id)
                continue
            reported_at.append(
                getattr(state, "last_reported", None) or state.last_updated
            )
            # Carried alongside so a skipped sample can name the source holding
            # the comparison back. Without it a high skip rate says only "the
            # sources disagree about when", which is not actionable.
            entity_ids.append(entity_id)
        return measure_coherence(
            reported_at,
            dt_util.utcnow(),
            entity_ids,
            quiescent_entity_ids=tuple(quiescent),
        )

    @callback
    def _sample_balance(self) -> None:
        """Record one energy-balance observation, when all sources are present.

        A failing sample never warns on its own. Only a run of consecutive
        *coherent* failures does, because a single instantaneous mismatch is far
        more likely to be two sensors caught mid-transient than a real fault.
        """
        # **The two attribution inputs, and they change no verdict.** The
        # allowance and the pass test are exactly what they were; these only let a
        # reader tell a meter caught mid-ramp from the installation's standing
        # DC/AC boundary term. See ``BalanceSample.regime``.
        elapsed: float | None = None
        if self._last_setpoint_write is not None:
            elapsed = max(
                0.0, (dt_util.utcnow() - self._last_setpoint_write).total_seconds()
            )
        sample = evaluate_balance(
            self.read_flows(),
            self._source_coherence(),
            seconds_since_dispatch_write=elapsed,
            setpoint_delta_kw_since_previous=self._setpoint_delta_kw,
        )
        if sample is None:
            unreadable = self._unreadable_balance_sources()
            self.balance.record_unavailable(unreadable)
            if unreadable:
                # House load logs its own problems because it is on the learning
                # path; the battery, PV and grid sources did not log anything at
                # all, so a dead one produced a silently unjudgeable balance
                # check and no other symptom. Throttled per source set.
                self._log.warning(
                    f"{_BALANCE_UNAVAILABLE_LOG}:{','.join(unreadable)}",
                    (
                        "Energy-balance sources %s cannot be read, so the "
                        "balance check has no verdict to give. Learning is "
                        "unaffected -- it does not use these sources -- but the "
                        "data-quality component of the confidence score drops "
                        "out until they return"
                    ),
                    ", ".join(unreadable),
                )
            else:
                self._log.clear(_BALANCE_UNAVAILABLE_LOG)
            return

        self.last_balance = sample
        outcome = self.balance.record(sample)

        if outcome == OUTCOME_SKIPPED_INCOHERENT:
            # Neither good nor bad news: the sources were too far apart in time
            # for the comparison to mean anything. Recorded in diagnostics only.
            return

        # The persisted tally now counts eligible samples only, so the stored
        # pass rate carries the same meaning as the in-memory one.
        self.store.balance.record(sample.within_tolerance)

        if sample.within_tolerance:
            self._log.clear(_BALANCE_LOG_MODERATE)
            self._log.clear(_BALANCE_LOG_GROSS)
            return

        if self.balance.should_warn():
            # Two wordings, because the two situations call for different action.
            # A residual several times its physical allowance means a term of the
            # identity is wrong, and the user should re-check the configuration.
            # A residual only somewhat over it is far more likely to be the
            # sources sitting on different electrical boundaries, which is worth
            # reporting but is not a mistake the user made.
            # The two wordings are throttled independently. Sharing one key let
            # the reassuring message suppress the escalated one for a full hour:
            # a moderate residual warned, a passing sample re-armed the debounce,
            # and the gross fault that followed within the throttle window was
            # dropped -- permanently, because only a passing coherent sample can
            # re-arm the one-shot flag and a real fault never produces one. The
            # user was left with "learning is unaffected" for a broken
            # configuration.
            if sample.gross_fault_suspected:
                log_key = _BALANCE_LOG_GROSS
                message = (
                    "Sustained energy-balance mismatch over %d consecutive checks "
                    "in mode %s (supply %.0f W vs demand %.0f W, %.0f%% off; "
                    "residual %.0f W against an allowance of %.0f W). A residual "
                    "this far outside the physical allowance usually means one "
                    "term of the identity is wrong: check the selected source "
                    "entities, the configured sign conventions, and whether one "
                    "source updates far more slowly than the others"
                )
            else:
                log_key = _BALANCE_LOG_MODERATE
                message = (
                    "Sustained energy-balance mismatch over %d consecutive checks "
                    "in mode %s (supply %.0f W vs demand %.0f W, %.0f%% off; "
                    "residual %.0f W against an allowance of %.0f W). This is a "
                    "moderate residual, consistent with the sources being "
                    "measured at different electrical boundaries -- PV and "
                    "battery on the inverter's DC side, house load and grid on "
                    "the AC side -- or with conversion losses larger than "
                    "allowed for. Learning is unaffected; only the confidence "
                    "score is"
                )
            args = (
                self.balance.consecutive_failures,
                sample.mode,
                sample.supply_w,
                sample.demand_w,
                sample.relative_error * 100,
                sample.residual_w,
                sample.allowed_residual_w,
            )
            # Only stamped when the line was really written, so the timestamp in
            # diagnostics always corresponds to an entry that exists in the log.
            if self._log.warning(log_key, message, *args):
                self.balance.last_warning = dt_util.utcnow().isoformat()

    # -- derived values --------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Recompute forecasts and confidence from the learned history."""
        now = dt_util.now()
        self.last_refresh_at = now
        # Names the cadence last entered, which is exact except while a solve is in
        # flight and the tick runs inside it. ``seq`` is the ordering authority.
        self._lifecycle_cadence = CADENCE_QUARTER_REFRESH
        tz = dt_util.get_default_time_zone()
        today = now.date()
        tomorrow = today + timedelta(days=1)
        records = list(self.store.days.values())

        # Prepared once and shared. The bucketing depends only on the records
        # and the reference day, so building it per target repeated the most
        # expensive part of the refresh -- around 90 ms at a year of history --
        # for no difference in result.
        inputs = collect_forecast_inputs(records, today)
        baseline_today = build_forecast(records, today, today, tz, inputs)
        forecast_tomorrow = build_forecast(records, today, tomorrow, tz, inputs)

        today_record = self.store.days.get(today)
        if today_record is not None:
            measured_baseline = [
                today_record.baseline_at(index)
                for index in range(today_record.interval_count)
            ]
            baseline_so_far = today_record.baseline_total_kwh
            measured_so_far = today_record.measured_total_kwh
            ev_so_far = today_record.ev_total_kwh
        else:
            measured_baseline = []
            baseline_so_far = 0.0
            measured_so_far = 0.0
            ev_so_far = 0.0

        # Chronological, so it cannot move backwards through a DST fold the way
        # a wall-clock slot index does.
        elapsed = self._elapsed_intervals(now, today, tz)

        adapted = adapt_today(
            baseline_today, measured_baseline, baseline_so_far, elapsed
        )

        learned = self.store.learned_days(before=today)
        breakdown = compute_confidence(learned, today, self.store.balance.score)

        record = await self._async_record_forecast_evidence(
            now=now,
            today=today,
            tz=tz,
            baseline_today=baseline_today,
            forecast_tomorrow=forecast_tomorrow,
            breakdown=breakdown,
        )

        # Before the plan, because the plan consumes it. A failure here is
        # contained and yields named unavailability rather than an exception, so
        # the plan is always built -- PV-aware when there is a forecast, PV-blind
        # and labelled when there is not.
        pv_forecasts = await self._async_pv_forecasts_safely(today=today, tz=tz)
        self.pv_forecasts = pv_forecasts
        await self._async_record_pv_evidence_safely(
            forecasts=pv_forecasts, now=now, today=today, tz=tz
        )
        # Read after PV and before the plan, and consumed by neither. It sits
        # here so one refresh carries one consistent instant across every layer,
        # which is what made the beta.9 symptom readable once it was fixed.
        #
        # The plan below is *not* passed prices, and that omission is the design:
        # a field with no consumer is an invitation, and keeping the series out of
        # the decision layer entirely makes "prices change no decision" something
        # the structure guarantees rather than something a test checks.
        price_forecasts = self._price_forecasts_safely(now=now, today=today, tz=tz)
        self.price_forecasts = price_forecasts
        # **The one measurement the unknown-price policy needs and nobody has
        # taken.** The publication *time* is deliberately not modelled anywhere --
        # day-ahead can publish early or late -- so the instant tomorrow first
        # becomes priceable is observed instead. Recorded once per session; a later
        # refresh must not overwrite it with a fresher clock reading.
        if self._tomorrow_prices_available_at is None:
            tomorrow_forecast = price_forecasts.get(now.date() + timedelta(days=1))
            if tomorrow_forecast is not None and tomorrow_forecast.available:
                self._tomorrow_prices_available_at = now.isoformat()
        await self._async_record_price_evidence_safely(
            forecasts=price_forecasts, now=now, tz=tz
        )

        absorb_surplus, absorption_reason = self._surplus_absorption()
        # The same control surface, read for the other direction. Read here beside
        # its sibling and passed down, so the whole refresh describes one inverter
        # rather than two reads that could disagree mid-refresh.
        ambient_modelled, ambient_reason = self._ambient_self_consumption()

        plan = self._build_battery_plan(
            today=today,
            elapsed=elapsed,
            baseline_today=baseline_today,
            tomorrow=forecast_tomorrow,
            tz_key=str(tz),
            pv_today=pv_forecasts.get(today),
            pv_tomorrow=pv_forecasts.get(tomorrow),
            absorb_surplus=absorb_surplus,
        )

        await self._async_record_reserve_evidence_safely(
            plan=plan,
            now=now,
            today=today,
            tomorrow=tomorrow,
            tz=tz,
            today_interval_count=baseline_today.interval_count,
            absorption_modelled=absorb_surplus,
            absorption_reason=absorption_reason,
        )

        economic = await self._async_economic_outcome_safely(
            plan=plan,
            today=today,
            tomorrow=tomorrow,
            tz=tz,
            today_interval_count=baseline_today.interval_count,
            price_forecasts=price_forecasts,
            ambient_self_consumption=ambient_modelled,
        )

        await self._async_record_economic_evidence_safely(
            outcome=economic,
            plan=plan,
            now=now,
            today=today,
            tz=tz,
        )

        # **Written once per civil day, and only here. beta.39.**
        #
        # The one datum a forecast revaluation needs is what the energy the day
        # opened with was worth *on the curve that existed then*, and nothing
        # retained it. It is written from the refresh rather than from the
        # publishing path on purpose: an entity read must never write storage, and
        # the Economic Value attributes are read on every state update.
        self._note_opening_valuation(outcome=economic, plan=plan, now=now, today=today)

        # **Written once per past day, and only here. beta.42.**
        #
        # Same reasoning as the line above and the same seam: an entity read must
        # not write storage, and the ROI attributes are read on every state update.
        # Sealing from the refresh also gives the pass the one thing it needs and a
        # midnight timer could not -- an authoritative plan, whose limits convert
        # stored state of charge to energy.
        #
        # Deliberately not "yesterday": every retained past day is offered, so a day
        # that was unfinalisable for a week and then became complete is picked up
        # rather than missed forever.
        self.seal_finalizable_days(plan, today)

        # Derived from runs already solved, so this costs no extra search.
        #
        # **Before the control report, and that ordering is load-bearing.** The
        # Stage B controller reads ``self.execution_targets``, so building them
        # afterwards would have it acting on the previous refresh's plan -- one
        # quarter stale, with a plan_id and revision lagging behind the plan they
        # describe. Found by walking twelve refreshes and noticing the window it
        # was working to had not started yet.
        self.execution_targets = self._execution_targets(
            outcome=economic,
            plan=plan,
            today_interval_count=baseline_today.interval_count,
            tz=tz,
            issued_at=now,
        )
        # Remembered across a restart, so a reboot does not tell Stage B that
        # every target it has been tracking for hours is brand new.
        self._remember_execution_revisions()

        control = self._build_control_report_safely(
            plan=plan,
            now=now,
            today=today,
            elapsed=elapsed,
            today_interval_count=baseline_today.interval_count,
        )
        # Refuses on every path in this release. Called unconditionally so the
        # refusal is exercised rather than merely assumed.
        await self._async_dispatch(control, now)
        # **Publish-ordering, not control. beta.39.** Two blocks of the report above
        # describe facts the write boundary has only just settled; this re-renders
        # them into the same dict before it becomes ``self.data``.
        self._settle_execution_payload(control)

        return {
            "today": adapted,
            "today_baseline": baseline_today,
            "tomorrow": forecast_tomorrow,
            "confidence": breakdown,
            "learning_days": breakdown.learned_days,
            "elapsed_intervals": elapsed,
            "measured_so_far_kwh": measured_so_far,
            "ev_so_far_kwh": ev_so_far,
            "forecast_yesterday_error": record.yesterday,
            "forecast_error_window": record.window,
            "battery_plan": plan,
            "economic": economic,
            "execution_targets": self.execution_targets,
            # The instant this refresh describes. Published so a consumer that
            # needs to know "now" reads the same one the plan, the reserve and the
            # economic solve were all computed at, rather than taking a second
            # clock reading of its own. A second clock is how a correct answer
            # came to look wrong beside the figures printed next to it once
            # already -- see the Phase-4 export check.
            "issued_at": now,
            "control": control,
            "pv_today": pv_forecasts.get(today),
            "pv_tomorrow": pv_forecasts.get(tomorrow),
            "pv_absorption": {
                "modelled": absorb_surplus,
                "reason": absorption_reason,
            },
            # **What an idle interval actually costs.** Published beside its
            # sibling because a reader comparing two marginal euro figures across
            # installations needs to know which counterfactual each was measured
            # against -- see ``counterfactual_basis`` on the plan.
            "ambient_self_consumption": {
                "modelled": ambient_modelled,
                "reason": ambient_reason,
            },
            "price_today": price_forecasts.get(today),
            "price_tomorrow": price_forecasts.get(tomorrow),
        }

    async def _async_economic_outcome_safely(
        self,
        *,
        plan: BatteryPlan | None,
        today: date,
        tomorrow: date,
        tz: tzinfo,
        today_interval_count: int,
        price_forecasts: dict[date, PriceForecast],
        ambient_self_consumption: bool = False,
    ) -> EconomicOutcome | None:
        """Solve the economic plan, or say nothing and keep the refresh.

        Wrapped like every additive layer since Phase 2, with its own throttle
        key: the optimizer is the newest and least proven thing in the refresh,
        and a fault in it must not cost the learning history, the forecasts, the
        reserve or the control report.
        """
        try:
            return await self._async_economic_outcome(
                plan=plan,
                today=today,
                tomorrow=tomorrow,
                tz=tz,
                today_interval_count=today_interval_count,
                price_forecasts=price_forecasts,
                ambient_self_consumption=ambient_self_consumption,
            )
        except Exception:
            self._log.warning(
                _ECONOMIC_LOG,
                (
                    "The economic plan could not be built this refresh. Every "
                    "other layer is unaffected, and nothing was going to be "
                    "executed in any case"
                ),
            )
            _LOGGER.debug("economic plan failed", exc_info=True)
            return None

    async def _async_economic_outcome(
        self,
        *,
        plan: BatteryPlan | None,
        today: date,
        tomorrow: date,
        tz: tzinfo,
        today_interval_count: int,
        price_forecasts: dict[date, PriceForecast],
        ambient_self_consumption: bool = False,
    ) -> EconomicOutcome | None:
        """Return this refresh's economic plan, or ``None``.

        Runs in the executor. The solve is pure and CPU-bound -- three backward
        inductions over a lattice -- and Home Assistant's event loop must not wait
        on it, however fast it is on this machine.
        """
        if plan is None or plan.state is None or plan.reserve_projection is None:
            return None
        limits = plan.state.limits
        floor_energy = limits.energy_for_soc(plan.reserve.configured_min_soc_percent)
        demands = tuple(plan.reserve_projection.demands)
        if not demands:
            return None

        prices = self._economic_prices(
            demands=demands,
            today=today,
            tomorrow=tomorrow,
            tz=tz,
            today_interval_count=today_interval_count,
            price_forecasts=price_forecasts,
        )
        raw_reserve = tuple(
            entry.required_dc_kwh for entry in plan.reserve_projection.intervals
        )
        ceiling = plan.reserve_projection.ceiling_energy_kwh
        above_capacity = max(
            (value - ceiling for value in raw_reserve if value is not None),
            default=0.0,
        )
        # ============ the beta.31 switch, and it is these lines ============
        #
        # Everything above still computes the **autonomy** requirement, and it is
        # still reported. What changes is which curve the solver is made to obey.
        #
        # Two passes over the reachability recursion, and they are exact rather
        # than iterative: the uncertainty margin depends on the demands and on the
        # distance to the first replenishment, neither of which moves when the
        # floor does. The first pass locates that distance; the second applies the
        # margin as the recursion's own floor.
        actionable = actionable_intervals(demands, prices)
        probe = build_reserve_reachable(
            limits=limits,
            floor_energy_kwh=floor_energy,
            demands=demands,
            grid_credit_intervals=actionable,
        )
        margin = uncertainty_margin(
            probe,
            mae_kwh_per_interval=self._forecast_mae_kwh_per_interval(),
            usable_capacity_kwh=limits.capacity_kwh,
        )
        reachability = build_reserve_reachable(
            limits=limits,
            # The hard floor plus a bounded margin, and nothing else. The floor is
            # enforced by the clamp regardless of anything decided here, so a
            # planning error is an expensive import rather than battery harm.
            floor_energy_kwh=floor_energy + margin.total_dc_kwh,
            demands=demands,
            grid_credit_intervals=actionable,
        )

        # **What a kWh still in the pack is worth when the prices run out.**
        # Neither a wall nor a cliff -- see ``edge_value_eur_per_kwh``. Withdrawn
        # to the extent forecast production needs the room, so terminal value can
        # never pay for displacing free energy.
        planning = PlanningInputs(
            enforced_reserve=tuple(
                entry.required_dc_kwh for entry in reachability.intervals
            ),
            autonomy_reserve=raw_reserve,
            reachability=reachability,
            uncertainty=margin,
            actionable_intervals=actionable,
            forecast_risk=self._forecast_risk(len(demands), today_interval_count),
            ambient_self_consumption=ambient_self_consumption,
            edge_value_eur_per_kwh=edge_value_eur_per_kwh(
                prices[:actionable],
                discharge_efficiency=limits.discharge_efficiency,
            ),
            compare_legacy=self.control_mode == CONTROL_MODE_SHADOW,
            edge_creditable_kwh=edge_creditable_energy_kwh(
                ceiling_kwh=limits.energy_for_soc(limits.max_soc_percent),
                forecast_surplus_kwh=sum(
                    demand.surplus_kwh for demand in demands[:actionable]
                ),
            ),
            terminal_value=self._terminal_value(
                demands=demands,
                prices=prices,
                horizon_intervals=actionable,
                limits=limits,
                edge_value_eur_per_kwh=edge_value_eur_per_kwh(
                    prices[:actionable],
                    discharge_efficiency=limits.discharge_efficiency,
                ),
                edge_creditable_kwh=edge_creditable_energy_kwh(
                    ceiling_kwh=limits.energy_for_soc(limits.max_soc_percent),
                    forecast_surplus_kwh=sum(
                        demand.surplus_kwh for demand in demands[:actionable]
                    ),
                ),
                today_interval_count=today_interval_count,
            ),
            head_run_state=self._head_run_state(),
        )

        # The configured physical floor, and nothing else.
        #
        # Until beta.18 this was the hold trajectory's end energy, on the reading
        # that a plan should never leave the battery worse off than doing nothing
        # would have. That duplicated what the dynamic reserve already says, and
        # on a horizon with no surplus production ahead the idle trajectory is
        # flat -- so the requirement became "end no lower than you are now",
        # forbidding net discharge and ratcheting upward every time the pack
        # charged. The reserve is the authoritative requirement, it is enforced at
        # every interval, and its own forecast outlives the price horizon.
        terminal = floor_energy

        outcome = await self.hass.async_add_executor_job(
            _solve_economic,
            limits,
            floor_energy,
            plan.state.energy_kwh,
            terminal,
            demands,
            prices,
            raw_reserve,
            max(0.0, above_capacity),
            self.config.minimum_trade_gain_eur,
            self.config.grid_charge_margin_eur_per_kwh,
            self.config.battery_throughput_cost_eur_per_kwh,
            planning,
            self.config.allow_grid_charging,
            self.config.allow_battery_export,
        )
        # **Written after the plan exists**, so a record can never describe a
        # decision that was not made. This is the only thing standing between a
        # money-spending optimiser and an unauditable one.
        self._record_decision(outcome, now=dt_util.now(), plan=plan, planning=planning)
        return outcome

    @callback
    def _remember_execution_revisions(self) -> None:
        """Persist just enough of each target for revisions to survive a restart.

        Only the fields :func:`economic.execution_revision` compares. Not the
        plan, not the progress, not the economics -- a restart should reconstruct
        those from evidence, and a snapshot of them would be a stale claim
        dressed as a fact.
        """
        remembered = {
            target["plan_id"]: {
                "plan_id": target["plan_id"],
                "revision": target.get("revision", 1),
                "intent": target.get("intent"),
                "battery_target_kwh": target.get("battery_target_kwh"),
                "grid_target_kwh": target.get("grid_target_kwh"),
                "window_end": target.get("window_end"),
            }
            for target in self.execution_targets
            if isinstance(target.get("plan_id"), str)
        }
        if remembered == self._execution_revisions:
            return
        self._execution_revisions = remembered
        self.store.execution_revisions = remembered
        self.store.schedule_save()

    @callback
    def _stage_b_intent(self, *, plan: Any, now: datetime) -> Any:
        """Return the ``ControlIntent`` Stage B wants, or ``None``.

        **An open quarter is asked first, and is authoritative for both executable
        intents since beta.29.** Below that, ``None`` for everything that is not an
        executable grid charge inside its window, which is what leaves the
        reserve-guard path untouched. The last Stage-B decision is reused rather than
        recomputed, so the intent and the published diagnostics describe the same
        refresh.
        """
        decision = self._stage_b_decision
        if decision is None or plan is None or plan.state is None:
            return None
        day = getattr(plan, "target_day", None)
        index = getattr(plan, "start_index", None)
        if day is None or index is None:
            return None
        # **An open quarter builds its own command, whichever intent it is.**
        #
        # beta.27 made the quarter the execution envelope and then asked it only for
        # ``net_export``; a charge still went through ``control_intent_for``, which
        # needs ``decision.wants_command`` and therefore a carried run with an
        # actionable window. So an open *charge* quarter whose parent run had ended
        # produced no command at all -- the beta.26 skipped-quarter fault, still
        # live, and masked only because charge runs usually span several quarters
        # and get affirmed by the next publication.
        #
        # A run ending is not consulted here. That is the whole point: the quarter
        # carries its own frozen intent, targets and provenance, so ``CarriedRun``
        # may end, roll or be ``None`` without stopping a quarter already open.
        #
        # ``control_intent_for`` below is unchanged and keeps its charge-only
        # guarantee. It is still the path for a publication carrying no quarter
        # schedule -- anything written before beta.27 -- and for the prepared and
        # no-quarter cases.
        quarter = self._quarter
        if quarter is not None and quarter.open_at(now):
            setpoint = self._dispatch_setpoint(now)
            # **A resting row says so, rather than producing nothing. beta.36.**
            #
            # The short-circuit matters as much as the flag: reading
            # ``setpoint.applied_kw`` on a satisfied row would pick up the
            # deadband-substituted *held* value, so a "hold" could arrive carrying
            # 0.1 kW and keep charging a met objective for the rest of the quarter.
            satisfied = self._quarter_is_satisfied(now)
            if satisfied:
                setpoint = None
            rate_kw = 0.0 if setpoint is None else abs(setpoint.applied_kw)
            # **A rate the actuator cannot express is a rest, not a vanishing.**
            #
            # This is the 2026-08-31 path. Production was covering the house and the
            # row's grid budget was nearly spent, so ``decide_charge`` clamped
            # ``applied_kw`` to 0 -- a correct clamp -- and ``quarter_intent_for``
            # answered ``None`` for a row that was open, owned, armed and mid-
            # campaign. ``safety.evaluate(None, ...)`` is unsafe by construction, and
            # an unsafe verdict on an owned live dispatch was promoted to
            # ``EXECUTION_STOP_SAFETY``: total teardown of a working campaign because
            # the sun came out.
            #
            # The band is ``CONTROL_MIN_POWER_KW`` -- two actuator steps, the same
            # figure ``safety`` uses for ``power_below_device_minimum`` and
            # ``limit_command`` for its floor -- so the three agree on what
            # "commandable" means rather than each holding an opinion. Anything
            # inside it rests at zero and **recovers inside its own row** the moment
            # the clamp lifts; nothing about the row, the plan, the campaign or the
            # claim is torn down to achieve that.
            holds = satisfied or rate_kw < CONTROL_MIN_POWER_KW
            self._hold_reason = (
                None
                if not holds
                else HOLD_REASON_QUARTER_SATISFIED
                if satisfied
                else HOLD_REASON_RATE_BELOW_RESOLUTION
            )
            return quarter_intent_for(
                quarter,
                # An unsigned magnitude: the sign lives in the action, and the
                # signed value is rebuilt at the Dispatch boundary.
                battery_power_kw=0.0 if holds else rate_kw,
                holds_at_zero=holds,
                floor_soc_percent=plan.reserve.configured_min_soc_percent,
                # **The pack's own maximum, and the only ceiling there is.** Passed
                # for a charge and ignored for an export, because a charge cutoff is
                # an upper state of charge and a discharge cutoff is a lower one.
                ceiling_soc_percent=plan.state.limits.max_soc_percent,
                horizon_minutes=CONTROL_HORIZON_MINUTES,
                target_day=day,
                start_index=index,
                built_at=now,
            )
        return control_intent_for(
            decision,
            floor_soc_percent=plan.reserve.configured_min_soc_percent,
            # The pack's own maximum, and the only ceiling there is. If it cannot
            # be read the device layer refuses the charge rather than substituting
            # the discharge floor or a constant.
            ceiling_soc_percent=plan.state.limits.max_soc_percent,
            horizon_minutes=CONTROL_HORIZON_MINUTES,
            target_day=day,
            start_index=int(index),
            built_at=now,
        )

    @callback
    def _remember_ended_run(
        self, reason: str, run: Any, plan: Any, now: datetime
    ) -> None:
        """Hold why the carried run ended, so a later refresh can still say.

        Written only when a run actually ends -- never on an affirmation, a rolling
        publication, a prepared state or an ordinary armed refresh -- so the record
        always describes a real lifecycle event rather than the latest refresh.

        The progress figures are read for the *ended* run's own key, which is still
        the accumulator's key at this moment, so they are its closing figures and
        not the next run's opening ones.
        """
        progress = self._execution_progress(run.run_id, plan)
        realized = max(0.0, float(progress.realized_kwh))
        target = float(run.target.battery_target_kwh)
        self._last_ended = {
            "reason": reason,
            "run_id": run.run_id,
            "plan_id": run.plan_id,
            "intent": run.intent,
            "ended_at": now.isoformat(),
            "battery_target_kwh": round(target, 3),
            "battery_realized_kwh": round(realized, 3),
            "remaining_battery_kwh": round(max(0.0, target - realized), 3),
            "window_start": run.window_start.isoformat(),
            "window_end": run.window_end.isoformat(),
            # What the lifecycle machine observed, restated in one token.
            "withdrawal_basis": withdrawal_basis(reason, run.intent),
            # **beta.35: the deadline the withdrawal was judged against.** The
            # 2026-08-29 capture could not be audited from the download at all,
            # because a run's own ``stale_after`` disappears with the run -- the
            # published figure belongs to the admitting *publication*, and the two
            # differ by exactly the amount that made the reset look impossible.
            "stale_after": run.stale_after.isoformat(),
            "affirmed_at": run.affirmed_at.isoformat(),
            "ended_branch": reason,
            "rule": (
                "the last carried run that ended, and why. session-local and not "
                "persisted, so a restart forgets it rather than restating a stale "
                "claim. written only when a run actually ends, never on an "
                "affirmation or an ordinary refresh. withdrawal_basis restates the "
                "branch that fired and is diagnostics only -- it is not a signal "
                "Stage A sent, because the contract carries none"
            ),
        }

    @callback
    def _off_report(self, now: datetime) -> dict[str, Any]:
        """Return the Off payload, having first stopped a dispatch of our own.

        Deliberately narrow. Off does not run Stage A, does not run the economics,
        does not admit a run and does not sustain one -- it reads the control surface
        and the persisted record only far enough to answer one question: is there a
        dispatch here that Alpha EMS started and still owns?

        If there is, the reason is not in doubt and needs no state machine to derive:
        the user selected Off while we were charging, so the reason is
        ``user_switched_off`` and the response is the full charge reset. If there is
        not -- the common case, and every refresh after the first -- nothing is read
        further and nothing is written.
        """
        snapshot = read_snapshot(self.hass)
        # **One construction of the evidence, shared by every reader.** A second
        # copy here would be a second place the signed readback could be
        # forgotten -- which is exactly how the degraded state became unreachable
        # the first time.
        evidence = self._evidence_for(snapshot, now)
        ownership = ownership_of(evidence)
        reset_action = self._owned_run_action()
        commands: tuple[Any, ...] = ()
        decision = ExecutionDecision(False, REFUSE_MODE_NOT_ACTIVE)
        source = None

        stage_one: tuple[Any, ...] = ()
        stage_two: tuple[Any, ...] = ()
        verify: str | None = None

        if ownership in (OWNERSHIP_OWNED, OWNERSHIP_DEGRADED):
            stage_one = plan_dispatch_stop()
            # A degraded stop is the narrow one: the cleanup is withheld until a
            # later refresh can verify the dispatch really stopped.
            stage_two = (
                () if ownership == OWNERSHIP_DEGRADED else plan_dispatch_cleanup()
            )
            verify = EXECUTION_VERIFY_DISPATCH_INACTIVE
            commands = stage_one + stage_two
            refusal = action_refusal(reset_action, commands) if commands else None
            if refusal is not None:
                commands = ()
                stage_one = stage_two = ()
                verify = None
            decision = authorize_reset(
                ownership=ownership,
                stopping_action=reset_action,
                stop_reason=EXECUTION_STOP_SWITCHED_OFF,
                steps_planned=len(commands),
                intent=self._executing_intent(),
            )
            source = "off_reset"
        elif stale_marker(evidence):
            # A marker with nothing behind it. Clearing it is not an ownership claim
            # and must not be blocked by the mode, or a marker left on by a crash
            # would latch there for as long as the user stays in Off.
            commands = plan_release_marker()
            # Nothing is running, so there is nothing to verify and nothing to
            # withhold: the release is the whole operation.
            stage_two = commands
            decision = authorize_marker_release(
                marker_is_stale=stale_marker(evidence),
                steps_planned=len(commands),
            )
            source = "off_marker_release"

        self._pending_commands = commands
        self._pending_stage_one = stage_one
        self._pending_stage_two = stage_two
        self._pending_verify = verify
        self._pending_power_kw = None
        self._pending_command = None
        self._pending_snapshot = snapshot
        self._pending_is_reset = bool(commands)
        self._pending_is_emergency = False
        self._pending_activates = False
        self._pending_deadline = None
        self._pending_run_id = None

        return {
            "mode": CONTROL_MODE_OFF,
            "state": CONTROL_STATE_OFF,
            "execution_available": CONTROL_EXECUTION_AVAILABLE,
            "execution_enabled": self.config.control_execution_enabled,
            "authorization": decision.as_dict(),
            "commands_planned": len(commands),
            "device": snapshot.as_dict(),
            "ownership": {"state": ownership},
            "write_boundary": {
                "action": reset_action if source == "off_reset" else None,
                "source": source,
                "stage_one": [step.as_dict() for step in stage_one],
                "stage_two": [step.as_dict() for step in stage_two],
                "stage_verification": verify,
                "steps": [step.as_dict() for step in commands],
                "stop_reason": (
                    EXECUTION_STOP_SWITCHED_OFF if source == "off_reset" else None
                ),
            },
            "off_semantics": (
                "off means this integration attempts no control and starts nothing. "
                "it does stop a dispatch it started itself and still owns, once, "
                "because the alternative is a charge the user cannot switch off. a "
                "foreign or unproven dispatch is never touched, and after the "
                "cleanup lands every refresh writes nothing"
            ),
            "execution_scope": _EXECUTION_SCOPE,
        }

    @callback
    def _owned_run_action(self) -> str | None:
        """Return the battery action the persisted record says we armed.

        **The authoritative source for what a reset stops**, and the only one that
        is available when a reset is needed. Every alternative is absent on at least
        one real stop path: the current command is ``None`` on all of them, the
        carried run is ``None`` on a withdrawal, and the ended run is absent on a
        mode change, a safety stop or a dead-man failure.

        This one is present by construction. ``ownership == owned`` requires
        ``record_matches``, which requires the record to name the run being executed
        -- so whenever a reset is entitled to run, the record exists and describes
        what Alpha EMS armed.

        ``None`` when the record has no usable action, and the caller must fail
        closed. Never inferred from anything else and never defaulted.
        """
        record = self.store.execution_record
        if not isinstance(record, dict):
            return None
        action = record.get("action")
        if isinstance(action, str) and action:
            return action
        # A record written before the action was stored. Derived from the intent it
        # does carry rather than guessed -- the mapping is total and fails closed.
        return action_for_intent(record.get("intent"))

    @callback
    def _owned_run_id(self) -> str | None:
        """Return the run a persisted causal record claims, if any."""
        record = self.store.execution_record
        if not isinstance(record, dict):
            return None
        run_id = record.get("run_id")
        return run_id if isinstance(run_id, str) and run_id else None

    @callback
    def _authority_run_id(self) -> str | None:
        """Return the run id whatever authority is executing, or ``None``.

        **beta.35, and it exists so three call sites cannot answer differently.**
        beta.34 taught the *arm* that an admitted plan is an authority in its own
        right and left the *sustain* comparing against ``self._carried`` alone --
        so on every ordinary beta.29 quarter-authority refresh the sustain saw
        ``None``, could not match, and fell through. The dispatch was armed by one
        rule and refused continuation by another.

        The arm, the ownership claim and the sustain now all read this. Same order
        of preference as ``_claim_authority``, and deliberately the same order: the
        carried run when there is one, the admitted plan otherwise.
        """
        if self._carried is not None:
            return self._carried.run_id
        plan = self._plan
        return None if plan is None else plan.run_id

    @callback
    def _opened_row_owns(self, now: datetime, carried: Any) -> bool:
        """Return whether a frozen row of *this run's* schedule is open at ``now``.

        **The fact ``carry_forward`` cannot compute, and beta.38's whole premise.**
        Stage A's horizon head is ``elapsed + 1``, so no publication issued once a
        row has opened can describe it; asking one to affirm it is asking the
        impossible, and for a run's final row it is impossible by construction. The
        answer therefore has to come from the frozen schedule instead.

        **Read from the plan, never from ``carried.window_start``.** A run whose
        schedule has been torn down must not keep itself alive on its own window:
        that is the degraded run-level fallback -- no admitted quarter, no per-row
        grid ceiling, no completed-row record -- which cost 9.889 of 16.11 kWh on
        2026-08-30. So this asks the schedule, and it asks whether the schedule is
        *this* run's.

        Called before ``carry_plan_verbose`` re-derives the plan, which is correct
        rather than merely tolerable: an opened plan is immutable and
        ``carry_plan_verbose`` returns it unchanged, so the object read here is the
        object that will still be there afterwards.

        Every clause is a bound: a plan, belonging to this run, not abandoned,
        opened, not ended, and covering this instant with a row.
        """
        if carried is None:
            return False
        plan = self._plan
        if plan is None or plan.run_id != carried.run_id:
            return False
        if self._admission_abandoned(plan):
            return False
        return bool(
            plan.has_opened(now)
            and now < plan.ends_at
            and plan.row_covering(now) is not None
        )

    @callback
    def _plan_authority_holds(self, now: datetime) -> bool:
        """Return whether the frozen schedule still owns this instant.

        **The rule ``carry_plan`` has stated since beta.29, applied where the stop
        is decided.** That function already refuses to re-derive, re-price or
        expire an opened plan before its own end, because Stage A's horizon head is
        ``elapsed + 1`` and no publication issued after a row opened can describe
        it. The stop path never honoured it, so a revision of the *future* aborted
        a row that was already frozen and already running.

        Every clause is a bound, and together they are why this cannot become
        indefinite execution: the plan must have opened, it must not have ended, it
        must actually cover this instant with a row, a quarter must be derived from
        it, something must already have been armed under its identity, and the
        campaign must not already have been abandoned. The vendor dead-man bounds it
        once more from outside, because it is re-armed only while the sustain
        actually runs.

        **The clause that asked the impossible, and what replaced it. beta.38.**

        Two drafts got this wrong in the same way, one refresh apart, and the
        history is worth keeping because the shape recurs.

        A first draft asked ``self._campaign_started_at is not None``. That is wrong
        by one refresh: the campaign lifecycle is advanced at the *end* of the report
        (``_note_campaign_progress``) and the stop is decided in the middle of it, so
        on the first refresh *after* an arm the campaign had not started yet.

        beta.29 replaced it with the persisted claim -- ``recorded == authority``,
        read as *"this schedule has armed something"*. That is wrong by one refresh
        in a **worse** place: the claim is written **by** an arm, at the write
        boundary, *after* the stop is decided in the same refresh. So on the refresh
        a row **opens** -- the first refresh that can arm anything -- no claim exists,
        the frozen schedule had no authority at all, and the withdrawal stood.
        Measured on 2026-09-01: ``record_present: false``, ``record_matches: false``,
        ``plan_authority_holds: false``, a ``stage_a_hold`` terminal filed against a
        4.53 kWh Sell, and 9.7 kW armed by the same refresh. A reset was avoided only
        because ``ownership_of`` answers ``none`` while the dispatch is still
        inactive -- with the marker already on it would have torn the campaign down.

        **So the question changed.** "Has this schedule already armed something?"
        cannot be answered on the refresh that arms. "Has something else been armed
        under a *different* authority?" can, always, and it is the question that
        actually matters: a record naming another run means this plan is not what is
        running and must not speak for it. No record at all means nothing else owns
        anything, and the opened frozen row is the only authority there is.

        Nothing is weakened. A foreign claim still refuses. And a plan with no claim
        can only ever *withhold* a withdrawal: ``resetting`` requires ``owned``, which
        requires ``record_matches``, so an authority proven this way can never itself
        stop a dispatch.

        Every clause is a bound, and together they are why this cannot become
        indefinite execution: the plan must have opened, it must not have ended, it
        must actually cover this instant with a row, a quarter must be derived from
        it, nothing else may be armed under another identity, and the campaign must
        not already have been abandoned. The vendor dead-man bounds it once more from
        outside, because it is re-armed only while the sustain actually runs.
        """
        plan = self._plan
        if plan is None or self._quarter is None:
            return False
        # Bound to locals before either is compared: Phase 4 forbids a comparison
        # whose text contains "owned", because that is what an ownership
        # *derivation* looks like. This is an identity read of a record Alpha EMS
        # wrote itself, and ``ownership_of`` remains the only thing that decides
        # ownership.
        recorded = self._owned_run_id()
        authority = self._authority_run_id()
        not_armed_under_another = recorded is None or recorded == authority
        return bool(
            plan.has_opened(now)
            and now < plan.ends_at
            and plan.row_covering(now) is not None
            and not_armed_under_another
            and not self._admission_abandoned(plan)
        )

    @callback
    def _claim_authority(self, run: Any) -> Any:
        """Return whatever may write the ownership claim for this arm, or ``None``.

        The carried run when there is one -- unchanged, and still the common case.
        Otherwise the admitted plan, which since beta.29 is a Stage-A authority in
        its own right: frozen at admission, immutable afterwards, and the thing the
        open row was derived from.

        ``None`` is the fail-closed answer and it means exactly one thing: this
        command came from no surviving authority at all. The caller sends nothing,
        not even stage one. Deliberately not "synthesise a run": an arm whose
        authority has genuinely gone is an arm that must not happen, and inventing
        an identity for it would be forging the record rather than writing it.
        """
        if run is not None:
            return run
        plan = self._plan
        if plan is None:
            return None
        # The row this claim is being made for. Without one there is nothing open
        # to execute and the plan is not an authority for anything right now.
        quarter = self._quarter
        if quarter is None:
            return None
        return _PlanAuthority(
            run_id=plan.run_id,
            plan_id=plan.plan_id,
            revision=plan.revision,
            intent=plan.intent,
            target=plan.target,
            admitted_at=plan.admitted_at,
            # **Never re-affirmed, and the record says so.** The publication that
            # would have affirmed it is the one that structurally cannot describe
            # an open row. Copying ``admitted_at`` here is the honest reading:
            # this authority is exactly as old as its admission.
            affirmed_at=plan.admitted_at,
            # **The plan's end, not the row's. beta.35, and this one line cost
            # a hardware Sell.**
            #
            # beta.34 bounded the claim by ``quarter.quarter_end``, reasoning that
            # an arm may not outlive the row it was made for. That is true of the
            # *arm*, which is reissued every quarter -- and false of the *claim*,
            # which has to survive the boundary for the next refresh to adopt it
            # and hand over. Persisted at the row's end, the record was already
            # expired by the time anything read it: ``_adopt_persisted_run``
            # rehydrated it into a ``CarriedRun`` whose deadline *was* the instant
            # the refresh had fired at, ``carry_forward``'s first guard read
            # ``stale_plan``, and a live export was reset 5.9 s into its second
            # quarter. Structural, not jitter -- it could never have been inside
            # that deadline.
            #
            # The plan's own end is the honest bound: it is frozen at admission,
            # immutable afterwards, and it is exactly how long this authority
            # legitimately lasts.
            stale_after=plan.ends_at,
        )

    @callback
    def _write_execution_record(
        self, run: Any, command: Any, snapshot: Any, now: datetime
    ) -> None:
        """Persist the causal record for a dispatch about to be armed.

        Written **before** the writes, so a failure mid-sequence leaves a record
        beside a marker and no dispatch -- recognisable, and clearable. The
        alternative ordering would leave a dispatch running that nothing could
        prove was ours.

        **The dispatch start is deliberately left empty**, and beta.24 made that
        explicit rather than incidental. This runs before the dispatch (re)starts,
        so whatever the register says now belongs to the *previous* activation --
        recording it would tie the claim to the wrong dispatch, which on a sustaining
        re-arm is precisely the dispatch that is about to be replaced.
        ``_stamp_dispatch_start`` completes the record from the readback once the
        dispatch it caused actually exists.

        Ownership is only granted later, when that readback lands -- so this cannot
        claim a dispatch Alpha EMS did not cause.
        """
        del snapshot
        self.store.execution_record = {
            # **The run itself, since beta.24, so a restart can adopt it.**
            # Without these a restart met a live dispatch of ours with a freshly
            # minted run id, ownership read ``unproven`` for a contradiction rather
            # than a real doubt, and the charge could be neither continued nor
            # stopped. Stored as the published payload so the reconstruction is a
            # round trip rather than a summary -- a serialiser that dropped the
            # headroom cap would restore a run allowed to charge past its ceiling.
            # **Present whenever the authority has one.** A carried run always
            # does. An admitted plan does too in production -- ``admit_plan``
            # keeps the publication it was admitted from -- and ``None`` here is
            # the defined answer ``carried_from_record`` already handles by
            # declining to adopt after a restart. Live ownership does not consult
            # it: that is ``run_id``, ``quarter_start``, ``admitted_plan_id`` and
            # the ``dispatch_start`` readback.
            "target": (None if run.target is None else target_as_published(run.target)),
            "admitted_at": run.admitted_at.isoformat(),
            "affirmed_at": run.affirmed_at.isoformat(),
            "stale_after": run.stale_after.isoformat(),
            # The run identity, so the record still names the right run after the
            # horizon has rolled several times and every published ``plan_id`` has
            # changed. The admitting publication is kept beside it for tracing.
            "run_id": run.run_id,
            "plan_id": run.plan_id,
            "revision": run.revision,
            "intent": run.intent,
            # **What a later reset will stop.** Derived once, here, so the stop path
            # reads a record of what was armed rather than reconstructing it from a
            # command that no longer exists.
            "action": action_for_intent(run.intent),
            "power_kw": None if command is None else command.power_kw,
            "cutoff_soc_percent": (
                None if command is None else command.cutoff_soc_percent
            ),
            "duration_minutes": None if command is None else command.duration_minutes,
            "written_at": now.isoformat(),
            "dispatch_start": None,
            # **The quarter this claim is for**, so a claim can never be reused to
            # justify a dispatch belonging to a different one. Ownership compares it
            # against the row Stage B is actually executing.
            "quarter_start": (
                None
                if self._quarter is None
                else self._quarter.quarter_start.isoformat()
            ),
            # **A stable identity for the physical claim itself.** The run id names
            # an economic run and legitimately outlives several arms; this names one
            # arm, so progress can be keyed to it and a later claim cannot inherit an
            # earlier one's measurements.
            "claim_id": _claim_id(run.run_id, now),
            # **The admitted plan this claim belongs to.** Ownership compares this,
            # not the row: one dispatch session spans every row of its plan, so a
            # row comparison would break ownership at each boundary.
            "admitted_plan_id": None if self._plan is None else self._plan.plan_id,
            "schema": CLAIM_SCHEMA_VERSION,
        }
        self.store.schedule_save()

    @callback
    def _deadman_is_stale(self, snapshot: Any, run_id: str | None) -> bool:
        """Return whether the last re-arm failed to move the device dead-man.

        **A measurement, not an assumption, and that is the whole point.** The one
        physical behaviour beta.24 could not verify in advance is whether
        re-activating an already-active dispatch refreshes the helper timer. Rather
        than depending on it, each sustain records the deadline it saw and the next
        refresh checks that it moved.

        Conservative in both directions. It answers ``False`` unless there really is
        a previous sustain for *this* run to compare against -- a missing timer
        reading is "no evidence it advanced", which must not become "it failed",
        because that would stop a healthy run on a temporarily unavailable entity.
        And it answers ``True`` only on a deadline that has not moved forward at all.
        """
        if run_id is None or self._sustained_run_id != run_id:
            return False
        previous = self._sustained_deadline
        if previous is None:
            return False
        current = None if snapshot is None else snapshot.dispatch_timer_finishes_at
        if current is None:
            return False
        return current <= previous

    @callback
    def _power_moved_materially(self, command: Any) -> bool:
        """Return whether the power helper needs rewriting.

        Measured against the power actually **written**, not against the power last
        computed, and on the *quantised* figure the device would receive -- two
        rolling requests that differ by less than one device step are the same
        command as far as the inverter is concerned.

        The deadband is :data:`CONTROL_MIN_POWER_KW`, which already means "smaller
        than this is not a command" everywhere else in this integration. A second
        threshold with its own constant would be a second thing to keep in step.

        No previous write means there is nothing to compare against, so the answer
        is yes: that is an arm, not a sustain.
        """
        last = self._last_control_power_kw
        if last is None:
            return True
        return abs(float(command.power_kw) - float(last)) >= CONTROL_MIN_POWER_KW

    @property
    def activation_confirmed(self) -> bool:
        """Return whether an activation write succeeded on this refresh.

        Read by the Activity surface, which may only say a run *started* when a
        command carrying an activation actually landed. True for one refresh.
        """
        return self._activation_confirmed

    @callback
    def _adopt_persisted_run(self, snapshot: Any, now: datetime) -> None:
        """Take up the run a live dispatch belongs to, if the record can prove it.

        Called on every refresh and does nothing on almost all of them: it acts
        only when nothing is carried *and* a dispatch is running *and* the marker is
        on -- which together is the restart case and very little else.

        **When the record cannot prove which run is running, nothing happens here,
        and that silence is the design.** A record written before beta.24 carries no
        target to rebuild. The tempting next step is to stop the dispatch anyway, and
        it is exactly the step this project has refused since Phase 4: a reset is a
        physical write, and issuing one against a dispatch whose provenance we cannot
        establish is the thing the foreign/unproven rule exists to prevent. So the
        charge is left to the device dead-man, ownership reports ``unproven``, the
        marker is **not** released -- releasing it would assert a conclusion we do
        not have -- and the evidence is reconciled later by the ordinary stale-marker
        path, once there is nothing running behind it.
        """
        self._adopted_this_refresh = False
        if self._carried is not None:
            return
        if snapshot is None or not snapshot.dispatch_active:
            return
        if not snapshot.owner_marker:
            return
        adopted = carried_from_record(self.store.execution_record)
        if adopted is not None:
            self._carried = adopted
            self._adopted_this_refresh = True
            # **An adopted dispatch is stopped, not continued.** beta.26 adopted the
            # run and carried on, which was sound while execution was measured
            # against a whole run: the run-level progress is reconstructible from
            # the record. A *quarter* is not -- ``CarriedQuarter`` is deliberately
            # not persisted, and its measured totals live only in memory.
            #
            # So after a restart the delivered energy inside the open quarter is
            # unknown, and the three options are: continue against unknown progress
            # and risk delivering the quarter twice; leave the device running while
            # sending nothing and rely on the vendor dead-man; or stop and wait for
            # the next admitted quarter. Only the third is both safe and honest, and
            # the cost is bounded -- at most the remainder of one quarter, which
            # Stage A will re-plan at the next boundary.
            #
            # This is *not* a relaxation of the foreign/unproven rule. It fires only
            # where ownership is provable, which is what the marker-and-record
            # guards above already established; an unprovable dispatch still gets
            # zero writes.
            #
            # **But only where a measurement was actually lost. beta.35.** All of
            # the above is an argument about a *restart*, and beta.34 applied it to
            # every adoption -- including the ordinary quarter boundary, where the
            # coordinator has been running throughout, ``self._plan`` still covers
            # the row and the quarter accumulators are intact. Nothing was lost
            # there, and saying it was forced a reset independently of any stop
            # reason (see the ``progress_unknown`` term in the control report), so
            # even a corrected staleness rule would still have aborted the run.
            #
            # A live plan that names the adopted run is the evidence that this is a
            # hand-over rather than a reboot.
            plan = self._plan
            continuous = (
                plan is not None
                and plan.has_opened(now)
                and now < plan.ends_at
                and adopted.run_id == plan.run_id
            )
            self._quarter_progress_unknown = not continuous

    @callback
    def _stamp_dispatch_start(self, evidence: Any, now: datetime) -> bool:
        """Complete our own record with the dispatch it caused, once.

        **The narrowest thing that makes a Live charge stoppable.** The record is
        written *before* the writes, so the device reports no ``dispatch_start``
        yet and the record stores ``None``. Requiring an exact match from there is
        unsatisfiable: ownership would never leave ``unproven``, ``reset_required``
        is gated on ``owned``, and Alpha EMS would arm a charge it could never stop.

        Four conditions, and every one of them is doing work:

        * the record is **ours** and names the run being executed -- so this is
          completing a claim, not making one;
        * it carries no start yet -- so a completed record is never rewritten, and
          the exact comparison governs from then on;
        * the marker is on and a dispatch is running -- the two factors, unchanged;
        * the dispatch **began when we wrote** -- so one that merely happens to be
          running now cannot be adopted. Measured between the write and the device's
          own start instant, which is the pair that is actually causally linked.

        Returns whether anything was stamped, so the caller can say so.
        """
        record = self.store.execution_record
        if not isinstance(record, dict) or record.get("dispatch_start") is not None:
            return False
        observed = evidence.dispatch_start
        # **Asked of the evidence rather than re-derived here**, so the stamp and the
        # ownership rule cannot disagree about what a settling claim is. Two copies
        # of that test is one too many, and the copy that drifted would be this one.
        if observed is None:
            return False
        # **Any provenance will do, not only the settle path.** Until beta.30 this
        # required ``settling``, and ``settling`` required the dispatch-start
        # register -- so one unvalidated assumption about that register foreclosed
        # the *stronger* ``exact`` proof as well, permanently. The two factors must
        # be independently reachable, which is what this restores.
        if evidence.record_provenance is None:
            return False
        record["dispatch_start"] = observed.isoformat()
        record["stamped_at"] = now.isoformat()
        self.store.execution_record = record
        self.store.schedule_save()
        return True

    @callback
    def _note_release_receipt(self, snapshot: Any, now: datetime) -> None:
        """Record what the register showed as our own dispatch was released. beta.43.

        **Written immediately before ``_clear_execution_record``, and that ordering
        is the whole design.** Clearing the record is what destroys
        ``record_causation_holds``, and without it a dispatch still draining the
        vendor dead-man reads ``foreign`` -- about a run Alpha EMS armed and has
        just stopped. The receipt is the one fact that survives, and it is deliberately
        the *observed* deadline rather than a duration we assume.

        Two things are kept, and both are load-bearing:

        * ``run_id`` -- which run this release ended, so a receipt can be attributed;
        * ``timer_finishes_at`` -- the register's own dead-man instant, which is
          later compared against the live one. A dispatch armed by somebody else
          after our release carries a *different* deadline, so the comparison is
          what stops the receipt from excusing a genuinely foreign run.

        A snapshot without a readable timer writes **no receipt at all**: the tail is
        then indistinguishable from a foreign dispatch on the evidence available, and
        inventing a fixed grace period would be exactly the guess this refuses.
        """
        finishes = None if snapshot is None else snapshot.dispatch_timer_finishes_at
        if finishes is None:
            self._release_receipt = None
            return
        self._release_receipt = {
            "run_id": self._execution_identity(),
            "released_at": now.isoformat(),
            "timer_finishes_at": finishes.isoformat(),
        }

    @callback
    def _clear_execution_record(self) -> None:
        """Forget the causal record. Called when an owned run is reset."""
        if self.store.execution_record is None:
            return
        self.store.execution_record = None
        self.store.schedule_save()

    @callback
    def _accrue_grid_attribution(
        self, moment: datetime, charge_w: float | None
    ) -> None:
        """Accumulate grid energy attributed to charging the pack.

        **An attribution estimate, not a metered channel.** No such channel exists:
        the meter reports one net figure and cannot say which electron reached the
        battery. So the pack's charge is attributed to production surplus first and
        the grid second --

            pv_surplus = max(0, pv - house load)
            grid_share = max(0, battery charge - pv_surplus)

        -- which is :func:`split_grid_energy`'s arithmetic inverted, over the same
        measured quantities, in the same order Stage A used to compute
        ``marginal_grid_import_kwh``. So the ceiling is enforced in the terms it was
        published in.

        This must be measured in **grid** energy rather than battery energy.
        Measured on the real installation, commanding 1.0 kW produced 1.135 kW of
        *total* battery charge while production was already supplying part of it --
        the helper commands the total rate and subsumes ambient charging. Counting
        battery energy would charge the sun against a grid allowance.

        Four defensive properties, each deliberate:

        * **monotonic** -- every increment is ``max(0, ...) * dt >= 0``, so a
          spuriously high production reading can only reduce an increment to zero,
          never below, and the total can never fall;
        * **gaps accrue nothing** -- the same tolerance ``QuarterAccumulator``
          uses; a silence longer than that contributes no energy rather than
          extrapolating the last reading across it;
        * **conservative on disagreement** -- where the readings are incoherent the
          production surplus is taken as *zero*, attributing more to the grid, so
          the ceiling binds earlier. A budget exists to bound buying;
        * **reset only at the plan boundary**, never per quarter and never on a
          revision bump.
        """
        previous = self._grid_attribution_at
        self._grid_attribution_at = moment
        if previous is None or charge_w is None:
            return
        seconds = (moment - previous).total_seconds()
        if seconds <= 0.0 or seconds > MAX_SAMPLE_GAP_SECONDS:
            # Out of order, or a silence too long to integrate across.
            return
        measured_kw = self._budget_surplus_kw()
        if measured_kw is None:
            # Incoherent or implausible inputs: attribute the whole charge to the
            # grid, which is the conservative direction for a ceiling on buying.
            surplus_w = 0.0
        else:
            surplus_w = measured_kw * 1000.0
        grid_w = max(0.0, charge_w - surplus_w)
        self._execution_grid_kwh += grid_w * seconds / 3_600_000.0

    @callback
    def _execution_progress(self, run_id: str, plan: Any) -> Any:
        """Return delivered battery energy inside ``target``'s window.

        Two bases, because neither alone is enough. The accumulator integrates
        measured battery power and is the better figure within a quarter; the
        state-of-charge difference is coarser but is the only basis that survives
        a restart. Both are reported, and where they disagree the disagreement is
        published rather than resolved -- picking one silently would hide exactly
        the case a reader needs to see.

        Never ``setpoint x elapsed``. That is what the inverter was *asked* for,
        and a clamp, a limit, a cloud or a full pack each make it a different
        number from what arrived.
        """
        stored = None if plan is None or plan.state is None else plan.state.energy_kwh
        if self._execution_run != run_id:
            # A genuinely different run -- not merely a new publication of this one,
            # and not a revision. Energy already in the pack is not un-delivered by
            # either.
            self._execution_run = run_id
            self._execution_window_start_kwh = stored
            self._execution_closed_kwh = 0.0
            self._execution_grid_kwh = 0.0
            if self._battery_charge_accumulator is not None:
                self._battery_charge_accumulator.reset()

        accumulated = None
        coverage = None
        if self._battery_charge_accumulator is not None:
            accumulator = self._battery_charge_accumulator
            if accumulator.started:
                # Closed quarters plus the quarter in flight. Reading the open
                # quarter alone is what sawtoothed.
                accumulated = self._execution_closed_kwh + accumulator.open_energy_kwh
                coverage = accumulator.open_coverage
            else:
                accumulated = self._execution_closed_kwh or None

        soc_delta = None
        opening = self._execution_window_start_kwh
        if stored is not None and opening is not None:
            # **Signed by what the run is for, since beta.35.** ``max(0, stored -
            # opening)`` is a charge's arithmetic, and it was applied to every run:
            # on a discharge the pack falls, the difference is negative, and the
            # clamp returned exactly zero. Together with the charge-only power
            # accumulator above it meant **every export campaign ever run reported
            # 0.0 kWh realised at run level** -- which is what published
            # ``5.75 / 0.0 / 5.75`` for a sale that had physically moved 2.211 kWh.
            soc_delta = (
                max(0.0, opening - stored)
                if self._run_is_discharge()
                else max(0.0, stored - opening)
            )

        return measure_progress(
            accumulated_kwh=accumulated,
            soc_delta_kwh=soc_delta,
            current_quarter_kwh=accumulated,
            coverage=coverage,
            minimum_coverage=MIN_QUARTER_COVERAGE,
            reconstructed=opening is None,
        )

    @callback
    def _run_is_discharge(self) -> bool:
        """Return whether the executing authority moves energy *out* of the pack."""
        intent = None
        if self._quarter is not None:
            intent = self._quarter.intent
        elif self._plan is not None:
            intent = self._plan.intent
        elif self._carried is not None:
            intent = self._carried.intent
        return intent == EXECUTION_INTENT_NET_EXPORT

    @callback
    def _objective_progress(self, plan: Any) -> tuple[float, str] | None:
        """Return the run's realised objective and the boundary it is measured at.

        **One source of truth, shared with the campaign. beta.35.**

        The objective of a charge is battery energy and the objective of an export
        is meter energy -- the quarter machinery has known that since beta.32 and
        chooses between them explicitly. The *run* level did not: it measured
        battery charge whatever the run was for, so an export's realised figure was
        structurally zero and every surface downstream inherited it.

        Where a campaign is open and belongs to this run, its accumulator is the
        answer: it already sums the completed quarters at the right boundary and
        adds the quarter in flight. Preferring it means the run total and the
        campaign total cannot disagree, which is a stronger guarantee than
        computing the same thing twice and hoping.

        ``None`` where no campaign is open, and the caller falls back to the
        battery-side measurement, which is correct for a charge and is what every
        pre-campaign run had.
        """
        del plan
        if self._campaign_id is None:
            return None
        boundary = self._campaign_boundary or CAMPAIGN_BOUNDARY_BATTERY
        return self._campaign_realized_now(), boundary

    @callback
    def _remaining_expected_pv_kwh(self, target: Any, now: datetime) -> float | None:
        """Return how much of Stage A's expected production is still to come.

        Pro-rated across the window by elapsed time. Crude on purpose: a finer
        split would need the per-interval production forecast, and reaching for
        that here would put Stage B one short step from forming its own view of
        what production is worth. The figure is only ever used to *reduce*
        charging, so a coarse estimate errs toward charging less rather than more.
        """
        expected = target.expected_pv_to_battery_kwh
        if expected is None:
            return None
        total = (target.window_end - target.window_start).total_seconds()
        if total <= 0.0:
            return 0.0
        remaining = max(0.0, (target.window_end - now).total_seconds())
        return max(0.0, expected * min(1.0, remaining / total))

    @callback
    def _stage_b_report(
        self,
        *,
        plan: Any,
        snapshot: Any,
        now: datetime,
        mode: str,
    ) -> dict[str, Any]:
        """Run the Stage B controller and return what it concluded.

        **Computes the command a Live run would send, and sends nothing.** There
        is one calculation path: the mode is passed in so the *lifecycle* can
        differ -- Shadow never acquires the owner marker -- but it does not reach
        the arithmetic, so the power computed here is the power a Live refresh
        would compute from the same inputs.
        """
        # Carry-forward first, because everything below is keyed on its verdict.
        # This reads *this* refresh's targets -- the ordering pinned in beta.19 --
        # so the run being evaluated is never a refresh behind the publication that
        # affirmed it.
        # **Adopt before carrying, on the first refresh after a restart.**
        # A restart discards the carried run and keeps the record. Carrying from
        # ``None`` would mint a *new* run id against a dispatch that is still
        # running, so ownership would read ``unproven`` for a contradiction rather
        # than a real doubt -- and the charge could be neither continued nor
        # stopped. Adopting first makes the run we are executing the run we started.
        #
        # Only while a dispatch is actually running: a record left behind by a
        # completed run must not resurrect it.
        self._adopt_persisted_run(snapshot, now)
        # **Both executable intents, not just the charge.** ``carry_forward``
        # defaults to charge-only, and beta.27 left the default in place -- so a
        # ``net_export`` run was never carried at all. Two consequences on real
        # hardware, and the second was the visible one:
        #
        # * no ``CarriedRun``, so no export could ever be admitted; and
        # * ``stage_b_holds_the_run`` stayed false, which un-suppressed the Phase-3
        #   reserve-guard fallback -- and that layer only ever discharges, so it
        #   produced a discharge into the house that ``evaluate`` correctly refused
        #   with ``would_export``. The reported inhibition was real and correct; it
        #   was describing a command Stage B never wanted.
        # **The run layer and the plan layer consult the same two facts. beta.36.**
        # Without this the run layer minted a fresh run from a target still naming a
        # torn-down campaign while the plan layer destroyed that run's plan on the
        # same refresh, once every fifteen minutes, for the rest of the session.
        outcome = carry_forward(
            self._carried,
            self.execution_targets,
            now,
            executable_intents=CONTROL_LIVE_DISPATCH_INTENTS,
            abandoned_admissions=frozenset(self._abandoned_admissions),
            final_campaigns=frozenset(self._final_campaigns),
            # **beta.38.** An opened frozen row is not withdrawn because Stage A's
            # new horizon cannot describe it. Computed here because only this layer
            # can see the schedule; see :meth:`_opened_row_owns`.
            row_open=self._opened_row_owns(now, self._carried),
        )
        self._carried = outcome.carried
        self._carry_refusal = outcome.refused
        # **The quarter is carried in its own right, and after the run.** After,
        # because a quarter admitted this refresh should be able to name the run
        # that authorised it; in its own right, because a run ending at a boundary
        # used to leave the quarter after it with no carrier at all -- which is R1,
        # and is why this cannot be a field on ``CarriedRun``.
        #
        # An **open** quarter is returned unchanged by ``carry_quarter`` and is not
        # re-derived here either: Stage A's horizon head is ``elapsed + 1``, so no
        # publication issued after ``quarter_start`` can contain the open quarter,
        # and any rule claiming to re-describe it would be inferring from evidence
        # that structurally cannot exist.
        previous_quarter = self._quarter
        # **The whole schedule is carried; the row is derived.** See
        # ``_refresh_executing_quarter`` for why a carried row could not work.
        self._admission_refusal = None
        self._plan, refusal = carry_plan_verbose(
            self._plan,
            self.execution_targets,
            now,
            run=self._carried,
            frozen_remaining_kwh=self._frozen_remaining_kwh(now),
            executable_intents=CONTROL_LIVE_DISPATCH_INTENTS,
        )
        self._admission_refusal = refusal
        # **After the derivation, because the derivation can itself refuse.** A plan
        # admitted here and destroyed one statement later by the abandonment latch is
        # the exact shape of both hardware incidents, and it must not read as "no
        # publication".
        self._refresh_executing_quarter(now)
        if previous_quarter is not None and (
            self._quarter is None
            or self._quarter.quarter_start != previous_quarter.quarter_start
        ):
            # It ended between ticks rather than on one -- record it before the
            # accumulators are rebased, or the history loses the quarter entirely.
            self._record_completed_quarter(
                previous_quarter,
                QUARTER_END_TARGET_REACHED
                if self._quarter_target_reached_at is not None
                else QUARTER_END_EXPIRED,
            )
            self._reset_quarter_progress(self._quarter)
        carried = outcome.carried
        if outcome.ended is not None and outcome.ended_run is not None:
            self._remember_ended_run(outcome.ended, outcome.ended_run, plan, now)
        # **The forward cap, replaced on every affirmation.**
        #
        # An admitted run's energy figures are immutable, which is what let a
        # Safety Buy admitted while tomorrow's prices were unknown keep delivering
        # after cheaper prices arrived. This is the second cap: what the *latest*
        # publication still wants, from *its* boundary onward.
        #
        # Replaced rather than accumulated, and that is what stops a healthy run
        # being trimmed: while its boundary is still ahead the cap is inactive, and
        # by the time the boundary passes a fresh affirmation has moved it on. It
        # bites in the case it exists for -- a publication that wants materially
        # less -- and when refreshes have stopped arriving.
        if outcome.affirmed or outcome.admitted:
            fresh = self._affirming_target(carried)
            if fresh is not None:
                self._forward = forward_authorisation(fresh)
        if carried is None:
            self._forward = None
        # One construction, shared. See ``_evidence_for``: the run identity comes
        # from the carried run, so a record naming a different run is
        # contradictory rather than merely old.
        evidence = self._evidence_for(snapshot, now, run_id=carried_run_id_of(carried))
        # Complete our own claim with the dispatch it caused, before the decision
        # reads ownership. Nothing else may write to the record here.
        stamped = self._stamp_dispatch_start(evidence, now)
        if stamped:
            evidence = replace(evidence, record=self.store.execution_record)
        # **On the refresh a run ends, report the run that ended.** Keyed on the
        # ended run, so the accumulators are read rather than reset -- otherwise the
        # closing figures read 0.00 against the target, which is what put "0.00 /
        # 8.06 kWh" in front of a run that had delivered 1.76.
        finishing = carried or outcome.ended_run
        progress = (
            measure_progress(accumulated_kwh=None, soc_delta_kwh=None)
            if finishing is None
            else self._execution_progress(finishing.run_id, plan)
        )
        decision = decide(
            # Shadow and off are both non-executing; only ``active`` would send.
            mode_executes=mode == CONTROL_MODE_ACTIVE,
            mode_off=mode == CONTROL_MODE_OFF,
            targets=self.execution_targets,
            now=now,
            evidence=evidence,
            progress=progress,
            current_energy_kwh=(
                None if plan is None or plan.state is None else plan.state.energy_kwh
            ),
            remaining_expected_pv_kwh=(
                None
                if carried is None
                else self._remaining_expected_pv_kwh(carried.target, now)
            ),
            grid_charged_kwh=self._execution_grid_kwh,
            configured_budget_kwh=self.config.grid_charge_budget_kwh,
            # The pack's own one-way figure, so the headroom allowance and the
            # projected stored energy cross the AC/DC boundary exactly once.
            # Stage B holds no efficiency of its own: a second copy of a physical
            # constant is a second thing to keep in step.
            charge_efficiency=(
                None
                if plan is None or plan.state is None
                else plan.state.limits.charge_efficiency
            ),
            running_run_id=self._owned_run_id(),
            carried=carried,
            carry_ended=outcome.ended,
            ended_run=outcome.ended_run,
            # Measured, not assumed: the last re-arm either moved the device
            # dead-man forward or it did not, and the run ends deliberately if not.
            deadman_stale=self._deadman_is_stale(
                snapshot, None if carried is None else carried.run_id
            ),
        )
        # Held so ``_stage_b_intent`` builds from the same decision this report
        # describes, rather than deciding twice and risking a disagreement.
        self._stage_b_decision = decision
        report = execution_as_dict(
            decision,
            mode=mode,
            executed=False,
            objective=self._objective_progress(plan),
        )
        report["actual_balance"] = self._execution_actuals(plan)
        report["safety"] = {
            "reserve_floor_kwh": (
                None if decision.target is None else decision.target.reserve_floor_kwh
            ),
            "stale": (
                None if decision.target is None else decision.target.stale_at(now)
            ),
            # **What this refresh will actually arm the dead-man to**, not a
            # configured figure. Through beta.32 both duration fields published the
            # user's "Command duration" setting -- which never reached the Dispatch
            # register at all -- so a reader comparing them against the device saw
            # two numbers that could not agree. That misreading is what sent the
            # beta.32 configuration audit down a false trail.
            "deadman_duration_minutes": self._duration_to_command(None, snapshot),
            "deadman_duration_basis": (
                "alternating_vendor_deadman"
                if self._executing_intent() in CONTROL_LIVE_DISPATCH_INTENTS
                else "advisory_helper_command"
            ),
            "deadman_alternation_minutes": list(DISPATCH_DEADMAN_MINUTES),
            "deadman_rule": (
                "the live Dispatch dead-man alternates between the two values "
                "above because the vendor automation triggers on the helper "
                "changing state -- writing the same duration twice re-arms "
                "nothing. it is a safety mechanism, not a horizon, and it is not "
                "user-configurable"
            ),
            "ownership_marker_entity": BOOLEAN_EXECUTION_OWNER,
            "ownership_marker": None if snapshot is None else snapshot.owner_marker,
            "record_present": self.store.execution_record is not None,
            "record_matches": evidence.record_matches,
        }
        # What the request becomes on the wire, and the device's own account of
        # what it is doing. There was previously **nothing** in this block to
        # compare a request against, so a Live day could not be read.
        power = report.get("power")
        if isinstance(power, dict):
            power["quantised_physical_power_kw"] = device_power_kw(
                max(0.0, decision.request_kw) * INTERVAL_HOURS, INTERVAL_HOURS
            )
        report["device_readback"] = {
            "dispatch_active": None if snapshot is None else snapshot.dispatch_active,
            "dispatch_mode": getattr(snapshot, "dispatch_mode", None),
            "dispatch_power_w": getattr(snapshot, "dispatch_power_w", None),
            "dispatch_time_s": getattr(snapshot, "dispatch_start", None),
            # The two halves a reader needs side by side: what we are about to
            # write, and what the device currently holds. During a Live run these
            # alternate 20 / 25 as each re-arm lands; between runs the readback
            # rests at the helper's own minimum, which is not a stale value.
            "commanded_duration_minutes": self._duration_to_command(None, snapshot),
            "readback_duration_minutes": (
                None if snapshot is None else snapshot.dispatch_duration_minutes
            ),
            "owner_marker": None if snapshot is None else snapshot.owner_marker,
            "rule": (
                "the raw dispatch surface reads signed: negative power is a "
                "charge. it is a readback and never an input to a command, "
                "because the helper families Alpha EMS writes take an unsigned "
                "magnitude with direction carried by the family"
            ),
        }
        latest = actionable_target(self.execution_targets, now)
        report["carried"] = {
            # **The whole bug was conflating these two**, so they are published
            # together and named differently. The publication is what Stage A said
            # this refresh; the run is what Stage B accepted and is executing.
            "publication": None
            if latest is None
            else {
                "plan_id": latest.plan_id,
                "intent": latest.intent,
                "window_start": latest.window_start.isoformat(),
                "window_end": latest.window_end.isoformat(),
            },
            "run": None if carried is None else carried.as_dict(),
            "affirmed_by_this_publication": outcome.affirmed,
            "admitted_this_refresh": outcome.admitted,
            "ended_reason": outcome.ended,
            # Truthful on the ending refresh only, which is why ``last_ended``
            # exists beside it.
            "last_ended": self._last_ended,
            "window_open": None if carried is None else carried.actionable_at(now),
            "rule": (
                "a published target always opens one interval from now, so the run "
                "whose window opens is the one admitted a refresh earlier. the "
                "admitted window is never moved by a later publication -- that is "
                "what makes activation reachable. a publication of the same intent "
                "whose window overlaps the accepted one re-affirms it; anything "
                "else withdraws it. this is an inference from instants, not a "
                "cancellation signal, because the contract carries none"
            ),
        }
        report["last_successful_write"] = (
            None
            if self._last_control_write is None
            else self._last_control_write.isoformat()
        )
        return report

    @callback
    def _execution_actuals(self, plan: Any) -> dict[str, Any]:
        """Return the measured side of the charge balance, for comparison.

        Every figure here has a Stage-A expectation beside it in ``target``. That
        is the point: a deviation should be readable rather than something a
        reader has to compute, and house load is present precisely because it
        explains the grid figure without ever entering the battery command.
        """
        flows = self.read_flows()
        state = None if plan is None else plan.state
        power = self._canonical_battery_power_w()
        return {
            "house_load_w": self._read_house_load_w(),
            "pv_production_w": self._read_pv_power_w(),
            "grid_import_w": None if flows is None else flows.grid_import_w,
            "grid_export_w": None if flows is None else flows.grid_export_w,
            "battery_power_w": power,
            "battery_charging": None if power is None else power > 0.0,
            "soc_percent": None if state is None else state.soc_percent,
            "current_headroom_kwh": (
                None if state is None else state.headroom_energy_kwh
            ),
            "stored_energy_kwh": None if state is None else state.energy_kwh,
            "balance_rule": (
                "house load and production explain the grid figure; neither is "
                "added to the battery charge setpoint. 3.7 kW of charging against "
                "1.1 kW of house and 0.63 kW of production draws 4.17 kW at the "
                "meter, and the meter figure is a consequence rather than a "
                "command"
            ),
        }

    @callback
    def _execution_targets(
        self,
        *,
        outcome: EconomicOutcome | None,
        plan: Any,
        today_interval_count: int,
        tz: tzinfo,
        issued_at: datetime,
    ) -> tuple[dict[str, Any], ...]:
        """Return the machine-readable targets the Stage B controller consumes.

        Built here because this is where the calendar is: a run is indexed by the
        plan's continuous chronological position, and turning that into an instant
        needs the civil day and its real length -- 92, 96 or 100 intervals. The
        optimizer deliberately has no calendar, which is why
        ``economic.execution_target`` takes instants rather than indices.

        Since beta.19 this also projects, for a charge, the window's energy
        balance and the headroom the plan needs preserved. **Both are readings of
        the trajectory the solve already chose** -- the per-interval production and
        baseline the horizon was built from, and the stored energy the plan lands
        on -- so publishing them cannot change a plan. A mutation test holds that.
        """
        if outcome is None or not outcome.available or plan is None:
            return ()
        day = plan.target_day
        if day is None or today_interval_count <= 0:
            return ()

        projection = plan.reserve_projection
        # **The enforced curve, not the autonomy one.** ``planning_reserve_kwh`` is
        # the reserve the recursion actually obeyed: aligned with
        # ``horizon.demands``, already quantised up to a bucket and capped at the
        # ceiling. What used to be read here was ``plan.reserve_projection`` -- the
        # *autonomy* curve, which ``reserve_as_dict`` labels
        # ``consumed_by: diagnostics_only`` and which has enforced nothing since
        # beta.31, having demanded 73 percent state of charge against a 20 percent
        # physical floor. Freezing that figure into a claim payload whose own field
        # comment reads "physical limits Stage B must honour" named the wrong
        # curve. Nothing downstream compares it today -- every consumer is a
        # declaration, a pass-through or a serialisation -- so this is a
        # provenance correction, made before some future reader starts trusting it.
        required = {
            demand.index: value
            for demand, value in zip(
                outcome.horizon.demands,
                outcome.horizon.planning_reserve_kwh,
                strict=False,
            )
        }
        hard_floor = projection.floor_energy_kwh if projection is not None else 0.0
        ceiling = projection.ceiling_energy_kwh if projection is not None else None
        previous = dict(self._execution_revisions)
        previous.update({item.get("plan_id"): item for item in self.execution_targets})

        # The horizon the solve actually ran over, and the trajectory it chose.
        # Keyed by chronological index, which is what a run carries.
        demands = {entry.index: entry for entry in outcome.horizon.demands}
        landed = {
            entry.index: entry.start_energy_dc_kwh + entry.battery_delta_dc_kwh
            for entry in outcome.desired.intervals
        }

        def moment(index: int) -> datetime | None:
            if index < 0:
                return None
            if index < today_interval_count:
                return interval_start_utc(day, index, tz)
            return interval_start_utc(
                day + timedelta(days=1), index - today_interval_count, tz
            )

        def window_totals(run: Any) -> tuple[float | None, float | None]:
            """Return forecast production and baseline across the run's window."""
            production = 0.0
            baseline = 0.0
            seen = False
            for index in range(run.start_index, run.end_index + 1):
                demand = demands.get(index)
                if demand is None:
                    continue
                seen = True
                production += demand.pv_kwh or 0.0
                baseline += demand.baseline_kwh or 0.0
            if not seen:
                return None, None
            return production, baseline

        def headroom_of(run: Any) -> tuple[float | None, float | None, int | None]:
            """Return the headroom this plan needs preserved after ``run``.

            The plan's own landing energy is the constraint. Stage A chose to hold
            that much and no more, and it chose it knowing what production was
            forecast afterwards -- so capping the pack there is exactly "do not
            fill early and displace the production this plan means to absorb".

            The deadline is the next interval at which the plan absorbs surplus
            production, because that is the first moment the headroom is spent.
            Absent when the plan absorbs nothing further: then nothing is being
            protected and the constraint would be noise.
            """
            end_energy = landed.get(run.end_index)
            if end_energy is None or ceiling is None:
                return None, None, None
            absorbs_at: int | None = None
            for entry in outcome.desired.intervals:
                if entry.index <= run.end_index:
                    continue
                if entry.absorbing and entry.battery_delta_dc_kwh > 0.0:
                    absorbs_at = entry.index
                    break
            if absorbs_at is None:
                return None, None, None
            return max(0.0, ceiling - end_energy), end_energy, absorbs_at

        # **The retention gate, built once per refresh. beta.40.**
        #
        # Purely economic: does the tariff prefer keeping one more free kWh to
        # selling it. Nothing physical enters it, because how much production the
        # plant can take is bounded by the one clamp that owns every physical
        # limit, and Stage B applies that clamp to every charge it commands.
        #
        # ``None`` publishes the pre-beta.40 row shape and is what a site with no
        # readable limits or no value curve gets -- absent rather than zero, on
        # the same terms as every other optional figure in this contract.
        retention: RetentionGate | None = None
        limits = None if plan is None or plan.state is None else plan.state.limits
        if (
            limits is not None
            and outcome.bucket_kwh > 0.0
            and outcome.desired.intervals
        ):
            # The optimiser's own dual at the head of this solve, which is the
            # layer whose value has the whole horizon still ahead. ``None`` with a
            # reason is a defined answer and becomes a published refusal.
            marginal, _reason = outcome.desired.marginal_value_eur_per_kwh(
                bucket_at_or_below_kwh(
                    outcome.desired.intervals[0].start_energy_dc_kwh,
                    bucket_kwh=outcome.bucket_kwh,
                ),
                bucket_kwh=outcome.bucket_kwh,
            )
            # **The whole curve, not just the head value.** The verdict answers
            # for the level the pack stands at; the ceiling needs every level a row
            # could reach, because the dual falls as the pack fills and a boolean
            # cannot express where it stops paying.
            current = bucket_at_or_below_kwh(
                outcome.desired.intervals[0].start_energy_dc_kwh,
                bucket_kwh=outcome.bucket_kwh,
            )
            curve = tuple(
                outcome.desired.marginal_value_eur_per_kwh(
                    bucket, bucket_kwh=outcome.bucket_kwh
                )[0]
                for bucket in range(len(outcome.desired.head_value))
            )
            retention = RetentionGate(
                marginal_value_eur_kwh=marginal,
                round_trip_efficiency=(
                    limits.charge_efficiency * limits.discharge_efficiency
                ),
                marginal_curve_eur_kwh=curve,
                current_bucket=current,
                bucket_dc_kwh=outcome.bucket_kwh,
            )

        # **Which campaign each run belongs to, resolved to absolute time here.**
        #
        # This is the wiring beta.32 shipped without, and its absence starved the
        # whole campaign layer: every published target carried ``campaign_id:
        # null``, so ``_note_campaign_progress`` never opened a campaign, the
        # realised accumulator never advanced, the target never froze and no
        # campaign terminal ever fired. The machinery was all present and simply
        # never fed.
        #
        # It has to be built *here* because the optimiser deliberately has no
        # calendar: a campaign is a span of horizon indices, and only this layer
        # knows which civil day each index falls in. See ``campaign_identity`` for
        # why the identity is anchored on the **end** instant.
        #
        # Keyed by every index the campaign spans -- not just its first -- because
        # a campaign's *runs* are label slices inside it and any of them may be the
        # one being turned into a target. That is what gives a ``serve_load``
        # segment the same identity as the export segments either side of it, which
        # is what keeps one lifecycle open across the gap. It does not make
        # ``serve_load`` executable: intent still decides that, and Stage B admits
        # only the intents in ``executable_intents``.
        campaign_of: dict[int, tuple[str, datetime]] = {}
        for campaign in outcome.desired.campaigns:
            closes_at = moment(campaign.end_index + 1)
            if closes_at is None:
                continue
            identity = campaign_identity(campaign.direction, closes_at)
            for index in range(campaign.start_index, campaign.end_index + 1):
                campaign_of[index] = (identity, closes_at)

        # **The lifecycle classification, resolved on the same wiring. beta.42.**
        #
        # Read straight off the planner's own purchase attribution, and never from
        # ``category_of()``. That helper is not observability-only: the category is a
        # hash input to ``plan_id_for``, an exact-match key in ``ActivityState.find``,
        # the trigger for the retraction path and a dict lookup selecting battery
        # direction -- and it defaults an unknown category to ``economic``, which
        # publishes "this purchase was entirely a choice" about a coverage buy. Being
        # a hash input is the sharp end: a campaign reclassified between refreshes
        # would get a new plan id, file a cancellation for the old line and open a
        # fresh Planned one, which is the churn that surface was rewritten to kill.
        #
        # Attribution is *allowed to move* under a surviving instance -- nothing
        # freezes the split, and a campaign spanning two admitted plans can legitimately
        # read one thing then another. So this is recomputed every refresh and the
        # lifecycle publishes both what it was at creation and what it finally was.
        self._campaign_classifications = self._classify_campaigns(outcome, campaign_of)

        targets: list[dict[str, Any]] = []
        runs_by_plan: dict[str, Any] = {}
        # Diagnostics only, and from a solve that already happened.
        attribution = outcome.safety_buy_attribution
        for run in outcome.desired.runs:
            opens = moment(run.start_index)
            closes = moment(run.end_index + 1)
            if opens is None or closes is None:
                continue
            floor = required.get(run.start_index)
            production, baseline = window_totals(run)
            headroom, max_end, absorbs_at = headroom_of(run)
            until = None if absorbs_at is None else moment(absorbs_at)
            target = execution_target(
                run,
                window_start=opens,
                window_end=closes,
                reserve_floor_kwh=(
                    hard_floor if floor is None else max(floor, hard_floor)
                ),
                # Anchored to this refresh, not to a window that may be a day
                # away. Command freshness cannot depend on when the command is for.
                issued_at=issued_at,
                stale_after=issued_at
                + timedelta(minutes=EXECUTION_TARGET_STALE_MINUTES),
                safety_buy=run.start_index in outcome.safety_buy_runs,
                margin_passed=True,
                expected_pv_production_kwh=production,
                expected_house_load_kwh=baseline,
                required_headroom_kwh=headroom,
                max_end_energy_kwh=max_end,
                headroom_until=until,
                # **The meter target for the quarter the window opens on**, read
                # off the solved plan own per-interval grid energies. The run
                # first interval, matching ``first_power_kw`` beside it: a run
                # legitimately varies quarter to quarter, and Stage B freezes the
                # figure for the quarter it is executing rather than averaging a
                # run it has not reached the end of.
                desired_grid_kw=desired_grid_kw_at(
                    outcome.desired.intervals, run.start_index
                ),
                safety_buy_kwh=attribution.get(run.start_index, (None, None))[0],
                economic_buy_kwh=attribution.get(run.start_index, (None, None))[1],
                # **The solved rows, so the per-quarter schedule can be built.**
                # Omitting these is what published an empty ``quarter_schedule``
                # for every run in beta.27: the parameter was an optional prebuilt
                # list, this call site never passed it, and Stage B consequently
                # admitted no quarter on real hardware while the contract's own
                # rule string sat beside the empty list describing what should
                # have been in it.
                intervals=outcome.desired.intervals,
                moment=moment,
                campaign_id=campaign_of.get(run.start_index, (None, None))[0],
                campaign_end=campaign_of.get(run.start_index, (None, None))[1],
                retention=retention,
            )
            target["revision"] = execution_revision(
                previous.get(target["plan_id"]), target
            )
            # **Kept privately, not published. beta.44.** The arm plan needs each
            # target's solved interval range to price it against the idle
            # counterfactual, and a horizon index is exactly the kind of internal
            # object the published contract deliberately addresses by instant
            # instead. So the mapping lives here rather than on the target.
            runs_by_plan[target["plan_id"]] = run
            targets.append(target)
        self._runs_by_plan_id = runs_by_plan
        # **Built here, where the solve is still in scope, and kept as a finished
        # dict rather than a reference to the outcome. beta.44.**
        self._arm_plan = self._arm_plan_block(outcome, targets, runs_by_plan)
        return tuple(targets)

    @callback
    def _observe_arm(self, snapshot: Any, now: datetime) -> None:
        """Measure one physical arm, from evidence this tick already read. beta.44.

        **Read-only, and deliberately not wired into the write boundary.** Nothing
        here can reach a command: it is driven entirely by the execution record and
        the snapshot the caller has already taken, so an instrumentation fault costs
        a null figure and never a dispatch. That is also why the arm is keyed on
        ``claim_id`` rather than hooked at the arm sequence -- the record's own
        comment says that field *"names one"* physical claim, and watching it change
        is enough to bracket an arm without touching the path that creates it.

        **Two clocks, never merged.** The 2026-09-05 capture is why: the claim was
        written at 22:15:05.2, the vendor register's own ``last_changed`` was
        22:15:42 -- 37.3 s -- and the first tick that *saw* it was 22:16:36.9, giving
        91.7 s. A single figure would have reported the vendor as 2.5x slower than it
        is, and a later release would have priced an arm against our own cadence.

        Every figure refuses rather than guesses. ``None`` means the evidence was not
        there; ``0`` means genuinely immediate and is only reachable from a real
        measurement.
        """
        record = self.store.execution_record
        claim = record.get("claim_id") if isinstance(record, dict) else None
        open_arm = self._arm_open
        if open_arm is not None and open_arm.get("claim_id") != claim:
            self._close_arm(open_arm, now)
            open_arm = None
        if claim is None:
            self._arm_open = None
            self._arm_saw_dispatch = bool(
                snapshot is not None and snapshot.dispatch_active
            )
            return
        if open_arm is None:
            written = instant_of(record.get("written_at"))
            quarter = self._quarter
            open_arm = {
                "claim_id": claim,
                "run_id": record.get("run_id"),
                "intent": self._executing_intent(),
                "claim_written_at": None if written is None else written.isoformat(),
                "row_start": None
                if quarter is None
                else quarter.quarter_start.isoformat(),
                "objective_kwh": None
                if quarter is None
                else round(self._objective_kwh_for(quarter), 3),
                "activation_latency_s": None,
                "observation_latency_s": None,
                "delivery_latency_s": None,
                "battery_delivery_latency_s": None,
                "objective_forgone_to_activation_kwh": None,
                "evidence": ARM_EVIDENCE_INCOMPLETE,
            }
            self._arm_open = open_arm

        written = instant_of(open_arm.get("claim_written_at"))
        active = bool(snapshot is not None and snapshot.dispatch_active)

        # **Activation: the vendor's own clock, and only across a transition.**
        # A dispatch that was already running when we claimed proves nothing about
        # this arm, so a pre-existing active register is refused rather than timed.
        if (
            open_arm["activation_latency_s"] is None
            and written is not None
            and active
            and not self._arm_saw_dispatch
        ):
            changed = self._dispatch_state_changed_at()
            if changed is None:
                open_arm["evidence"] = ARM_EVIDENCE_NO_TRANSITION
            elif changed < written:
                open_arm["evidence"] = ARM_EVIDENCE_STALE_REGISTER
            else:
                open_arm["activation_latency_s"] = round(
                    (changed - written).total_seconds(), 1
                )
                open_arm["evidence"] = None

        # **Observation: our own clock, and it includes our cadence on purpose.**
        if (
            open_arm["observation_latency_s"] is None
            and written is not None
            and active
            and self._ownership_now(snapshot, now) == OWNERSHIP_OWNED
        ):
            open_arm["observation_latency_s"] = round(
                (now - written).total_seconds(), 1
            )

        self._observe_arm_delivery(open_arm, snapshot, now, written)
        self._arm_saw_dispatch = active

    @callback
    def _dispatch_state_changed_at(self) -> datetime | None:
        """Return when the vendor dispatch register last changed state. beta.44.

        The state machine's own ``last_changed``, which is a *timestamp*, not the
        register's numeric value -- whose meaning this component has never
        established and deliberately does not interpret. Its precision is bounded by
        the source integration's poll interval, which is disclosed beside the figure
        and never corrected for: subtracting an assumed cadence would be a guess
        about somebody else's timing.
        """
        state = self.hass.states.get(SENSOR_DISPATCH_START)
        return None if state is None else state.last_changed

    @callback
    def _observe_arm_delivery(
        self,
        arm: dict[str, Any],
        snapshot: Any,
        now: datetime,
        written: datetime | None,
    ) -> None:
        """Time the first delivery attributable to this arm. beta.44.

        **Attributable, which for a charge is the whole difficulty.** Ambient
        production can already be charging the pack before a forced grid charge
        activates, so battery movement alone does not prove the dispatch started.
        The grid-caused share is what does, and it is the same construction the
        run-level budget already uses: measured charge less measured production
        surplus. Both are published, so a reader can tell activation from absorption
        rather than being handed one number that conflates them.

        Export is measured at the meter and against the same surplus, so
        pre-existing photovoltaic export is never credited to the arm.
        """
        if arm["delivery_latency_s"] is not None or written is None:
            return
        if self._coherence not in (None, COHERENCE_OK):
            arm.setdefault("delivery_evidence", ARM_EVIDENCE_INCOHERENT)
            return
        surplus = self._budget_surplus_kw()
        if surplus is None:
            arm["delivery_evidence"] = ARM_EVIDENCE_UNATTRIBUTABLE
            return
        flows = self.read_flows()
        export = arm.get("intent") == EXECUTION_INTENT_NET_EXPORT
        if export:
            if flows.grid_export_w is None:
                arm["delivery_evidence"] = ARM_EVIDENCE_INCOHERENT
                return
            caused = max(0.0, max(0.0, flows.grid_export_w) / 1000.0 - surplus)
        else:
            if flows.battery_charge_w is None:
                arm["delivery_evidence"] = ARM_EVIDENCE_INCOHERENT
                return
            charge_kw = max(0.0, flows.battery_charge_w / 1000.0)
            caused = max(0.0, charge_kw - surplus)
            if (
                arm["battery_delivery_latency_s"] is None
                and charge_kw > DISPATCH_POWER_DEADBAND_KW
            ):
                arm["battery_delivery_latency_s"] = round(
                    (now - written).total_seconds(), 1
                )
        if caused <= DISPATCH_POWER_DEADBAND_KW:
            return
        arm["delivery_latency_s"] = round((now - written).total_seconds(), 1)
        arm["delivery_evidence"] = None
        objective = arm.get("objective_kwh")
        if objective:
            # **Derived, and an upper bound.** The objective is prorated at the
            # row's own average rate over the interval that delivered nothing
            # attributable. Where ambient behaviour delivered part of it anyway the
            # true loss is smaller, which is why the basis says bound rather than
            # measurement.
            elapsed = min(arm["delivery_latency_s"], QUARTER_SECONDS)
            arm["objective_forgone_to_activation_kwh"] = round(
                objective * elapsed / QUARTER_SECONDS, 3
            )

    @callback
    def _close_arm(self, arm: dict[str, Any], now: datetime) -> None:
        """File a finished arm measurement. beta.44."""
        arm["closed_at"] = now.isoformat()
        if arm.get("activation_latency_s") is None and not arm.get("evidence"):
            arm["evidence"] = ARM_EVIDENCE_INCOMPLETE
        arm.setdefault("delivery_evidence", ARM_EVIDENCE_INCOMPLETE)
        arm["basis"] = (
            "activation_latency_s is the vendor register's own last_changed less "
            "our claim, accepted only across an observed inactive-to-active "
            "transition at or after the claim, and bounded in precision by the "
            "source integration's poll interval. observation_latency_s is the first "
            "tick that saw the dispatch owned and active, and deliberately includes "
            "our own sixty-second cadence -- on 2026-09-05 the two were 37.3 s and "
            "91.7 s for one arm, so they are never merged. delivery_latency_s times "
            "the first coherent delivery attributable to the dispatch: caused "
            "export above the measured production surplus at the meter, or "
            "grid-caused charge above it at the battery. "
            "battery_delivery_latency_s times any battery charge and is published "
            "beside it precisely so ambient absorption cannot be read as proof the "
            "dispatch started. objective_forgone_to_activation_kwh is derived, not "
            "measured: the row objective prorated over the undelivered interval, "
            "and an upper bound because ambient behaviour may have delivered part "
            "of it. null is never zero"
        )
        self._arm_measurements.append(arm)

    @callback
    def _armable_stretches(self, target: dict[str, Any]) -> list[tuple[int, int]]:
        """Return the maximal contiguous executable row spans of one target.

        **Each span is one physical arm. beta.44.**

        The dispatch stops whenever no *executable* row covers the instant:
        ``AdmittedPlan.executing_quarter`` returns ``None`` for a row that is not
        executable, the tick reads that as ``stop``, and the row-scope teardown
        clears the carried run -- so the next executable row mints a fresh ``run_id``
        and runs the full two-stage arm with a marker claim. An ordinary boundary
        *between* two executable rows does none of that: the slot advances and the
        sustain path re-arms the dead-man without claiming anything.

        So the count is a property of the published schedule alone, which is why it
        can be derived here without asking the plan or the device.
        """
        spans: list[tuple[int, int]] = []
        first: int | None = None
        rows = target.get("quarter_schedule") or ()
        for index, row in enumerate(rows):
            armable = row.get("not_executable") is None
            if armable and first is None:
                first = index
            elif not armable and first is not None:
                spans.append((first, index - 1))
                first = None
        if first is not None:
            spans.append((first, len(rows) - 1))
        return spans

    @callback
    def _advantage_eur(self, run: Any, intervals: dict[int, Any]) -> float:
        """Return one index range's advantage over leaving the battery alone."""
        if run is None:
            return 0.0
        total = 0.0
        for index in range(run.start_index, run.end_index + 1):
            entry = intervals.get(index)
            if entry is not None:
                total -= entry.marginal_cost_eur
        return total

    @callback
    def _arm_entry(
        self,
        target: dict[str, Any],
        span: tuple[int, int],
        arm_index: int,
        run: Any,
        intervals: dict[int, Any],
    ) -> dict[str, Any]:
        """Return one planned arm, at the boundary its objective is paid at."""
        rows = target.get("quarter_schedule") or ()
        first, last = span
        export = target.get("intent") == EXECUTION_INTENT_NET_EXPORT
        key = "grid_export_target_kwh" if export else "battery_kwh"
        objective = sum(float(rows[i].get(key) or 0.0) for i in range(first, last + 1))
        value = 0.0
        if run is not None:
            for offset in range(first, last + 1):
                entry = intervals.get(run.start_index + offset)
                if entry is not None:
                    value -= entry.marginal_cost_eur
        return {
            "arm_index": arm_index,
            "campaign_id": target.get("campaign_id"),
            "intent": target.get("intent"),
            "starts_at": rows[first].get("start"),
            "ends_at": rows[last].get("end"),
            "row_count": last - first + 1,
            "objective_kwh": round(objective, 3),
            "objective_boundary": (
                CAMPAIGN_BOUNDARY_METER if export else CAMPAIGN_BOUNDARY_BATTERY
            ),
            "marginal_value_eur": round(value, 4),
        }

    @callback
    def _arm_plan_block(
        self,
        outcome: Any,
        targets: list[dict[str, Any]],
        runs_by_plan: dict[str, Any],
    ) -> dict[str, Any]:
        """Return how many physical arms this plan asks for, and what each buys.

        **beta.44, and it exists because no published figure answered it.** The DP
        prices a campaign as one uninterrupted run and charges one fee for it, while
        Stage B may arm, stop and re-arm several times inside that same campaign: a
        ``serve_load`` gap between two exports, or a PV-only ``hold`` inside a
        charge, each force a stop and a fresh claim. On the 2026-09-05 horizon that
        was **eleven arms against two direction changes**.

        Nothing here decides anything. Every figure is derived from targets already
        published and a trajectory already final.
        """
        desired = getattr(outcome, "desired", None)
        intervals: dict[int, Any] = (
            {}
            if desired is None
            else {entry.index: entry for entry in desired.intervals}
        )
        arms: list[dict[str, Any]] = []
        arm_count = 0
        refused = 0
        refused_energy = 0.0
        refused_value = 0.0
        published = 0
        for target in targets:
            if target.get("intent") not in CONTROL_LIVE_DISPATCH_INTENTS:
                # ``serve_load`` carries the campaign identity across a gap and
                # commands nothing. It is not a run Stage B could ever have armed,
                # so counting it as refused would inflate the very figure this
                # block exists to measure.
                continue
            published += 1
            run = runs_by_plan.get(target.get("plan_id"))
            spans = self._armable_stretches(target)
            if not spans:
                export = target.get("intent") == EXECUTION_INTENT_NET_EXPORT
                key = "grid_export_target_kwh" if export else "battery_kwh"
                rows = target.get("quarter_schedule") or ()
                refused += 1
                refused_energy += sum(float(row.get(key) or 0.0) for row in rows)
                refused_value += self._advantage_eur(run, intervals)
                continue
            for span in spans:
                arm_count += 1
                if len(arms) < MAX_ARM_PLAN_ENTRIES_PUBLISHED:
                    arms.append(
                        self._arm_entry(target, span, arm_count, run, intervals)
                    )

        campaigns = () if desired is None else desired.campaigns
        return {
            "arm_count": arm_count,
            "campaign_count": len(campaigns),
            "segment_count": sum(len(c.segments) for c in campaigns),
            "executable_segment_count": sum(
                1 for c in campaigns for segment in c.segments if segment.executable
            ),
            "direction_changes": (
                None if desired is None else desired.direction_changes
            ),
            "runs_total": 0 if desired is None else len(desired.runs),
            "runs_published": published,
            "runs_refused_nothing_armable": refused,
            "energy_planned_on_refused_runs_kwh": round(refused_energy, 3),
            "value_planned_on_refused_runs_eur": round(refused_value, 4),
            "refused_run_value_basis": REFUSED_RUN_VALUE_BASIS,
            "arms": arms,
            "arms_truncated": max(0, arm_count - len(arms)),
            "rule": (
                "one arm is one maximal contiguous stretch of executable rows, "
                "because a non-executable row makes the tick stop the dispatch and "
                "the next executable row claim the marker again. direction_changes "
                "counts what the DP charged a fee for and is a different question: "
                "the objective's run state does not distinguish exporting from "
                "serving load, nor charging from absorbing, so one campaign can ask "
                "for several arms and pay one fee. a refused run is one Stage B can "
                "never arm because no row of it is executable, and its value is the "
                "advantage over leaving the battery alone -- ambient production and "
                "unavoidable import sit inside the idle counterfactual on both "
                "sides of that difference, so neither is counted as dispatch-caused "
                "value. nothing here decides anything"
            ),
        }

    @callback
    def realized_today(self, plan: Any) -> dict[str, Any]:
        """Return what today actually cost, from measured flows and stored prices.

        Read-only with respect to every decision. The optimizer, the reserve, the
        policy and the safety gate are given nothing from here -- an optimizer that
        learned from its own recorded outcomes would be a later phase wearing this
        one's clothes, and a structural test pins that it does not.
        """
        if plan is None or plan.target_day is None:
            return {"available": False, "reason": "no_day_record"}
        record = self.store.days.get(plan.target_day)
        if record is None:
            return {"available": False, "reason": "no_day_record"}
        resolved = self._prices_for_day(plan.target_day, record.interval_count)
        if resolved is None:
            return {"available": False, "reason": "no_stored_prices"}
        buy, sell, price_basis = resolved

        limits = plan.state.limits if plan.state is not None else None
        series = self._realized_series(record, (buy, sell), limits)
        window = self._realized_window_for(series, limits)
        return {
            "available": True,
            "day": plan.target_day.isoformat(),
            "price_basis": price_basis,
            **window.as_dict(),
        }

    def realized_days(self, plan: Any, *, days: int) -> dict[str, Any]:
        """Return the same ledger over the last ``days`` civil days, oldest first.

        **The ledger is not day-scoped, and a battery is not either.** A pack
        charged at 03:00 and sold at 19:00 the next evening is one economic
        position, and a view that resets at midnight cannot describe it -- which is
        also the moment a stored-energy value would appear to step discontinuously
        for no physical reason.

        **No new storage.** Every input is already persisted: ``DayRecord`` keeps
        measured import, export, load, production and state of charge for a year,
        and ``PriceSnapshot`` keeps the prices those intervals were valued at. This
        concatenates the series and prices them exactly as one day is priced, so it
        rebuilds itself after a restart with nothing else to remember. A day whose
        prices were never stored is skipped rather than valued at zero, and
        ``days_priced`` says how many were used.
        """
        if plan is None or plan.target_day is None:
            return {"available": False, "reason": "no_day_record"}
        limits = plan.state.limits if plan.state is not None else None
        wanted = max(1, int(days))

        merged: dict[str, list[Any]] = {}
        used: list[date] = []
        bases: set[str] = set()
        for offset in range(wanted - 1, -1, -1):
            day = plan.target_day - timedelta(days=offset)
            record = self.store.days.get(day)
            if record is None:
                continue
            resolved = self._prices_for_day(day, record.interval_count)
            if resolved is None:
                continue
            buy, sell, price_basis = resolved
            bases.add(price_basis)
            for key, values in self._realized_series(
                record, (buy, sell), limits
            ).items():
                merged.setdefault(key, []).extend(values)
            used.append(day)

        if not used:
            return {"available": False, "reason": "no_stored_prices"}

        window = self._realized_window_for(merged, limits)
        return {
            "available": True,
            "days_requested": wanted,
            "days_priced": len(used),
            "first_day": used[0].isoformat(),
            "last_day": used[-1].isoformat(),
            "price_basis": sorted(bases),
            **window.as_dict(),
        }

    @staticmethod
    def _negated(value: Any) -> float | None:
        """Return ``-value``, or ``None``.

        The terminal publishes a signed *tracking error* -- negative when the plant
        under-delivered -- while a public result publishes a *shortfall*, which is
        positive for the same event. One negation, in one place, rather than a sign
        convention a reader has to carry between two payloads.
        """
        if value is None:
            return None
        return round(-float(value), 4)

    # == public campaign lifecycle ==========================================
    #
    # Four transitions per attempt, at most one event each, persisted so a restart
    # replays none of them. The identity is ``campaign_instance_id``, which already
    # exists as the exactly-once attempt key and is minted at exactly one site.

    @callback
    def _fire_lifecycle(self, kind: str, data: dict[str, Any]) -> None:
        """Publish one lifecycle transition on the event bus."""
        self.hass.bus.async_fire(EVENT_CAMPAIGN_LIFECYCLE, {"kind": kind, **data})

    @callback
    def _lifecycle_common(self) -> dict[str, Any]:
        """Return the fields every kind carries."""
        live = self._campaign_classification(self._campaign_id)
        mark = self.store.campaign_lifecycle or {}
        return {
            "campaign_id": self._campaign_id,
            "campaign_instance_id": self._campaign_instance_id,
            "purpose": mark.get("purpose"),
            "classification": live.get("classification", LIFECYCLE_CLASS_UNKNOWN),
            "classification_at_creation": mark.get("classification_at_creation"),
            "objective_boundary": self._campaign_boundary,
            "planned_kwh": mark.get("planned_kwh"),
            "window_start": mark.get("window_start"),
            "window_end": (
                None
                if self._campaign_end_utc is None
                else self._campaign_end_utc.isoformat()
            ),
            "revision": mark.get("revision"),
        }

    @callback
    def _lifecycle_created(self, now: datetime, quarter: Any) -> None:
        """Open the public lifecycle for the instance just minted.

        Fired where the campaign actually becomes public -- when
        ``_note_campaign_progress`` opens it -- with **no pre-announcement and no
        timing promise**. A plan Stage A has published but Stage B has not yet
        placed is not a campaign; announcing one would put a line in the log for
        every plan that a later refresh replaced before anything happened.
        """
        instance_id = self._campaign_instance_id
        if instance_id is None:
            return
        live = self._campaign_classification(self._campaign_id)
        self.store.campaign_lifecycle = {
            "instance_id": instance_id,
            "campaign_id": self._campaign_id,
            "opened_at": now.isoformat(),
            "marks": [LIFECYCLE_KIND_CREATED],
            "purpose": getattr(quarter, "intent", None),
            "classification_at_creation": live.get(
                "classification", LIFECYCLE_CLASS_UNKNOWN
            ),
            "objective_boundary": self._campaign_boundary,
            "planned_kwh": (
                None
                if self._campaign_opening_target_kwh is None
                else round(self._campaign_opening_target_kwh, 3)
            ),
            "window_start": now.isoformat(),
            "window_end": (
                None
                if self._campaign_end_utc is None
                else self._campaign_end_utc.isoformat()
            ),
            "revision": getattr(quarter, "revision", None),
            "started_at": None,
            "stopped_at": None,
            "stop_reason": None,
            "realized_kwh": 0.0,
            "measurable": True,
            "result": None,
        }
        self.store.schedule_save()
        self._fire_lifecycle(LIFECYCLE_KIND_CREATED, self._lifecycle_common())

    @callback
    def _lifecycle_started(self, now: datetime) -> None:
        """Note the first confirmed activation of the open instance.

        ``started`` is already monotonic in the executor and this only mirrors it:
        ``_campaign_started_at`` is written once behind an idempotent guard, and the
        two writes that clear it both happen after the instance identity has already
        changed or been cleared.
        """
        mark = self.store.campaign_lifecycle
        if mark is None or LIFECYCLE_KIND_STARTED in mark["marks"]:
            return
        mark["marks"].append(LIFECYCLE_KIND_STARTED)
        mark["started_at"] = now.isoformat()
        self.store.schedule_save()
        self._fire_lifecycle(
            LIFECYCLE_KIND_STARTED,
            {**self._lifecycle_common(), "started_at": mark["started_at"]},
        )

    @callback
    def _lifecycle_stopped(self, now: datetime, reason: str | None) -> None:
        """Note that execution ended for good.

        **Only from a campaign-scoped stop or the terminal**, never from a row-scope
        one. A row-scope stop means this row is done and a later executable row
        remains: the frozen plan and the campaign instance survive, the dispatch
        stops and the next boundary arms again. Emitting there would put a line in
        the log at every row boundary and at every ``serve_load`` gap -- which is
        the per-quarter noise this surface exists to replace.

        Nothing is published for an instance that never started, because nothing
        physical had begun to stop.
        """
        mark = self.store.campaign_lifecycle
        if mark is None or LIFECYCLE_KIND_STOPPED in mark["marks"]:
            return
        if LIFECYCLE_KIND_STARTED not in mark["marks"]:
            return
        mark["marks"].append(LIFECYCLE_KIND_STOPPED)
        mark["stopped_at"] = now.isoformat()
        mark["stop_reason"] = reason
        mark["realized_kwh"] = round(self._campaign_realized_now(), 3)
        mark["measurable"] = self._campaign_measurable
        # **The evidence, not merely the mark.** Without the frozen target and the
        # tolerance, a restart landing between this and the terminal -- which follows
        # in the same call, so the window is narrow but real -- could not tell a
        # finished campaign from an interrupted one, and would report a success as a
        # failure.
        mark["frozen_target_kwh"] = self._campaign_frozen_target_kwh
        mark["success_tolerance_kwh"] = round(
            self._completion_tolerance_kwh(
                self._campaign_frozen_target_kwh, self._campaign_quarters_admitted
            ),
            4,
        )
        self.store.schedule_save()
        self._fire_lifecycle(
            LIFECYCLE_KIND_STOPPED,
            {
                **self._lifecycle_common(),
                "realised_kwh": mark["realized_kwh"],
                "started_at": mark.get("started_at"),
                "stopped_at": mark["stopped_at"],
                "stop_reason": reason,
            },
        )

    @callback
    def _lifecycle_removed(
        self,
        now: datetime,
        *,
        result: str,
        completion_reason: str | None,
        terminal: dict[str, Any] | None,
    ) -> None:
        """File the public terminal, exactly once per attempt.

        **The latch here is a telemetry one and touches neither of the two that
        already exist.** ``_final_campaigns`` means a campaign may never run again;
        ``_closed_instances`` is session-local and bounded. A never-started campaign
        has to receive this event *and* stay free to be attempted properly later, so
        filing its terminal through either would block a legitimate later attempt --
        which is exactly what ``_close_campaign`` returns early to avoid.
        """
        mark = self.store.campaign_lifecycle
        instance_id = (
            self._campaign_instance_id
            if mark is None
            else mark.get("instance_id", self._campaign_instance_id)
        )
        if instance_id is None or self.store.lifecycle_closed(instance_id):
            self.store.campaign_lifecycle = None
            return
        common = self._lifecycle_common()
        common["campaign_instance_id"] = instance_id
        live = common["classification"]
        payload = {
            **common,
            "final_classification": live,
            "realised_kwh": (
                0.0 if terminal is None else terminal.get("objective_realized_kwh", 0.0)
            ),
            "shortfall_kwh": (
                None
                if terminal is None
                else self._negated(terminal.get("objective_tracking_error_kwh"))
            ),
            "first_executable_at": (None if mark is None else mark.get("started_at")),
            "started_at": (None if mark is None else mark.get("started_at")),
            "stopped_at": (None if mark is None else mark.get("stopped_at")),
            "finished_at": now.isoformat(),
            "result": result,
            "completion_reason": completion_reason,
            "objective_measurable": (
                True if terminal is None else terminal.get("objective_measurable", True)
            ),
            "success_tolerance_kwh": (
                None if terminal is None else terminal.get("success_tolerance_kwh")
            ),
        }
        breakdown = self._campaign_classification(common["campaign_id"])
        for key in (
            "safety_buy_kwh",
            "coverage_buy_kwh",
            "economic_buy_kwh",
            "grid_export_kwh",
        ):
            if key in breakdown:
                payload[key] = breakdown[key]
        self.store.note_lifecycle_closed(instance_id)
        self.store.campaign_lifecycle = None
        self.store.schedule_save()
        self._last_campaign_result = payload
        self._fire_lifecycle(LIFECYCLE_KIND_REMOVED, payload)

    @callback
    def _recover_campaign_lifecycle(self, now: datetime) -> str | None:
        """Close a lifecycle left dangling by a restart. Returns what it did.

        **A ``created`` that can never be closed breaks the one guarantee this
        surface is built on**, and before beta.42 every restart mid-campaign left
        one: the open instance lived only in memory, so a fresh id was minted with a
        new ``opened_at`` and the pre-restart instance never received ``removed``.
        One physical objective appeared twice in the log under two ids.

        Closing rather than resuming is what the executor already decided. beta.27's
        restart policy is to **stop** an owned dispatch and mark progress unknown --
        ``_adopt_persisted_run`` adopts a run only in order to stop it. A restart
        really does end the attempt, so the lifecycle agrees with the executor rather
        than overruling it.

        **But the four states do not close the same way, and calling them all
        ``failed`` would be closing the log rather than reporting it:**

        ``created`` only
            Nothing physical happened, so ``not_executed`` -- and **no** ``stopped``,
            because nothing had begun to stop. Execution finality is not touched,
            which is precisely what leaves a legitimate later attempt permitted.

        ``created`` + ``started``
            ``failed``, with ``quarter_progress_unknown``. Not a judgement invented
            here: the adoption marks progress unknown, which clears
            ``_campaign_measurable``, and the honesty guard already makes success
            unreachable when the total is not a measurement.

        ``created`` + ``started`` + ``stopped``
            The verdict was reachable before the restart, so it is **reconstructed
            from the persisted evidence** rather than downgraded. This is why the
            marks carry the frozen target, the realised total, the measurability and
            the reason -- without them this state is indistinguishable from the one
            above, and the log would report a finished campaign as failed.

        ``removed``
            Already published. The mark is cleared when it fires, so there is nothing
            here to find.
        """
        mark = self.store.campaign_lifecycle
        if mark is None:
            return None
        instance_id = mark.get("instance_id")
        if self.store.lifecycle_closed(instance_id):
            self.store.campaign_lifecycle = None
            return "already_closed"
        marks = mark.get("marks") or []
        campaign_id = mark.get("campaign_id")

        if LIFECYCLE_KIND_STARTED not in marks:
            reason = self._dangling_creation_reason(mark, now)
            if reason is None:
                # **Deliberately left open.** The store is read during setup and the
                # first solve happens later, so at restore time there is no
                # authoritative plan to say whether a replacement exists -- guessing
                # ``plan_replaced`` there would be inventing a fact. The instance is
                # re-examined every refresh until one of the two answers is true, so
                # it closes *late* rather than *wrongly*. Nothing is blocked
                # meanwhile: this state never latched execution finality.
                return "deferred"
            self._publish_recovered_terminal(
                mark,
                now,
                result=OUTCOME_NOT_EXECUTED,
                completion_reason=reason,
                realised_kwh=0.0,
                shortfall_kwh=None,
                measurable=True,
                emit_stopped=False,
            )
            return "not_executed"

        realised = float(mark.get("realized_kwh") or 0.0)
        target = mark.get("frozen_target_kwh")
        tolerance = mark.get("success_tolerance_kwh")
        if LIFECYCLE_KIND_STOPPED in marks:
            measurable = bool(mark.get("measurable", True))
            result = self._recovered_outcome(
                measurable=measurable,
                target_kwh=target,
                realised_kwh=realised,
                tolerance_kwh=tolerance,
                stop_reason=mark.get("stop_reason"),
            )
            self._publish_recovered_terminal(
                mark,
                now,
                result=result,
                completion_reason=mark.get("stop_reason"),
                realised_kwh=realised,
                shortfall_kwh=(None if target is None else round(target - realised, 4)),
                measurable=measurable,
                emit_stopped=False,
            )
            return result

        # Started and never stopped: the restart is the stop.
        self._publish_recovered_terminal(
            mark,
            now,
            result=OUTCOME_FAILED,
            completion_reason=EXECUTION_STOP_QUARTER_PROGRESS_UNKNOWN,
            realised_kwh=realised,
            shortfall_kwh=(None if target is None else round(target - realised, 4)),
            measurable=False,
            emit_stopped=True,
        )
        _LOGGER.debug(
            "Campaign %s instance %s was open across a restart; closed as failed "
            "with quarter_progress_unknown",
            campaign_id,
            instance_id,
        )
        return OUTCOME_FAILED

    @callback
    def _recovered_outcome(
        self,
        *,
        measurable: bool,
        target_kwh: float | None,
        realised_kwh: float,
        tolerance_kwh: float | None,
        stop_reason: str | None,
    ) -> str:
        """Return the outcome the pre-restart evidence supports.

        **The same precedence ``_close_campaign`` applies, over persisted evidence
        instead of live state**, and it is written out rather than shared because the
        two read different things: that one reads the coordinator, this one reads a
        document. Sharing the body would mean giving the live path a dictionary
        interface it does not want.

        Unmeasurable outranks a met objective, exactly as it does live: a total that
        is not a measurement cannot be evidence of success.
        """
        if not measurable:
            return OUTCOME_FAILED
        if target_kwh is None:
            return OUTCOME_PARTIAL
        if tolerance_kwh is not None and target_kwh - realised_kwh <= tolerance_kwh:
            return OUTCOME_SUCCESS
        if stop_reason in EXECUTION_FAILED_STOP_REASONS:
            return OUTCOME_FAILED
        if stop_reason == EXECUTION_STOP_PLAN_REPLACED:
            return OUTCOME_SUPERSEDED
        if stop_reason in EXECUTION_COMPLETION_STOP_REASONS:
            return OUTCOME_PARTIAL
        if stop_reason:
            return OUTCOME_CANCELED
        return OUTCOME_PARTIAL

    @callback
    def _dangling_creation_reason(
        self, mark: dict[str, Any], now: datetime
    ) -> str | None:
        """Return why a never-started campaign ended, or ``None`` to ask again later.

        Two authoritative answers and no third: a newer plan demonstrably covers the
        window, or the window is simply past. Anything else means the question cannot
        be answered yet, and the honest response to that is to wait rather than to
        pick one.
        """
        stored = mark.get("window_end")
        window_end = dt_util.parse_datetime(stored) if isinstance(stored, str) else None
        plan = self._plan
        if (
            plan is not None
            and plan.campaign_id is not None
            and plan.campaign_id != mark.get("campaign_id")
        ):
            return EXECUTION_STOP_PLAN_REPLACED
        if window_end is not None and now >= window_end:
            return EXECUTION_STOP_WINDOW_ENDED
        return None

    @callback
    def _publish_recovered_terminal(
        self,
        mark: dict[str, Any],
        now: datetime,
        *,
        result: str,
        completion_reason: str | None,
        realised_kwh: float,
        shortfall_kwh: float | None,
        measurable: bool,
        emit_stopped: bool,
    ) -> None:
        """Emit the closing events for a recovered instance, exactly once.

        The classification published here is the one recorded at creation, and only
        that one. A live reclassification would come from *this* boot's solve, which
        never saw the campaign -- so it would be a fact about a different plan wearing
        the recovered campaign's name.
        """
        instance_id = mark.get("instance_id")
        if instance_id is None or self.store.lifecycle_closed(instance_id):
            self.store.campaign_lifecycle = None
            return
        recorded = mark.get("classification_at_creation", LIFECYCLE_CLASS_UNKNOWN)
        common = {
            "campaign_id": mark.get("campaign_id"),
            "campaign_instance_id": instance_id,
            "purpose": mark.get("purpose"),
            "classification": recorded,
            "classification_at_creation": recorded,
            "objective_boundary": mark.get("objective_boundary"),
            "planned_kwh": mark.get("planned_kwh"),
            "window_start": mark.get("window_start"),
            "window_end": mark.get("window_end"),
            "revision": mark.get("revision"),
            "recovered_after_restart": True,
        }
        if emit_stopped and LIFECYCLE_KIND_STOPPED not in (mark.get("marks") or []):
            self._fire_lifecycle(
                LIFECYCLE_KIND_STOPPED,
                {
                    **common,
                    "realised_kwh": realised_kwh,
                    "started_at": mark.get("started_at"),
                    "stopped_at": now.isoformat(),
                    "stop_reason": completion_reason,
                },
            )
        payload = {
            **common,
            "final_classification": recorded,
            "realised_kwh": realised_kwh,
            "shortfall_kwh": shortfall_kwh,
            "first_executable_at": mark.get("started_at"),
            "started_at": mark.get("started_at"),
            "stopped_at": mark.get("stopped_at")
            or (now.isoformat() if emit_stopped else None),
            "finished_at": now.isoformat(),
            "result": result,
            "completion_reason": completion_reason,
            "objective_measurable": measurable,
            "success_tolerance_kwh": mark.get("success_tolerance_kwh"),
        }
        self.store.note_lifecycle_closed(instance_id)
        self.store.campaign_lifecycle = None
        self.store.schedule_save()
        self._last_campaign_result = payload
        self._fire_lifecycle(LIFECYCLE_KIND_REMOVED, payload)

    @callback
    def _classify_campaigns(
        self, outcome: Any, campaign_of: dict[int, tuple[str, datetime]]
    ) -> dict[str, dict[str, Any]]:
        """Return the lifecycle classification of each campaign this solve published.

        **Derived from the three disjoint purchase categories, in their precedence**,
        and from nothing else. Safety outranks Coverage outranks Economic, the
        planner already subtracts coverage out of the economic half so the published
        pair cannot report a coverage kWh as a trade, and every purchased kWh belongs
        to exactly one. Summing them per campaign is therefore a partition, not an
        estimate.

        Exactly one category present names the campaign; more than one makes it
        ``mixed_buy`` with the three-way breakdown beside it, because "mixed" without
        the split is a word rather than a figure.

        **A discharge campaign is judged on whether it sells, not on whether it
        moves energy.** One whose segments are all ``serve_load`` has an objective of
        zero at a battery boundary and commands no actuator: the inverter is serving
        the house from the pack, which is ordinary self-consumption and not a trade.
        Calling it an export would put a lifecycle line in the log for every evening.
        """
        buys: dict[str, dict[str, float]] = {}
        sells: dict[str, float] = {}
        safety = outcome.safety_buy_attribution
        coverage = outcome.coverage_buy_attribution
        for run in outcome.desired.runs:
            resolved = campaign_of.get(run.start_index)
            if resolved is None:
                continue
            identity = resolved[0]
            if run.start_index in safety or run.start_index in coverage:
                safety_kwh, economic_kwh = safety.get(run.start_index, (0.0, 0.0))
                totals = buys.setdefault(
                    identity,
                    {
                        "safety_buy_kwh": 0.0,
                        "coverage_buy_kwh": 0.0,
                        "economic_buy_kwh": 0.0,
                    },
                )
                totals["safety_buy_kwh"] += float(safety_kwh)
                totals["economic_buy_kwh"] += float(economic_kwh)
                totals["coverage_buy_kwh"] += float(coverage.get(run.start_index, 0.0))
            else:
                sells[identity] = sells.get(identity, 0.0) + float(
                    getattr(run, "grid_export_kwh", 0.0) or 0.0
                )

        classified: dict[str, dict[str, Any]] = {}
        for identity, totals in buys.items():
            present = [
                name
                for name, value in (
                    (LIFECYCLE_CLASS_SAFETY_BUY, totals["safety_buy_kwh"]),
                    (LIFECYCLE_CLASS_COVERAGE_BUY, totals["coverage_buy_kwh"]),
                    (LIFECYCLE_CLASS_ECONOMIC_BUY, totals["economic_buy_kwh"]),
                )
                if value > CAMPAIGN_CLASSIFICATION_EPSILON_KWH
            ]
            if len(present) == 1:
                classification = present[0]
            elif present:
                classification = LIFECYCLE_CLASS_MIXED_BUY
            else:
                classification = LIFECYCLE_CLASS_UNKNOWN
            classified[identity] = {
                "classification": classification,
                **{name: round(value, 4) for name, value in totals.items()},
            }
        for identity, exported in sells.items():
            if identity in classified:
                continue
            classified[identity] = {
                "classification": (
                    LIFECYCLE_CLASS_ECONOMIC_EXPORT
                    if exported > CAMPAIGN_CLASSIFICATION_EPSILON_KWH
                    else LIFECYCLE_CLASS_SERVE_LOAD
                ),
                "grid_export_kwh": round(exported, 4),
            }
        return classified

    @callback
    def _campaign_classification(self, campaign_id: str | None) -> dict[str, Any]:
        """Return the live classification of one campaign, or an unknown marker.

        ``unknown`` when this refresh's solve does not name the campaign -- which is
        an ordinary state for a campaign whose remaining rows are all behind the
        current head, not an error. It never guesses ``economic``, because guessing
        is the specific defect this surface exists to avoid.
        """
        if campaign_id is None:
            return {"classification": LIFECYCLE_CLASS_UNKNOWN}
        return dict(
            self._campaign_classifications.get(
                campaign_id, {"classification": LIFECYCLE_CLASS_UNKNOWN}
            )
        )

    @callback
    def battery_return(self, today: date) -> dict[str, Any]:
        """Return what the battery has recovered of what it cost, and on what basis.

        **The numerator is the cash comparator and nothing else**: what the meter
        would have cost with no battery, less what it actually cost, summed over
        finalised days. Deliberately *not* the figure that shipped as
        ``realized_net_value_eur`` -- that one equals
        ``TRUE - sum(p*min(I,N)) + sum(s*X)``, the household's whole position. It
        subtracts an unavoidable import bill and credits PV export that needed no
        battery, so ``sum(p*min(I,N))`` dominates and it is structurally negative for
        any household that imports anything. Shown as "battery savings" it would have
        said the battery destroys value. Its own docstring is accurate; the name was
        the problem, and this is the correction rather than a second opinion.

        **Half the price basis is genuinely cash and half is a reconstruction, and
        this publishes which rather than averaging over the difference.** The import
        leg is all-in: wholesale plus market tax plus sourcing markup plus energy
        tax, VAT-inclusive, taken from ``total_price_eur_kwh``. The export leg is not
        -- the source publishes no feed-in price, so it is rebuilt as
        ``market_price + adjustment`` with the adjustment defaulting to zero and the
        VAT flag defaulting to off. On a stock configuration ``s`` is bare wholesale.
        The battery's benefit is mostly avoided import, so the error is small on this
        installation and could be large on an export-heavy one -- which is why the
        size of the reconstructed leg is a **published figure** rather than a
        reassurance that it is bounded.

        Nothing here is forecast or planner-derived. No opening or closing inventory
        value, no remaining-expected, no revaluation, no ``model_terms``: every one
        of those revalues on each refresh, which would make a lifetime total move
        without a day having passed.
        """
        config = self.config
        gross = config.battery_investment_eur
        if gross is None:
            return {
                "available": False,
                "unavailable_reason": ROI_UNAVAILABLE_NO_INVESTMENT,
            }
        subsidy = config.battery_subsidy_eur or 0.0
        credit = config.other_one_time_credit_eur or 0.0
        net = round(gross - subsidy - credit, 2)

        lifetime = self.lifetime_benefit(today)
        cumulative = lifetime["cumulative_realised_benefit_eur"]
        sample_days = lifetime["sealed_days"]
        # **Not "are any days still retained".** A figure whose days have all aged
        # out of the 365-day window is still a measurement -- that is precisely the
        # case the running total exists for -- so the availability gate is whether
        # anything has ever been sealed, not whether the evidence is still on disk.
        if not sample_days:
            return {
                "available": False,
                "unavailable_reason": ROI_UNAVAILABLE_NO_HISTORY,
                "gross_investment_eur": gross,
                "subsidy_eur": subsidy,
                "other_one_time_credit_eur": credit,
                "net_investment_eur": net,
            }

        short = self._trailing_benefit(today, ROI_TRAILING_SHORT_DAYS)
        long = self._trailing_benefit(today, ROI_TRAILING_LONG_DAYS)
        recovered = None if net <= 0.0 else round(100.0 * cumulative / net, 2)
        payback = self._payback_from(long, net, cumulative, today)

        return {
            "available": True,
            "gross_investment_eur": gross,
            "subsidy_eur": subsidy,
            "other_one_time_credit_eur": credit,
            "net_investment_eur": net,
            "cumulative_realised_benefit_eur": cumulative,
            "remaining_to_recover_eur": round(max(0.0, net - cumulative), 2),
            "recovered_percent": recovered,
            "average_realised_benefit_per_day_eur": (
                round(cumulative / sample_days, 4) if sample_days else None
            ),
            "trailing_30d_eur": short["total_eur"],
            "trailing_30d_days": short["days"],
            "trailing_90d_eur": long["total_eur"],
            "trailing_90d_days": long["days"],
            "sample_days": sample_days,
            **payback,
            **self._roi_provenance(lifetime, today),
            **self._roi_price_basis(today),
        }

    @callback
    def _trailing_benefit(self, today: date, window: int) -> dict[str, Any]:
        """Return the sealed benefit over the last ``window`` civil days.

        **Sealed values only, never a re-derivation.** A trailing mean assembled by
        re-pricing would move whenever a day's prices were re-issued, and a payback
        estimate built on a mean that moves is not an estimate.

        ``days`` is published beside the total because a 30-day window over 11
        sealed days is a different figure from one over 30, and only the count says
        which it was.
        """
        first = today - timedelta(days=window)
        values = [
            record.benefit_eur_final
            for day, record in self.store.days.items()
            if first <= day < today and record.benefit_eur_final is not None
        ]
        return {"total_eur": round(sum(values), 4), "days": len(values)}

    @callback
    def _payback_from(
        self,
        trailing: dict[str, Any],
        net_investment_eur: float,
        cumulative_eur: float,
        today: date,
    ) -> dict[str, Any]:
        """Return the payback estimate, or a named reason there is none.

        **One published estimate, from the trailing 90-day mean**, with the 30-day
        figure beside it so a reader can see the spread rather than being offered two
        answers and asked to choose.

        Two refusals, both named. Under ``ROI_MIN_SAMPLE_DAYS`` priceable days the
        extrapolation says more about the season than the battery. At or below a zero
        trailing mean the recorded period did not pay -- which is a real measurement,
        published as one -- and dividing by it would give either an error or a date
        in the past, and a date in the past would read as a fact.
        """
        days = trailing["days"]
        if days < ROI_MIN_SAMPLE_DAYS:
            return {
                "estimated_payback_date": None,
                "estimated_payback_years": None,
                "payback_unavailable_reason": (
                    ROI_PAYBACK_UNAVAILABLE_INSUFFICIENT_HISTORY
                ),
            }
        per_day = trailing["total_eur"] / days
        if per_day <= 0.0:
            return {
                "estimated_payback_date": None,
                "estimated_payback_years": None,
                "payback_unavailable_reason": ROI_PAYBACK_UNAVAILABLE_NO_BENEFIT,
            }
        remaining = max(0.0, net_investment_eur - cumulative_eur)
        days_left = remaining / per_day
        return {
            "estimated_payback_date": (
                today + timedelta(days=round(days_left))
            ).isoformat(),
            "estimated_payback_years": round(days_left / 365.25, 2),
            "payback_unavailable_reason": None,
        }

    @callback
    def _roi_provenance(self, lifetime: dict[str, Any], today: date) -> dict[str, Any]:
        """Return what period the cumulative figure actually covers.

        **The gap is reported, never estimated.** An operator may enter a purchase
        date earlier than the first day this integration has authoritative accounting
        for, and the figure must not read as "benefit since purchase" when the
        evidence starts later. The total is still published -- it is a true
        measurement of the days it covers -- and the reader is told which days those
        are, which is this module's own rule that a total missing one of its terms is
        a different number wearing the same name.
        """
        configured = self.config.battery_investment_date
        available = lifetime["history_available_since"]
        start = available
        if configured and available:
            start = max(configured, available)
        elif configured:
            start = configured
        complete = bool(configured and available and available <= configured)
        return {
            "investment_date": configured,
            "history_available_since": available,
            "accounting_start_date": start,
            "sealed_through": lifetime["sealed_through"],
            "unsealed_past_days": lifetime["unsealed_past_days"],
            # **The split, because the two halves have different evidence behind
            # them.** The retained half can still be checked against the day records
            # on disk; the evicted half cannot, and a reader auditing the figure
            # needs to know which part they can go and look at.
            "retained_sealed_eur": lifetime["retained_sealed_eur"],
            "retained_sealed_days": lifetime["retained_sealed_days"],
            "sealed_evicted_eur": lifetime["sealed_evicted_eur"],
            "sealed_evicted_through": lifetime["sealed_evicted_through"],
            "lifetime_history_complete": complete,
            "benefit_basis_version": lifetime["basis_version"],
        }

    @callback
    def _roi_price_basis(self, today: date) -> dict[str, Any]:
        """Return how each price leg was formed, beside the figure it produced.

        **Read from the days the figure actually covers**, not from today's
        configuration. Each stored issuance carries its own ``export_basis`` per
        interval, expressly so a later reader can tell "the market moved" from "the
        rule changed" -- and a lifetime figure spanning a configuration change has to
        report both bases rather than the current one.

        ``export_leg_is_cash`` sits next to the euro figures rather than inside a
        nested basis map, following the ``model_terms.is_cash`` precedent: a caveat
        reachable only through the diagnostics download is a caveat nobody reads.
        """
        # **Memoised on the sealed set, because this is read from an entity.**
        # Home Assistant re-reads attributes on every state update, and the scan
        # below walks a year of days and 96 basis tokens each -- roughly 35 000
        # set operations to answer a question whose answer changes at most once a
        # day, when a day is sealed. The key is the sealed-day count and the civil
        # day, which are exactly the two things that can move the answer: a new
        # seal, or midnight bringing a different fallback into view.
        key = (len(self.store.days), self.store.sealed_day_count, today)
        cached = self._roi_basis_cache
        if cached is not None and cached[0] == key:
            return dict(cached[1])
        observed: set[str] = set()
        for day, record in self.store.days.items():
            if record.final_benefit is None:
                continue
            snapshot = self.history.latest_price_snapshot(day)
            if snapshot is not None:
                observed.update(snapshot.export_basis)
        forecast = (self.price_forecasts or {}).get(today)
        if not observed and forecast is not None:
            observed.update(
                interval.export_basis
                for interval in forecast.intervals
                if getattr(interval, "export_basis", None)
            )
        observed.discard(PRICE_EXPORT_BASIS_UNKNOWN)
        # **Cash only where the evidence says cash.** A published feed-in price is
        # the real thing; a reconstruction that had VAT applied is close enough to
        # call cash. A bare ``market_price_plus_adjustment`` is not: the adjustment
        # defaults to zero and the VAT flag to off, so on a stock configuration that
        # token means the wholesale price with nothing added, and the token itself
        # cannot say whether an adjustment was configured. Refusing to claim cash
        # there is the same choice as refusing to guess a category elsewhere.
        is_cash = bool(observed) and observed <= {
            PRICE_EXPORT_BASIS_API_FIELD,
            PRICE_EXPORT_BASIS_ADJUSTMENT_VAT,
        }
        published = {
            "import_leg_basis": PRICE_LEG_ALL_IN_CASH,
            "export_leg_basis": sorted(observed) or [PRICE_EXPORT_BASIS_UNKNOWN],
            "export_leg_is_cash": is_cash,
            "calculation_basis": (
                CALCULATION_BASIS_IMPORT_CASH_EXPORT_CASH
                if is_cash
                else CALCULATION_BASIS_IMPORT_CASH_EXPORT_RECONSTRUCTED
            ),
            "rule": (
                "the import leg is all-in cash -- wholesale, market tax, sourcing "
                "markup and energy tax, VAT inclusive -- and excludes fixed daily "
                "and annual charges, which are not per-kWh and so cannot distort a "
                "marginal figure, but do mean this will never reconcile to a "
                "supplier invoice. the export leg is reconstructed from the market "
                "price unless the source published a real feed-in price or VAT was "
                "applied to the reconstruction, and export_leg_is_cash says which. "
                "no forecast, planner valuation or inventory revaluation reaches "
                "any figure here"
            ),
        }
        self._roi_basis_cache = (key, published)
        return dict(published)

    @callback
    def seal_finalizable_days(self, plan: Any, today: date) -> int:
        """Seal every retained past day that qualifies. Returns how many moved.

        **Runs on an ordinary refresh rather than on a midnight timer**, because
        the condition being waited for is evidence, not a time. Yesterday's last
        quarter closes after midnight and is filed against yesterday, so the first
        valid refresh of today is the earliest moment the day is actually complete
        -- and a day that is *not* complete then simply gets asked again on the next
        one. A day is therefore sealed late rather than sealed short.

        Ascending, so the lifetime cursor can only ever move forwards, and
        idempotent twice over: :meth:`DayRecord.note_final_benefit` refuses a day
        that already has a figure, and the pass skips it before computing one.
        """
        if plan is None:
            return 0
        limits = plan.state.limits if plan.state is not None else None
        stamp = dt_util.utcnow().isoformat()
        sealed = 0
        for day in sorted(self.store.days):
            record = self.store.days[day]
            if record.final_benefit is not None:
                continue
            usable, _reason = self.day_finalizable(day, today)
            if not usable:
                continue
            benefit = self._day_benefit_eur(record, day, limits)
            if benefit is None:
                continue
            if record.note_final_benefit(
                finalized_at=stamp,
                benefit_eur=benefit,
                basis_version=REALIZED_BENEFIT_BASIS_VERSION,
            ):
                sealed += 1
        if sealed:
            self.store.schedule_save()
        return sealed

    @callback
    def _day_benefit_eur(self, record: Any, day: date, limits: Any) -> float | None:
        """Return one finalised day's realised battery benefit in EUR, or ``None``.

        The cash comparator and nothing else: what the meter would have cost with
        no battery, less what it actually cost. Both legs are measured, so the
        figure does not move when a forecast does -- which is the property that lets
        it be summed into a lifetime total at all.
        """
        resolved = self._prices_for_day(day, record.interval_count)
        if resolved is None:
            return None
        buy, sell, _basis = resolved
        window = self._realized_window_for(
            self._realized_series(record, (buy, sell), limits), limits
        )
        return window.realized_battery_benefit_eur

    @callback
    def lifetime_benefit(self, today: date) -> dict[str, Any]:
        """Return the cumulative realised battery benefit, and what it covers.

        ``sealed_benefit_eur`` carries the days the store no longer retains and the
        retained sealed days are added back, so the total is complete over
        ``[history_available_since, sealed_through_retained]`` and says so rather
        than implying a longer reach. A day inside that span that never qualified is
        reported as a hole -- the figure is still a true measurement of the days it
        covers, and a reader is told which days those are.
        """
        finalised = {
            day: record.benefit_eur_final
            for day, record in self.store.days.items()
            if record.benefit_eur_final is not None
        }
        retained_total = round(sum(finalised.values()), 6)
        total = round(self.store.sealed_benefit_eur + retained_total, 6)

        earliest = self.store.sealed_through
        if finalised:
            first_retained = min(finalised)
            earliest = first_retained if earliest is None else earliest
        last = max(finalised) if finalised else self.store.sealed_through

        # A past day the store retains, that carries no sealed figure, is a day the
        # lifetime total does not include. Counted rather than hidden: the total is
        # honest about its own coverage or it is not a lifetime total.
        unsealed = sorted(
            day
            for day, record in self.store.days.items()
            if day < today and record.final_benefit is None
        )
        return {
            "cumulative_realised_benefit_eur": total,
            "sealed_evicted_eur": self.store.sealed_benefit_eur,
            "sealed_evicted_through": (
                None
                if self.store.sealed_through is None
                else self.store.sealed_through.isoformat()
            ),
            "retained_sealed_eur": retained_total,
            # **Every day the figure covers, retained or not.** Counting only the
            # retained ones would make the published average climb each time a day
            # aged out of the window -- on a figure whose entire point is not to move
            # when nothing has happened.
            "sealed_days": len(finalised) + self.store.sealed_day_count,
            "retained_sealed_days": len(finalised),
            "history_available_since": (
                None if earliest is None else earliest.isoformat()
            ),
            "sealed_through": None if last is None else last.isoformat(),
            "unsealed_past_days": len(unsealed),
            "basis_version": REALIZED_BENEFIT_BASIS_VERSION,
        }

    @callback
    def day_finalizable(self, day: date, today: date) -> tuple[bool, str]:
        """Return whether ``day`` can be sealed, and the reason when it cannot.

        **Midnight is not the same thing as final, and treating it as such is how a
        lifetime total quietly loses its last quarter every day.** A quarter that
        spans midnight closes *after* it and is filed against the day it started in,
        so at 00:00 yesterday's last interval does not exist yet. Sealing on the
        clock would seal a day short, once, permanently.

        Every clause here is a way a day can be complete-looking and not complete:

        * the day is in the past -- an unfinished day has intervals still to come;
        * a record exists at all -- a day the integration was switched off for has
          no evidence, and an absent record is not a zero;
        * every one of its **own** ``interval_count`` intervals is present. That
          count is 92, 96 or 100 from real timezone arithmetic, so a DST day is
          judged against its own length rather than a nominal 96;
        * prices exist for the day, on either basis;
        * **nothing was skipped for want of a price.** This is the clause that
          matters most for a *cumulative* figure: an unpriced past interval shrinks
          the day's total in silence, always in the same direction, and a lifetime
          sum of quietly-short days is biased rather than noisy. A day with a price
          hole is not sealed at a smaller number, it is not sealed.

        A day that fails any clause stays re-derivable and is re-examined on the next
        refresh. If it never qualifies the cursor does not advance past it, and the
        lifetime figure says its history is incomplete rather than pretending
        otherwise.
        """
        if day >= today:
            return False, "day_not_past"
        record = self.store.days.get(day)
        if record is None:
            return False, "no_day_record"
        count = record.interval_count
        if any(record.measured[index] is None for index in range(count)):
            return False, "intervals_missing"
        # Separate from the clause above, because they fail for different reasons
        # and only one of them is fixable. ``total_load_at`` is also ``None`` when a
        # flexible load was expected and never recorded -- the whole-house figure is
        # then unknown by exactly the amount nobody measured, which is not a smaller
        # day, it is an unpriceable one.
        if any(record.total_load_at(index) is None for index in range(count)):
            return False, "load_boundary_incomplete"
        if self._prices_for_day(day, count) is None:
            return False, "no_stored_prices"
        return True, "finalizable"

    @callback
    def _prices_for_day(
        self, day: date, count: int
    ) -> tuple[list[float | None], list[float | None], str] | None:
        """Return ``(buy, sell, basis)`` for one civil day, or ``None``.

        **Live forecast first, persisted snapshot second, and the second half is
        what beta.42 added.** ``price_forecasts`` is rebuilt every refresh and holds
        only today and tomorrow, so any older day fell through and was skipped. The
        multi-day ledger therefore priced exactly one day while its own docstring
        claimed it read the stored issuances -- and the published window has been
        reporting ``days_priced: 1`` ever since it was written.

        The issuances *are* stored, for a year, with the day's own fixed components
        beside them. A past day is priced on the basis that was published for it and
        never on today's configuration: the operator may since have changed a feed-in
        adjustment or a VAT flag, and re-pricing history under a later setting would
        rewrite what already happened.
        """
        forecast = (self.price_forecasts or {}).get(day)
        if forecast is not None:
            buy: list[float | None] = [None] * count
            sell: list[float | None] = [None] * count
            for interval in forecast.intervals:
                if 0 <= interval.index < count:
                    buy[interval.index] = interval.import_price_eur_kwh
                    sell[interval.index] = interval.export_price_eur_kwh
            return buy, sell, PRICE_BASIS_LIVE_FORECAST

        snapshot = self.history.latest_price_snapshot(day)
        if snapshot is None:
            return None
        stored_buy = list(snapshot.import_price[:count])
        stored_sell = list(snapshot.export_price[:count])
        stored_buy += [None] * (count - len(stored_buy))
        stored_sell += [None] * (count - len(stored_sell))
        return stored_buy, stored_sell, PRICE_BASIS_STORED_SNAPSHOT

    def _realized_series(
        self,
        record: Any,
        prices: tuple[list[float | None], list[float | None]],
        limits: Any,
    ) -> dict[str, list[Any]]:
        """Return one day's measured series, aligned and priced by interval index.

        Extracted so the single-day and multi-day views cannot drift apart: they
        are the same arithmetic over a longer list.

        **Takes price lists rather than a forecast. beta.42.** A past day's prices
        do not come from the live forecast -- that object only ever holds today and
        tomorrow -- they come from the persisted snapshot of what was published at
        the time. Both sources produce two lists, so this takes the lists and lets
        :meth:`_prices_for_day` decide where they came from.
        """
        count = record.interval_count
        buy, sell = prices

        capacity = None if limits is None else limits.capacity_kwh
        energies = soc_series_to_energy(
            [record.soc_at(i) for i in range(count)], capacity_kwh=capacity
        )
        # **Differenced here rather than inside ``realized``**, because the split
        # needs per-interval movement and that module is handed series, never
        # asked to reconstruct them. Same arithmetic the battery totals already
        # use; both directions kept, because a ledger that discards discharge is
        # the defect beta.35 spent its lifecycle half fixing.
        charge_series: list[float | None] = [None] * count
        discharge_series: list[float | None] = [None] * count
        previous: float | None = None
        for index, energy in enumerate(energies):
            if energy is not None and previous is not None:
                delta = energy - previous
                charge_series[index] = max(0.0, delta)
                discharge_series[index] = max(0.0, -delta)
            if energy is not None:
                previous = energy
        return {
            "grid_import_kwh": [record.grid_import_at(i) for i in range(count)],
            "grid_export_kwh": [record.grid_export_at(i) for i in range(count)],
            "import_price_eur_kwh": buy,
            "export_price_eur_kwh": sell,
            # **The whole household, not the baseline. beta.42.**
            #
            # This fed ``baseline_at`` -- measured less the flexible load -- into a
            # counterfactual that is differenced against ``grid_import_at``, which
            # includes it. Only one of the two terms had been re-based, so on any
            # interval the vehicle charged, ``max(0, load - pv) - import`` collapsed
            # to zero and the battery's whole contribution vanished from the
            # realised figures without anything saying so.
            #
            # The forecast path keeps ``baseline_at``, which is correct for it.
            "load_kwh": [record.total_load_at(i) for i in range(count)],
            "production_kwh": [record.pv_at(i) for i in range(count)],
            "stored_energy_kwh": list(energies),
            "battery_charge_kwh": charge_series,
            "battery_discharge_kwh": discharge_series,
        }

    @callback
    def _note_opening_valuation(
        self, *, outcome: Any, plan: Any, now: datetime, today: date
    ) -> bool:
        """Value the energy this civil day opened with, once. Returns whether it wrote.

        **beta.39, and it is the only new persistence in the release.**

        Timing is the whole of the design. The opening energy the ledger reports is
        ``opening_inventory_kwh`` -- the *first recorded* state of charge of the day
        -- and a state of charge is sampled when a quarter closes, so at 00:00 there
        is nothing to value. This therefore fires at the first refresh of the day
        that has a recorded level, which is normally 00:15 or 00:30, and
        ``valued_at`` records exactly when. Valuing the live head energy at midnight
        instead would have been available sooner and wrong: the revaluation is a
        difference between two valuations of **one** energy, and that energy has to
        be the one the ledger publishes.

        Idempotent through ``DayRecord.note_opening_valuation``, whose guard is the
        field's own absence -- so a reload, a restart or an extra refresh cannot
        re-open a day and move the reference the whole day is measured from.

        **The write-once rule lives in exactly one place, deliberately.** An early
        return here as well would read as defence in depth and behave as a
        blindfold: with two guards in series, breaking either one on purpose
        changes nothing observable, so neither can be tested and both rot. The
        guard is on the record, where the field is.

        The floor and the bucket pitch are stored beside the value because
        ``V(floor) - V(e)`` is only comparable across refreshes if the lattice it
        was integrated over is the same one; see ``_forecast_revaluation_eur``.
        """
        record = self.store.days.get(today)
        if record is None:
            return False
        if not isinstance(outcome, EconomicOutcome) or not outcome.available:
            return False
        bucket_kwh = outcome.bucket_kwh
        if not bucket_kwh:
            return False
        limits = (
            plan.state.limits if plan is not None and plan.state is not None else None
        )
        energies = soc_series_to_energy(
            [record.soc_at(index) for index in range(record.interval_count)],
            capacity_kwh=None if limits is None else limits.capacity_kwh,
        )
        opening = opening_inventory_kwh(list(energies))
        if opening is None:
            return False
        value = self._position_value_eur(outcome, opening)
        if value is None:
            return False
        wrote = record.note_opening_valuation(
            valued_at=now.isoformat(),
            stored_energy_kwh=opening,
            position_value_eur=value,
            floor_kwh=outcome.desired.terminal_floor_kwh,
            bucket_kwh=bucket_kwh,
        )
        if wrote:
            self.store.schedule_save()
        return wrote

    @callback
    def _forecast_revaluation_eur(
        self, *, outcome: Any, record: Any, opening_kwh: float | None
    ) -> tuple[float | None, str | None, float | None, str | None]:
        """Return the revaluation, why not, and the persisted valuation behind it.

        ``V[now](e_open) - V[open](e_open)``: what the energy the day opened with is
        worth on this refresh's curve, less what it was worth on the curve that
        existed when the day opened. **Required rather than convenient**, and the
        proof is a subtraction: the position total less beta.38's operational
        identity is exactly this quantity, so without it forecast movement is
        silently attributed to today's operation. On the 2026-09-02 captures the
        *same* 12.269 kWh was worth 2.3001 EUR at 20:45 and 2.3659 EUR at 21:00 --
        6.6 cents of pure curve movement in one quarter.

        It is read from persistence and never reconstructed. The tempting shortcut
        -- marginal value times stored energy -- is a slope where this is an
        integral, over a curve the model itself reports as kinked, and on the same
        capture it gives 2.30 EUR against an actual 3.0942 EUR.

        Three refusals, each with its own reason and never a zero:

        * no record for the day yet, a day whose first refresh had no usable level,
          or a session in which writes are suspended -- ``no_opening_valuation``;
        * the bucket pitch has moved, so the two integrals were taken over
          different lattices and their difference is not a revaluation --
          ``valuation_reference_moved``;
        * the persisted energy and the level the ledger now reports disagree by
          more than one state-of-charge quantum, so the two valuations are not of
          the same energy -- ``opening_energy_mismatch``.

        **The reserve floor is deliberately not a refusal.** It moves with the load
        and production forecasts, and its effect on what the position is worth is
        forecast revaluation in the plainest sense. Both floors are published
        beside the figure so a reader can see how much of the movement is which.
        """
        stored = None if record is None else record.open_value
        if not isinstance(stored, dict):
            return None, ACCOUNTING_UNAVAILABLE_NO_OPENING_VALUATION, None, None
        valued_at = stored.get("at")
        persisted = stored.get("v")
        if persisted is None:  # pragma: no cover - validated on load
            return None, ACCOUNTING_UNAVAILABLE_NO_OPENING_VALUATION, None, None
        if not isinstance(outcome, EconomicOutcome) or not outcome.available:
            return None, ACCOUNTING_UNAVAILABLE_NO_PLAN, persisted, valued_at
        bucket_kwh = outcome.bucket_kwh
        # **Compared at the precision it is stored at, not at float precision.**
        # The document rounds to six decimals for compactness, so an exact test
        # against the live figure fails by ~3e-7 on an entirely unchanged lattice --
        # and did, publishing ``valuation_reference_moved`` on every refresh of a
        # stable installation. The pitch is ``quarter_dc / k`` for integer ``k`` and
        # depends only on the configured limits, so at six decimals a difference is
        # a genuinely different lattice: a reconfigured pack, a changed power limit,
        # or a state budget that flipped ``k``. Across one of those the two value
        # functions are not valuations of the same system.
        if not bucket_kwh or round(bucket_kwh, 6) != round(float(stored["b"]), 6):
            return (
                None,
                ACCOUNTING_UNAVAILABLE_VALUATION_REFERENCE_MOVED,
                persisted,
                valued_at,
            )
        if opening_kwh is None:
            return None, ACCOUNTING_UNAVAILABLE_NO_POSITION_VALUE, persisted, valued_at
        if (
            abs(float(stored["e"]) - opening_kwh)
            > ACCOUNTING_OPENING_ENERGY_TOLERANCE_KWH
        ):
            return (
                None,
                ACCOUNTING_UNAVAILABLE_OPENING_ENERGY_MISMATCH,
                persisted,
                valued_at,
            )
        # **Valued at the persisted energy, not at the level read again here.** The
        # two agree within a quantum by the guard above, and using the persisted
        # figure is what makes the subtraction a difference of one energy on two
        # curves rather than of two energies on two curves.
        current = self._position_value_eur(outcome, float(stored["e"]))
        if current is None:
            return None, ACCOUNTING_UNAVAILABLE_NO_POSITION_VALUE, persisted, valued_at
        return current - float(persisted), None, persisted, valued_at

    @callback
    def _open_quarter_value_eur(
        self, now: datetime
    ) -> tuple[float | None, float | None]:
        """Return what the quarter in flight has realised so far, and its coverage.

        **Measured, from the live integrators, and it has to be.** Storage does not
        hold this interval: a quarter is persisted when it closes, so the realised
        slice structurally cannot contain it -- which is why the 2026-09-02 captures
        had one quarter in neither the realised window nor the plan's remaining
        slice, and at 20:45 the missing index was the Sell row that delivered the
        1.437 kWh.

        The arithmetic is :func:`realized.open_quarter_value_eur`, shared with the
        closed intervals so a partial quarter and the history it becomes cannot rest
        on different rules. Coverage is published beside it because a term computed
        two seconds into a quarter is honestly near zero and a reader has to be able
        to tell that from nothing having happened.
        """
        if (
            self._grid_import_accumulator is None
            or self._grid_export_accumulator is None
            or self._pv_accumulator is None
        ):
            return None, None
        buy, sell = self.current_prices(now)
        house = self._accumulator.open_energy_kwh
        flexible = (
            0.0
            if self._ev_accumulator is None
            else self._ev_accumulator.open_energy_kwh
        )
        # The same baseline the ledger prices: house load less the flexible load,
        # never the raw meter reading. ``DayRecord.baseline_at`` subtracts it for
        # every closed interval and an open one must not be the exception.
        value = open_quarter_value_eur(
            grid_import_kwh=self._grid_import_accumulator.open_energy_kwh,
            grid_export_kwh=self._grid_export_accumulator.open_energy_kwh,
            load_kwh=max(0.0, house - flexible),
            production_kwh=self._pv_accumulator.open_energy_kwh,
            import_price_eur_kwh=buy,
            export_price_eur_kwh=sell,
        )
        return value, round(self._accumulator.open_coverage, 4)

    @callback
    def _sliced_series(
        self, series: dict[str, list[Any]], stop: int
    ) -> dict[str, list[Any]]:
        """Return the same series truncated to the first ``stop`` intervals.

        **Truncated rather than blanked.** Setting the excluded intervals to
        ``None`` would count every one of them in ``intervals_skipped``, and a
        reader comparing that against the partition would see a day full of gaps
        that are simply the future.
        """
        limit = max(0, stop)
        return {key: list(values)[:limit] for key, values in series.items()}

    @callback
    def _today_accounting(
        self, outcome: Any, plan: Any, now: datetime
    ) -> dict[str, Any]:
        """Return the civil day's economic position, as four terms and a total.

        **Publish-only and diagnostic-only.** Nothing here solves, nothing here
        writes, and no optimiser, reserve, policy, safety or control path reads it.
        The one write the release adds is ``_note_opening_valuation``, which runs on
        the refresh and not on this path.

        The partition is the plan's own: ``h`` is ``plan.intervals[0].index`` -- the
        first interval Stage A is still planning -- so the closed part of the day is
        ``[0, h-1)``, the quarter in flight is ``h-1`` and what remains is
        ``[h, N)``. Disjoint and exhaustive by construction on a 92, 96 or
        100-interval day, because it is defined by ``h`` and not by which intervals
        happen to carry data.
        """
        if plan is None or plan.target_day is None:
            return day_accounting(
                realised=None,
                in_progress_eur=None,
                in_progress_index=None,
                in_progress_coverage=None,
                remaining_expected_eur=None,
                forecast_revaluation_eur=None,
                unavailable_reason=ACCOUNTING_UNAVAILABLE_NO_PLAN,
            ).as_dict()
        record = self.store.days.get(plan.target_day)
        if record is None:
            return day_accounting(
                realised=None,
                in_progress_eur=None,
                in_progress_index=None,
                in_progress_coverage=None,
                remaining_expected_eur=None,
                forecast_revaluation_eur=None,
                unavailable_reason=ACCOUNTING_UNAVAILABLE_NO_DAY_RECORD,
            ).as_dict()
        forecast = (self.price_forecasts or {}).get(plan.target_day)
        if forecast is None:
            return day_accounting(
                realised=None,
                in_progress_eur=None,
                in_progress_index=None,
                in_progress_coverage=None,
                remaining_expected_eur=None,
                forecast_revaluation_eur=None,
                unavailable_reason=ACCOUNTING_UNAVAILABLE_NO_STORED_PRICES,
            ).as_dict()

        count = record.interval_count
        head = count
        if isinstance(outcome, EconomicOutcome) and outcome.desired.intervals:
            head = outcome.desired.intervals[0].index
        # **One rule, and it lives in the pure layer.** See ``day_partition``: the
        # three slices are defined by the plan's own head index and not by which
        # intervals happen to carry data, which is what makes them exhaustive on a
        # 92, 96 or 100-interval day.
        closed, in_progress_index, remaining_slice = day_partition(
            head=head, interval_count=count
        )

        limits = plan.state.limits if plan.state is not None else None
        resolved = self._prices_for_day(record.day, count)
        if resolved is None:  # pragma: no cover - the guard above already returned
            return day_accounting(
                realised=None,
                in_progress_eur=None,
                in_progress_index=None,
                in_progress_coverage=None,
                remaining_expected_eur=None,
                forecast_revaluation_eur=None,
                unavailable_reason=ACCOUNTING_UNAVAILABLE_NO_STORED_PRICES,
            ).as_dict()
        series = self._realized_series(record, (resolved[0], resolved[1]), limits)
        realised = self._realized_window_for(
            self._sliced_series(series, len(closed)), limits
        )

        in_progress, coverage = (
            self._open_quarter_value_eur(now)
            if in_progress_index is not None
            else (0.0, None)
        )

        remaining, remaining_count, remaining_reason = self._remaining_expected_eur(
            outcome, count
        )
        # **A day with a hole in it has no day total.**
        #
        # The plan's priced horizon is not guaranteed to reach local midnight: the
        # source publishes a *market* day and the plan is a local civil day, and on
        # a household whose Home Assistant runs outside the market's timezone one
        # published day cannot span the other -- the documented partial-coverage
        # case. Those intervals are then neither realised nor planned, and adding
        # up what is left would publish a figure that looks like the day and is not.
        # Counted in the partition and named, never quietly dropped.
        if remaining_count is not None and remaining_count != len(remaining_slice):
            remaining = None
            remaining_reason = ACCOUNTING_UNAVAILABLE_HORIZON_SHORT_OF_MIDNIGHT
        revaluation, reval_reason, persisted, valued_at = (
            self._forecast_revaluation_eur(
                outcome=outcome,
                record=record,
                opening_kwh=realised.opening_inventory_kwh,
            )
        )
        reason = remaining_reason or reval_reason
        if in_progress is None and reason is None:
            reason = ACCOUNTING_UNAVAILABLE_NO_OPEN_QUARTER_MEASUREMENT
        return day_accounting(
            realised=realised,
            in_progress_eur=in_progress,
            in_progress_index=in_progress_index,
            in_progress_coverage=coverage,
            remaining_expected_eur=remaining,
            forecast_revaluation_eur=revaluation,
            opening_valuation_eur=persisted,
            opening_valued_at=valued_at,
            opening_floor_kwh=(record.open_value or {}).get("f"),
            opening_bucket_kwh=(record.open_value or {}).get("b"),
            current_floor_kwh=(
                outcome.desired.terminal_floor_kwh
                if isinstance(outcome, EconomicOutcome) and outcome.desired.available
                else None
            ),
            current_bucket_kwh=(
                outcome.bucket_kwh if isinstance(outcome, EconomicOutcome) else None
            ),
            interval_count=count,
            realised_interval_count=len(closed),
            remaining_interval_count=len(remaining_slice),
            remaining_planned_interval_count=remaining_count,
            unavailable_reason=reason,
        ).as_dict()

    @callback
    def _remaining_expected_eur(
        self, outcome: Any, today_interval_count: int
    ) -> tuple[float | None, int | None, str | None]:
        """Return what the plan still expects today, on the **no-battery** basis.

        ``export_revenue - grid_import_cost + avoided_import_no_battery``, summed
        over the plan's own today slice. Term for term the construction
        ``realized.realized_net_value_eur`` uses on measured flows, which is what
        makes the two addable -- and the reason the avoidance is recomputed rather
        than taken from ``avoided_import_eur``, which rests on the per-interval idle
        counterfactual and is a different and smaller number wherever the inverter
        would have served the house by itself.

        ``switching_cost_eur`` is excluded. It is a hurdle rate with
        ``is_cash: False``, and the four model terms and the terminal credit are not
        in ``cost_eur`` either. Neither ``decision_advantage_eur`` nor
        ``today_interval_value_eur`` is used: the first is the plan against one
        whole-horizon ambient walk and the second is each interval against its own
        idle baseline, and the project already asserts that neither may be added to
        a realised figure.

        An empty slice returns ``0.0`` and not ``None``: late in the evening there
        genuinely is nothing left to expect, and that is an answer.
        """
        if not isinstance(outcome, EconomicOutcome) or not outcome.desired.available:
            return None, None, ACCOUNTING_UNAVAILABLE_NO_PLAN
        block = day_block_for(
            outcome.desired, today_interval_count=today_interval_count
        )
        intervals = block.get("intervals") or 0
        if not intervals:
            return 0.0, 0, None
        value = block.get("no_battery_value_eur")
        if value is None:  # pragma: no cover - the block computes it whenever priced
            return None, intervals, ACCOUNTING_UNAVAILABLE_AVOIDANCE_BASIS
        if block.get("avoidance_basis") != AVOIDANCE_BASIS_NO_BATTERY:
            # **Fail closed on a basis change.** If a later release re-bases the
            # day block, this figure stops being addable to a measured one and the
            # honest answer is no total rather than a mixed one.
            return None, intervals, ACCOUNTING_UNAVAILABLE_AVOIDANCE_BASIS
        return float(value), intervals, None

    @callback
    def _realized_window_for(self, series: dict[str, list[Any]], limits: Any) -> Any:
        """Price an assembled set of series, with this refresh's planner terms."""
        outcome = (self.data or {}).get("economic")
        return realized_window(
            grid_import_kwh=series["grid_import_kwh"],
            grid_export_kwh=series["grid_export_kwh"],
            import_price_eur_kwh=series["import_price_eur_kwh"],
            export_price_eur_kwh=series["export_price_eur_kwh"],
            load_kwh=series["load_kwh"],
            production_kwh=series["production_kwh"],
            stored_energy_kwh=series["stored_energy_kwh"],
            capacity_kwh=None if limits is None else limits.capacity_kwh,
            charge_efficiency=None if limits is None else limits.charge_efficiency,
            discharge_efficiency=(
                None if limits is None else limits.discharge_efficiency
            ),
            battery_charge_kwh=series["battery_charge_kwh"],
            battery_discharge_kwh=series["battery_discharge_kwh"],
            # **Valued at the level the ledger itself reports, not at the plan
            # head. beta.38.** Through beta.37 this was ``_stored_value_eur`` -- the
            # *head* position -- while the kWh beside it was the last recorded
            # level. In production the two are within a bucket of each other and the
            # discrepancy was invisible; inside an identity it is not, and a
            # difference between two ends priced at two different energies is not a
            # position change at all. Both ends now use one rule and one curve.
            closing_inventory_value_eur=self._position_value_eur(
                outcome, closing_inventory_kwh(series["stored_energy_kwh"])
            ),
            # **The term that was simply never passed. beta.38.**
            #
            # ``realized_window`` has accepted it since beta.35 and no caller ever
            # supplied one, so ``opening_inventory_value_eur`` was ``None`` in
            # production and ``realised_plus_remaining_value_eur`` was ``None`` with
            # it -- a total missing one of its terms, which is a different number
            # wearing the same name. Nothing was wrong with the arithmetic; the
            # caller was the incomplete half.
            #
            # Valued on **this refresh's curve**, the same one the closing figure
            # uses, so their difference is what operating the battery achieved and
            # carries no revaluation. And valued at the energy the ledger itself
            # reports, through the one shared rule, so the number priced is provably
            # the number published. Revaluation -- what the *opening* position was
            # worth under the curve that existed *then* -- needs a persisted historic
            # value and is beta.39 work. It is not approximated here.
            opening_inventory_value_eur=self._position_value_eur(
                outcome, opening_inventory_kwh(series["stored_energy_kwh"])
            ),
            model_switching_cost_eur=(
                None
                if not isinstance(outcome, EconomicOutcome)
                else outcome.desired.switching_cost_eur
            ),
            model_grid_charge_margin_eur=(
                None
                if not isinstance(outcome, EconomicOutcome)
                else outcome.desired.grid_charge_margin_eur
            ),
            model_throughput_cost_eur=(
                None
                if not isinstance(outcome, EconomicOutcome)
                else outcome.desired.battery_throughput_cost_eur
            ),
        )

    @callback
    def _economic_prices(
        self,
        *,
        demands: tuple[IntervalDemand, ...],
        today: date,
        tomorrow: date,
        tz: tzinfo,
        today_interval_count: int,
        price_forecasts: dict[date, PriceForecast],
    ) -> tuple[IntervalPrice, ...]:
        """Align the price series onto the plan's own interval identity.

        By absolute instant, never by position. The plan indexes a continuous
        chronological run through today and on into tomorrow, while the source
        files prices under a *market* day that need not share either boundary --
        so an interval is priced by looking up the instant it begins, and an
        instant nobody priced yields an unknown rather than a neighbour's figure.
        """
        by_start: dict[datetime, IntervalPrice] = {}
        for forecast in price_forecasts.values():
            for interval in forecast.intervals:
                by_start[interval.start_utc] = IntervalPrice(
                    import_eur_kwh=interval.import_price_eur_kwh,
                    export_eur_kwh=interval.export_price_eur_kwh,
                )

        aligned: list[IntervalPrice] = []
        for demand in demands:
            if demand.index < today_interval_count:
                start = interval_start_utc(today, demand.index, tz)
            else:
                start = interval_start_utc(
                    tomorrow, demand.index - today_interval_count, tz
                )
            aligned.append(by_start.get(start, IntervalPrice()))
        return tuple(aligned)

    async def _async_record_economic_evidence_safely(
        self,
        *,
        outcome: EconomicOutcome | None,
        plan: BatteryPlan | None,
        now: datetime,
        today: date,
        tz: tzinfo,
    ) -> None:
        """Record what Phase 8 believed, or say why it could not be recorded."""
        try:
            await self._async_record_economic_evidence(
                outcome=outcome, plan=plan, now=now, today=today, tz=tz
            )
        except Exception:
            self._log.warning(
                _ECONOMIC_LOG,
                (
                    "Economic evidence could not be recorded this refresh. The "
                    "plan itself and every other layer are unaffected; the "
                    "record for this issuance is simply not stored"
                ),
            )
            _LOGGER.debug("economic evidence recording failed", exc_info=True)

    async def _async_record_economic_evidence(
        self,
        *,
        outcome: EconomicOutcome | None,
        plan: BatteryPlan | None,
        now: datetime,
        today: date,
        tz: tzinfo,
    ) -> None:
        """Store the plan, and the settings and capability it was computed under.

        **The settings fingerprint is why this exists.** Prices, load, production
        and the reserve are all persisted already, so the arithmetic is
        reproducible -- but a threshold the user changed, or an opt-in they turned
        on, would otherwise make every earlier plan unverifiable.

        Change-triggered by *input* fingerprint, so ninety-six refreshes against
        unchanged inputs store one document. The plan itself differs every
        quarter-hour, which is exactly why it is not what the digest is over.
        """
        if outcome is None or plan is None:
            return

        try:
            await self.history.async_ensure_days([today])
        except Exception:
            _LOGGER.debug("economic evidence partitions unavailable", exc_info=True)
            return

        load_snapshots = self.history.snapshots(today)
        pv_snapshot = self.history.latest_pv_snapshot(today)
        price_snapshot = self.history.latest_price_snapshot(today)
        reserve_snapshot = self.history.latest_reserve_snapshot(today)
        snapshot = build_economic_snapshot(
            outcome,
            issued_at=now,
            target_day=today,
            tz_key=str(tz),
            execution_blocked_reason=self.economic_blocked_reason,
            config_fingerprint=fingerprint_battery_config(
                capacity_kwh=self.config.battery_capacity_kwh,
                min_soc_percent=self.config.battery_min_soc_percent,
                max_charge_kw=self.config.battery_max_charge_kw,
                max_discharge_kw=self.config.battery_max_discharge_kw,
                round_trip_efficiency_percent=(
                    self.config.battery_round_trip_efficiency_percent
                ),
                max_soc_percent=BATTERY_MAX_SOC_PERCENT,
            ),
            settings_fingerprint=fingerprint_settings(
                minimum_trade_gain_eur=self.config.minimum_trade_gain_eur,
                grid_charge_margin_eur_per_kwh=(
                    self.config.grid_charge_margin_eur_per_kwh
                ),
                battery_throughput_cost_eur_per_kwh=(
                    self.config.battery_throughput_cost_eur_per_kwh
                ),
                allow_grid_charging=self.config.allow_grid_charging,
                allow_battery_export=self.config.allow_battery_export,
                bucket_kwh=outcome.bucket_kwh,
            ),
            price_fingerprint=(
                None if price_snapshot is None else price_snapshot.fingerprint
            ),
            load_fingerprint=(
                load_snapshots[-1].fingerprint if load_snapshots else None
            ),
            pv_fingerprint=None if pv_snapshot is None else pv_snapshot.fingerprint,
            reserve_fingerprint=(
                None if reserve_snapshot is None else reserve_snapshot.fingerprint
            ),
            # **The same derivation the entity publishes. beta.37.** Passed in rather
            # than recomputed inside the builder, so a persisted figure and a
            # dashboard figure describing the same solve cannot differ -- and so the
            # pure snapshot layer keeps knowing nothing about calendars or prices.
            economic_value=self._economic_value_for(outcome, plan, now),
        )
        if self.history.add_economic_snapshot(snapshot):
            # Debounced, like the four evidence layers beside it.
            self.history.schedule_save()

    async def _async_record_reserve_evidence_safely(
        self,
        *,
        plan: BatteryPlan | None,
        now: datetime,
        today: date,
        tomorrow: date,
        tz: tzinfo,
        today_interval_count: int,
        absorption_modelled: bool,
        absorption_reason: str,
    ) -> None:
        """Record what reserve was believed necessary, or say why it could not be.

        Wrapped like every evidence step beside it: the learning history is the
        irreplaceable half, so a fault here must never cost a refresh.
        """
        try:
            await self._async_record_reserve_evidence(
                plan=plan,
                now=now,
                today=today,
                tomorrow=tomorrow,
                tz=tz,
                today_interval_count=today_interval_count,
                absorption_modelled=absorption_modelled,
                absorption_reason=absorption_reason,
            )
        except Exception:
            self._log.warning(
                _RESERVE_LOG,
                (
                    "Reserve evidence could not be recorded this refresh. The "
                    "requirement itself and every other layer are unaffected; "
                    "the record for this issuance is simply not stored"
                ),
            )
            _LOGGER.debug("reserve evidence recording failed", exc_info=True)

    async def _async_record_reserve_evidence(
        self,
        *,
        plan: BatteryPlan | None,
        now: datetime,
        today: date,
        tomorrow: date,
        tz: tzinfo,
        today_interval_count: int,
        absorption_modelled: bool,
        absorption_reason: str,
    ) -> None:
        """Store the requirement, and the configuration it was computed against.

        **The configuration fingerprint is the whole reason this exists.** Both
        forecasts are already persisted, so the arithmetic is reproducible -- but
        capacity, the floor, the power limits and the efficiency live in the
        config entry, which keeps no history. Without this, a user raising their
        minimum state of charge would make every earlier belief unverifiable.

        Change-triggered by content fingerprint, so ninety-six refreshes a day
        against unchanged forecasts do not store ninety-six identical documents.
        The absorption pair is stored verbatim and reaches no figure: the
        projection below never saw it.
        """
        projection = None if plan is None else plan.reserve_projection
        if projection is None:
            return

        try:
            await self.history.async_ensure_days([today])
        except Exception:
            _LOGGER.debug("reserve evidence partitions unavailable", exc_info=True)
            return

        load_snapshots = self.history.snapshots(today)
        pv_snapshot = self.history.latest_pv_snapshot(today)
        start, end = _reserve_horizon_edges(
            projection,
            today=today,
            tomorrow=tomorrow,
            today_interval_count=today_interval_count,
            tz=tz,
        )
        snapshot = build_reserve_snapshot(
            projection,
            issued_at=now,
            target_day=today,
            tz_key=str(tz),
            floor_soc_percent=plan.reserve.configured_min_soc_percent,
            config_fingerprint=fingerprint_battery_config(
                capacity_kwh=self.config.battery_capacity_kwh,
                min_soc_percent=self.config.battery_min_soc_percent,
                max_charge_kw=self.config.battery_max_charge_kw,
                max_discharge_kw=self.config.battery_max_discharge_kw,
                round_trip_efficiency_percent=(
                    self.config.battery_round_trip_efficiency_percent
                ),
                max_soc_percent=BATTERY_MAX_SOC_PERCENT,
            ),
            horizon_start=start,
            horizon_end=end,
            same_interval_only=plan.reserve_same_interval_only,
            pv_blind=plan.reserve_pv_blind,
            load_fingerprint=(
                load_snapshots[-1].fingerprint if load_snapshots else None
            ),
            pv_fingerprint=None if pv_snapshot is None else pv_snapshot.fingerprint,
            pv_absorption_modelled=absorption_modelled,
            pv_absorption_reason=absorption_reason,
        )
        if self.history.add_reserve_snapshot(snapshot):
            # Debounced, like the three evidence layers beside it. Ninety-odd
            # refreshes a day must not each force a document write.
            self.history.schedule_save()

    async def _async_record_price_evidence_safely(
        self,
        *,
        forecasts: dict[date, PriceForecast],
        now: datetime,
        tz: tzinfo,
    ) -> None:
        """Record what prices were known, or say why it could not be recorded.

        Wrapped like every evidence step beside it: the learning history is the
        irreplaceable half and evidence is the newest half, so a fault here must
        never cost a refresh.
        """
        try:
            await self._async_record_price_evidence(forecasts=forecasts, now=now, tz=tz)
        except Exception:
            self._log.warning(
                _PRICE_LOG,
                (
                    "Price evidence could not be recorded this refresh. The "
                    "series itself and every other layer are unaffected; the "
                    "evidence for this issuance is simply not stored"
                ),
            )
            _LOGGER.debug("price evidence recording failed", exc_info=True)

    async def _async_record_price_evidence(
        self,
        *,
        forecasts: dict[date, PriceForecast],
        now: datetime,
        tz: tzinfo,
    ) -> None:
        """Store which prices were visible, at the instant they were visible.

        **There is no outcome half, and that is not an omission.** A price has no
        "what actually happened" to be scored against -- it was the price. What
        cannot be recovered afterwards is *which future prices were on screen when
        a plan was made*: they are revised and republished, so a later phase
        reading today's series has no way to tell what nine o'clock knew. That is
        a hindsight bias which can only be avoided in advance.

        Change-triggered by content fingerprint, so ninety-six refreshes a day
        against a source that republishes a handful of times do not store
        ninety-six identical documents. Nothing is learned here and nothing is
        corrected: this release records, and what the record *means* belongs to a
        later phase that can only answer it honestly from an unadjusted series.
        """
        if not self.config.frank_entry_id:
            return

        days = sorted(forecasts)
        try:
            await self.history.async_ensure_days(days)
        except Exception:
            _LOGGER.debug("price evidence partitions unavailable", exc_info=True)
            return

        changed = False
        for day, forecast in sorted(forecasts.items()):
            snapshot = build_price_snapshot(
                forecast,
                issued_at=now,
                interval_count=expected_quarters_for(day, tz),
            )
            if self.history.add_price_snapshot(snapshot):
                changed = True

        if changed:
            # Debounced, like the two evidence layers beside it. Ninety-odd
            # refreshes a day must not each force a document write.
            self.history.schedule_save()

    async def _async_record_pv_evidence_safely(
        self,
        *,
        forecasts: dict[date, PvForecast],
        now: datetime,
        today: date,
        tz: tzinfo,
    ) -> None:
        """Record PV evidence, or say why it could not be recorded.

        Wrapped for the reason the forecast-evidence step beside it is: evidence
        is the newest half and the learning history is the irreplaceable half, so
        a fault here must never take a refresh down with it.
        """
        try:
            await self._async_record_pv_evidence(
                forecasts=forecasts, now=now, today=today, tz=tz
            )
        except Exception:
            self._log.warning(
                _PV_LOG,
                (
                    "PV forecast evidence could not be recorded this refresh. "
                    "The forecast itself, the plan and every other layer are "
                    "unaffected; the evidence for this issuance is simply not "
                    "stored"
                ),
            )
            _LOGGER.debug("PV evidence recording failed", exc_info=True)

    async def _async_record_pv_evidence(
        self,
        *,
        forecasts: dict[date, PvForecast],
        now: datetime,
        today: date,
        tz: tzinfo,
    ) -> None:
        """Store what was forecast, and score days that have finished.

        Two halves, and neither computes a correction of any kind. Phase 5 records;
        deciding what the record *means* is Phase 9's job, and it can only do that
        honestly from a series that was never adjusted on the way in.

        Issuance is change-triggered by content fingerprint. Ninety-six refreshes a
        day against a source that updates a handful of times would otherwise store
        ninety-six identical documents.

        Scoring happens once a day is over, from the measured PV array in the
        learning store. A PV-blind interval is never scored: comparing a forecast
        that was never obtained against a real reading would manufacture error out
        of an outage, which is the same mistake the load-side scoring already
        refuses for a partly observed day.
        """
        if not self.config.has_pv:
            return

        yesterday = today - timedelta(days=1)
        days = sorted({*forecasts, yesterday})
        try:
            await self.history.async_ensure_days(days)
        except Exception:
            _LOGGER.debug("PV evidence partitions unavailable", exc_info=True)
            return

        changed = False
        for _day, forecast in sorted(forecasts.items()):
            snapshot = build_pv_snapshot(forecast, issued_at=now, today=today)
            if self.history.add_pv_snapshot(snapshot):
                changed = True

        if self.history.pv_outcome(yesterday) is None:
            snapshot = self.history.latest_pv_snapshot(yesterday)
            record = self.store.days.get(yesterday)
            if snapshot is not None and record is not None:
                outcome = score_pv_day(
                    snapshot,
                    actual=record.pv,
                    finalized_at=now,
                    tz_key=str(tz),
                    interval_count=record.interval_count,
                    target_day=yesterday,
                    ac_limit_kw=self._inverter_ac_limit_kw(),
                    flags=self._pv_day_flags(yesterday, snapshot),
                )
                if self.history.set_pv_outcome(outcome):
                    changed = True

        if changed:
            self.history.schedule_save()

    @callback
    def _pv_day_flags(self, day: date, snapshot: PvSnapshot) -> tuple[str, ...]:
        """Return the day-level flags for a finalised PV comparison.

        Membership is a **hard** barrier and the physical model is another: a
        forecast of a different set of roofs, or of the same roofs re-rated, is not
        poolable with what came before. A site merely appearing in the source
        without joining the declaration is informational, because it changed
        nothing about what was forecast.
        """
        flags: list[str] = []
        earlier = [
            other
            for other in self.history.pv_snapshots(day)
            if other.fingerprint != snapshot.fingerprint
        ]
        if any(
            other.provenance.selected_sites_identity
            != snapshot.provenance.selected_sites_identity
            for other in earlier
        ):
            flags.append(PV_FLAG_SELECTED_SITES_CHANGED)
        if any(
            other.provenance.selected_sites_model
            != snapshot.provenance.selected_sites_model
            for other in earlier
        ):
            flags.append(PV_FLAG_SELECTED_MODEL_CHANGED)
        if any(
            other.provenance.available_sites_identity
            != snapshot.provenance.available_sites_identity
            for other in earlier
        ):
            flags.append(PV_FLAG_AVAILABLE_SITES_CHANGED)
        if any(
            other.provenance.correction_key != snapshot.provenance.correction_key
            for other in earlier
        ):
            flags.append(PV_FLAG_SOURCE_CORRECTION_CHANGED)
        return tuple(flags)

    @callback
    def _inverter_ac_limit_kw(self) -> float | None:
        """Return the inverter's configured AC limit, in kW, or ``None``.

        Read from the vendor helper when it exists, for one purpose only:
        distinguishing a clipped day from an over-forecast one. On a big day the
        forecast exceeds the actual *by design*, because an inverter cannot pass
        more than its limit however bright it is -- and a later phase must not
        learn a correction for that.

        ``None`` when the helper is absent, which suppresses the detection rather
        than guessing a limit. A guessed ceiling would produce a flag that looked
        like evidence.
        """
        state = self.hass.states.get(SELECT_INVERTER_AC_LIMIT)
        if state is None:
            return None
        return _optional_number(str(state.state).split()[0] if state.state else None)

    @callback
    def _surplus_absorption(self) -> tuple[bool, str]:
        """Return whether the inverter is storing surplus, without ever raising.

        Wrapped for the same reason every layer added since Phase 2 is: this reads
        the vendor control surface, and a fault there must cost the projected state
        of charge rather than the whole refresh. The existing control-isolation
        test found this the moment it was added, which is exactly what it is for.

        A failure means the state could not be established, which is the same
        answer as an unreadable entity: absorption is not modelled.
        """
        try:
            return self._surplus_absorption_from_device()
        except Exception:
            self._log.warning(
                _PV_LOG,
                (
                    "Whether the inverter is storing surplus production could not "
                    "be determined; the projected state of charge treats surplus "
                    "as exported, which is the conservative reading. Nothing else "
                    "is affected"
                ),
            )
            _LOGGER.debug("Surplus absorption state unreadable", exc_info=True)
            return False, PV_ABSORPTION_STATE_UNREADABLE

    @callback
    def _surplus_absorption_from_device(self) -> tuple[bool, str]:
        """Return whether the inverter is storing surplus, and how that is known.

        The approved design treated "the inverter absorbs surplus autonomously" as
        unconditional physics. The vendor control surface contradicts that, and its
        own design notes say so: with **Excess Export** switched on, PV output
        below the inverter's AC limit is directed to house load and feed-in and the
        battery is charged with *zero*. Peak Shaving arms its own dispatch, and the
        dispatch vocabulary itself contains modes that forbid battery charging
        outright.

        So absorption is predicated on observable state rather than asserted. What
        it is *not* predicated on is the charging/discharging settings helper,
        whose four options govern grid charging and timed discharge only -- nothing
        there touches PV to battery, which is why baseline self-consumption
        absorption is real in the default configuration.

        Three outcomes, and the distinction between the last two is the point:

        * A feature is present and **on**, or a dispatch is running: absorption is
          suppressed or unknowable, so it is not modelled.
        * A feature is present but **unreadable**: it could be suppressing
          absorption and we cannot see it, so it is not modelled either.
        * A feature is **absent**: it does not exist on this installation and
          therefore cannot be suppressing anything, so absorption is modelled.
          Reading absence as ignorance would leave every installation without the
          vendor package permanently pessimistic about its own battery, which
          would be wrong rather than cautious.

        When absorption is not modelled the surplus becomes simulated export.
        That projects a *lower* state of charge and never claims stored energy the
        inverter is actually sending to the grid, which is the direction to be
        wrong in.

        This reads the inverter's state in every control mode, including ``off``.
        Reading is not controlling: ``off`` still means this integration attempts
        no control and writes nothing. A projected state of charge that silently
        assumed the opposite of the truth would be worse than one that looked at
        three booleans to find out.
        """
        capability = discover(self.hass)
        if capability.excess_export_active:
            return False, PV_ABSORPTION_EXCESS_EXPORT
        if capability.peak_shaving_active:
            return False, PV_ABSORPTION_PEAK_SHAVING
        # Checked before the dispatch read, because a feature boolean that cannot
        # be read could be hiding a feature that is on -- and neither boolean is
        # in the required-entity set, so neither appears in ``unavailable``.
        if capability.feature_flags_present and not capability.feature_flags_readable:
            return False, PV_ABSORPTION_STATE_UNREADABLE
        if capability.unavailable:
            return False, PV_ABSORPTION_STATE_UNREADABLE
        if read_snapshot(self.hass).dispatch_active:
            return False, PV_ABSORPTION_DISPATCH_ACTIVE
        if capability.missing or not capability.feature_flags_present:
            # No suppressing feature exists here, so ordinary self-consumption
            # applies. Named rather than folded into the permitted case, because
            # "there is nothing to suppress it" and "we checked and nothing is"
            # are different pieces of evidence.
            return True, PV_ABSORPTION_NO_SUPPRESSING_FEATURE
        return True, PV_ABSORPTION_SELF_CONSUMPTION

    # -- photovoltaic forecast -------------------------------------------

    async def _async_pv_forecasts_safely(
        self, *, today: date, tz: tzinfo
    ) -> dict[date, PvForecast]:
        """Read the PV forecast, or report why there is none.

        Isolated exactly as the three layers before it are, and under its own
        throttle key. A Solcast failure costs the PV forecast and nothing else:
        learning, both load forecasts, the forecast-error sensors, the battery
        plan and the control layer all read paths that never touch this one, so a
        third party's integration being reloaded must not cost a refresh.
        """
        tomorrow = today + timedelta(days=1)
        try:
            return await self._async_pv_forecasts(today=today, tz=tz)
        except Exception:
            self._log.warning(
                _PV_LOG,
                (
                    "The PV forecast could not be read this refresh. Planning "
                    "continues PV-blind and every other layer is unaffected; no "
                    "value is substituted for the missing forecast"
                ),
            )
            _LOGGER.debug("PV forecast build failed", exc_info=True)
            return {
                day: self._pv_unavailable(day, tz, PV_UNAVAILABLE_SERVICE_FAILED)
                for day in (today, tomorrow)
            }

    def _pv_unavailable(
        self,
        day: date,
        tz: tzinfo,
        reason: str,
        provenance: PvProvenance | None = None,
    ) -> PvForecast:
        """Return a named unavailability of the right shape for one day."""
        return PvForecast.unavailable_for(
            target_day=day,
            tz_key=str(tz),
            interval_count=expected_quarters_for(day, tz),
            reason=reason,
            provenance=provenance,
            daylight=self._daylight_window(day, tz),
        )

    async def _async_pv_forecasts(
        self, *, today: date, tz: tzinfo
    ) -> dict[date, PvForecast]:
        """Read today's and tomorrow's expected generation.

        One request covers both days -- the source returns a contiguous series and
        the same rows are mapped twice, once per civil day, by two different index
        resolvers. Splitting it into two requests would double the work for
        identical data.
        """
        tomorrow = today + timedelta(days=1)
        days = (today, tomorrow)

        if not self.config.use_pv_forecast:
            reason = PV_UNAVAILABLE_NOT_CONFIGURED
            return {day: self._pv_unavailable(day, tz, reason) for day in days}

        capability = discover_solcast(self.hass, self.config.solcast_entry_id)
        self.pv_capability = capability
        if not capability.usable:
            reason = capability.unavailable_reason or PV_UNAVAILABLE_SERVICE_MISSING
            return {day: self._pv_unavailable(day, tz, reason) for day in days}

        facts = await read_facts(self.hass) if capability.diagnostic_service else None
        self.pv_facts = facts
        discovered = facts.site_ids if facts is not None else ()

        selected, origin, reason = await self._async_resolve_site_selection(discovered)
        if reason is not None:
            provenance = self._pv_provenance(facts, selected, discovered, origin=origin)
            return {
                day: self._pv_unavailable(day, tz, reason, provenance) for day in days
            }

        complete = bool(discovered) and set(selected) == set(discovered)
        start = utc_midnight(today, tz)
        # Through the *start* of the day after tomorrow: the requested range is
        # half-open, so a row beginning exactly at the end is not returned, which
        # is what makes this cover both days exactly and neither more nor less.
        end = utc_midnight(tomorrow + timedelta(days=1), tz)

        if complete:
            # Every discovered site belongs here, so the source's own aggregate is
            # the right series and one request is enough.
            queries = [await read_forecast(self.hass, start=start, end=end)]
            site_rows = [(PV_AGGREGATE_SITE, list(queries[0].rows))]
        else:
            queries = [
                await read_forecast(self.hass, start=start, end=end, site_id=site_id)
                for site_id in selected
            ]
            site_rows = [
                (query.site_id, list(query.rows))
                for query in queries
                if not query.failed
            ]

        if all(query.failed for query in queries):
            provenance = self._pv_provenance(
                facts, selected, discovered, complete, origin
            )
            return {
                day: self._pv_unavailable(
                    day, tz, PV_UNAVAILABLE_SERVICE_FAILED, provenance
                )
                for day in days
            }

        provenance = self._pv_provenance(facts, selected, discovered, complete, origin)
        return {
            day: build_pv_forecast(
                site_rows,
                target_day=day,
                tz_key=str(tz),
                interval_count=expected_quarters_for(day, tz),
                index_of=self._pv_index_resolver(day, tz),
                daylight=self._daylight_window(day, tz),
                provenance=provenance,
            )
            for day in days
        }

    async def _async_resolve_site_selection(
        self, discovered: tuple[str, ...]
    ) -> tuple[tuple[str, ...], str, str | None]:
        """Return the selected identifiers, how they were chosen, and why not.

        The origin is returned rather than read back from the entry afterwards.
        Reading it back reported ``user`` on the very refresh that had just
        resolved it automatically, because the write had already landed by then --
        which would have labelled the first snapshot of every installation as a
        decision the user never made.

        Three distinct states, deliberately kept distinct:

        * **No answer stored yet.** Every discovered site is selected and the
          resolved set is written to the entry options *once*. Persisting it is
          the point: resolving "all of them" afresh on every refresh would mean a
          site added to Solcast next year silently joined this installation's
          plan, which is the exact failure the option exists to prevent. After
          persistence a new site is reported as available but unselected.
        * **A stored answer.** Used exactly as stored. A site the source no longer
          offers stays in it and is reported as missing rather than dropped, so a
          Solcast outage cannot quietly narrow the declaration.
        * **A stored empty answer.** A named unavailability. It is a decision, and
          falling back to "all of them" would overrule it.
        """
        if self.config.solcast_selection_stored:
            selected = self.config.selected_solcast_site_ids
            if not selected:
                return (), PV_SELECTION_ORIGIN_STORED, PV_UNAVAILABLE_EMPTY_SELECTION
            return selected, PV_SELECTION_ORIGIN_STORED, None

        if not discovered:
            # Nothing to persist, and nothing guessed. The next refresh tries
            # again; a failed discovery must never write a selection.
            return (
                (),
                PV_SELECTION_ORIGIN_AUTO,
                PV_UNAVAILABLE_NO_SITES_DISCOVERED,
            )

        # Scheduled rather than written inline. Writing the options fires this
        # entry's own update listener, which reloads the entry -- and doing that
        # from inside a running refresh tears down the coordinator halfway
        # through the very refresh that had just resolved the answer. The task
        # runs once the refresh has finished, so the reload is clean.
        #
        # The resolved set is returned and used immediately, so this refresh
        # already produces a PV-aware plan rather than waiting for the reload.
        if not self._pv_selection_write_scheduled:
            self._pv_selection_write_scheduled = True
            self.hass.async_create_task(self._async_store_site_selection(discovered))
        return discovered, PV_SELECTION_ORIGIN_AUTO, None

    async def _async_store_site_selection(self, discovered: tuple[str, ...]) -> None:
        """Persist the resolved site membership exactly once.

        Re-checked before writing: a refresh may have overlapped with the user
        answering the question themselves in the options form, and their answer
        must win over a default resolved from discovery.
        """
        self.config = SourceConfig.from_entry(self.entry)
        if self.config.solcast_selection_stored:
            return
        self.hass.config_entries.async_update_entry(
            self.entry,
            options={
                **self.entry.options,
                CONF_SELECTED_SOLCAST_SITE_IDS: list(discovered),
            },
        )
        _LOGGER.info(
            "Solcast site membership resolved to %s on first discovery and "
            "stored; a site added later will be reported as available but not "
            "selected",
            ", ".join(discovered),
        )

    def _pv_provenance(
        self,
        facts: SolcastFacts | None,
        selected: tuple[str, ...],
        discovered: tuple[str, ...],
        complete: bool = False,
        origin: str = PV_SELECTION_ORIGIN_AUTO,
    ) -> PvProvenance:
        """Assemble the provenance block from what the source actually said."""
        by_id = (
            {} if facts is None else {site.resource_id: site for site in facts.sites}
        )
        chosen = [by_id[site_id] for site_id in selected if site_id in by_id]
        ac_total = _capacity_total(site.capacity_kw for site in chosen)
        dc_total = _capacity_total(site.capacity_dc_kw for site in chosen)

        return PvProvenance(
            integration_version=None if facts is None else facts.integration_version,
            selected_site_ids=tuple(selected),
            selected_site_display_names=tuple(site.name for site in chosen),
            selected_sites_identity=sites_identity(selected),
            selected_sites_model=sites_model(
                chosen, () if facts is None else facts.excluded_sites
            ),
            selected_site_count=len(selected),
            available_site_count=len(discovered),
            available_sites_identity=sites_identity(discovered),
            selection_complete=complete,
            selection_origin=origin,
            membership_declared=bool(selected),
            selected_capacity_ac_total_kw=ac_total,
            selected_capacity_dc_total_kw=dc_total,
            excluded_sites=() if facts is None else facts.excluded_sites,
            estimate_key=None if facts is None else facts.estimate_key,
            dampened=None if facts is None else facts.dampening_enabled,
            auto_dampening_active=None if facts is None else facts.auto_dampening,
            get_actuals=None if facts is None else facts.get_actuals,
            use_actuals=None if facts is None else facts.use_actuals,
            hard_limit_raw=None if facts is None else facts.hard_limit_raw,
            hard_limit_binding=(
                None if facts is None else facts.hard_limit_binds(dc_total)
            ),
            api_limit=None if facts is None else facts.api_limit,
            api_used=None if facts is None else facts.api_used,
            forecast_health=None if facts is None else facts.forecast_health,
            actual_pv_entity=self.config.pv_power_entity,
        )

    @callback
    # -- quarter-hour prices ---------------------------------------------

    def _price_index_resolver(
        self, day: date, tz: tzinfo
    ) -> Callable[[datetime], int | None]:
        """Return a **bounded** index resolver for one civil day.

        Bounded where the PV resolver is not, because the two models differ: a PV
        forecast is a fixed-length day and range-checks downstream, while a price
        series holds only the intervals it actually knows. An unbounded index here
        would file a block from the neighbouring market day at a position that
        does not exist -- so out of range returns ``None`` and is counted, which is
        the normal and expected outcome whenever Home Assistant does not run in
        the market's own timezone.
        """
        count = expected_quarters_for(day, tz)

        def index_of(start: datetime) -> int | None:
            index = index_for_start_utc(day, start, tz)
            return index if 0 <= index < count else None

        return index_of

    def _price_unavailable(
        self,
        day: date,
        tz: tzinfo,
        reason: str,
        provenance: PriceProvenance | None = None,
    ) -> PriceForecast:
        """Return a named unavailability of the right shape for one day."""
        return unavailable_price_forecast(
            tz_key=str(tz),
            reason=reason,
            target_day=day,
            expected_intervals=expected_quarters_for(day, tz),
            provenance=provenance,
        )

    def _price_forecasts_safely(
        self, *, now: datetime, today: date, tz: tzinfo
    ) -> dict[date, PriceForecast]:
        """Read the price series, or report why there is none.

        Isolated under its own log key exactly as the layers before it are. A
        price failure costs the price block and nothing else -- and since nothing
        in the decision layer reads prices at all, it cannot cost a battery plan
        even in principle.
        """
        tomorrow = today + timedelta(days=1)
        try:
            return self._price_forecasts(now=now, today=today, tz=tz)
        except Exception:
            self._log.warning(
                _PRICE_LOG,
                (
                    "The price series could not be read this refresh. Nothing "
                    "downstream depends on it: no battery decision, no policy "
                    "and no command reads a price in this release"
                ),
            )
            _LOGGER.debug("price series build failed", exc_info=True)
            return {
                day: self._price_unavailable(
                    day, tz, PRICE_UNAVAILABLE_SOURCE_UNAVAILABLE
                )
                for day in (today, tomorrow)
            }

    def _price_forecasts(
        self, *, now: datetime, today: date, tz: tzinfo
    ) -> dict[date, PriceForecast]:
        """Read both published days and normalise them onto our own identity.

        Synchronous, and that is the substantive point rather than a detail:
        obtaining prices calls **no service at all**. Both days are read from
        entity state the source has already published, so there is no call site
        to misuse and the permitted service-caller set is untouched by this
        phase. "Alpha EMS cannot make the price source fetch" is structural.

        Both source days feed both of our days. The source publishes a *market*
        day and we plan a local civil day; when Home Assistant runs outside the
        market's timezone those are different spans, so part of one of our days is
        priced by the neighbouring market day. That is why the mapping is by
        instant and why partial coverage is reported rather than repaired.
        """
        tomorrow = today + timedelta(days=1)
        days = (today, tomorrow)
        entry_id = self.config.frank_entry_id

        capability = discover_frank(self.hass, entry_id)
        self.price_capability = capability
        if not capability.usable:
            reason = capability.unavailable_reason or PRICE_UNAVAILABLE_NOT_CONFIGURED
            self.price_options = FrankOptions(readable=False)
            return {day: self._price_unavailable(day, tz, reason) for day in days}

        options = read_options(self.hass, entry_id)
        self.price_options = options

        today_read = read_today(self.hass, capability)
        tomorrow_read = read_tomorrow(self.hass, capability)

        if not today_read.available:
            # Today is not optional, whatever the next day's publication state is.
            provenance = self._price_provenance(capability, options, today_read)
            reason = today_read.reason or PRICE_UNAVAILABLE_SOURCE_UNAVAILABLE
            return {
                day: self._price_unavailable(day, tz, reason, provenance)
                for day in days
            }

        provenance = self._price_provenance(capability, options, today_read)
        flags = () if options.readable else (PRICE_UNAVAILABLE_OPTIONS_UNREADABLE,)
        blocks = (
            ("today", list(today_read.blocks)),
            ("tomorrow", list(tomorrow_read.blocks)),
        )

        forecasts = {
            day: build_price_forecast(
                blocks,
                tz_key=str(tz),
                index_of=self._price_index_resolver(day, tz),
                target_day=day,
                expected_intervals=expected_quarters_for(day, tz),
                # An unreadable configuration yields no export price rather than a
                # guessed one: a fabricated adjustment would look exactly like a
                # real figure, which is worse than an absent one.
                adjustment=options.adjustment if options.readable else None,
                apply_vat=options.apply_vat,
                today_available=True,
                tomorrow_available=tomorrow_read.available,
                tomorrow_reason=tomorrow_read.reason,
                provenance=provenance,
                extra_flags=flags,
            )
            for day in days
        }
        return {
            day: self._price_cross_checked(forecast, capability, now)
            for day, forecast in forecasts.items()
        }

    def _price_provenance(
        self,
        capability: FrankCapability,
        options: FrankOptions,
        today_read: DayRead,
    ) -> PriceProvenance:
        """Return what is known about where this series came from."""
        return PriceProvenance(
            source_entry_id=self.config.frank_entry_id,
            source_country=capability.country,
            market_timezone=capability.market_timezone,
            today_entity_id=capability.today_entity_id,
            tomorrow_entity_id=capability.tomorrow_entity_id,
            availability_entity_id=capability.availability_entity_id,
            feed_in_adjustment=options.adjustment if options.readable else None,
            apply_feed_in_vat=options.apply_vat if options.readable else None,
            options_readable=options.readable,
            reported_resolution_minutes=today_read.reported_resolution_minutes,
            # Observed rather than reported: the source does not publish its own
            # last-update instant on any entity, so this is when the state machine
            # last wrote, which is a different fact and labelled as one.
            source_updated_at=today_read.updated_at,
            observed_freshness=True,
        )

    def _price_cross_checked(
        self,
        forecast: PriceForecast,
        capability: FrankCapability,
        now: datetime,
    ) -> PriceForecast:
        """Compare the current interval against the source's own two figures.

        The one check in this phase that can fail when the **source** changes
        rather than when our reading of a fixture changes. Agreement proves the
        reconstruction and the interval alignment simultaneously, against the
        running integration instead of against our own assumptions -- which is the
        direct answer to a defect that shipped because a fixture agreed with the
        parser that produced it.

        A disagreement is recorded. It never overrides the series, and it reaches
        no decision, because nothing in the decision layer reads prices.
        """
        current = forecast.interval_at(now)
        if current is None:
            return forecast

        their_import, their_export = read_current_prices(self.hass, capability)
        import_result = cross_check(current.import_price_eur_kwh, their_import)
        export_result = cross_check(current.export_price_eur_kwh, their_export)

        flags = list(forecast.flags)
        if import_result == PRICE_CROSS_CHECK_DISAGREES:
            flags.append(PRICE_FLAG_IMPORT_CROSS_CHECK_FAILED)
        if export_result == PRICE_CROSS_CHECK_DISAGREES:
            flags.append(PRICE_FLAG_EXPORT_CROSS_CHECK_FAILED)

        return replace(
            forecast,
            flags=tuple(flags),
            provenance=replace(
                forecast.provenance,
                import_cross_check=import_result,
                export_cross_check=export_result,
            ),
        )

    def _pv_index_resolver(
        self, day: date, tz: tzinfo
    ) -> Callable[[datetime], int | None]:
        """Return an index resolver for one civil day.

        The storage coupling stays here rather than travelling into the pure
        module, which is both a structural rule of this project and the reason
        every mapping case is testable against a resolver written by hand.
        """

        def index_of(start: datetime) -> int | None:
            return index_for_start_utc(day, start, tz)

        return index_of

    @callback
    def _daylight_window(self, day: date, tz: tzinfo) -> tuple[bool, ...]:
        """Return which of a day's intervals fall between sunrise and sunset.

        Advisory throughout: it never modifies a forecast value and it is on no
        safety path. What it buys is the best available detector for a whole class
        of timezone and offset bugs -- generation forecast in the dark -- caught on
        an installation rather than only in a test.

        ``sun.sun`` alone cannot do this, because it exposes only the *next*
        sunrise and sunset and so cannot describe tomorrow. Home Assistant's own
        astral helper can, for any date, with no new dependency. When it cannot
        answer, every interval is reported as non-daylight, which suppresses the
        detector rather than inventing a window -- the conservative direction for
        something whose only job is to raise a suspicion.
        """
        count = expected_quarters_for(day, tz)
        try:
            sunrise = get_astral_event_date(self.hass, SUN_EVENT_SUNRISE, day)
            sunset = get_astral_event_date(self.hass, SUN_EVENT_SUNSET, day)
        except Exception:  # pragma: no cover - astral is bundled and stable
            return (False,) * count
        if sunrise is None or sunset is None:
            return (False,) * count

        midnight = utc_midnight(day, tz)
        window: list[bool] = []
        for index in range(count):
            start = midnight + timedelta(minutes=QUARTER_MINUTES * index)
            end = start + timedelta(minutes=QUARTER_MINUTES)
            # An interval counts as daylight when any part of it is lit, so the
            # two boundary intervals are included rather than excluded. A forecast
            # of real generation in the interval containing sunrise is not a bug.
            window.append(end > sunrise and start < sunset)
        return tuple(window)

    def _staged_write_landed(self, verify: str) -> bool:
        """Return whether stage one's write can be *read back*.

        **Positive tests only.** Anything that is not an observed success is a
        failure, so a state nobody thought of does not pass by not being listed.

        Both readings are of local ``input_boolean`` helpers, which settle inside
        the blocking service call that wrote them. That is what makes checking in
        the same refresh meaningful, and it is why neither reading is
        ``sensor.alphaess_dispatch_start``: the device register lags a poll, so a
        cleanup gated on it would be withheld every time and the marker released
        never. The register is still what ownership and the dead-man read; it is
        not what tells us our own write landed.
        """
        snapshot = read_snapshot(self.hass)
        if verify == EXECUTION_VERIFY_MARKER_ON:
            return snapshot.owner_marker is True
        if verify == EXECUTION_VERIFY_NO_FAMILY_ACTIVE:
            return not snapshot.active_modes
        if verify == EXECUTION_VERIFY_DISPATCH_INACTIVE:
            # The **enable helper**, which is what we wrote, and not
            # ``sensor.alphaess_dispatch_start``: that register lags a poll, so a
            # cleanup gated on it would be withheld every time.
            return snapshot.dispatch_enabled is False
        if verify == EXECUTION_VERIFY_DISPATCH_SETPOINT:
            # A commanded power is only verified as *readable and signed the way
            # we sent it*. The exact float is not compared: the helper quantises,
            # and demanding equality would fail on the device's own rounding.
            setpoint = snapshot.dispatch_setpoint_kw
            return setpoint is not None and setpoint <= 0.0
        return False  # pragma: no cover - an unknown check fails closed

    async def _async_dispatch(
        self, report: dict[str, Any] | None, now: datetime
    ) -> None:
        """Send the authorized command, if there is one. There never is yet.

        **The single send site, and the only one there will ever be.** Isolated
        into its own method for two reasons: the report itself is synchronous and
        pure, so a send cannot hide inside it; and one narrow function is a thing
        an architecture test can assert about.

        ``authorize`` has already refused -- it checks the mode, the execution
        option and ``CONTROL_EXECUTION_AVAILABLE`` -- so this is unreachable in
        this release, and the adapter refuses again behind it. It exists now,
        unreachable, so that beta.20 flips a constant rather than also introducing
        a call site. It also assigns the two fields the cooldown gate reads, which
        have been dead since beta.14 and would otherwise make that gate
        ornamental at the moment it first mattered.
        """
        if report is None:
            return
        # One refresh only. Cleared before anything can set it again, so a stale
        # true cannot make a later refresh claim a start it did not make.
        self._activation_confirmed = False
        authorization = report.get("authorization") or {}
        if not authorization.get("authorized"):
            return
        steps = report.get("commands_planned") or 0
        if steps <= 0:  # pragma: no cover - authorize already refuses this
            return
        commands = self._pending_commands
        if not commands:  # pragma: no cover - defensive
            return
        # **Before the writes, and the ordering is the point.** The record is the
        # second of the two ownership factors, and writing it afterwards would leave
        # a window in which a dispatch is running that nothing can prove Alpha EMS
        # caused. Written first, a failure mid-sequence leaves a record beside a
        # marker and no dispatch -- which the ownership rule already reads as stale,
        # and which the marker release can clear.
        #
        # It is a *claim*, not a grant: ownership still requires a later readback
        # whose ``dispatch_start`` matches, so this cannot appropriate a dispatch
        # somebody else started. And never for a reset, which ends a run rather
        # than beginning one.
        run = self._carried
        # **The claim is written once, by the sequence that actually arms.**
        #
        # Until beta.25 every non-stopping refresh rewrote it, which cleared the
        # ``dispatch_start`` stamp each time -- and that was survivable only
        # because the old sustain re-issued the activation boolean, so the device
        # restamped its own start instant to match the new write. beta.25's sustain
        # deliberately does not toggle the enable: writing the duration re-arms the
        # vendor timer on its own, and not toggling it is what keeps the dispatch
        # continuously live.
        #
        # So without this the settle window could never match again after the first
        # refresh, ownership fell back to ``unproven`` on the second, and a charge
        # became unstoppable while still running. The claim is about the *arming*;
        # once stamped it is completed rather than replaced.
        # **Whichever authority actually produced this command.** beta.34.
        #
        # Until now this read ``self._carried`` and nothing else, while the command
        # was built from the admitted plan's open row -- and since beta.29 those
        # two are routinely not the same thing. Stage A's horizon head is
        # ``elapsed + 1``, so the publication made *at* 19:45 covers 20:00 onward
        # and structurally cannot affirm the 19:45 run; the run ends, the row stays
        # open, and the row is the execution authority. That is beta.29's whole
        # design and it is correct.
        #
        # What was wrong is that the claim did not follow it. An arm from an open
        # row wrote no causal record, so ownership had nothing to prove and the
        # dispatch became untouchable -- the 13:30 incident of 2026-08-29, where
        # every subsequent tick read ``ownership_not_owned`` and declined to write
        # while the pack charged 3.1 kWh nobody had authorised.
        #
        # This grants nothing. The claim is still only a claim; ownership still
        # requires the later ``dispatch_start`` readback to match, so a dispatch
        # somebody else started still cannot be appropriated.
        claim = self._claim_authority(run)
        claiming = (
            claim is not None and not self._pending_is_reset and self._pending_activates
        )
        # **An activation nobody can claim is never sent. beta.34.**
        #
        # The two conditions used to disagree: the claim required a carried run,
        # the command list did not. A refresh could therefore arm the vendor
        # helper from the admitted plan's frozen schedule after Stage A had
        # withdrawn the run -- which it did, on 2026-08-29 at 13:30 -- and leave a
        # real dispatch running that ownership could never prove was ours. Stage B
        # then refused to touch it, correctly, for twenty minutes, and the vendor
        # dead-man ended it.
        #
        # Fail-closed, and deliberately at the send site rather than earlier: this
        # is the last point where the step list and the claim are both visible, so
        # it is the only place the two can be held to the same condition. Nothing
        # is written, not even stage one.
        if self._pending_activates and not self._pending_is_reset and claim is None:
            _mark_execution_error(report, CONTROL_REFUSE_NO_CLAIMABLE_RUN)
            _mark_arm_refused(report, CONTROL_REFUSE_NO_CLAIMABLE_RUN)
            _LOGGER.warning(
                "A dispatch activation was refused because neither a carried run "
                "nor an admitted plan could claim it. Nothing was written; the "
                "helper was not armed"
            )
            return
        if claiming:
            self._write_execution_record(
                claim, self._pending_command, self._pending_snapshot, now
            )
        # **Two stages, and stage two is conditional.** This is the whole of
        # beta.25 at the send site: an activation may not be issued until the
        # ownership claim has been read back, and a running dispatch's fields may
        # not be disturbed until the deactivation has been read back.
        stage_one = self._pending_stage_one
        stage_two = self._pending_stage_two
        verify = self._pending_verify
        landed = True
        try:  # pragma: no cover - the barrier makes this unreachable
            # **The quarter-boundary sequence takes the lock too.**
            #
            # It is the *other* write path, and the one whose interleaving would
            # be worst: mode, power, cutoff and duration must all be settled
            # before the enable, so a sixty-second correction landing in the
            # middle would arm a dispatch against half-written values. Held
            # across the readback as well, because a verification that runs after
            # the lock is released is reading a world another sequence may
            # already have moved.
            #
            # No re-entry: this path is reached from the refresh, which never
            # holds the lock, and the tick's own sequences use
            # ``_async_send_locked`` inside their held section rather than
            # re-acquiring it here.
            async with self._execution_lock:
                intent = self._executing_intent()
                if stage_one:
                    await async_execute(self.hass, stage_one, intent=intent)
                    landed = verify is None or self._staged_write_landed(verify)
                if landed and stage_two:
                    await async_execute(self.hass, stage_two, intent=intent)
        except ControlExecutionUnavailable:
            # The expected outcome while the barrier stands, and recorded rather
            # than swallowed: a release that believes it sent something is worse
            # than one that knows it could not.
            #
            # The claim is withdrawn rather than left behind. The barrier refuses
            # before the first service call, so this cannot discard a record for a
            # dispatch that did start.
            if claiming:
                self._clear_execution_record()
            # Not a failure: the barrier refused, which is what it is for.
            _mark_execution_error(report, "execution_unavailable", failed=False)
        except Exception:
            # **A failed write costs the write, never the refresh.** The send site
            # sits outside ``_build_control_report_safely`` -- it has to, because it
            # is the one place awaiting the adapter -- so without this an
            # unavailable helper or a rejected value would take down the whole
            # coordinator update.
            #
            # That is worse than losing one command, because the refresh loop is
            # what would *retry*. A stop interrupted partway leaves the dispatch
            # possibly still running, and recovering from it needs another refresh
            # to come round, read ``owned`` again and re-send the reset.
            #
            # **The claim is deliberately left in place.** ``plan_reset`` releases
            # the marker as its last step, so an interrupted stop leaves marker on
            # and record intact -- which reads as ``owned`` next refresh and
            # re-attempts the stop. Clearing it here would drop to ``unproven``,
            # and an unproven dispatch is never touched again.
            _LOGGER.exception(
                "A control command could not be sent. Ownership evidence has been "
                "left intact so the next refresh can retry"
            )
            _mark_execution_error(report, "write_failed")
        else:
            if not landed:
                # **Stage two was withheld, and which failure it was matters.**
                #
                # An unverified *claim* means nothing was armed -- the activation
                # lives in stage two -- so the record is withdrawn and the marker,
                # whatever it did or did not do, is left to the ordinary
                # stale-marker path. The alternative is a dispatch running under a
                # claim that was never real, which is the beta.24 fault itself.
                #
                # An unverified *stop* means the opposite: something may well
                # still be running, so every piece of ownership evidence is kept
                # so the next refresh reads ``owned`` and tries again. Clearing it
                # here would drop to ``unproven``, and an unproven dispatch is
                # never touched again -- the run would latch on for good.
                if verify == EXECUTION_VERIFY_MARKER_ON:
                    if claiming:
                        self._clear_execution_record()
                    _mark_execution_error(report, CONTROL_REFUSE_MARKER_NOT_VERIFIED)
                else:
                    _mark_execution_error(report, CONTROL_REFUSE_STOP_NOT_VERIFIED)
                _LOGGER.warning(
                    "A staged control sequence was stopped after its first stage "
                    "because the write could not be read back (%s). Nothing "
                    "further was sent",
                    verify,
                )
                return
            # **What the EMS is doing, not whether the last write returned.**
            #
            # A write that carries an activation or a power setpoint, on a refresh
            # that is not a stop, is the battery moving: that is ``executing``. A
            # cleanup, a marker release and a stop are not, and they leave the
            # eligibility state alone -- which for a stop is ``idle``, because
            # after it nothing is running.
            #
            # ``executed`` is not lost; it moves to the execution result below,
            # where "the last helper write succeeded" is exactly the question
            # being asked.
            _mark_command_result(report, CONTROL_STATE_EXECUTED)
            if not self._pending_is_reset and (
                self._pending_activates
                or any(step.entity_id == DISPATCH_POWER for step in commands)
            ):
                report["state"] = CONTROL_STATE_EXECUTING
            # **One record of what is on the wire, whichever path wrote it.** The
            # quarter refresh and the sixty-second tick both command power, so a
            # deadband comparing against only one of them would compare against a
            # stale figure and either chatter or go deaf.
            for step in commands:
                if step.entity_id == DISPATCH_POWER and step.value is not None:
                    previous = self._applied_setpoint_kw
                    self._applied_setpoint_kw = step.value
                    self._last_setpoint_write = now
                    self._setpoint_delta_kw = (
                        None if previous is None else step.value - previous
                    )
            # **From the power actually written**, not from Stage B's request.
            # beta.19 copied ``requested_kw`` here, so the report would have
            # asserted one figure while a different one was on the wire -- and it
            # dereferenced a block that is ``None`` in every state reachable
            # today, which would have failed the whole refresh.
            #
            # **And only when a power step was on the wire at all. beta.34.** Every
            # successful staged write reached this line, including a stale-marker
            # release whose entire command list is one ``input_boolean.turn_off``.
            # The live installation published ``applied_kw: 0.9, executed: true``
            # at 14:00 on 2026-08-29 while the dispatch was off and the only thing
            # sent was the marker. A figure describing a command that was not
            # issued is worse than no figure.
            if any(step.entity_id == DISPATCH_POWER for step in commands):
                _mark_execution_applied(report, self._pending_power_kw)
            self._last_control_write = now
            self._last_control_power_kw = self._pending_power_kw
            # **"Started" means a write carrying an activation succeeded**, and this
            # is the only place that can know it. Deriving it from the controller
            # state would say "started" for an *armed* decision -- computed, sent
            # nothing -- which is the one claim a release that writes must not get
            # wrong.
            self._activation_confirmed = self._pending_activates
            if self._pending_activates:
                # **The freeze happens in the same transition as the write. beta.38.**
                # The report that ran a moment ago could not have known this: it is
                # built before the send, so gating the freeze on
                # ``activation_confirmed`` alone put it a whole refresh late and
                # published ``started: false`` beside an executing quarter.
                # Idempotent, so the report-side call for every other path is safe.
                self._note_campaign_started(now)
            if self._pending_is_reset:
                # A verified stop *and* its cleanup both landed. Naming it is what
                # keeps ``idle`` from being the only thing a reader ever sees after
                # a run, and it is a transition rather than an inference.
                #
                # **Both words, since beta.39.** ``plan_reset`` is one staged
                # sequence carrying the stop, the resting values and the marker
                # release, so reaching this line means the cleanup landed too --
                # and a reader tracing a terminal needs the same two transitions
                # the tick path publishes, not a shorter sequence because a
                # different cadence found the ending.
                self._note_lifecycle(LIFECYCLE_STOPPED, now)
                self._note_lifecycle(LIFECYCLE_CLEANUP_COMPLETE, now)
            if any(step.entity_id == DISPATCH_DURATION for step in commands):
                # **Recorded on the re-arm, not on the activation.**
                #
                # Until beta.25 the two were the same event, because a sustain
                # re-issued the activation boolean. They are not the same now: the
                # duration write is what re-arms the vendor timer, and the enable
                # is left alone so the dispatch stays continuously live. Keying
                # this on activation meant the observation was taken once, at the
                # arm, and never again -- so a dead-man that stopped advancing
                # halfway through a run was compared against a deadline from the
                # start of it and looked fine.
                self._sustained_deadline = self._pending_deadline
                self._sustained_run_id = self._pending_run_id
            # **The claim is released only once the stop has actually landed.**
            # Clearing it while building the report would have been simpler and
            # would abandon a dispatch that is still running: with the record gone
            # but the marker on, ownership reads ``unproven``, and an unproven
            # dispatch is never touched again -- so a failed reset would latch the
            # run on permanently, which is the very fault F16 named.
            if self._pending_is_reset or self._pending_is_emergency:
                # **The whole authority state, not a hand-written subset. beta.35.**
                #
                # This block used to clear the record, the dead-man observation, the
                # row and the restart flag -- and argued the rule correctly for the
                # row: *its authority came from a dispatch that is no longer
                # running*. The same sentence is true of the schedule the row came
                # from, and beta.34 never applied it. So ``self._plan`` survived
                # every reset, and on 2026-08-29 the terminated campaign's frozen
                # schedule went on being narrated through a quarter it was not
                # executing and then **re-armed the inverter** from its third row.
                #
                # One teardown now, shared with the two tick-path stops and with
                # the refresh's own emergency stop, and it closes the campaign here
                # rather than leaving it to a later refresh that may never come. See
                # ``_abandon_execution``.
                self._abandon_execution(
                    now,
                    self._pending_stop_reason
                    or (
                        EXECUTION_STOP_MARKER_LOST
                        if self._pending_is_emergency
                        else None
                    ),
                )

    def _build_control_report_safely(
        self,
        *,
        plan: BatteryPlan | None,
        now: datetime,
        today: date,
        elapsed: int,
        today_interval_count: int,
    ) -> dict[str, Any] | None:
        """Build the control report, or ``None`` if it could not be built.

        Isolated the same way the two layers before it are, and under its own
        throttle key so a fault here cannot silence one there. A control failure
        costs the two control entities and nothing else: learning, both
        forecasts, the forecast-error sensors and the three battery entities all
        read from paths that never touch this one.
        """
        try:
            return self._build_control_report(
                plan=plan,
                now=now,
                today=today,
                elapsed=elapsed,
                today_interval_count=today_interval_count,
            )
        except Exception:
            self._log.warning(
                _CONTROL_LOG,
                (
                    "The control layer could not be evaluated this refresh. "
                    "Learning, both forecasts, the forecast-error sensors and "
                    "the battery plan are unaffected -- nothing in those paths "
                    "reads the control layer -- but the two control entities "
                    "will read unknown until it recovers. No command was sent; "
                    "no command was sent this refresh, and the physical controller "
                    "holds its last applied setpoint until the layer recovers"
                ),
            )
            _LOGGER.debug("Control report build failed", exc_info=True)
            return None

    def _build_control_report(
        self,
        *,
        plan: BatteryPlan | None,
        now: datetime,
        today: date,
        elapsed: int,
        today_interval_count: int,
    ) -> dict[str, Any]:
        """Run the control pipeline and report what it decided.

        The whole pipeline, in shadow as much as in active: the same intent, the
        same gate, the same command list. Only :func:`~.safety.authorize` sees
        the mode, and only it can refuse for a reason that is not a hazard. That
        is what makes shadow worth watching -- its verdict is the real verdict.

        Nothing here writes. The command list is computed, reported, and then
        deliberately dropped: authorization refuses on the release barrier, and
        the executor refuses again behind it.
        """
        mode = self.control_mode
        if mode == CONTROL_MODE_OFF:
            # No intent, no gate, no economics and no new run. Off means this
            # integration is not attempting control.
            #
            # **It does clean up after itself, once.** Until the amendment Off
            # returned here before reading the control surface, so a charge Alpha
            # EMS had started in Live and still owned would keep running -- the user
            # selecting Off could not stop it, which is the opposite of what
            # selecting Off means. So Off now looks for a dispatch of its own and
            # stops it; after that it is silent, and every later refresh returns
            # from here having written nothing.
            return self._off_report(now)

        capability = discover(self.hass)
        snapshot = read_snapshot(self.hass)
        # One snapshot for the whole evaluation, so the gate cannot see a
        # different world halfway through deciding.
        flows_now = self.read_flows()

        # Why there is no intent, resolved here because this is where the plan
        # lives. The gate relays it rather than reaching into Phase 3 itself.
        problem: str | None = None
        if plan is None:
            problem = INHIBIT_NO_PLAN
        elif plan.unavailable_reason is not None:
            problem = INHIBIT_PLAN_UNAVAILABLE
        elif not plan.decision.decided:
            problem = INHIBIT_NO_DECISION

        # Stage B first, because for a grid charge it is the authority. Computed
        # before the command so its intent can *be* the command.
        stage_b = self._stage_b_report(plan=plan, snapshot=snapshot, now=now, mode=mode)
        ownership_state = (stage_b.get("ownership") or {}).get("state")
        owned = ownership_state == OWNERSHIP_OWNED
        # **Degraded is not owned**, and the name is the point: the marker has
        # gone, so nothing may be adjusted -- but causation is still provable, so
        # one narrow write is authorised to end the run deliberately rather than
        # leaving it to the device dead-man.
        degraded = ownership_state == OWNERSHIP_DEGRADED
        # Read once, so the emergency grant below and the ownership state above
        # cannot disagree about the same snapshot.
        evidence_now = self._evidence_for(snapshot, now)
        charge_intent = self._stage_b_intent(plan=plan, now=now)

        # **The command source, and the one place it is decided.**
        #
        # Stage B drives a grid charge; everything else keeps the Phase-3
        # reserve-guard behaviour byte-for-byte. Two sources never compete for the
        # same action: Stage B returns an intent only for ``grid_charge``, and the
        # reserve guard never emits a charge at all.
        #
        # Until beta.20 the command was always the reserve-guard one, so Stage B's
        # power reached no actuator and opening the barrier would have armed a
        # discharge while the economic plan asked to buy.
        intent = charge_intent
        # **The reserve guard is suppressed while Stage B holds a run**, and this
        # is one of the two changes that make charge-only Live safe rather than
        # merely intended.
        #
        # The fallback exists so that a refresh with nothing to charge keeps the
        # Phase-3 behaviour byte-for-byte. But Stage B returns no intent on a
        # refresh where its own run is *waiting* -- ownership settling, a window not
        # yet open, a request reduced to nothing -- and on those refreshes the
        # fallback would hand the wheel to a layer that only ever discharges. In
        # Live that is a discharge command issued into a charge Alpha EMS is running
        # itself.
        #
        # So while a run is carried, or while a dispatch of ours is running or
        # protected, there is no second opinion. Outside that, nothing changes.
        stage_b_holds_the_run = self._carried is not None or (
            ownership_state in (OWNERSHIP_OWNED, OWNERSHIP_UNPROVEN)
            and self.store.execution_record is not None
        )
        if intent is None and not stage_b_holds_the_run:
            intent = translate(plan, now=now, horizon_minutes=CONTROL_HORIZON_MINUTES)
        requested = build_command(intent) if intent is not None else None

        # One context, read once, describing the *requested* command. The export
        # bound is taken from it, the command is clamped to fit, and the single
        # field that changed is replaced.
        #
        # ``replace`` rather than a second assembly, deliberately. Every other
        # field is a live reading, and re-reading them would let the two contexts
        # describe different instants -- the exact defect that once made a correct
        # inhibit look arithmetically wrong beside the figures printed next to it.
        # This way the gate provably evaluates the same world the bound came from.
        #
        # Sound because ``safe_discharge_power_kw`` reads only the meter, the
        # battery power and the margin, never ``device_power_kw``: the bound is
        # the same whichever power the context carries, so taking it before the
        # clamp cannot be circular. ``test_safe_discharge_clamp`` pins that.
        context = self._control_context(
            mode=mode,
            capability=capability,
            snapshot=snapshot,
            flows_now=flows_now,
            problem=problem,
            now=now,
            today=today,
            elapsed=elapsed,
            today_interval_count=today_interval_count,
            command=requested,
        )
        context = replace(context, dispatch_owned=owned)
        safe_power_kw = safe_discharge_power_kw(context)
        # **The bound is an export bound, so it governs a discharge only.** It is
        # the capacity the house can absorb, and its whole purpose is to stop
        # discharged energy reaching the grid. A charge cannot export, so clamping
        # one against it would cut a 4.3 kW charge to whatever the house happened to
        # be drawing -- silently under-delivering an approved plan for a reason that
        # does not apply to it.
        #
        # ``safety.evaluate`` already draws this line for the meter reading it needs
        # ("only a discharge can export"); the clamp did not, which is the same
        # direction-blindness the cooldown gate had. Discharge behaviour is
        # unchanged, byte for byte.
        # **``limit_command`` is untouched and still clamps the reserve-guard
        # discharge.** It is simply not applied to an authorised export, because the
        # bound it applies is "do not reach the meter" and an export's whole purpose
        # is to reach it. Applying it would silently reduce every authorised export
        # to whatever the house happened to be drawing.
        #
        # The export is not unbounded as a result: :mod:`.dispatch` has already
        # clamped it in the documented order, and ``authorize_export`` re-checks
        # every one of those bounds before the command is authorised at all.
        command = requested
        if (
            requested is not None
            and requested.action == ACTION_DISCHARGE
            and not self._export_quarter_open(now)
        ):
            command = limit_command(requested, safe_power_kw)
        if command is not None and command is not requested:
            context = replace(context, device_power_kw=command.power_kw)
        # **Two obligations, and beta.24 keeps them apart.** An earlier draft
        # gated the whole re-arm on a material power change, which is wrong: a
        # charge holding steady at 3.0 kW would never re-arm, so its dead-man would
        # never be refreshed and the dispatch would expire mid-run while this
        # controller believed it was still going. Constant power is the *common*
        # case, so that would have hit the first real campaign.
        #
        # The dead-man is refreshed on **every** refresh of an owned, active run,
        # whatever the figures say. The power helper is rewritten only when the
        # quantised power has actually moved -- writing a helper a value it already
        # holds is a service call that buys nothing.
        # Which run the persisted record names, read once. Not an ownership
        # derivation -- ownership is ``ownership_of`` and nothing else -- but the
        # identity check that makes "the same run" mean the same run.
        #
        # **Against whatever authority is executing, since beta.35.** This read
        # ``self._carried.run_id`` alone, which is ``None`` on every ordinary
        # quarter-authority refresh -- so the sustain could never match and a live
        # export could never be continued by the path meant to continue it. See
        # ``_authority_run_id``.
        recorded_run_id = self._owned_run_id()
        authority_run_id = self._authority_run_id()
        # **Resting inside a run is its own operation. beta.36.**
        #
        # It cannot be ``sustaining``: that requires ``command.moves_battery``, and a
        # hold moves nothing -- which is the honest answer, so the flag is not what
        # changes. Nor can it fall through to the arm branch, which requires a
        # non-zero setpoint. Without this branch a held row would plan **no writes at
        # all**, and the dead-man would expire mid-campaign and raise
        # ``EXECUTION_STOP_TIMER_NOT_REFRESHED`` -- a member of the abort family. The
        # campaign would die by a different door, fifteen minutes later, and the
        # payload would blame a timer.
        #
        # The authority conditions are identical to the sustain's, deliberately: a
        # hold is a continuation of a run we own and can prove we own, never a way
        # to write to a dispatch we cannot.
        holding = (
            command is not None
            and command.holds_at_zero
            and owned
            and bool(snapshot is not None and snapshot.dispatch_active)
            and recorded_run_id is not None
            and recorded_run_id == authority_run_id
        )
        sustaining = (
            command is not None
            and command.moves_battery
            and owned
            and bool(snapshot is not None and snapshot.dispatch_active)
            and recorded_run_id is not None
            and recorded_run_id == authority_run_id
        )
        # **Which operation this refresh performs, decided before anything is
        # built.** Until the amendment the order was inverted: a start was
        # authorised, and then the step list was swapped for a reset afterwards --
        # so the thing authorised was not the thing sent, and the reset was judged
        # by the start path's questions. Both faults come from that ordering, so
        # the ordering is what changed.
        #
        # A safety verdict that has turned unsafe under a dispatch of ours is
        # itself a stop condition. "Do not start this" and "do not stop what is
        # already running" look alike and are opposites; the second is never right.
        # **Two authorisation paths, and the export one is separate on purpose.**
        # ``evaluate`` refuses any discharge above the measured absorbing capacity,
        # which is right for the Phase-3 reserve guard -- there, energy reaching the
        # meter is an accident. For an admitted ``net_export`` the meter *is* the
        # objective, so the same question has the opposite answer.
        #
        # Nothing about ``evaluate`` changed. It still governs every charge, every
        # hold and every reserve-guard discharge, ``INHIBIT_WOULD_EXPORT`` still
        # fires on them, and the absorbing-capacity clamp still binds them. This is
        # a path that did not exist before, not a gate that was widened.
        verdict = evaluate(intent, context)
        if (
            intent is not None
            and self._quarter is not None
            and self._quarter.intent == EXECUTION_INTENT_NET_EXPORT
        ):
            verdict = self._export_verdict(intent, context, now)
        dispatch_active = bool(snapshot is not None and snapshot.dispatch_active)
        result = stage_b.get("result") or {}
        stop_reason = result.get("stop_reason")
        # **A failed command is a stop reason, and beta.31 recorded it as a note.**
        # ``_mark_execution_error`` has always written ``execution_error`` into this
        # block, and nothing has ever read it -- so ``Command Failed`` was in the
        # Activity vocabulary and structurally unreachable, which is R10. Read here,
        # and only where no more specific reason already stands.
        if not stop_reason and result.get("execution_error"):
            stop_reason = EXECUTION_STOP_EXECUTION_ERROR
        # **An unsafe verdict is not automatically a hazard. beta.36.**
        #
        # This line used to read ``owned and dispatch_active and not verdict.safe``,
        # with no discrimination at all, and the specific ``inhibit_reason`` was then
        # discarded on the way to the campaign terminal -- which carries no verdict
        # field. So *every* refusal on an owned live dispatch became
        # ``EXECUTION_STOP_SAFETY``: a member of the abort family, structurally
        # unsuppressable, total teardown, campaign blacklisted. On 2026-08-31 the
        # thing that took that path was "the authorised rate this instant is below
        # what the actuator can express", and it destroyed a charge campaign whose
        # plant was working perfectly.
        #
        # The partition is by *closed enumeration with a hazard default*
        # (``INHIBIT_HAZARD_REASONS`` is derived by subtraction, so a new inhibit
        # added later is a hazard until somebody argues otherwise in a diff). Nothing
        # in the hazard class is weakened: a stale sensor, a lost marker, a contested
        # dispatch, an out-of-range cutoff or a would-export still abort, still
        # unsuppressably.
        inhibit = verdict.inhibit_reason
        hazard = not verdict.safe and inhibit not in (
            INHIBIT_WITHDRAWAL_REASONS + INHIBIT_NO_COMMAND_REASONS
        )
        unsafe_while_owned = owned and dispatch_active and hazard
        if unsafe_while_owned and not stop_reason:
            stop_reason = EXECUTION_STOP_SAFETY
        # **Stage A publishing nothing is a statement about the future.** It is
        # therefore a withdrawal, and while a frozen admitted plan still covers this
        # instant it is withheld and published rather than obeyed -- bounded
        # identically to every other withdrawal, by the plan's own end, by the row
        # covering now, and by the vendor dead-man. Through beta.35 it arrived here as
        # an unsafe verdict and was promoted to ``safety``, so one missing solve
        # destroyed a multi-quarter charge.
        if (
            not stop_reason
            and owned
            and dispatch_active
            and inhibit in INHIBIT_WITHDRAWAL_REASONS
        ):
            stop_reason = EXECUTION_STOP_NO_BATTERY_PLAN
        # **A restart over our own live dispatch is itself a stop condition**, for
        # the reason set out in ``_adopt_persisted_run``: the quarter's progress did
        # not survive the process and cannot be reconstructed.
        progress_unknown = owned and self._quarter_progress_unknown
        if progress_unknown and not stop_reason:
            stop_reason = EXECUTION_STOP_QUARTER_PROGRESS_UNKNOWN
        # **Stage A revising the future may not abort the frozen present. beta.35.**
        #
        # Three stop reasons say only that Stage A has stopped carrying this run:
        # its freshness deadline passed, no publication re-affirmed it, or a
        # different run is running. Every one is a statement about what comes
        # *next*. Through beta.34 all three produced ``reset_required`` and killed
        # the dispatch -- on 2026-08-29 that reset a real 10 kW export 5.9 s into
        # its second quarter, while ``self._plan``, ``self._quarter`` and
        # ``control.intent`` all still described it correctly.
        #
        # Withheld, never hidden: the reason is published beside the authority that
        # outranked it, so a reader sees both. And withheld only for the withdrawal
        # family -- safety, a lost marker, a stalled dead-man, a failed command, the
        # user's own switch and a genuinely lost measurement are in
        # ``EXECUTION_ABORT_STOP_REASONS`` and are never suppressed, which is what
        # keeps this from being a way to ignore bad news.
        plan_authority_holds = self._plan_authority_holds(now)
        withheld_stop_reason: str | None = None
        if (
            plan_authority_holds
            and stop_reason in EXECUTION_WITHDRAWAL_STOP_REASONS
            and not unsafe_while_owned
            and not progress_unknown
            and not degraded
        ):
            withheld_stop_reason = stop_reason
            stop_reason = None
        resetting = (
            owned
            and withheld_stop_reason is None
            and bool(
                result.get("reset_required") or unsafe_while_owned or progress_unknown
            )
        )
        if degraded and not stop_reason:
            stop_reason = EXECUTION_STOP_MARKER_LOST
        # The ownership layer's own verdict, read rather than recomputed. There is
        # one definition of "a marker with nothing behind it" -- ``stale_marker`` in
        # the module that holds the evidence -- and neither this function nor
        # ``safety`` is allowed a second one.
        marker_is_stale = bool(
            (stage_b.get("ownership") or {}).get("clear_stale_marker")
        )
        releasing = not resetting and not owned and marker_is_stale

        # **The action of the run being stopped, from the record of arming it.**
        # Never from ``command``, which is ``None`` on every stopping refresh -- that
        # is what made the "reset" a lone marker release. Ownership already requires
        # the record to name the run being executed, so when a reset is entitled to
        # run the record is the proof of what it is stopping. Absent or unpermitted
        # fails closed; it is never defaulted to a charge.
        reset_action = self._owned_run_action()

        # **The stages are built first and the published list is their sum.**
        # Deriving ``commands`` from the two halves rather than beside them means
        # the report can never describe a sequence the send site would not send:
        # there is one construction, not two that have to agree.
        # **The Live actuator family is Dispatch, and it is the only one.** The
        # Force Charging helpers are still read -- they are one of the six
        # conflicting families -- but nothing here commands them. A split runtime
        # would be worse than either surface alone: an arm on one and a stop on
        # the other cannot be reasoned about at all.
        stage_one: tuple[Any, ...] = ()
        stage_two: tuple[Any, ...] = ()
        verify: str | None = None
        # Whether this refresh would physically begin a dispatch, read from the
        # branch that plans one rather than inferred from the step list afterwards.
        arming = False
        # **Which actuator surface this refresh may use, decided once.** Read from
        # the executing intent rather than from the command, because the intent is
        # what names a surface -- see the advisory branch below, which is where
        # getting this wrong would have cost a helper-family write.
        live_intent = self._executing_intent()
        setpoint = self._dispatch_setpoint(now)
        conflicts = self._blocking_conflicts(snapshot)
        if degraded:
            # The emergency authority: one write, and the cleanup withheld until
            # inactivity is verified on a later refresh.
            stage_one = plan_dispatch_stop()
            verify = EXECUTION_VERIFY_DISPATCH_INACTIVE
        elif resetting:
            stage_one = plan_dispatch_stop()
            stage_two = plan_dispatch_cleanup()
            verify = EXECUTION_VERIFY_DISPATCH_INACTIVE
        elif releasing:
            stage_two = plan_release_marker()
        elif command is None or conflicts:
            # A conflicting family nobody can prove is ours means standing down,
            # never switching it off: the vendor automation would do that
            # silently, and destroying a feature the user chose is not a safe
            # default.
            pass
        elif holding:
            # **Power to zero, then everything that keeps the run alive.**
            #
            # The zero write appears only when the device is not already at rest, on
            # the same "has it moved" test the sustain and the tick use -- except
            # that this one asks the applied setpoint directly rather than going
            # through ``_dispatch_setpoint``. ``_finish`` substitutes the *held*
            # value for any move smaller than ``DISPATCH_POWER_DEADBAND_KW`` (0.2 kW,
            # two whole actuator steps), so a row resting from 0.1 kW would keep
            # drawing 0.1 kW with ``within_deadband`` printed beside it. Hysteresis
            # exists to suppress noise; a commanded rest is not noise.
            #
            # The cutoff is re-asserted for the same reason the sustain re-asserts
            # it, and the dead-man is re-armed on the economic cadence exactly as a
            # sustain would. A rest that stopped re-arming would be a stop with extra
            # steps.
            if self._applied_setpoint_kw != 0.0:
                stage_two += plan_dispatch_power(0.0)
            stage_two += plan_dispatch_cutoff(command.cutoff_soc_percent)
            stage_two += plan_dispatch_rearm(
                deadman_minutes(
                    None if snapshot is None else snapshot.dispatch_duration_minutes
                )
            )
        elif sustaining:
            # **A running run is always sustained.** Whether the commanded power
            # moved is a different question from whether the run continues, and
            # conflating them is the beta.24 fault: a charge holding steady at
            # 3.0 kW re-armed nothing and expired mid-run while the controller
            # believed it was still going. Constant power is the *common* case.
            #
            # Order is the approved one: power, cutoff, then the dead-man. The
            # power step appears only when the setpoint materially moved, which is
            # the same deadband decision the sixty-second tick makes, from the
            # same pure function -- so the two writers cannot disagree about what
            # "moved" means.
            stage_two = ()
            if setpoint is not None and setpoint.update_needed:
                stage_two += plan_dispatch_power(setpoint.applied_kw)
            stage_two += plan_dispatch_cutoff(command.cutoff_soc_percent)
            # **The dead-man cadence, and only here.** A sixty-second correction
            # never re-arms: that would extend a run on a cadence the economics
            # never chose. The alternation is what makes the vendor automation
            # fire at all -- it triggers on the helper changing state.
            stage_two += plan_dispatch_rearm(
                deadman_minutes(
                    None if snapshot is None else snapshot.dispatch_duration_minutes
                )
            )
        elif live_intent not in CONTROL_LIVE_DISPATCH_INTENTS:
            # **The advisory path, and the branch that must not be keyed on the
            # action.** It used to read ``command.action != ACTION_CHARGE``, which
            # was sound only while ``ACTION_CHARGE`` was the one action an intent
            # could map to. beta.27 maps ``net_export -> ACTION_DISCHARGE`` so the
            # stop path can name what it stops -- and under the old condition an
            # export command would have fallen in here and been armed on the
            # **Force Discharging helper family**, silently reintroducing a helper
            # family as the physical actuator for a new capability.
            #
            # So the question asked is the correct one: *is this intent one of the
            # validated Live Dispatch intents?* An intent is what carries the
            # actuator surface; an action only carries a battery direction, and
            # two intents can share one.
            #
            # Everything else about this path is unchanged. The Phase-3 reserve
            # guard emits discharges and no release executes one: the typed barrier
            # refuses the action at authorisation and the send site refuses the
            # entities. It still has to be *planned*, because shadow reporting is
            # what a user reads to decide whether to trust the layer at all.
            stage_two = plan_arm_parameters(command)
            stage_one = plan_marker_claim() if stage_two else ()
            verify = EXECUTION_VERIFY_MARKER_ON if stage_one else None
        elif (
            setpoint is not None
            and setpoint.applied_kw != 0.0
            and sign_matches_intent(live_intent, setpoint.applied_kw)
        ):
            # **The sign is checked against the intent**, not asserted to be
            # negative. Two directions are executable in beta.27 and a hardcoded
            # comparison would either block the new one or admit the wrong one.
            # Zero is excluded here for the reason stated below -- it is not an arm
            # -- and a sign that does not match the admitted intent falls through
            # to no writes at all rather than being corrected into one.
            arming = True
            stage_two = plan_dispatch_arm(
                mode=DISPATCH_MODE_SOC_CONTROL,
                power_kw=setpoint.applied_kw,
                cutoff_soc_percent=command.cutoff_soc_percent,
                duration_minutes=deadman_minutes(None),
                pv_enabled=True,
            )
            # **No claim without something to arm.** A command that moves no
            # battery is not an arm, and a lone marker write would claim
            # ownership of nothing and then have to be cleaned up as stale.
            stage_one = plan_marker_claim()
            verify = EXECUTION_VERIFY_MARKER_ON
        commands = stage_one + stage_two

        # Layer 3 of the direction interlock, and it now guards the reset list too:
        # checked against the entity list itself, not against the intention that
        # built it. A malformed command is refused whole -- there are no partial
        # writes.
        if resetting:
            checked = reset_action
        elif releasing:
            # The marker alone belongs to no family, and validating it against one
            # would refuse the one write that is safe without a claim.
            checked = None
        else:
            checked = None if command is None else command.action
        refusal = None
        if commands and checked is not None:
            refusal = action_refusal(checked, commands)
        # And the value check, which the entity test structurally cannot make.
        # **Keyed on the intent since beta.27**: which sign is permitted is only
        # answerable once you know what is being executed, and an unknown intent
        # fails closed rather than defaulting to a direction.
        if refusal is None and commands:
            refusal = dispatch_refusal(live_intent, commands)
        if refusal is not None:
            commands = ()
            stage_one = stage_two = ()
            verify = None
        # **The physical controller, and the ring behind it.** Siblings of the
        # write boundary rather than fields inside it: the boundary describes one
        # sequence, and the controller describes the decision that produced it.
        # Diagnostics are rarely captured at the moment production moved, so a
        # download taken later has to be able to reconstruct the quarter rather
        # than only describe the instant it was taken.
        stage_b["controller"] = self._controller_block(setpoint, now)
        self._record_dispatch_start_sample(
            snapshot, now, cadence=CADENCE_QUARTER_REFRESH
        )
        stage_b["quarter"] = self._quarter_block(now)
        self._note_lifecycle(
            self._lifecycle_state_from(
                ownership_state=ownership_state,
                stop_reason=stop_reason,
                resetting=resetting,
                releasing=releasing,
                arming=arming,
                now=now,
            ),
            now,
        )
        stage_b["lifecycle"] = self._lifecycle_block()
        stage_b["admission"] = self._admission_block(now)
        stage_b["dispatch_start_probe"] = list(self._dispatch_start_samples)
        # **beta.44 calibration, read by nothing.** One entry per finished physical
        # claim, with the two clocks kept apart. See ``_observe_arm``.
        stage_b["arm_measurements"] = list(self._arm_measurements)
        stage_b["arm_plan"] = self._arm_plan
        stage_b["dispatch_start_active_probe"] = {
            "samples": list(self._dispatch_start_active),
            "count": len(self._dispatch_start_active),
            "mode_2_zero_kw_rule": (
                "each sample carries the measured battery, pv, load and meter "
                "beside the commanded setpoint, so a sample with "
                "helper_setpoint_kw 0.0 and a hold_reason answers what mode 2 at "
                "zero does to the pack: battery_charge_w and battery_discharge_w "
                "both near zero means the dispatch holds; a rising "
                "battery_discharge_w with no matching load means it fell back to "
                "self-consumption. read-only, and the only artefact in this "
                "integration that can settle it"
            ),
            "rule": (
                "appended only while a dispatch is running, so an idle sample can "
                "never displace one taken during a run. this is where the "
                "register's semantics will be read from: raw_delta_since_previous "
                "separates a fixed start instant from elapsed seconds from a "
                "countdown, and a jump at phase after_rearm would show the re-arm "
                "re-anchoring it. diagnostic only -- dispatch_start is not an "
                "ownership factor and must not become one again"
            ),
        }
        # **The reason is written back into ``result``, which is the field every
        # reader consults.** Three reasons were computed above -- safety turning
        # unsafe under our own dispatch, a restart that lost the quarter's progress,
        # and a marker that vanished -- and all three were published only to
        # ``write_boundary``. So the surfaces saw ``None`` and printed
        # "Plan Replaced" for a safety stop, which is R10's false claim rather than
        # a wording problem.
        if stop_reason:
            existing = stage_b.get("result")
            if not isinstance(existing, dict):
                existing = {}
                stage_b["result"] = existing
            existing["stop_reason"] = stop_reason
            existing.setdefault("reason_vocabulary", REASON_VOCABULARY_RUN_STOP)

        # **The campaign lifecycle, advanced once per refresh, after the reason is
        # known and before anything is published.** This is the ordering that lets
        # the incident's 17:45 refresh speak at all.
        # **Before the live lifecycle advances, and on every refresh. beta.42.**
        #
        # A campaign left open by a restart has to close before this boot's campaign
        # can open, or one physical objective appears twice in the log under two
        # ids -- which is precisely the defect the persistence exists to close.
        #
        # Every refresh rather than once at startup, because a never-started
        # instance cannot be classified at restore time: the store is read during
        # setup and the first solve happens later, so there is no authoritative plan
        # yet to say whether a replacement exists. It is asked again until one of the
        # two authoritative answers is true, and closes late rather than wrongly.
        self._recover_campaign_lifecycle(now)
        self._note_campaign_progress(now, stop_reason)
        stage_b["completed_campaign"] = self._closed_campaign
        stage_b["open_campaign"] = self._open_campaign_block()
        stage_b["admitted_plan"] = None if self._plan is None else self._plan.as_dict()
        stage_b["physical_decisions"] = list(self._physical_decisions)
        stage_b["write_boundary"] = {
            "refusal": refusal,
            # **The two stages, published separately.** A reader has to be able
            # to see that the activation is not in the same stage as the claim,
            # because "activation last" and "activation only after the claim was
            # read back" are different guarantees and beta.24 had only the first.
            "stage_one": [step.as_dict() for step in stage_one],
            "stage_two": [step.as_dict() for step in stage_two],
            "stage_verification": verify,
            "staging_rule": (
                "stage two is conditional. an arm may not reach its activation "
                "until the owner marker reads back on; a stop may not disturb a "
                "running dispatch's fields until no activation boolean is on. "
                "both checks read the helper the stage itself wrote, never the "
                "device register, which lags a poll and would withhold every "
                "cleanup for ever"
            ),
            # The action of whatever is being sent. On a reset that is the action
            # the record says we armed, which is the whole point: a stopping refresh
            # has no command to take it from.
            "action": checked,
            "family": (None if checked not in FAMILIES else FAMILIES[checked].activate),
            "steps": [step.as_dict() for step in commands],
            "authority": {
                "plan_authority_holds": plan_authority_holds,
                "authority_basis": (
                    AUTHORITY_BASIS_CARRIED_RUN
                    if self._carried is not None
                    else AUTHORITY_BASIS_ADMITTED_PLAN
                    if self._plan is not None
                    else AUTHORITY_BASIS_NONE
                ),
                "authority_run_id": authority_run_id,
                # **The withdrawal that was outranked, named. beta.35.** A reset
                # that does not happen must still be readable, or the next
                # investigation starts from a silence.
                "withheld_stop_reason": withheld_stop_reason,
                "claim_stale_after": (
                    (self.store.execution_record or {}).get("stale_after")
                ),
                "adopted_this_refresh": self._adopted_this_refresh,
                # **Admissions and instances, not identities. beta.36.**
                #
                # ``abandoned_campaigns`` counted campaign *identities*, and because
                # ``campaign_identity`` is a digest of the campaign's end it is
                # byte-identical across every republication of one live campaign --
                # so one abort barred that campaign from ever admitting a plan again
                # for the rest of the session. Both 2026-08-30 and 2026-08-31 died
                # that way. The latch is now the admission attempt, which is what an
                # abort is actually about.
                "abandoned_admissions": len(self._abandoned_admissions),
                "closed_instances": len(self._closed_instances),
                "final_campaigns": len(self._final_campaigns),
                "rule": (
                    "an opened frozen schedule outranks Stage A revising the "
                    "future, and nothing else: safety, a lost marker, a stalled "
                    "dead-man, a failed command, the user's own switch and a "
                    "genuinely lost measurement are never withheld. bounded by "
                    "the plan's own end, by the row covering this instant, and by "
                    "the vendor dead-man, which is re-armed only while the "
                    "sustain actually runs"
                ),
            },
            "source": (
                "stage_b_reset"
                if resetting
                else "stale_marker_release"
                if releasing
                else "stage_b"
                if charge_intent is not None
                else "reserve_guard"
            ),
            "sequence": (
                "emergency_self_stop"
                if degraded
                else "reset"
                if resetting
                else "marker_release"
                if releasing
                else "hold"
                if holding
                else "sustain"
                if sustaining
                else "arm"
            ),
            # Published only on a refresh that is actually holding, so the field can
            # never carry a reason left behind by a sixty-second tick that held
            # earlier in the quarter.
            "hold_reason": self._hold_reason if holding else None,
            "hold_rule": (
                "a hold keeps ownership, the claim, the frozen schedule and the "
                "campaign, and keeps re-arming the dead-man. it commands zero once "
                "and bypasses the setpoint deadband to do it. two reasons reach it: "
                "quarter_satisfied, which does not recover, and "
                "rate_below_resolution, which recovers inside its own row as soon "
                "as the clamp that caused it lifts"
            ),
            "reset_action": reset_action,
            "stop_reason": stop_reason,
            "deadman_minutes": (None if command is None else command.duration_minutes),
            "timer_finishes_at": (
                None
                if snapshot is None or snapshot.charge_timer_finishes_at is None
                else snapshot.charge_timer_finishes_at.isoformat()
            ),
            "rule": (
                "direction is the helper family and the magnitude is unsigned. the "
                "raw dispatch surface takes signed power with the opposite "
                "convention and is never written. a command naming the other "
                "family, the raw surface, a negative magnitude or an unpermitted "
                "service is refused in full"
            ),
        }

        # **Each operation is authorised on its own terms, and the thing authorised
        # is the thing sent.** Three questions, and they are genuinely different:
        # "may we begin or continue moving energy", "may we return a dispatch we own
        # to rest", and "may we clear a marker with nothing behind it". Until the
        # amendment all three went through the first one, so the last two were
        # refused every time -- and the step list was swapped afterwards, so the
        # answer did not even describe the question.
        if degraded:
            # **A fourth question, and it has to be its own.** "May we stop a
            # dispatch we still own" is ``authorize_reset``, and it requires
            # ownership the degraded state has by definition lost. Asking it here
            # would refuse every time; asking the *start* question would refuse
            # for the wrong reason. This one grants exactly ``Dispatch enable ->
            # OFF`` and refuses any list that is not precisely that.
            decision = authorize_emergency_self_stop(
                authorized=emergency_self_stop_authorized(
                    dispatch_active=bool(
                        snapshot is not None and snapshot.dispatch_active
                    ),
                    marker_present_and_on=bool(
                        snapshot is not None and snapshot.owner_marker
                    ),
                    record_matches_run=evidence_now.record_causation_holds,
                    readback_compatible=evidence_now.readback_compatible,
                    contradicted=False,
                ),
                steps=tuple(step.entity_id for step in commands),
                attempts_made=self._emergency_attempts,
            )
            if decision.authorized:
                self._emergency_attempts += 1
        elif resetting:
            decision = authorize_reset(
                ownership=ownership_state or OWNERSHIP_NONE,
                stopping_action=reset_action,
                stop_reason=stop_reason,
                steps_planned=len(commands),
                # Without this an admitted export could be started and never
                # stopped, which strands a running dispatch on the device dead-man.
                intent=self._executing_intent(),
            )
        elif releasing:
            decision = authorize_marker_release(
                marker_is_stale=marker_is_stale,
                steps_planned=len(commands),
            )
        else:
            # **A sustaining refresh is a continuation, not a start.** The cooldown
            # is a quarter of an hour and so is the refresh interval, so a re-arm
            # sits exactly on the boundary -- and a cooldown that refused one would
            # expire the run it was protecting.
            starts_or_increases = (
                command is not None
                and command.moves_battery
                and not sustaining
                and (
                    self._last_control_power_kw is None
                    or command.power_kw > self._last_control_power_kw
                )
            )
            decision = authorize_start(
                verdict,
                context,
                commands_planned=len(commands),
                starts_or_increases=starts_or_increases,
                action=None if command is None else command.action,
                # **The authority the direction is permitted under.** The
                # unconditional action set stays charge-only, so the reserve
                # guard's discharge is refused exactly as before; an admitted
                # ``net_export`` unlocks the discharge direction and nothing else
                # does.
                intent=self._executing_intent(),
            )

        state = CONTROL_STATE_INHIBITED
        if resetting or releasing:
            # A stop is not an eligibility question. Reporting ``inhibited`` because
            # the world is unsafe would describe the condition that *caused* the
            # stop as though it had prevented it.
            #
            # **And it is not a plan either. beta.34.** ``eligible`` renders as
            # "Planned", which is the opposite of what a stop or a stale-marker
            # release means: after one of these, nothing is running and nothing is
            # queued. ``idle`` is the honest present tense in both cases, and the
            # steps that were sent are in ``commands`` for anyone who needs them.
            state = CONTROL_STATE_IDLE
        elif verdict.safe:
            state = CONTROL_STATE_ELIGIBLE if commands else CONTROL_STATE_IDLE

        # **The refresh's own outcome, recorded here because here is the first
        # point at which it is knowable.**
        #
        # It used to be built at the write boundary, *before* authorization ran --
        # so it could not see the authorization decision at all and fell back to
        # Stage B's run-level ``stop_reason``. On the real installation that
        # published ``target_reached`` for a refresh which had planned a correct
        # START and been refused ``ownership_not_provable``: the one fact a reader
        # needed was absent, and a fact about a different question was in its place.
        #
        # Precedence, most-specific first, so the reason names what actually decided
        # the refresh:
        #
        # 1. the write-boundary refusal -- the step list was malformed, so nothing
        #    downstream got a say;
        # 2. the authorization refusal, carrying ``unsafe_reason`` when the gate is
        #    what refused, because "unsafe" alone does not say which condition;
        # 3. ``stop_reason`` **only while actually stopping**. A stop reason on a
        #    refresh that was starting describes a different question, which is the
        #    defect above;
        # 4. otherwise whether a command was planned at all.
        outcome_reason = refusal
        if outcome_reason is None and decision is not None and not decision.authorized:
            outcome_reason = decision.unsafe_reason or decision.refusal
        if outcome_reason is None and (resetting or releasing):
            outcome_reason = stop_reason
        if outcome_reason is None:
            outcome_reason = "commands_planned" if commands else "no_command"
        self._refresh_outcome = TickOutcome(
            cadence=CADENCE_QUARTER_REFRESH,
            reason=outcome_reason,
            # **Planned and permitted are different**, and conflating them is how a
            # refused START read as a write that had happened.
            wrote=bool(commands) and bool(decision is not None and decision.authorized),
            at=now,
            phase="write_boundary",
        )
        # **What this row attempted, recorded where the answer is finally known.**
        #
        # After authorisation, because "did anything reach the inverter" is not
        # answerable before it -- the defect that let the 2026-08-30 row of 0.56 kWh
        # be admitted, derived, ticked against fifteen times and moved nothing while
        # its only published trace was ``quarter_expired``, which is also exactly
        # what a mid-row teardown writes.
        #
        # ``arm_attempts`` counts arm sequences that were **authorised**, so
        # ``armed`` is exactly true; a refused one is recorded as a refusal instead
        # of being counted as an arm. A refresh that planned nothing at all for an
        # open row is the most important case of the three and was the one with no
        # field to land in.
        if self._quarter is not None and self._quarter.open_at(now):
            if commands and self._refresh_outcome.wrote:
                if arming:
                    self._note_quarter_arm_attempt()
            else:
                self._note_quarter_refusal(outcome_reason)
        self._record_control_event(now, state, verdict, decision)
        # Held for the one async method that would send them. Not published and
        # not read anywhere else: the report already carries the step list for
        # diagnostics, and a second reader of the live tuple would be a second
        # path to the inverter.
        self._pending_commands = commands
        self._pending_stage_one = stage_one
        self._pending_stage_two = stage_two
        self._pending_verify = verify
        self._pending_power_kw = None if command is None else command.power_kw
        self._pending_command = command
        self._pending_snapshot = snapshot
        self._pending_is_reset = (stage_b.get("write_boundary") or {}).get(
            "source"
        ) in ("stage_b_reset", "stale_marker_release")
        # **The refresh's emergency stop is an abort too, and beta.34 forgot it.**
        #
        # A marker that has gone out from under a running dispatch is a lost claim:
        # ``degraded`` grants exactly one write, the stop, and the *device* cleanup
        # is rightly withheld until inactivity is verified. The **authority**
        # teardown is a different thing and was withheld with it -- so the frozen
        # schedule, the campaign and the record all survived an emergency stop, and
        # the next row armed the inverter again fifteen minutes later. Measured:
        # a degraded refresh turned the dispatch off, and the refresh after it sent
        # the full seven-step arm sequence for the following row.
        #
        # ``_async_emergency_self_stop`` -- the sixty-second tick's version of the
        # same stop -- has always torn down. This is the refresh path joining it, so
        # every genuine abort reaches one helper rather than three.
        self._pending_is_emergency = bool(degraded)
        # Carried to the teardown so the campaign terminal names why it ended,
        # rather than being closed with an anonymous ``None``.
        self._pending_stop_reason = (stage_b.get("write_boundary") or {}).get(
            "stop_reason"
        )
        # Whether this list would physically start or re-start a dispatch. Read from
        # the steps rather than from the intention, so "started" can only ever be
        # said about a list that actually carries an activation.
        self._pending_activates = any(
            step.entity_id == DISPATCH_ENABLE for step in commands
        )
        # What the dead-man read *before* this write. Compared against the reading
        # after it, next refresh.
        self._pending_deadline = (
            None if snapshot is None else snapshot.dispatch_timer_finishes_at
        )
        self._pending_run_id = authority_run_id

        return {
            "mode": mode,
            "state": state,
            "execution_available": CONTROL_EXECUTION_AVAILABLE,
            "execution_enabled": self.config.control_execution_enabled,
            "capability": capability.as_dict(),
            "device": snapshot.as_dict(),
            "intent": None if intent is None else intent.as_dict(),
            "command": None if command is None else command.as_dict(),
            "commands": [step.as_dict() for step in commands],
            "commands_planned": len(commands),
            "safety": verdict.as_dict(),
            "export_check": _export_check(
                context,
                requested=requested,
                command=command,
                safe_power_kw=safe_power_kw,
                inhibit_reason=verdict.inhibit_reason,
            ),
            "authorization": decision.as_dict(),
            "execution": stage_b,
            "last_write": (
                None
                if self._last_control_write is None
                else self._last_control_write.isoformat()
            ),
            "events": list(self._control_events),
            "soc_coherence": self.soc_coherence.as_dict(),
            "shadow_basis": (
                "the safety verdict and the command list above are computed by "
                "the same functions the active path uses, so what shadow reports "
                "is what active would have attempted -- including the export "
                "clamp, so a safely reduced power in shadow is the power active "
                "would have sent"
            ),
            "execution_scope": _EXECUTION_SCOPE,
        }

    @callback
    def _control_context(
        self,
        *,
        mode: str,
        capability: Any,
        snapshot: Any,
        flows_now: Any,
        problem: str | None,
        now: datetime,
        today: date,
        elapsed: int,
        today_interval_count: int,
        command: Any,
    ) -> ControlContext:
        """Assemble every live fact the gate needs, around one command.

        Every reading is taken from the snapshot and flow pair passed in, so two
        contexts built in the same refresh differ **only** in the command they
        describe. That is what makes it sound to take the export bound from one
        and evaluate the other.
        """
        return ControlContext(
            mode=mode,
            execution_enabled=self.config.control_execution_enabled,
            missing_entities=capability.missing,
            unavailable_entities=capability.unavailable,
            failsafe_available=capability.failsafe_available,
            excess_export_active=capability.excess_export_active,
            peak_shaving_active=capability.peak_shaving_active,
            dispatch_active=snapshot.dispatch_active,
            battery_configured=self.battery_planning_configured,
            plan_problem=problem,
            current_start_index=min(elapsed + 1, today_interval_count),
            today=today,
            now=now,
            soc_percent=self._read_soc_percent(),
            soc_age_seconds=self._state_age_seconds(self.config.battery_soc_entity),
            battery_power_w=self._canonical_battery_power_w(),
            battery_power_age_seconds=self._state_age_seconds(
                self.config.battery_power_entity
            ),
            house_load_w=sanitize_load_w(
                self._read_power(self.config.house_load_entity)
            ),
            house_load_age_seconds=self._state_age_seconds(
                self.config.house_load_entity
            ),
            # The meter, canonical and unsigned. This is what the export check is
            # measured against, so it is read through the same splitter the rest
            # of the integration uses rather than off the raw entity.
            grid_import_w=flows_now.grid_import_w,
            grid_export_w=flows_now.grid_export_w,
            grid_age_seconds=self._state_age_seconds(self.config.grid_power_entity),
            # **Control grade, not diagnostics grade. beta.42.**
            #
            # This was ``BALANCE_MAX_SOURCE_AGE_SECONDS``, which is 300 and was
            # calibrated for the balance *diagnostic*. ``ControlCoherence`` already
            # argues in its own docstring why that figure is wrong for an actuator:
            # "reused as an actuator threshold it would accept a five-minute-old
            # photovoltaic reading as the basis for a live setpoint, which is not a
            # bound at all on a controller that corrects every sixty seconds."
            # ``CONTROL_MAX_SOURCE_AGE_SECONDS`` was introduced for exactly that and
            # the safety gate -- the thing that authorises an individual write --
            # never received it.
            #
            # It matters most for the state of charge. Coherence times only the four
            # balance *flows*; the state of charge is not one of them and has no
            # place in an identity it does not appear in. So between ninety and three
            # hundred seconds a stale state of charge was caught by nothing, and the
            # floor guarantee rests on it. Now the freshness gate covers it at the
            # same grade as everything else it authorises.
            max_source_age_seconds=CONTROL_MAX_SOURCE_AGE_SECONDS,
            device_power_kw=0.0 if command is None else command.power_kw,
            device_cutoff_percent=(
                0 if command is None else command.cutoff_soc_percent
            ),
            device_duration_minutes=self._duration_to_command(command, snapshot),
            export_margin_percent=self.config.control_export_margin_percent,
            seconds_since_last_write=(
                None
                if self._last_control_write is None
                else (now - self._last_control_write).total_seconds()
            ),
        )

    @callback
    def _record_control_event(
        self,
        now: datetime,
        state: str,
        verdict: SafetyVerdict,
        decision: ExecutionDecision,
    ) -> None:
        """Keep a bounded trail of what the control layer decided and why."""
        self._control_events.insert(
            0,
            {
                "at": now.isoformat(),
                "state": state,
                "inhibit_reason": verdict.inhibit_reason,
                "refusal": decision.refusal,
            },
        )
        del self._control_events[MAX_CONTROL_EVENTS_REPORTED:]

    def _build_battery_plan(
        self,
        *,
        today: date,
        elapsed: int,
        baseline_today: DayForecast,
        tomorrow: DayForecast,
        tz_key: str,
        pv_today: PvForecast | None = None,
        pv_tomorrow: PvForecast | None = None,
        absorb_surplus: bool = False,
    ) -> BatteryPlan | None:
        """Build this refresh's battery plan, or ``None`` if it could not be.

        Wrapped for the same reason the forecast-evidence step is: Phase 3 is
        additive, the six Phase-1 and Phase-2 sensors do not read any of it, and
        taking the whole integration unavailable because a battery calculation
        raised would trade the important half for the newest half. A ``None``
        here degrades exactly three entities and nothing else.

        The forecasts are converted to the public ``LoadForecast`` shape by the
        same helper ``api.current_forecast`` uses, so the decision layer sees
        precisely what a later phase would see through the boundary -- the
        *unadapted* baseline, never the Today entity's hybrid of prediction and
        measurement.
        """
        try:
            return build_plan(
                soc_percent=self._read_soc_percent(),
                capacity_kwh=self.config.battery_capacity_kwh,
                max_charge_kw=self.config.battery_max_charge_kw,
                max_discharge_kw=self.config.battery_max_discharge_kw,
                round_trip_efficiency_percent=(
                    self.config.battery_round_trip_efficiency_percent
                ),
                configured_min_soc_percent=self.config.battery_min_soc_percent,
                today_forecast=load_forecast_from(baseline_today, tz_key=tz_key),
                tomorrow_forecast=load_forecast_from(tomorrow, tz_key=tz_key),
                elapsed_intervals=elapsed,
                today=today,
                battery_power_w=self._canonical_battery_power_w(),
                # Empty when there is no forecast, which leaves every interval
                # PV-blind rather than forecasting darkness.
                today_pv=() if pv_today is None else pv_today.intervals,
                tomorrow_pv=() if pv_tomorrow is None else pv_tomorrow.intervals,
                absorb_surplus=absorb_surplus,
            )
        except Exception:
            self._log.warning(
                _BATTERY_PLAN_LOG,
                (
                    "The battery decision layer could not be evaluated this "
                    "refresh. Learning, both forecasts and the forecast-error "
                    "sensors are unaffected -- nothing in those paths reads the "
                    "battery plan -- but the three battery entities will read "
                    "unknown until it recovers"
                ),
            )
            _LOGGER.debug("Battery plan build failed", exc_info=True)
            return None

    async def _async_record_forecast_evidence(
        self,
        *,
        now: datetime,
        today: date,
        tz: Any,
        baseline_today: DayForecast,
        forecast_tomorrow: DayForecast,
        breakdown: ConfidenceBreakdown,
    ) -> RecorderResult:
        """Persist this refresh as forecast evidence, and read the metrics back.

        Wrapped so that nothing in the evidence layer can fail a refresh. The
        four Phase-1 sensors do not depend on any of it, and taking the whole
        integration unavailable because a forecast-history document could not be
        written would trade the important half for the useful half.
        """
        try:
            self.last_record = await self.recorder.async_record(
                now=now,
                today=today,
                tz_key=str(tz),
                tz=tz,
                baseline_today=baseline_today,
                tomorrow=forecast_tomorrow,
                learned_days=breakdown.learned_days,
                confidence_percent=breakdown.percent,
                confidence=breakdown.as_dict(),
                ev_power_entity=self.config.ev_power_entity,
            )
        except Exception:
            self._log.warning(
                _FORECAST_HISTORY_LOG,
                (
                    "Forecast-error history could not be updated this refresh. "
                    "Load learning and both forecasts are unaffected -- nothing "
                    "in the learning path reads this evidence -- but the "
                    "forecast-error sensors will not advance until it recovers"
                ),
            )
            _LOGGER.debug("Forecast history update failed", exc_info=True)
            self.last_record = RecorderResult(
                yesterday=self.last_record.yesterday,
                window=self.last_record.window,
            )
        return self.last_record

    @staticmethod
    def _elapsed_intervals(now: datetime, today: date, tz: Any) -> int:
        """Return how many quarter-hour intervals of ``today`` have completed.

        Measured as absolute elapsed time since local midnight rather than from
        the wall clock, so the repeated hour of a fall-back day advances the
        count instead of rewinding it.
        """
        elapsed = (now.astimezone(UTC) - utc_midnight(today, tz)).total_seconds()
        return max(0, int(elapsed // (QUARTER_MINUTES * 60)))

    # -- consumed integrations -------------------------------------------

    @property
    def frank_available(self) -> bool:
        """Return whether the price source can actually be read right now.

        Established from facts -- a selected entry, that entry present, the price
        entities resolvable through the registry by unique id -- and **never**
        from the setup state of somebody else's config entry.

        That distinction is not theoretical. The property this replaces asked
        whether the referenced entry was in state ``LOADED``, and its own
        docstring carried the warning: the PV layer asked the same question two
        releases ago and produced a live false negative on every restart, because
        an integration's usability has nothing to do with which phase of setup its
        entry happens to be in when we look. There is no lifecycle probe left in
        this file.
        """
        if not self.config.frank_entry_id:
            return False
        return discover_frank(self.hass, self.config.frank_entry_id).usable

    @property
    def solcast_available(self) -> bool:
        """Return whether the Solcast source can actually be read right now.

        Delegates to the same capability probe the PV layer uses, so the two can
        never disagree. They did in beta.9, and the disagreement is what made the
        defect so hard to read: this property asked whether the config entry was
        in state ``LOADED`` and answered *yes* at download time, while the PV
        block carried a snapshot from a refresh where the entry had not finished
        setting up and answered *no*. Two definitions and two instants, printed
        side by side as though they described the same thing.

        One definition now, and it is the one that matters: can the two read-only
        actions be called.
        """
        if not self.config.use_pv_forecast:
            return False
        return discover_solcast(self.hass, self.config.solcast_entry_id).usable

    # -- accessors used by sensors and diagnostics ------------------------

    @property
    def today_forecast(self) -> TodayForecast | None:
        """Return today's adapted forecast."""
        return (self.data or {}).get("today")

    @property
    def tomorrow_forecast(self) -> DayForecast | None:
        """Return tomorrow's forecast."""
        return (self.data or {}).get("tomorrow")

    @property
    def confidence(self) -> ConfidenceBreakdown | None:
        """Return the confidence breakdown."""
        return (self.data or {}).get("confidence")

    @property
    def open_quarter_coverage(self) -> float:
        """Return coverage accrued in the quarter currently being measured."""
        return self._accumulator.open_coverage

    @property
    def battery_plan(self) -> BatteryPlan | None:
        """Return the current battery plan, or ``None`` when there is none."""
        return (self.data or {}).get("battery_plan")

    @property
    def control_report(self) -> dict[str, Any] | None:
        """Return this refresh's control report, or ``None``."""
        return (self.data or {}).get("control")

    @property
    def economic_blocked_reason(self) -> str:
        """Return why nothing is sent right now, most fundamental reason first.

        **One answer, read by every surface.** The attribute, the diagnostics
        payload and the stored evidence snapshot each carried this field, and two
        of the three hardcoded ``execution_unavailable`` -- a release-level
        statement that no command reaches the battery at all, which stopped being
        true in beta.24. A user reading diagnostics while a Live charge was running
        was told execution was unavailable.

        The order is the point: the deepest reason first, so a reader is not told
        "no primitive for export" when the real answer is that the mode is off.

        1. the release genuinely ships no actuator;
        2. the user has not enabled sending commands;
        3. the mode is not Active;
        4. then, and only then, per-action reasons.
        """
        if not CONTROL_EXECUTION_AVAILABLE:
            return ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE
        if not self.config.control_execution_enabled:
            return ECONOMIC_BLOCKED_NOT_ENABLED
        if self.control_mode != CONTROL_MODE_ACTIVE:
            return ECONOMIC_BLOCKED_MODE_NOT_ACTIVE
        outcome = (self.data or {}).get("economic")
        if isinstance(outcome, EconomicOutcome):
            if outcome.action == ECONOMIC_ACTION_EXPORT:
                # Executable since beta.27, so the only thing that can stand in the
                # way is the user's own permission -- which is theirs to change.
                if not self.config.allow_battery_export:
                    return ECONOMIC_BLOCKED_EXPORT_NOT_PERMITTED
                return ECONOMIC_BLOCKED_NONE
            if outcome.action == ECONOMIC_ACTION_CURTAIL:
                # Genuinely absent: no release commands the inverter to decline
                # production.
                return ECONOMIC_BLOCKED_NO_PRIMITIVE_CURTAIL
            if outcome.action in CONTROL_EXECUTABLE_ACTIONS:
                return ECONOMIC_BLOCKED_NONE
            return ECONOMIC_BLOCKED_ACTION_NOT_EXECUTABLE
        return ECONOMIC_BLOCKED_ACTION_NOT_EXECUTABLE

    @property
    def current_soc_percent(self) -> float | None:
        """Return the sanitised battery state of charge, for diagnostics."""
        return self._read_soc_percent()

    @property
    def battery_planning_configured(self) -> bool:
        """Return whether every Phase-3 hardware fact has been supplied."""
        return None not in (
            self.config.battery_capacity_kwh,
            self.config.battery_max_charge_kw,
            self.config.battery_max_discharge_kw,
        )

    @property
    def ev_configured(self) -> bool:
        """Return whether a flexible-load source is configured."""
        return bool(self.config.ev_power_entity)

    @property
    def ev_available(self) -> bool:
        """Return whether the flexible-load source currently reads usably.

        Judged on the *sanitised* value, which is what the learning path actually
        consumes. Testing the raw reading instead made diagnostics report a
        charger stuck at -3000 W as available with a null power, while every
        interval it touched was simultaneously being counted as invalid -- three
        adjacent fields describing one reading, two of them wrong.
        """
        return self.current_ev_power_w is not None

    @property
    def current_ev_power_w(self) -> float | None:
        """Return the normalised flexible-load power, or ``None``."""
        if not self.ev_configured:
            return None
        return sanitize_ev_w(self._read_ev_power_w())

    @property
    def ev_open_quarter_coverage(self) -> float | None:
        """Return coverage accrued for the flexible load in the open interval."""
        if self._ev_accumulator is None:
            return None
        return self._ev_accumulator.open_coverage

    @property
    def open_pv_coverage(self) -> float | None:
        """Return the coverage accrued so far in the open PV quarter."""
        if self._pv_accumulator is None:
            return None
        return self._pv_accumulator.open_coverage

    def today_date(self) -> date:
        """Return the current local civil date.

        Read through the same clock the refresh uses, so a consumer of the
        public API can never end up a day away from the evidence the coordinator
        has just recorded.
        """
        return dt_util.now().date()

    def learned_day_dates(self) -> list[date]:
        """Return the dates that currently count as learned.

        Excludes the in-progress day, so this agrees with the Learning Days
        sensor and with the confidence score. Without ``before`` it counted today
        the moment its baseline coverage crossed ``MIN_DAY_COMPLETENESS``, which
        is the same defect that made diagnostics disagree with the entity.
        """
        return [
            record.day
            for record in self.store.learned_days(before=dt_util.now().date())
        ]
