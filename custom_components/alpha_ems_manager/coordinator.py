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
    DISPATCH_DURATION,
    DISPATCH_ENABLE,
    DISPATCH_MODE_SOC_CONTROL,
    DISPATCH_POWER,
    FAMILIES,
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
    ACTION_CHARGE,
    ACTION_DISCHARGE,
    BALANCE_MAX_SOURCE_AGE_SECONDS,
    BATTERY_MAX_SOC_PERCENT,
    CAP_NONE,
    CONF_ALLOW_BATTERY_EXPORT,
    CONF_ALLOW_GRID_CHARGING,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_MAX_CHARGE_KW,
    CONF_BATTERY_MAX_DISCHARGE_KW,
    CONF_BATTERY_MIN_SOC_PERCENT,
    CONF_BATTERY_POWER_ENTITY,
    CONF_BATTERY_POWER_SIGN,
    CONF_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
    CONF_BATTERY_SOC_ENTITY,
    CONF_CONTROL_EXECUTION_ENABLED,
    CONF_CONTROL_EXPORT_MARGIN_PERCENT,
    CONF_CONTROL_HORIZON_MINUTES,
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
    CONF_PV_POWER_ENTITY,
    CONF_SELECTED_SOLCAST_SITE_IDS,
    CONF_SOLCAST_ENTRY_ID,
    CONF_USE_PV_FORECAST,
    CONTROL_EXECUTABLE_DISPATCH_SIGN,
    CONTROL_EXECUTION_AVAILABLE,
    CONTROL_MIN_POWER_KW,
    CONTROL_MODE_ACTIVE,
    CONTROL_MODE_OFF,
    CONTROL_MODE_OPTIONS,
    CONTROL_REFUSE_MARKER_NOT_VERIFIED,
    CONTROL_REFUSE_STOP_NOT_VERIFIED,
    CONTROL_STATE_ELIGIBLE,
    CONTROL_STATE_EXECUTED,
    CONTROL_STATE_IDLE,
    CONTROL_STATE_INHIBITED,
    CONTROL_STATE_OFF,
    DEFAULT_ALLOW_BATTERY_EXPORT,
    DEFAULT_ALLOW_GRID_CHARGING,
    DEFAULT_BATTERY_MIN_SOC_PERCENT,
    DEFAULT_BATTERY_POWER_SIGN,
    DEFAULT_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
    DEFAULT_CONTROL_EXECUTION_ENABLED,
    DEFAULT_CONTROL_EXPORT_MARGIN_PERCENT,
    DEFAULT_CONTROL_HORIZON_MINUTES,
    DEFAULT_GRID_CHARGE_BUDGET_KWH,
    DEFAULT_GRID_CHARGE_MARGIN_EUR_PER_KWH,
    DEFAULT_GRID_POWER_SIGN,
    DEFAULT_MINIMUM_TRADE_GAIN_EUR,
    DISPATCH_LIMIT_NONE,
    DISPATCH_POWER_DEADBAND_KW,
    ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE,
    EV_ABSENCE_GRACE_REFRESHES,
    EXECUTION_STOP_COHERENCE_LOST,
    EXECUTION_STOP_MARKER_LOST,
    EXECUTION_STOP_SAFETY,
    EXECUTION_STOP_SWITCHED_OFF,
    EXECUTION_TARGET_STALE_MINUTES,
    EXECUTION_VERIFY_DISPATCH_INACTIVE,
    EXECUTION_VERIFY_DISPATCH_SETPOINT,
    EXECUTION_VERIFY_MARKER_ON,
    EXECUTION_VERIFY_NO_FAMILY_ACTIVE,
    INHIBIT_NO_DECISION,
    INHIBIT_NO_PLAN,
    INHIBIT_PLAN_UNAVAILABLE,
    LOG_THROTTLE_SECONDS,
    MAX_CONTROL_EVENTS_REPORTED,
    MAX_PHYSICAL_DECISIONS_REPORTED,
    MAX_SAMPLE_GAP_SECONDS,
    MIN_QUARTER_COVERAGE,
    OWNERSHIP_DEGRADED,
    OWNERSHIP_NONE,
    OWNERSHIP_OWNED,
    OWNERSHIP_PROVENANCE_SETTLING,
    OWNERSHIP_UNPROVEN,
    PRICE_CROSS_CHECK_DISAGREES,
    PRICE_FLAG_EXPORT_CROSS_CHECK_FAILED,
    PRICE_FLAG_IMPORT_CROSS_CHECK_FAILED,
    PRICE_UNAVAILABLE_NOT_CONFIGURED,
    PRICE_UNAVAILABLE_OPTIONS_UNREADABLE,
    PRICE_UNAVAILABLE_SOURCE_UNAVAILABLE,
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
    QUARTER_MINUTES,
    REFUSE_MODE_NOT_ACTIVE,
    SAFETY_SAMPLE_SECONDS,
    SELECT_INVERTER_AC_LIMIT,
    TICK_APPLIED,
    TICK_SKIPPED_INCOHERENT,
    TICK_SKIPPED_LOCK_HELD,
    TICK_SKIPPED_NO_RUN,
    TICK_SKIPPED_NOT_LIVE,
    TICK_SKIPPED_STALE_TARGET,
)
from .control import translate
from .dispatch import (
    ChargeLimits,
    deadman_minutes,
)
from .dispatch import decide as decide_setpoint
from .economic import (
    EconomicOutcome,
    IntervalPrice,
    build_economic_snapshot,
    build_horizon,
    build_outcome,
    build_physics_table,
    desired_grid_kw_at,
    execution_revision,
    execution_target,
    fingerprint_settings,
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
    CarriedRun,
    ForwardAuthorisation,
    OwnershipEvidence,
    action_for_intent,
    actionable_target,
    affirms,
    carried_from_record,
    carry_forward,
    control_intent_for,
    decide,
    forward_authorisation,
    measure_progress,
    ownership_of,
    parse_target,
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
from .realized import realized_window, soc_series_to_energy
from .reserve import (
    ReserveProjection,
    build_reserve_snapshot,
    fingerprint_battery_config,
)
from .safety import (
    ControlContext,
    ExecutionDecision,
    SafetyVerdict,
    absorbing_capacity_kw,
    authorize_emergency_self_stop,
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
_EXECUTION_SCOPE = (
    "the control pipeline is fully evaluated and only a stage-b grid charge may "
    "execute: every other direction -- discharge, export, curtailment and the "
    "phase-3 reserve guard -- is refused at the authorization stage and again at "
    "the send site, and executes nothing"
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
    control_horizon_minutes: int
    control_export_margin_percent: float
    #: Read, and deliberately absent from the options form while the release
    #: barrier makes it unable to change anything.
    control_execution_enabled: bool
    #: A ceiling, in kWh, on grid energy one Live charge run may buy. Zero means
    #: the commissioning tightener is off, never that charging is forbidden.
    grid_charge_budget_kwh: float
    #: Phase-8 economic settings. One threshold and two opt-ins, and all three
    #: are in the form because all three change the *published plan* -- unlike
    #: the execution flag above, which cannot change anything in this release.
    minimum_trade_gain_eur: float
    grid_charge_margin_eur_per_kwh: float
    allow_grid_charging: bool
    allow_battery_export: bool

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
            control_horizon_minutes=int(
                _number(
                    value(CONF_CONTROL_HORIZON_MINUTES),
                    DEFAULT_CONTROL_HORIZON_MINUTES,
                )
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
            allow_grid_charging=bool(
                value(CONF_ALLOW_GRID_CHARGING, DEFAULT_ALLOW_GRID_CHARGING)
            ),
            allow_battery_export=bool(
                value(CONF_ALLOW_BATTERY_EXPORT, DEFAULT_ALLOW_BATTERY_EXPORT)
            ),
        )


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

    horizon = build_horizon(
        demands=demands,
        prices=prices,
        required_reserve_kwh=raw_reserve,
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
        allow_grid_charging=allow_grid_charging,
        allow_battery_export=allow_battery_export,
        reserve_above_capacity_kwh=reserve_above_capacity_kwh,
        table_ms=table_ms,
        bucket_rule=bucket_rule,
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


def _mark_execution_error(report: dict[str, Any] | None, reason: str) -> None:
    """Record an execution error without assuming the report is well-formed."""
    block = _execution_block(report)
    if block is None:
        return
    result = block.get("result")
    if not isinstance(result, dict):
        result = {}
        block["result"] = result
    result["execution_error"] = reason


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
        # **The two stages of the pending sequence, and what is checked between
        # them.** Held apart rather than as one list because the whole point of
        # beta.24.1 is that stage two is *conditional*: an arm may not activate
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
        #: The last setpoint actually written, for the deadband comparison. Never
        #: the last *calculated* one: the deadband exists to compare against what
        #: is on the wire.
        self._applied_setpoint_kw: float | None = None
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
            self._last_tick_reason = "controller_error"

    async def _async_correct_setpoint(self, now: datetime) -> None:
        """Run one correction, with the execution lock already held."""
        run = self._carried
        snapshot = read_snapshot(self.hass)
        if run is None or not snapshot.dispatch_active:
            self._note_tick(now, TICK_SKIPPED_NO_RUN)
            return
        if self._ownership_now(snapshot, now) != OWNERSHIP_OWNED:
            self._note_tick(now, TICK_SKIPPED_NO_RUN)
            return
        if run.stale_at(now) or not run.actionable_at(now):
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
                await self._async_stop_owned_run(
                    now, snapshot, EXECUTION_STOP_COHERENCE_LOST
                )
            return

        decision = self._setpoint_for(run.target, now)
        if decision is None:
            self._note_tick(now, TICK_SKIPPED_STALE_TARGET)
            return
        self._record_physical_decision(now, decision, coherence)
        if not decision.update_needed:
            self._last_tick_reason = decision.update_reason
            return
        await self._async_send_locked(
            plan_dispatch_power(decision.applied_kw),
            now=now,
            verify=EXECUTION_VERIFY_DISPATCH_SETPOINT,
        )
        self._applied_setpoint_kw = decision.applied_kw
        self._last_tick_reason = TICK_APPLIED

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
            minutes = max(1.0, demand.remaining_minutes)
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
            await async_execute(self.hass, steps)
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

    async def _async_stop_owned_run(
        self, now: datetime, snapshot: Any, reason: str
    ) -> None:
        """Stop a dispatch we own, in the approved order, with the lock held.

        Enable **off first**, then verified inactive, and only then the resting
        values and the marker. The cleanup is withheld on an unverified stop for a
        concrete reason: writing the duration restarts the vendor timer, so tidying
        up a dispatch that did not actually stop would extend the run being ended.
        """
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
        await self._async_send_locked(plan_dispatch_cleanup(), now=now, verify=None)
        self._clear_execution_record()
        self._carried = None
        self._applied_setpoint_kw = None
        self._coherence = None
        self._forward = None
        self._emergency_attempts = 0
        self._sustained_deadline = None
        self._sustained_run_id = None
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
                readback_compatible=dispatch_readback_compatible(
                    snapshot,
                    expected_mode=DISPATCH_MODE_SOC_CONTROL,
                    expected_sign=CONTROL_EXECUTABLE_DISPATCH_SIGN,
                ),
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
        self._clear_execution_record()
        self._carried = None
        self._applied_setpoint_kw = None
        self._forward = None
        self._sustained_deadline = None
        self._sustained_run_id = None
        self._last_tick_reason = EXECUTION_STOP_MARKER_LOST

    @callback
    def _note_tick(self, now: datetime, reason: str) -> None:
        """Record a physical tick that wrote nothing, and why.

        Deliberately **not** a clamp reason: nothing was calculated, so naming a
        clamp would invent an explanation for a decision that was never made.
        """
        self._last_tick_reason = reason
        self._physical_decisions.append(
            {"controller_refresh_at": now.isoformat(), "update_reason": reason}
        )

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
    def _evidence_for(self, snapshot: Any, now: datetime) -> OwnershipEvidence:
        """Return the ownership evidence, including the signed readback."""
        return OwnershipEvidence(
            dispatch_active=bool(snapshot is not None and snapshot.dispatch_active),
            marker_on=bool(snapshot is not None and snapshot.owner_marker),
            record=self.store.execution_record,
            dispatch_start=_dispatch_start_instant(snapshot, now),
            run_id=self._owned_run_id(),
            now=now,
            readback_compatible=bool(
                snapshot is not None
                and dispatch_readback_compatible(
                    snapshot,
                    expected_mode=DISPATCH_MODE_SOC_CONTROL,
                    expected_sign=CONTROL_EXECUTABLE_DISPATCH_SIGN,
                )
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
        sample = evaluate_balance(self.read_flows(), self._source_coherence())
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
        await self._async_record_price_evidence_safely(
            forecasts=price_forecasts, now=now, tz=tz
        )

        absorb_surplus, absorption_reason = self._surplus_absorption()

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
        )

        await self._async_record_economic_evidence_safely(
            outcome=economic,
            plan=plan,
            now=now,
            today=today,
            tz=tz,
        )

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

        return await self.hass.async_add_executor_job(
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
            self.config.allow_grid_charging,
            self.config.allow_battery_export,
        )

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

        ``None`` for everything that is not an executable grid charge inside its
        window, which is what leaves the reserve-guard path untouched. The last
        Stage-B decision is reused rather than recomputed, so the intent and the
        published diagnostics describe the same refresh.
        """
        decision = self._stage_b_decision
        if decision is None or plan is None or plan.state is None:
            return None
        day = getattr(plan, "target_day", None)
        index = getattr(plan, "start_index", None)
        if day is None or index is None:
            return None
        return control_intent_for(
            decision,
            floor_soc_percent=plan.reserve.configured_min_soc_percent,
            # The pack's own maximum, and the only ceiling there is. If it cannot
            # be read the device layer refuses the charge rather than substituting
            # the discharge floor or a constant.
            ceiling_soc_percent=plan.state.limits.max_soc_percent,
            horizon_minutes=self.config.control_horizon_minutes,
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
        evidence = OwnershipEvidence(
            dispatch_active=bool(snapshot.dispatch_active),
            marker_on=bool(snapshot.owner_marker),
            record=self.store.execution_record,
            dispatch_start=_dispatch_start_instant(snapshot, now),
            run_id=self._owned_run_id(),
            now=now,
        )
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
            "target": target_as_published(run.target),
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
    def _adopt_persisted_run(self, snapshot: Any) -> None:
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
        if self._carried is not None:
            return
        if snapshot is None or not snapshot.dispatch_active:
            return
        if not snapshot.owner_marker:
            return
        adopted = carried_from_record(self.store.execution_record)
        if adopted is not None:
            self._carried = adopted

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
        if evidence.record_provenance != OWNERSHIP_PROVENANCE_SETTLING:
            return False
        record["dispatch_start"] = observed.isoformat()
        record["stamped_at"] = now.isoformat()
        self.store.execution_record = record
        self.store.schedule_save()
        return True

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
        pv_w = self._read_pv_power_w()
        load_w = self._read_house_load_w()
        if pv_w is None or load_w is None:
            # Incoherent inputs: attribute the whole charge to the grid, which is
            # the conservative direction for a ceiling on buying.
            surplus_w = 0.0
        else:
            surplus_w = max(0.0, pv_w - load_w)
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
            soc_delta = max(0.0, stored - opening)

        return measure_progress(
            accumulated_kwh=accumulated,
            soc_delta_kwh=soc_delta,
            current_quarter_kwh=accumulated,
            coverage=coverage,
            minimum_coverage=MIN_QUARTER_COVERAGE,
            reconstructed=opening is None,
        )

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
        self._adopt_persisted_run(snapshot)
        outcome = carry_forward(self._carried, self.execution_targets, now)
        self._carried = outcome.carried
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
        evidence = OwnershipEvidence(
            dispatch_active=bool(snapshot is not None and snapshot.dispatch_active),
            # A marker that does not exist is not a marker that is off: without it
            # ownership cannot be established at all, so a running dispatch reads
            # as foreign rather than as free.
            marker_on=bool(snapshot is not None and snapshot.owner_marker),
            record=self.store.execution_record,
            # Supplied since beta.20. Both were hardcoded ``None``, which made
            # ``record_matches`` permanently false and ``owned`` unreachable -- so
            # the continuation relaxation added in beta.19 could never fire, and
            # Alpha EMS would have inhibited itself the moment it armed anything.
            dispatch_start=_dispatch_start_instant(snapshot, now),
            run_id=None if carried is None else carried.run_id,
            # Read once, so the settle window is measured against this refresh
            # rather than against whenever a property happens to be evaluated.
            now=now,
        )
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
        report = execution_as_dict(decision, mode=mode, executed=False)
        report["actual_balance"] = self._execution_actuals(plan)
        report["safety"] = {
            "reserve_floor_kwh": (
                None if decision.target is None else decision.target.reserve_floor_kwh
            ),
            "stale": (
                None if decision.target is None else decision.target.stale_at(now)
            ),
            "deadman_duration_minutes": self.config.control_horizon_minutes,
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
            "commanded_duration_minutes": self.config.control_horizon_minutes,
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
        required = {
            entry.index: entry.required_dc_kwh
            for entry in (projection.intervals if projection else ())
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

        targets: list[dict[str, Any]] = []
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
            )
            target["revision"] = execution_revision(
                previous.get(target["plan_id"]), target
            )
            targets.append(target)
        return tuple(targets)

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
        forecast = (self.price_forecasts or {}).get(plan.target_day)
        if forecast is None:
            return {"available": False, "reason": "no_stored_prices"}

        count = record.interval_count
        buy: list[float | None] = [None] * count
        sell: list[float | None] = [None] * count
        for interval in forecast.intervals:
            if 0 <= interval.index < count:
                buy[interval.index] = interval.import_price_eur_kwh
                sell[interval.index] = interval.export_price_eur_kwh

        limits = plan.state.limits if plan.state is not None else None
        capacity = None if limits is None else limits.capacity_kwh
        window = realized_window(
            grid_import_kwh=[record.grid_import_at(i) for i in range(count)],
            grid_export_kwh=[record.grid_export_at(i) for i in range(count)],
            import_price_eur_kwh=buy,
            export_price_eur_kwh=sell,
            load_kwh=[record.baseline_at(i) for i in range(count)],
            production_kwh=[record.pv_at(i) for i in range(count)],
            stored_energy_kwh=soc_series_to_energy(
                [record.soc_at(i) for i in range(count)], capacity_kwh=capacity
            ),
            capacity_kwh=capacity,
            charge_efficiency=None if limits is None else limits.charge_efficiency,
            discharge_efficiency=(
                None if limits is None else limits.discharge_efficiency
            ),
        )
        return {
            "available": True,
            "day": plan.target_day.isoformat(),
            **window.as_dict(),
        }

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
            execution_blocked_reason=ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE,
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
        claiming = (
            run is not None and not self._pending_is_reset and self._pending_activates
        )
        if claiming:
            self._write_execution_record(
                run, self._pending_command, self._pending_snapshot, now
            )
        # **Two stages, and stage two is conditional.** This is the whole of
        # beta.24.1 at the send site: an activation may not be issued until the
        # ownership claim has been read back, and a running dispatch's fields may
        # not be disturbed until the deactivation has been read back.
        stage_one = self._pending_stage_one
        stage_two = self._pending_stage_two
        verify = self._pending_verify
        landed = True
        try:  # pragma: no cover - the barrier makes this unreachable
            if stage_one:
                await async_execute(self.hass, stage_one)
                landed = verify is None or self._staged_write_landed(verify)
            if landed and stage_two:
                await async_execute(self.hass, stage_two)
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
            _mark_execution_error(report, "execution_unavailable")
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
            report["state"] = CONTROL_STATE_EXECUTED
            # **One record of what is on the wire, whichever path wrote it.** The
            # quarter refresh and the sixty-second tick both command power, so a
            # deadband comparing against only one of them would compare against a
            # stale figure and either chatter or go deaf.
            for step in commands:
                if step.entity_id == DISPATCH_POWER and step.value is not None:
                    self._applied_setpoint_kw = step.value
            # **From the power actually written**, not from Stage B's request.
            # beta.19 copied ``requested_kw`` here, so the report would have
            # asserted one figure while a different one was on the wire -- and it
            # dereferenced a block that is ``None`` in every state reachable
            # today, which would have failed the whole refresh.
            _mark_execution_applied(report, self._pending_power_kw)
            self._last_control_write = now
            self._last_control_power_kw = self._pending_power_kw
            # **"Started" means a write carrying an activation succeeded**, and this
            # is the only place that can know it. Deriving it from the controller
            # state would say "started" for an *armed* decision -- computed, sent
            # nothing -- which is the one claim a release that writes must not get
            # wrong.
            self._activation_confirmed = self._pending_activates
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
            if self._pending_is_reset:
                self._clear_execution_record()
                # A stopped run has no dead-man to keep alive, and leaving the
                # observation behind would make the next run's first sustain compare
                # against a deadline from a run that no longer exists.
                self._sustained_deadline = None
                self._sustained_run_id = None

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
                    "this release cannot send one"
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
            intent = translate(
                plan, now=now, horizon_minutes=self.config.control_horizon_minutes
            )
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
        command = requested
        if requested is not None and requested.action == ACTION_DISCHARGE:
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
        recorded_run_id = self._owned_run_id()
        carried_run_id = self._carried.run_id if self._carried is not None else None
        sustaining = (
            command is not None
            and command.moves_battery
            and owned
            and bool(snapshot is not None and snapshot.dispatch_active)
            and recorded_run_id is not None
            and recorded_run_id == carried_run_id
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
        verdict = evaluate(intent, context)
        dispatch_active = bool(snapshot is not None and snapshot.dispatch_active)
        result = stage_b.get("result") or {}
        stop_reason = result.get("stop_reason")
        unsafe_while_owned = owned and dispatch_active and not verdict.safe
        if unsafe_while_owned and not stop_reason:
            stop_reason = EXECUTION_STOP_SAFETY
        resetting = owned and bool(result.get("reset_required") or unsafe_while_owned)
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
        setpoint = self._setpoint_for(
            None if self._carried is None else self._carried.target, now
        )
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
        elif command.action != ACTION_CHARGE:
            # **The advisory path, unchanged.** The Phase-3 reserve guard emits
            # discharges, and no release executes one: the typed barrier refuses
            # the action at authorisation and the send site refuses the entities.
            # It still has to be *planned*, because shadow reporting is what a
            # user reads to decide whether to trust the layer at all.
            #
            # This is not a second Live path. Live charge is Dispatch and only
            # Dispatch; the helper families remain the advisory surface for
            # actions that have no actuator, and remain refused at every
            # boundary.
            stage_two = plan_arm_parameters(command)
            stage_one = plan_marker_claim() if stage_two else ()
            verify = EXECUTION_VERIFY_MARKER_ON if stage_one else None
        elif setpoint is not None and setpoint.applied_kw < 0.0:
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
        if refusal is None and commands:
            refusal = dispatch_refusal(commands)
        if refusal is not None:
            commands = ()
            stage_one = stage_two = ()
            verify = None
        stage_b["write_boundary"] = {
            "refusal": refusal,
            # **The physical controller, and the ring behind it.** Diagnostics
            # are rarely captured at the moment production moved, so a download
            # taken later has to be able to reconstruct the quarter rather than
            # only describe the instant it was taken.
            "controller": self._controller_block(setpoint, now),
            "physical_decisions": list(self._physical_decisions),
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
                "reset"
                if resetting
                else "marker_release"
                if releasing
                else "sustain"
                if sustaining
                else "arm"
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
        if resetting:
            decision = authorize_reset(
                ownership=ownership_state or OWNERSHIP_NONE,
                stopping_action=reset_action,
                stop_reason=stop_reason,
                steps_planned=len(commands),
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
            )

        state = CONTROL_STATE_INHIBITED
        if resetting or releasing:
            # A stop is not an eligibility question. Reporting ``inhibited`` because
            # the world is unsafe would describe the condition that *caused* the
            # stop as though it had prevented it.
            state = CONTROL_STATE_ELIGIBLE if commands else CONTROL_STATE_IDLE
        elif verdict.safe:
            state = CONTROL_STATE_ELIGIBLE if commands else CONTROL_STATE_IDLE

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
        self._pending_run_id = carried_run_id

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
            max_source_age_seconds=BALANCE_MAX_SOURCE_AGE_SECONDS,
            device_power_kw=0.0 if command is None else command.power_kw,
            device_cutoff_percent=(
                0 if command is None else command.cutoff_soc_percent
            ),
            device_duration_minutes=(
                0 if command is None else command.duration_minutes
            ),
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
