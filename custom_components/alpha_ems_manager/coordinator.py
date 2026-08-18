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
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import EVENT_CORE_CONFIG_UPDATE
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .confidence import ConfidenceBreakdown, compute_confidence
from .const import (
    CONF_BATTERY_POWER_ENTITY,
    CONF_BATTERY_POWER_SIGN,
    CONF_BATTERY_SOC_ENTITY,
    CONF_DAILY_HOUSE_LOAD_ENTITY,
    CONF_EV_POWER_ENTITY,
    CONF_FRANK_ENTRY_ID,
    CONF_GRID_POWER_ENTITY,
    CONF_GRID_POWER_SIGN,
    CONF_HAS_PV,
    CONF_HOUSE_LOAD_ENTITY,
    CONF_PV_POWER_ENTITY,
    CONF_SOLCAST_ENTRY_ID,
    CONF_USE_PV_FORECAST,
    DEFAULT_BATTERY_POWER_SIGN,
    DEFAULT_GRID_POWER_SIGN,
    DOMAIN_FRANK,
    DOMAIN_SOLCAST,
    LOG_THROTTLE_SECONDS,
    QUARTER_MINUTES,
    SAFETY_SAMPLE_SECONDS,
)
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
from .normalization import (
    PowerFlows,
    describe_power_problem,
    normalize_energy_kwh,
    normalize_power_w,
    split_battery_power,
    split_grid_power,
)
from .quarter import (
    QuarterAccumulator,
    QuarterResult,
    sanitize_ev_w,
    sanitize_load_w,
)
from .storage import LearningStore, index_for_start_utc, utc_midnight

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
_BALANCE_LOG_MODERATE = "energy_balance_moderate"
_BALANCE_LOG_GROSS = "energy_balance_gross"


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
        )


def _tally(counts: dict[str, int], key: str) -> None:
    """Increment ``key`` in a bounded counter mapping."""
    counts[key] = counts.get(key, 0) + 1


def _non_negative(value: float | None) -> float | None:
    """Return ``value`` when it is a usable non-negative power, else ``None``."""
    if value is None or value < 0:
        return None
    return value


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
        self._accumulator = QuarterAccumulator(dt_util.get_default_time_zone())
        self._ev_accumulator: QuarterAccumulator | None = None
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

    # -- lifecycle -------------------------------------------------------

    async def async_prepare(self) -> None:
        """Load persisted history before entities are added."""
        await self.store.async_load(str(dt_util.get_default_time_zone()))

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

        watched = [
            entity_id
            for entity_id in (
                self.config.house_load_entity,
                self.config.ev_power_entity,
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
        """Flush pending learning data to disk."""
        await self.store.async_save_now()

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
        pv = _non_negative(pv)

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

        self._ingest(house_results, ev_results)

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
    def _ingest(
        self,
        house_results: list[QuarterResult],
        ev_results: list[QuarterResult],
    ) -> None:
        """Persist finalised intervals that carry enough coverage.

        Intervals are stored by chronological index rather than by wall-clock
        slot, so a fall-back day keeps both occurrences of the repeated hour.
        """
        if not house_results:
            return

        tz = dt_util.get_default_time_zone()
        ev_expected = self._ev_accumulator is not None
        # The two accumulators are advanced together, so equal-length result
        # lists are the normal case; pairing by start instant keeps the mapping
        # correct even if one of them was created later.
        ev_by_start = {result.start_utc: result for result in ev_results}

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

            if not record.record_interval(
                index,
                measured_kwh=result.energy_kwh,
                ev_kwh=ev_kwh,
                ev_expected=ev_expected,
            ):
                # The index fell outside the day. Unreachable under a stable
                # timezone, so reaching it means the stored day's shape and the
                # instant being filed disagree -- which must be counted and
                # named rather than leaving a quarter that reports itself as
                # finalised while having stored nothing.
                self._record_rejected_quarter(result, REJECT_INTERVAL_OUT_OF_RANGE)
                continue

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
            self.balance.record_unavailable()
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

        return {
            "today": adapted,
            "today_baseline": baseline_today,
            "tomorrow": forecast_tomorrow,
            "confidence": breakdown,
            "learning_days": breakdown.learned_days,
            "elapsed_intervals": elapsed,
            "measured_so_far_kwh": measured_so_far,
            "ev_so_far_kwh": ev_so_far,
        }

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
        """Return whether a referenced config entry is present and loaded."""
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
        """Return whether the configured Solcast entry is loaded."""
        if not self.config.use_pv_forecast:
            return False
        return self._entry_loaded(DOMAIN_SOLCAST, self.config.solcast_entry_id)

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
