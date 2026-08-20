"""Runtime orchestration for one Alpha EMS Manager config entry.

The coordinator owns the whole data path: it listens to the configured source
entities, integrates house load into quarter-hour buckets, persists finalised
quarters, and derives the forecasts and confidence the four sensors display.

It never contacts an external service. Frank, Solcast, AlphaESS and the grid
meter are read purely through the Home Assistant state machine and config-entry
registry, so this integration adds no API traffic of its own.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
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

from .alphaess_adapter import discover, read_snapshot
from .alphaess_device import build_command, plan_commands
from .api import load_forecast_from
from .battery import INTERVAL_HOURS, sanitize_soc_percent
from .confidence import ConfidenceBreakdown, compute_confidence
from .const import (
    BALANCE_MAX_SOURCE_AGE_SECONDS,
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
    CONF_GRID_POWER_ENTITY,
    CONF_GRID_POWER_SIGN,
    CONF_HAS_PV,
    CONF_HOUSE_LOAD_ENTITY,
    CONF_PV_POWER_ENTITY,
    CONF_SELECTED_SOLCAST_SITE_IDS,
    CONF_SOLCAST_ENTRY_ID,
    CONF_USE_PV_FORECAST,
    CONTROL_EXECUTION_AVAILABLE,
    CONTROL_MODE_OFF,
    CONTROL_MODE_OPTIONS,
    CONTROL_STATE_ELIGIBLE,
    CONTROL_STATE_IDLE,
    CONTROL_STATE_INHIBITED,
    CONTROL_STATE_OFF,
    DEFAULT_BATTERY_MIN_SOC_PERCENT,
    DEFAULT_BATTERY_POWER_SIGN,
    DEFAULT_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
    DEFAULT_CONTROL_EXECUTION_ENABLED,
    DEFAULT_CONTROL_EXPORT_MARGIN_PERCENT,
    DEFAULT_CONTROL_HORIZON_MINUTES,
    DEFAULT_GRID_POWER_SIGN,
    DOMAIN_FRANK,
    INHIBIT_NO_DECISION,
    INHIBIT_NO_PLAN,
    INHIBIT_PLAN_UNAVAILABLE,
    LOG_THROTTLE_SECONDS,
    MAX_CONTROL_EVENTS_REPORTED,
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
    SAFETY_SAMPLE_SECONDS,
    SELECT_INVERTER_AC_LIMIT,
)
from .control import translate
from .energy_balance import (
    OUTCOME_SKIPPED_INCOHERENT,
    BalanceMonitor,
    BalanceSample,
    SourceCoherence,
    evaluate_balance,
    measure_coherence,
)
from .forecast import (
    DayForecast,
    TodayForecast,
    adapt_today,
    build_forecast,
    collect_forecast_inputs,
)
from .forecast_recorder import ForecastRecorder, RecorderResult
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
from .safety import (
    ControlContext,
    ExecutionDecision,
    SafetyVerdict,
    absorbing_capacity_kw,
    authorize,
    evaluate,
)
from .soc_coherence import SocCoherenceMonitor
from .solcast_source import SolcastCapability, SolcastFacts, read_facts, read_forecast
from .solcast_source import discover as discover_solcast
from .storage import (
    DayRecord,
    LearningStore,
    expected_quarters_for,
    index_for_start_utc,
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

#: The statement every control surface in this release repeats, because it is the
#: single most important fact about it.
_CONTROLS_NOTHING = (
    "the control pipeline is fully evaluated but cannot execute: no command "
    "reaches the inverter in this release"
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
            control_execution_enabled=bool(
                value(
                    CONF_CONTROL_EXECUTION_ENABLED,
                    DEFAULT_CONTROL_EXECUTION_ENABLED,
                )
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
        self._log = _ThrottledLogger()
        self.last_balance: BalanceSample | None = None
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
        #: for the lifetime of this release, because nothing is ever sent.
        self._last_control_write: datetime | None = None
        self._last_control_power_kw: float | None = None
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

    # -- lifecycle -------------------------------------------------------

    async def async_prepare(self) -> None:
        """Load persisted history before entities are added.

        Both documents are read here so the first refresh already has the
        forecast evidence in hand: without it, that refresh would look like a
        fresh installation and re-issue snapshots that are already on disk.
        """
        await self.store.async_load(str(dt_util.get_default_time_zone()))
        await self.history.async_load()

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
        """Advance integration even while the source is quiet."""
        self._sample(dt_util.as_local(now))
        self._sample_balance()

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

        self._ingest(house_results, ev_results, pv_results)

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
            self._ev_problem = REJECT_SOURCE_MISSING
            self._log.warning(
                "missing_ev",
                (
                    "EV charger entity %s does not exist; baseline learning is "
                    "paused while measured house load keeps being recorded"
                ),
                entity_id,
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

            if not record.record_interval(
                index,
                measured_kwh=result.energy_kwh,
                ev_kwh=ev_kwh,
                ev_expected=ev_expected,
                soc_percent=soc_percent if result.start_utc == latest_start else None,
                pv_kwh=None if pv_result is None else pv_result.energy_kwh,
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

        control = self._build_control_report_safely(
            plan=plan,
            now=now,
            today=today,
            elapsed=elapsed,
            today_interval_count=baseline_today.interval_count,
        )

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
            "control": control,
            "pv_today": pv_forecasts.get(today),
            "pv_tomorrow": pv_forecasts.get(tomorrow),
            "pv_absorption": {
                "modelled": absorb_surplus,
                "reason": absorption_reason,
            },
        }

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
            # No intent, no gate, no command planning, and nothing read from the
            # control surface. Off means this integration is not attempting
            # control at all.
            return {
                "mode": mode,
                "state": CONTROL_STATE_OFF,
                "execution_available": CONTROL_EXECUTION_AVAILABLE,
                "execution_enabled": self.config.control_execution_enabled,
                "off_semantics": (
                    "off means this integration attempts no control and writes "
                    "nothing; it does not mean an inverter reverts, and in this "
                    "release the distinction cannot arise because nothing here "
                    "can start a dispatch. it does still read whether the "
                    "inverter is storing surplus production, because a projected "
                    "state of charge that assumed the opposite would be wrong -- "
                    "reading is not controlling"
                ),
                "controls_nothing": _CONTROLS_NOTHING,
            }

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

        intent = translate(
            plan, now=now, horizon_minutes=self.config.control_horizon_minutes
        )
        command = build_command(intent) if intent is not None else None
        commands = plan_commands(command) if command is not None else ()

        context = ControlContext(
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

        verdict = evaluate(intent, context)
        starts_or_increases = (
            command is not None
            and command.moves_battery
            and (
                self._last_control_power_kw is None
                or command.power_kw > self._last_control_power_kw
            )
        )
        decision = authorize(
            verdict,
            context,
            commands_planned=len(commands),
            starts_or_increases=starts_or_increases,
        )

        state = CONTROL_STATE_INHIBITED
        if verdict.safe:
            state = CONTROL_STATE_ELIGIBLE if commands else CONTROL_STATE_IDLE

        self._record_control_event(now, state, verdict, decision)

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
            # The export bound as the gate actually saw it, at the instant the
            # gate saw it. Without this a ``would_export`` verdict could not be
            # reconstructed from a diagnostics download, because the flow block
            # elsewhere in the payload is read at download time and describes a
            # different instant -- which is exactly how a correct inhibit came to
            # look arithmetically wrong beside the readings printed next to it.
            "export_check": {
                "absorbing_capacity_kw": round(absorbing_capacity_kw(context), 4),
                "grid_import_w": context.grid_import_w,
                "grid_export_w": context.grid_export_w,
                "battery_power_w": context.battery_power_w,
                "margin_percent": context.export_margin_percent,
                "commanded_power_kw": context.device_power_kw,
                "basis": (
                    "capacity = max(0, grid_import - grid_export + battery "
                    "discharge), measured at the meter; the margin reduces the "
                    "capacity and never the command, and the command is refused "
                    "whole rather than scaled to fit"
                ),
            },
            "authorization": decision.as_dict(),
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
                "is what active would have attempted"
            ),
            "controls_nothing": _CONTROLS_NOTHING,
        }

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

    def _entry_loaded(self, domain: str, entry_id: str | None) -> bool:
        """Return whether a referenced config entry is present and loaded.

        **Do not use this to decide whether another integration can be read.**
        Only ``frank_available`` still consults it, and only to report a status
        nothing acts on -- Frank is not consumed yet.

        The PV layer used to ask this question and it was wrong to. Setup state
        says nothing about whether a registered action can be called: an
        integration that registers its actions at component level has them
        available while its config entry is still setting up, so requiring
        ``LOADED`` produced a live false negative on every restart. See
        ``solcast_source.SolcastCapability``, which establishes capability from
        the entry existing and the actions being registered instead.

        Phase 6 will start consuming Frank, and this is the trap it should not
        walk into: work out what can actually be called, not what state somebody
        else's entry happens to be in.
        """
        for entry in self.hass.config_entries.async_entries(domain):
            if entry_id is not None and entry.entry_id != entry_id:
                continue
            return entry.state is ConfigEntryState.LOADED
        return False

    @property
    def frank_available(self) -> bool:
        """Return whether the configured Frank Quarter Prices entry is loaded."""
        return self._entry_loaded(DOMAIN_FRANK, self.config.frank_entry_id)

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
