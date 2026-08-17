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
from .forecast import DayForecast, TodayForecast, adapt_today, build_forecast
from .normalization import (
    PowerFlows,
    normalize_energy_kwh,
    normalize_power_w,
    split_battery_power,
    split_grid_power,
)
from .quarter import QuarterAccumulator, QuarterResult, sanitize_ev_w
from .storage import LearningStore, index_for_start_utc, utc_midnight

_LOGGER = logging.getLogger(__name__)

#: Seconds after each quarter boundary at which the bucket is closed. The small
#: delay lets sources that publish exactly on the boundary land first.
_QUARTER_TRIGGER_SECOND = 5


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

    def warning(self, key: str, message: str, *args: Any) -> None:
        """Log ``message`` unless the same ``key`` fired recently."""
        now = dt_util.utcnow()
        previous = self._last.get(key)
        if previous is not None and (now - previous) < timedelta(
            seconds=LOG_THROTTLE_SECONDS
        ):
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            return
        skipped = self._suppressed.pop(key, 0)
        self._last[key] = now
        if skipped:
            _LOGGER.warning(
                "%s (%d further occurrences suppressed)", message % args, skipped
            )
        else:
            _LOGGER.warning(message, *args)

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
        #: Intervals whose measured load was accepted but whose flexible-load
        #: reading was not, so their baseline is unusable.
        self.invalid_ev_quarters = 0

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

        # Seed the accumulator so integration starts from the current reading
        # rather than from the first future state change.
        self._sample(dt_util.now())

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
        house = _non_negative(self._read_power(self.config.house_load_entity))
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
            self._log.warning(
                "missing_house_load",
                "House load entity %s does not exist; learning is paused",
                entity_id,
            )
            return None

        value_w = normalize_power_w(
            state.state, state.attributes.get("unit_of_measurement")
        )
        if value_w is None:
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
            self._log.warning(
                "missing_ev",
                (
                    "EV charger entity %s does not exist; baseline learning is "
                    "paused while measured house load keeps being recorded"
                ),
                entity_id,
            )
            return None

        value_w = normalize_power_w(
            state.state, state.attributes.get("unit_of_measurement")
        )
        if value_w is None:
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
                self.rejected_quarters += 1
                continue

            record = self.store.get_or_create(result.day, tz)
            index = index_for_start_utc(result.day, result.start_utc, tz)

            ev_kwh: float | None = None
            if ev_expected:
                ev_result = ev_by_start.get(result.start_utc)
                if ev_result is not None and ev_result.accepted:
                    ev_kwh = ev_result.energy_kwh
                else:
                    self.invalid_ev_quarters += 1

            record.record_interval(
                index,
                measured_kwh=result.energy_kwh,
                ev_kwh=ev_kwh,
                ev_expected=ev_expected,
            )
            self.last_finalized_quarter = result.start_utc
            self.store.last_finalized = result.start_utc.isoformat()
            changed = True

        if changed:
            self.store.schedule_save()

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
    def _source_coherence(self) -> SourceCoherence:
        """Return how closely aligned in time the balance sources are.

        ``last_reported`` is preferred over ``last_updated`` because it advances
        on every publication, including one that repeats the previous value. A
        steady battery power that has read the same figure for ten minutes is
        perfectly current, but its ``last_updated`` is ten minutes old and would
        look stale.
        """
        reported_at: list[datetime] = []
        for entity_id in self._balance_source_entities():
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            reported_at.append(
                getattr(state, "last_reported", None) or state.last_updated
            )
        return measure_coherence(reported_at, dt_util.utcnow())

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
            self._log.clear("energy_balance")
            return

        if self.balance.should_warn():
            # Two wordings, because the two situations call for different action.
            # A residual several times its physical allowance means a term of the
            # identity is wrong, and the user should re-check the configuration.
            # A residual only somewhat over it is far more likely to be the
            # sources sitting on different electrical boundaries, which is worth
            # reporting but is not a mistake the user made.
            if sample.gross_fault_suspected:
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
            self.balance.last_warning = dt_util.utcnow().isoformat()
            self._log.warning("energy_balance", message, *args)

    # -- derived values --------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Recompute forecasts and confidence from the learned history."""
        now = dt_util.now()
        tz = dt_util.get_default_time_zone()
        today = now.date()
        tomorrow = today + timedelta(days=1)
        records = list(self.store.days.values())

        baseline_today = build_forecast(records, today, today, tz)
        forecast_tomorrow = build_forecast(records, today, tomorrow, tz)

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
        """Return the dates that currently count as learned."""
        return [record.day for record in self.store.learned_days()]
