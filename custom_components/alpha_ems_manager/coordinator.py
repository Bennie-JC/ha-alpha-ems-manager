"""Data update coordinator for Alpha EMS Manager.

The coordinator is responsible for:

* Reading the configured source sensors (house load, PV, prices, battery).
* Learning the household load profile per 15-minute interval, bucketed by
  season and by weekday/weekend.
* Persisting the learned profile so it survives restarts.
* Combining the learned load with PV forecasts and battery state to estimate
  the reserve energy required between the next sell window and the next buy
  window.

This is a scaffold: the learning and reserve maths are intentionally simple and
self-contained so they can be refined later. No AlphaESS write commands are
performed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from collections.abc import Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    BATTERY_FLOOR_KWH,
    CONF_BATTERY_CAPACITY_KWH_ENTITY,
    CONF_BATTERY_CURRENT_KWH_SENSOR,
    CONF_BATTERY_SOC_SENSOR,
    CONF_CUMULATIVE_HOUSE_LOAD_SENSOR,
    CONF_EV_CHARGER_POWER_SENSOR,
    CONF_FRANK_CHEAPEST_TIME_TODAY_SENSOR,
    CONF_FRANK_CHEAPEST_TIME_TOMORROW_SENSOR,
    CONF_FRANK_MOST_EXPENSIVE_TIME_TODAY_SENSOR,
    CONF_FRANK_MOST_EXPENSIVE_TIME_TOMORROW_SENSOR,
    CONF_FRANK_PRICES_TODAY_SENSOR,
    CONF_FRANK_PRICES_TOMORROW_SENSOR,
    CONF_PV_ACTUAL_TODAY_SENSOR,
    CONF_PV_EAST_SENSOR,
    CONF_PV_FORECAST_TODAY_SENSOR,
    CONF_PV_FORECAST_TOMORROW_SENSOR,
    CONF_PV_WEST_SENSOR,
    CONFIDENCE_DAYS_TARGET,
    CONFIDENCE_UPDATES_TARGET,
    DOMAIN,
    GLOBAL_PROFILE_KEY,
    INTERVAL_MINUTES,
    INTERVALS_PER_DAY,
    LEARNING_ALPHA,
    MAX_QUARTER_DELTA_KWH,
    MIN_CONFIDENT_SLOTS,
    MINIMUM_SPREAD_ENTITY,
    TRADE_MINIMUM_SPREAD_DEFAULT,
    PV_CONFIDENCE_DAYS_TARGET,
    PV_CONFIDENCE_UPDATES_TARGET,
    PV_FACTOR_ALPHA,
    PV_FACTOR_MAX,
    PV_FACTOR_MIN,
    RESERVE_CONFIDENCE_DAYS_TARGET,
    RESERVE_FACTOR_ALPHA,
    RESERVE_FACTOR_MAX,
    RESERVE_FACTOR_MIN,
    RESERVE_MISS_TARGET_MULTIPLIER,
    RESERVE_SUCCESS_MARGIN_KWH,
    SEASONS,
    STORAGE_KEY,
    STORAGE_VERSION,
    TRADE_BATTERY_CAPACITY_KWH,
)
from .trade_engine import TradePredictionLearningModel, compute_trade_prediction

_LOGGER = logging.getLogger(__name__)


def _season_for(moment: datetime) -> str:
    """Return a coarse meteorological season label for the given moment."""
    month = moment.month
    if month in (12, 1, 2):
        return "winter"
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    return "autumn"


def _day_type_for(moment: datetime) -> str:
    """Return 'weekend' for Saturday/Sunday, otherwise 'weekday'."""
    return "weekend" if moment.weekday() >= 5 else "weekday"


def _interval_index(moment: datetime) -> int:
    """Return the 0-based 15-minute interval index within the day."""
    return (moment.hour * 60 + moment.minute) // INTERVAL_MINUTES


def _profile_key(season: str, day_type: str) -> str:
    """Return the storage key for a season/day-type profile."""
    return f"{season}_{day_type}"


@dataclass
class LearningModel:
    """Container for learned per-interval load profiles.

    ``profiles`` maps a profile key to a list of ``INTERVALS_PER_DAY`` floats,
    each representing the learned energy (kWh) consumed during that 15-minute
    interval. A ``"global"`` profile aggregates everything; season/day-type
    profiles are maintained alongside it for future, more granular planning.
    """

    profiles: dict[str, list[float]] = field(default_factory=dict)
    # Baseline tracking for cumulative-delta calculation.
    previous_house_load: float | None = None
    previous_slot: int | None = None
    previous_update_time: str | None = None
    # Diagnostics / debug bookkeeping for the most recent learning cycle.
    last_raw_delta: float | None = None
    last_delta_per_slot: float | None = None
    distributed_slots: list[int] = field(default_factory=list)
    last_learned_slots: list[int] = field(default_factory=list)
    last_update: str | None = None
    update_count: int = 0
    # Distinct calendar dates on which a valid sample was learned.
    learned_dates: list[str] = field(default_factory=list)
    # EV exclusion diagnostics (per learning cycle, not persisted except totals).
    last_ev_power_kw: float = 0.0
    ev_excluded_last_quarter_kwh: float = 0.0
    ev_excluded_today_kwh: float = 0.0
    ev_today_date: str | None = None
    house_load_raw_last_quarter_kwh: float | None = None
    house_load_corrected_last_quarter_kwh: float | None = None
    ev_exclusion_active: bool = False

    @property
    def learned_days(self) -> int:
        """Return the number of distinct days with learned data."""
        return len(self.learned_dates)

    def profile(self, key: str) -> list[float]:
        """Return (creating if needed) the profile for a given key."""
        if key not in self.profiles:
            self.profiles[key] = [0.0] * INTERVALS_PER_DAY
        return self.profiles[key]

    def global_profile(self) -> list[float]:
        """Return the aggregate (global) learned profile."""
        return self.profile(GLOBAL_PROFILE_KEY)

    def to_dict(self) -> dict[str, Any]:
        """Serialise the model for persistent storage."""
        return {
            "profiles": self.profiles,
            "previous_house_load": self.previous_house_load,
            "previous_slot": self.previous_slot,
            "previous_update_time": self.previous_update_time,
            "last_raw_delta": self.last_raw_delta,
            "last_delta_per_slot": self.last_delta_per_slot,
            "distributed_slots": self.distributed_slots,
            "last_learned_slots": self.last_learned_slots,
            "last_update": self.last_update,
            "update_count": self.update_count,
            "learned_dates": self.learned_dates,
            # EV exclusion daily totals (persist so restart mid-day is accurate).
            "ev_excluded_today_kwh": self.ev_excluded_today_kwh,
            "ev_today_date": self.ev_today_date,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> LearningModel:
        """Restore a model from persistent storage.

        Falls back to the previous storage schema keys
        (``last_cumulative_value``/``last_interval_index``) so existing learned
        data is not lost on upgrade.
        """
        if not data:
            return cls()
        return cls(
            profiles=data.get("profiles", {}),
            previous_house_load=data.get(
                "previous_house_load", data.get("last_cumulative_value")
            ),
            previous_slot=data.get(
                "previous_slot", data.get("last_interval_index")
            ),
            previous_update_time=data.get("previous_update_time"),
            last_raw_delta=data.get("last_raw_delta", data.get("last_delta")),
            last_delta_per_slot=data.get("last_delta_per_slot"),
            distributed_slots=data.get("distributed_slots", []),
            last_learned_slots=data.get("last_learned_slots", []),
            last_update=data.get("last_update"),
            update_count=data.get("update_count", 0),
            learned_dates=data.get("learned_dates", []),
            ev_excluded_today_kwh=data.get("ev_excluded_today_kwh", 0.0),
            ev_today_date=data.get("ev_today_date"),
        )


@dataclass
class PvLearningModel:
    """Learned Solcast PV forecast-correction state.

    Learns a multiplicative correction factor (``actual / forecast``) for the
    daily Solcast forecast, both globally and per season, using an exponential
    moving average. The corrected forecast feeds the PV-aware reserve
    calculation. This is intentionally simple (daily totals); a future version
    can move to quarter-hour PV profiles.
    """

    # Correction factors (start neutral at 1.0).
    global_factor: float = 1.0
    season_factors: dict[str, float] = field(
        default_factory=lambda: {season: 1.0 for season in SEASONS}
    )
    # Running tracking of the active day so it can be finalised at rollover.
    current_date: str | None = None
    current_season: str | None = None
    last_actual_today: float | None = None
    last_forecast_today: float | None = None
    # Diagnostics for the most recently finalised day.
    last_pv_error: float | None = None
    last_pv_error_factor: float | None = None
    last_update: str | None = None
    update_count: int = 0
    # Distinct calendar dates on which a PV correction was learned.
    pv_learned_dates: list[str] = field(default_factory=list)

    @property
    def pv_learning_days(self) -> int:
        """Return the number of distinct days with learned PV corrections."""
        return len(self.pv_learned_dates)

    def season_factor(self, season: str) -> float:
        """Return the correction factor for a season (default neutral)."""
        return self.season_factors.get(season, 1.0)

    def to_dict(self) -> dict[str, Any]:
        """Serialise the PV model for persistent storage."""
        return {
            "global_factor": self.global_factor,
            "season_factors": self.season_factors,
            "current_date": self.current_date,
            "current_season": self.current_season,
            "last_actual_today": self.last_actual_today,
            "last_forecast_today": self.last_forecast_today,
            "last_pv_error": self.last_pv_error,
            "last_pv_error_factor": self.last_pv_error_factor,
            "last_update": self.last_update,
            "update_count": self.update_count,
            "pv_learned_dates": self.pv_learned_dates,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> PvLearningModel:
        """Restore a PV model from persistent storage."""
        if not data:
            return cls()
        season_factors = {season: 1.0 for season in SEASONS}
        season_factors.update(data.get("season_factors", {}))
        return cls(
            global_factor=data.get("global_factor", 1.0),
            season_factors=season_factors,
            current_date=data.get("current_date"),
            current_season=data.get("current_season"),
            last_actual_today=data.get("last_actual_today"),
            last_forecast_today=data.get("last_forecast_today"),
            last_pv_error=data.get("last_pv_error"),
            last_pv_error_factor=data.get("last_pv_error_factor"),
            last_update=data.get("last_update"),
            update_count=data.get("update_count", 0),
            pv_learned_dates=data.get("pv_learned_dates", []),
        )


@dataclass
class ReserveLearningModel:
    """Self-learning correction for the reserve calculation.

    Observes the integration's own battery current energy (kWh) and learns
    whether the reserve estimate was too optimistic (the battery hit the
    protective floor) or comfortably conservative (it stayed safely above the
    floor for a whole day), nudging a multiplicative correction factor with an
    exponential moving average. The factor never drops below ``1.0`` and is
    capped at ``2.0``.
    """

    reserve_correction_factor: float = 1.0
    reserve_miss_count: int = 0
    reserve_success_count: int = 0
    last_reserve_miss: str | None = None
    last_reserve_success: str | None = None
    last_battery_energy: float | None = None
    reserve_learning_status: str = "learning"
    # Internal day tracking for once-per-day miss/success evaluation.
    current_date: str | None = None
    day_min_battery_energy: float | None = None
    last_miss_date: str | None = None
    last_success_date: str | None = None
    last_update: str | None = None
    update_count: int = 0
    # Distinct calendar dates that were evaluated (miss or success).
    reserve_learned_dates: list[str] = field(default_factory=list)

    @property
    def reserve_learning_days(self) -> int:
        """Return the number of distinct evaluated reserve days."""
        return len(self.reserve_learned_dates)

    def to_dict(self) -> dict[str, Any]:
        """Serialise the reserve model for persistent storage."""
        return {
            "reserve_correction_factor": self.reserve_correction_factor,
            "reserve_miss_count": self.reserve_miss_count,
            "reserve_success_count": self.reserve_success_count,
            "last_reserve_miss": self.last_reserve_miss,
            "last_reserve_success": self.last_reserve_success,
            "last_battery_energy": self.last_battery_energy,
            "reserve_learning_status": self.reserve_learning_status,
            "current_date": self.current_date,
            "day_min_battery_energy": self.day_min_battery_energy,
            "last_miss_date": self.last_miss_date,
            "last_success_date": self.last_success_date,
            "last_update": self.last_update,
            "update_count": self.update_count,
            "reserve_learned_dates": self.reserve_learned_dates,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ReserveLearningModel:
        """Restore a reserve model from persistent storage."""
        if not data:
            return cls()
        return cls(
            reserve_correction_factor=data.get("reserve_correction_factor", 1.0),
            reserve_miss_count=data.get("reserve_miss_count", 0),
            reserve_success_count=data.get("reserve_success_count", 0),
            last_reserve_miss=data.get("last_reserve_miss"),
            last_reserve_success=data.get("last_reserve_success"),
            last_battery_energy=data.get("last_battery_energy"),
            reserve_learning_status=data.get("reserve_learning_status", "learning"),
            current_date=data.get("current_date"),
            day_min_battery_energy=data.get(
                "day_min_battery_energy", data.get("day_min_battery")
            ),
            last_miss_date=data.get("last_miss_date"),
            last_success_date=data.get("last_success_date"),
            last_update=data.get("last_update"),
            update_count=data.get("update_count", 0),
            reserve_learned_dates=data.get("reserve_learned_dates", []),
        )


class AlphaEmsCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinate state reading, learning and reserve calculation."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialise the coordinator.

        No fixed ``update_interval`` is used. Instead the coordinator is driven
        by a wall-clock listener (see :meth:`async_setup_quarter_hour_tracking`)
        so that learning runs exactly at minutes 0, 15, 30 and 45 and quarter
        deltas are attributed to the correct wall-clock quarter.
        """
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=None,
        )
        self.entry = entry
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.model = LearningModel()
        self.pv_model = PvLearningModel()
        self.reserve_model = ReserveLearningModel()
        self.trade_model = TradePredictionLearningModel()
        self.storage_loaded = False
        self.storage_saved = False

    @callback
    def async_setup_quarter_hour_tracking(self) -> Callable[[], None]:
        """Schedule learning updates on wall-clock quarter-hour boundaries.

        Returns an unsubscribe callable that the config entry registers for
        teardown.
        """

        @callback
        def _handle_quarter_hour(now: datetime) -> None:
            _LOGGER.debug("Quarter-hour boundary reached at %s", now.isoformat())
            self.hass.async_create_task(self.async_request_refresh())

        return async_track_time_change(
            self.hass,
            _handle_quarter_hour,
            minute=[0, 15, 30, 45],
            second=0,
        )

    # -- Configuration helpers -------------------------------------------------

    def _config(self, key: str) -> str | None:
        """Return a configured entity id, preferring options over data."""
        return self.entry.options.get(key, self.entry.data.get(key))

    def _state(self, key: str) -> State | None:
        """Return the Home Assistant state object for a configured entity."""
        entity_id = self._config(key)
        if not entity_id:
            return None
        return self.hass.states.get(entity_id)

    def _float(self, key: str) -> float | None:
        """Return a configured entity's numeric state, or None if unavailable."""
        state = self._state(key)
        if state is None or state.state in (None, "", "unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    # -- Persistent storage ----------------------------------------------------

    async def async_load_store(self) -> None:
        """Load the learned models (house load + PV) from disk."""
        stored = await self._store.async_load()
        self.model = LearningModel.from_dict(stored)
        # PV learning data is nested under "pv" so the existing house-load
        # schema at the top level stays backward compatible.
        self.pv_model = PvLearningModel.from_dict(
            stored.get("pv") if stored else None
        )
        # Reserve learning data is nested under "reserve" for the same reason.
        self.reserve_model = ReserveLearningModel.from_dict(
            stored.get("reserve") if stored else None
        )
        # Trade prediction learning data nested under "trade".
        self.trade_model = TradePredictionLearningModel.from_dict(
            stored.get("trade") if stored else None
        )
        self.storage_loaded = True
        _LOGGER.debug(
            "Store loaded: %s load profiles, %s load days, %s PV days, "
            "global_pv_factor=%.3f, reserve_factor=%.3f",
            len(self.model.profiles),
            self.model.learned_days,
            self.pv_model.pv_learning_days,
            self.pv_model.global_factor,
            self.reserve_model.reserve_correction_factor,
        )

    async def async_save_store(self) -> None:
        """Persist the learned models (house load + PV + reserve) to disk."""
        data = self.model.to_dict()
        data["pv"] = self.pv_model.to_dict()
        data["reserve"] = self.reserve_model.to_dict()
        data["trade"] = self.trade_model.to_dict()
        await self._store.async_save(data)
        self.storage_saved = True
        _LOGGER.debug(
            "Store saved: %s load profiles, %s load days, %s PV days, "
            "%s reserve days",
            len(self.model.profiles),
            self.model.learned_days,
            self.pv_model.pv_learning_days,
            self.reserve_model.reserve_learning_days,
        )

    # -- Learning --------------------------------------------------------------

    @staticmethod
    def _slots_between(previous_slot: int, current_slot: int) -> list[int]:
        """Return the slots to learn into, from ``previous_slot`` (exclusive).

        Distributes across every missed quarter up to and including
        ``current_slot``. Handles midnight rollover by wrapping from the end of
        the day back to the start.
        """
        if current_slot >= previous_slot:
            return list(range(previous_slot + 1, current_slot + 1))
        # Day rollover: previous_slot+1..95 then 0..current_slot.
        return list(range(previous_slot + 1, INTERVALS_PER_DAY)) + list(
            range(0, current_slot + 1)
        )

    def _rebase_baseline(self, cumulative: float, slot: int, now: datetime) -> None:
        """Advance the baseline without learning (reset/spike handling)."""
        self.model.previous_house_load = cumulative
        self.model.previous_slot = slot
        self.model.previous_update_time = now.isoformat()
        self.model.last_delta_per_slot = None
        self.model.distributed_slots = []
        self.model.last_learned_slots = []

    def _learn_house_load(self, now: datetime) -> bool:
        """Update the learned profile from the cumulative house-load sensor.

        The source sensor is cumulative daily kWh and resets at midnight. On
        each quarter-hour boundary we compute the delta since the previous
        reading and, when valid, distribute it across every missed quarter slot
        using an exponential moving average. Both the global profile and the
        current season/day-type profile are updated.

        Returns ``True`` when the model changed and should be persisted.
        """
        source_entity = self._config(CONF_CUMULATIVE_HOUSE_LOAD_SENSOR)
        cumulative = self._float(CONF_CUMULATIVE_HOUSE_LOAD_SENSOR)

        self.model.update_count += 1
        self.model.last_update = now.isoformat()

        slot = _interval_index(now)
        previous = self.model.previous_house_load

        _LOGGER.debug(
            "House-load read: source=%s current=%s previous=%s slot=%s "
            "(update #%s)",
            source_entity,
            cumulative,
            previous,
            slot,
            self.model.update_count,
        )

        if cumulative is None:
            _LOGGER.debug(
                "House-load source %s unavailable; skipping learning",
                source_entity,
            )
            return True

        # First reading after start/reset: only establish a baseline.
        if previous is None:
            self._rebase_baseline(cumulative, slot, now)
            _LOGGER.debug(
                "Established house-load baseline=%.3f kWh at slot %s",
                cumulative,
                slot,
            )
            return True

        raw_delta = cumulative - previous
        self.model.last_raw_delta = round(raw_delta, 3)

        _LOGGER.debug(
            "Raw delta calculated: %.3f - %.3f = %.3f kWh (slot %s)",
            cumulative,
            previous,
            raw_delta,
            slot,
        )

        # Negative delta => daily counter reset at midnight: rebase only.
        if raw_delta < 0:
            self._rebase_baseline(cumulative, slot, now)
            _LOGGER.debug(
                "Negative raw delta (%.3f kWh) treated as midnight reset; "
                "rebased baseline to %.3f kWh",
                raw_delta,
                cumulative,
            )
            return True

        # Implausibly large delta => spike: ignore learning, rebase safely.
        if raw_delta > MAX_QUARTER_DELTA_KWH:
            self._rebase_baseline(cumulative, slot, now)
            _LOGGER.debug(
                "Raw delta %.3f kWh exceeds spike limit %.1f kWh; ignoring "
                "sample and rebasing baseline",
                raw_delta,
                MAX_QUARTER_DELTA_KWH,
            )
            return True

        # Determine the slots to learn into, distributing across missed quarters.
        previous_slot = self.model.previous_slot
        if previous_slot is None:
            target_slots = [slot]
        else:
            target_slots = self._slots_between(previous_slot, slot)
        if not target_slots:
            target_slots = [slot]

        # --- EV exclusion: subtract EV charging energy from the raw delta -----
        ev_sensor = self._config(CONF_EV_CHARGER_POWER_SENSOR)
        ev_power_kw = 0.0
        if ev_sensor:
            raw_ev = self._float(CONF_EV_CHARGER_POWER_SENSOR)
            if raw_ev is not None and raw_ev >= 0:
                ev_power_kw = raw_ev
        ev_delta_kwh = ev_power_kw * 0.25
        corrected_delta = max(raw_delta - ev_delta_kwh, 0.0)
        ev_excluded_this_quarter = raw_delta - corrected_delta

        # Daily accumulator: reset when the calendar date changes.
        date_str_ev = now.date().isoformat()
        if self.model.ev_today_date != date_str_ev:
            self.model.ev_excluded_today_kwh = 0.0
            self.model.ev_today_date = date_str_ev
        self.model.ev_excluded_today_kwh = round(
            self.model.ev_excluded_today_kwh + ev_excluded_this_quarter, 3
        )

        self.model.last_ev_power_kw = round(ev_power_kw, 3)
        self.model.ev_excluded_last_quarter_kwh = round(ev_excluded_this_quarter, 3)
        self.model.house_load_raw_last_quarter_kwh = round(raw_delta, 3)
        self.model.house_load_corrected_last_quarter_kwh = round(corrected_delta, 3)
        self.model.ev_exclusion_active = (
            bool(ev_sensor) and ev_power_kw > 0 and ev_excluded_this_quarter > 0
        )

        if ev_excluded_this_quarter > 0:
            _LOGGER.debug(
                "EV exclusion: raw_delta=%.3f kWh, ev_power=%.3f kW, "
                "ev_delta=%.3f kWh, corrected_delta=%.3f kWh (today_total=%.3f kWh)",
                raw_delta,
                ev_power_kw,
                ev_delta_kwh,
                corrected_delta,
                self.model.ev_excluded_today_kwh,
            )
        # -----------------------------------------------------------------------

        delta_per_slot = corrected_delta / len(target_slots)
        self.model.last_delta_per_slot = round(delta_per_slot, 4)
        self.model.distributed_slots = list(target_slots)
        self.model.last_learned_slots = list(target_slots)

        season = _season_for(now)
        day_type = _day_type_for(now)
        profile_key = _profile_key(season, day_type)

        global_profile = self.model.global_profile()
        keyed_profile = self.model.profile(profile_key)

        for target in target_slots:
            for prof in (global_profile, keyed_profile):
                old_value = prof[target]
                if old_value == 0.0:
                    prof[target] = delta_per_slot
                else:
                    prof[target] = (
                        old_value * (1 - LEARNING_ALPHA)
                        + delta_per_slot * LEARNING_ALPHA
                    )

        # Record the learned day for the confidence model.
        date_str = now.date().isoformat()
        if date_str not in self.model.learned_dates:
            self.model.learned_dates.append(date_str)

        # Advance the baseline.
        self.model.previous_house_load = cumulative
        self.model.previous_slot = slot
        self.model.previous_update_time = now.isoformat()

        _LOGGER.debug(
            "Distributed raw_delta=%.3f kWh as %.4f kWh/slot over slots %s "
            "for profile %s (global + keyed)",
            raw_delta,
            delta_per_slot,
            target_slots,
            profile_key,
        )
        return True

    def _global_learned_slots_count(self) -> int:
        """Return the number of non-zero slots in the global profile."""
        return sum(1 for value in self.model.global_profile() if value > 0.0)

    def _learning_confidence(self) -> float:
        """Return a 0..100 confidence score for the learned profile."""
        learned_slots = self._global_learned_slots_count()
        learned_days = self.model.learned_days
        confidence = (
            (learned_slots / INTERVALS_PER_DAY) * 40
            + (learned_days / CONFIDENCE_DAYS_TARGET) * 40
            + (self.model.update_count / CONFIDENCE_UPDATES_TARGET) * 20
        )
        confidence = max(0.0, min(100.0, confidence))
        _LOGGER.debug(
            "Confidence: slots=%s days=%s updates=%s -> %.1f%%",
            learned_slots,
            learned_days,
            self.model.update_count,
            confidence,
        )
        return round(confidence, 1)

    def _predicted_daily_load(self) -> float:
        """Return the learned total load (kWh): sum of the 96 global slots."""
        return round(sum(self.model.global_profile()), 3)

    def _predicted_remaining_load(self, now: datetime) -> float:
        """Return learned load (kWh) from the current slot to end of day."""
        index = _interval_index(now)
        return round(sum(self.model.global_profile()[index:]), 3)

    # -- PV learning -----------------------------------------------------------

    def _finalise_pv_day(
        self, actual: float | None, forecast: float | None, season: str, date: str
    ) -> None:
        """Fold a completed day's Solcast error into the correction factors."""
        if forecast is None or actual is None or forecast <= 0:
            _LOGGER.debug(
                "PV day %s not finalised (actual=%s forecast=%s)",
                date,
                actual,
                forecast,
            )
            return

        raw_factor = actual / forecast
        factor = max(PV_FACTOR_MIN, min(PV_FACTOR_MAX, raw_factor))

        self.pv_model.last_pv_error = round(actual - forecast, 3)
        self.pv_model.last_pv_error_factor = round(factor, 4)

        # EMA: new = old * 0.80 + factor * 0.20.
        self.pv_model.global_factor = round(
            self.pv_model.global_factor * (1 - PV_FACTOR_ALPHA)
            + factor * PV_FACTOR_ALPHA,
            4,
        )
        old_season = self.pv_model.season_factor(season)
        self.pv_model.season_factors[season] = round(
            old_season * (1 - PV_FACTOR_ALPHA) + factor * PV_FACTOR_ALPHA, 4
        )

        if date not in self.pv_model.pv_learned_dates:
            self.pv_model.pv_learned_dates.append(date)

        _LOGGER.debug(
            "PV day %s finalised: actual=%.3f forecast=%.3f raw_factor=%.3f "
            "clamped=%.3f -> global=%.3f season[%s]=%.3f",
            date,
            actual,
            forecast,
            raw_factor,
            factor,
            self.pv_model.global_factor,
            season,
            self.pv_model.season_factors[season],
        )

    def _learn_pv(self, now: datetime) -> bool:
        """Track daily PV totals and finalise the correction at day rollover.

        Returns ``True`` when the PV model changed and should be persisted.
        """
        actual = self._float(CONF_PV_ACTUAL_TODAY_SENSOR)
        forecast = self._float(CONF_PV_FORECAST_TODAY_SENSOR)
        today = now.date().isoformat()
        season = _season_for(now)

        self.pv_model.update_count += 1
        self.pv_model.last_update = now.isoformat()

        _LOGGER.debug(
            "PV read: actual_today=%s forecast_today=%s date=%s season=%s",
            actual,
            forecast,
            today,
            season,
        )

        if self.pv_model.current_date is None:
            # First observation: start tracking the current day.
            self.pv_model.current_date = today
            self.pv_model.current_season = season
        elif today != self.pv_model.current_date:
            # Day rollover: finalise the previous day using its last readings.
            self._finalise_pv_day(
                self.pv_model.last_actual_today,
                self.pv_model.last_forecast_today,
                self.pv_model.current_season or season,
                self.pv_model.current_date,
            )
            self.pv_model.current_date = today
            self.pv_model.current_season = season
            self.pv_model.last_actual_today = None
            self.pv_model.last_forecast_today = None

        # Update the running end-of-day values for the active day.
        if actual is not None:
            self.pv_model.last_actual_today = actual
        if forecast is not None:
            self.pv_model.last_forecast_today = forecast

        return True

    def _corrected_forecast(self, raw_forecast: float | None, season: str) -> float | None:
        """Apply global and season correction factors to a raw forecast."""
        if raw_forecast is None:
            return None
        corrected = (
            raw_forecast
            * self.pv_model.global_factor
            * self.pv_model.season_factor(season)
        )
        return round(max(corrected, 0.0), 3)

    def _expected_remaining_pv_today(
        self, corrected_today: float | None, actual_today: float | None
    ) -> float:
        """Return corrected forecast minus actual PV so far today (>= 0)."""
        if corrected_today is None:
            return 0.0
        actual = actual_today or 0.0
        return round(max(corrected_today - actual, 0.0), 3)

    def _pv_learning_confidence(self) -> float:
        """Return a 0..100 confidence score for the PV correction."""
        days = self.pv_model.pv_learning_days
        confidence = (
            (days / PV_CONFIDENCE_DAYS_TARGET) * 80
            + (self.pv_model.update_count / PV_CONFIDENCE_UPDATES_TARGET) * 20
        )
        confidence = max(0.0, min(100.0, confidence))
        _LOGGER.debug(
            "PV confidence: days=%s updates=%s -> %.1f%%",
            days,
            self.pv_model.update_count,
            confidence,
        )
        return round(confidence, 1)

    # -- Reserve calculation ---------------------------------------------------

    def _reserve_learning_status(self) -> str:
        """Return a lifecycle label for the reserve correction model."""
        days = self.reserve_model.reserve_learning_days
        if days < 3:
            return "learning"
        if days < 7:
            return "improving"
        if days < RESERVE_CONFIDENCE_DAYS_TARGET:
            return "good"
        return "excellent"

    def _learn_reserve(self, now: datetime, battery_energy: float | None) -> bool:
        """Learn from the observed battery energy versus the reserve floor.

        Reserve learning is evaluated *once per calendar day*. Throughout the
        day the running minimum battery energy is tracked. When the day rolls
        over the finished day is judged exactly once: a *miss* (reserve too
        optimistic) if the day's minimum touched the floor, or a *success* if
        it stayed safely above ``floor + margin``. Each calendar day can
        produce at most one miss and at most one success.

        Returns ``True`` when the persisted reserve model changed.
        """
        if battery_energy is None:
            return False

        model = self.reserve_model
        today = now.date().isoformat()
        changed = False

        # Day rollover: judge the day that just finished exactly once.
        if model.current_date is not None and model.current_date != today:
            previous_day = model.current_date
            day_min = model.day_min_battery_energy

            if day_min is not None:
                if (
                    day_min <= BATTERY_FLOOR_KWH
                    and previous_day != model.last_miss_date
                ):
                    # Reserve miss: the battery touched the protective floor.
                    model.reserve_miss_count += 1
                    model.last_reserve_miss = now.isoformat()
                    model.last_miss_date = previous_day
                    target = min(
                        model.reserve_correction_factor
                        * RESERVE_MISS_TARGET_MULTIPLIER,
                        RESERVE_FACTOR_MAX,
                    )
                    model.reserve_correction_factor = min(
                        RESERVE_FACTOR_MAX,
                        model.reserve_correction_factor
                        * (1 - RESERVE_FACTOR_ALPHA)
                        + target * RESERVE_FACTOR_ALPHA,
                    )
                    if previous_day not in model.reserve_learned_dates:
                        model.reserve_learned_dates.append(previous_day)
                    _LOGGER.debug(
                        "Reserve miss for %s: day_min=%.3f <= floor=%.3f "
                        "-> factor=%.4f",
                        previous_day,
                        day_min,
                        BATTERY_FLOOR_KWH,
                        model.reserve_correction_factor,
                    )
                elif (
                    day_min >= (BATTERY_FLOOR_KWH + RESERVE_SUCCESS_MARGIN_KWH)
                    and previous_day != model.last_success_date
                ):
                    # Reserve success: the battery stayed comfortably above floor.
                    model.reserve_success_count += 1
                    model.last_reserve_success = now.isoformat()
                    model.last_success_date = previous_day
                    # Ease the factor back toward neutral (1.0), never below it.
                    model.reserve_correction_factor = max(
                        RESERVE_FACTOR_MIN,
                        model.reserve_correction_factor
                        * (1 - RESERVE_FACTOR_ALPHA)
                        + RESERVE_FACTOR_MIN * RESERVE_FACTOR_ALPHA,
                    )
                    if previous_day not in model.reserve_learned_dates:
                        model.reserve_learned_dates.append(previous_day)
                    _LOGGER.debug(
                        "Reserve success for %s: day_min=%.3f >= %.3f "
                        "-> factor=%.4f",
                        previous_day,
                        day_min,
                        BATTERY_FLOOR_KWH + RESERVE_SUCCESS_MARGIN_KWH,
                        model.reserve_correction_factor,
                    )

            # Reset the running minimum for the new day.
            model.day_min_battery_energy = battery_energy
            changed = True
        elif model.current_date is None:
            model.day_min_battery_energy = battery_energy

        model.current_date = today

        # Track the running minimum battery energy for the current day.
        if (
            model.day_min_battery_energy is None
            or battery_energy < model.day_min_battery_energy
        ):
            model.day_min_battery_energy = battery_energy

        model.last_battery_energy = battery_energy
        model.reserve_learning_status = self._reserve_learning_status()
        model.last_update = now.isoformat()
        model.update_count += 1
        return True

    def _required_reserve(
        self,
        now: datetime,
        predicted_remaining_load: float,
        expected_remaining_pv: float,
        confidence: float,
        correction_factor: float,
    ) -> float:
        """Estimate reserve energy (kWh) needed for the rest of the day.

        Uses the *remaining* learned load (never the full daily load) scaled by
        the self-learning reserve correction factor, reduced by the expected
        remaining PV, plus a confidence-scaled safety margin. The result is
        never lower than the protective battery floor and is clamped between 0
        and the battery capacity.
        """
        safety_margin = predicted_remaining_load * (1 - confidence / 100) * 0.25

        reserve = max(
            BATTERY_FLOOR_KWH,
            (predicted_remaining_load * correction_factor)
            - expected_remaining_pv
            + safety_margin,
        )
        reserve = max(reserve, 0.0)

        capacity = self._float(CONF_BATTERY_CAPACITY_KWH_ENTITY)
        if capacity is not None:
            reserve = min(reserve, capacity)

        _LOGGER.debug(
            "Reserve: floor=%.3f, remaining_load=%.3f * factor=%.3f - "
            "expected_pv=%.3f + safety=%.3f (confidence=%.1f%%) -> %.3f kWh "
            "(capacity=%s)",
            BATTERY_FLOOR_KWH,
            predicted_remaining_load,
            correction_factor,
            expected_remaining_pv,
            safety_margin,
            confidence,
            reserve,
            capacity,
        )
        return round(reserve, 3)

    # -- Coordinator update ----------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Read sources, learn, and compute derived values."""
        now = dt_util.now()

        changed = self._learn_house_load(now)
        pv_changed = self._learn_pv(now)

        # Persist learned data after each learning cycle.
        if changed or pv_changed:
            await self.async_save_store()

        battery_current = self._float(CONF_BATTERY_CURRENT_KWH_SENSOR)
        battery_capacity = self._float(CONF_BATTERY_CAPACITY_KWH_ENTITY)
        battery_soc = self._float(CONF_BATTERY_SOC_SENSOR)

        season = _season_for(now)
        day_type = _day_type_for(now)
        profile_key = _profile_key(season, day_type)
        current_slot = _interval_index(now)
        learned_slots_count = self._global_learned_slots_count()
        confidence = self._learning_confidence()
        current_house_load = self._float(CONF_CUMULATIVE_HOUSE_LOAD_SENSOR)

        # PV correction and forecasts.
        raw_forecast_today = self._float(CONF_PV_FORECAST_TODAY_SENSOR)
        raw_forecast_tomorrow = self._float(CONF_PV_FORECAST_TOMORROW_SENSOR)
        actual_pv_today = self._float(CONF_PV_ACTUAL_TODAY_SENSOR)
        global_pv_factor = round(self.pv_model.global_factor, 4)
        season_pv_factor = round(self.pv_model.season_factor(season), 4)
        effective_pv_factor = round(global_pv_factor * season_pv_factor, 4)
        corrected_forecast_today = self._corrected_forecast(
            raw_forecast_today, season
        )
        corrected_forecast_tomorrow = self._corrected_forecast(
            raw_forecast_tomorrow, season
        )
        expected_remaining_pv_today = self._expected_remaining_pv_today(
            corrected_forecast_today, actual_pv_today
        )
        pv_confidence = self._pv_learning_confidence()
        pv_learning_days = self.pv_model.pv_learning_days

        _LOGGER.debug(
            "PV forecasts: raw_today=%s corrected_today=%s raw_tomorrow=%s "
            "corrected_tomorrow=%s expected_remaining=%s (factor=%.3f)",
            raw_forecast_today,
            corrected_forecast_today,
            raw_forecast_tomorrow,
            corrected_forecast_tomorrow,
            expected_remaining_pv_today,
            effective_pv_factor,
        )

        # Reserve now considers learned remaining load AND expected PV.
        predicted_remaining_load = self._predicted_remaining_load(now)
        required_reserve = self._required_reserve(
            now,
            predicted_remaining_load,
            expected_remaining_pv_today,
            confidence,
            self.reserve_model.reserve_correction_factor,
        )
        # Learn from the observed battery energy versus the protective floor.
        reserve_changed = self._learn_reserve(now, battery_current)
        if reserve_changed:
            await self.async_save_store()
        reserve_correction_factor = round(
            self.reserve_model.reserve_correction_factor, 4
        )
        reserve_satisfied = (
            battery_current is not None and battery_current >= required_reserve
        )
        recommendation = "hold" if reserve_satisfied else "charge"
        _LOGGER.debug(
            "Recommendation: battery_current=%s vs required_reserve=%.3f -> %s",
            battery_current,
            required_reserve,
            recommendation,
        )

        # Profile status: a short human-readable lifecycle label.
        if learned_slots_count < MIN_CONFIDENT_SLOTS:
            profile_status = "learning"
        elif self.model.learned_days < 7:
            profile_status = "improving"
        else:
            profile_status = "ready"

        # PV profile lifecycle label.
        if pv_learning_days == 0:
            pv_profile_status = "learning"
        elif pv_learning_days < 7:
            pv_profile_status = "improving"
        else:
            pv_profile_status = "ready"

        # --- Trade Prediction Engine -----------------------------------------
        def _state_attrs(key: str) -> dict:
            st = self._state(key)
            return dict(st.attributes) if st is not None else {}

        # Read minimum spread from the HA input_number helper.
        # Always resolves to a concrete float — falls back to the default when the
        # entity is missing, unavailable, or unparseable so the trade engine can
        # still evaluate rather than short-circuiting on None.
        minimum_spread_entity = MINIMUM_SPREAD_ENTITY
        minimum_spread_value: float = TRADE_MINIMUM_SPREAD_DEFAULT
        minimum_spread_source: str

        spread_state = self.hass.states.get(MINIMUM_SPREAD_ENTITY)
        if spread_state is None:
            minimum_spread_source = "fallback_default"
        elif spread_state.state in ("unknown", "unavailable", ""):
            minimum_spread_source = "unavailable"
        else:
            try:
                minimum_spread_value = float(spread_state.state)
                minimum_spread_source = "helper"
            except (TypeError, ValueError):
                minimum_spread_source = "fallback_default"

        _LOGGER.debug(
            "Minimum spread: value=%.4f source=%s entity=%s",
            minimum_spread_value,
            minimum_spread_source,
            minimum_spread_entity,
        )

        # Sun entity for PV curve sunrise/sunset slot indices.
        trade_sunrise_slot: int | None = None
        trade_sunset_slot: int | None = None
        sun_state = self.hass.states.get("sun.sun")
        if sun_state is not None:
            for _sun_attr, _is_rising in (
                ("next_rising", True),
                ("next_setting", False),
            ):
                _attr_val = sun_state.attributes.get(_sun_attr)
                if _attr_val:
                    try:
                        _sun_dt = dt_util.parse_datetime(str(_attr_val))
                        if _sun_dt:
                            _local_dt = dt_util.as_local(_sun_dt)
                            if _is_rising:
                                trade_sunrise_slot = _interval_index(_local_dt)
                            else:
                                trade_sunset_slot = _interval_index(_local_dt)
                    except (ValueError, TypeError):
                        pass

        # Battery capacity source tracking.
        battery_capacity_source = (
            "configured_entity" if battery_capacity is not None else "default_fallback"
        )
        trade_battery_capacity = battery_capacity or TRADE_BATTERY_CAPACITY_KWH

        trade_result, trade_model_changed = compute_trade_prediction(
            now=now,
            battery_current_kwh=battery_current,
            battery_capacity_kwh=trade_battery_capacity,
            battery_capacity_source=battery_capacity_source,
            global_load_profile=self.model.global_profile(),
            learning_confidence=confidence,
            expected_remaining_pv_today=expected_remaining_pv_today,
            corrected_forecast_tomorrow=corrected_forecast_tomorrow,
            pv_east_kwh=self._float(CONF_PV_EAST_SENSOR),
            pv_west_kwh=self._float(CONF_PV_WEST_SENSOR),
            sunrise_slot=trade_sunrise_slot,
            sunset_slot=trade_sunset_slot,
            reserve_floor_kwh=BATTERY_FLOOR_KWH,
            reserve_correction_factor=self.reserve_model.reserve_correction_factor,
            frank_cheapest_time_today=self._raw_state(
                CONF_FRANK_CHEAPEST_TIME_TODAY_SENSOR
            ),
            frank_cheapest_time_tomorrow=self._raw_state(
                CONF_FRANK_CHEAPEST_TIME_TOMORROW_SENSOR
            ),
            frank_most_expensive_time_today=self._raw_state(
                CONF_FRANK_MOST_EXPENSIVE_TIME_TODAY_SENSOR
            ),
            frank_most_expensive_time_tomorrow=self._raw_state(
                CONF_FRANK_MOST_EXPENSIVE_TIME_TOMORROW_SENSOR
            ),
            frank_cheapest_today_attrs=_state_attrs(
                CONF_FRANK_CHEAPEST_TIME_TODAY_SENSOR
            ),
            frank_cheapest_tomorrow_attrs=_state_attrs(
                CONF_FRANK_CHEAPEST_TIME_TOMORROW_SENSOR
            ),
            frank_expensive_today_attrs=_state_attrs(
                CONF_FRANK_MOST_EXPENSIVE_TIME_TODAY_SENSOR
            ),
            frank_expensive_tomorrow_attrs=_state_attrs(
                CONF_FRANK_MOST_EXPENSIVE_TIME_TOMORROW_SENSOR
            ),
            frank_prices_today_attrs=_state_attrs(CONF_FRANK_PRICES_TODAY_SENSOR),
            frank_prices_tomorrow_attrs=_state_attrs(CONF_FRANK_PRICES_TOMORROW_SENSOR),
            frank_price_today=self._float(CONF_FRANK_PRICES_TODAY_SENSOR),
            frank_price_tomorrow=self._float(CONF_FRANK_PRICES_TOMORROW_SENSOR),
            minimum_spread=minimum_spread_value,
            trade_model=self.trade_model,
        )
        if trade_model_changed:
            await self.async_save_store()

        return {
            "season": season,
            "day_type": day_type,
            "profile_key": profile_key,
            "interval_index": current_slot,
            "intervals_per_day": INTERVALS_PER_DAY,
            "predicted_daily_load_kwh": self._predicted_daily_load(),
            "predicted_remaining_load_kwh": predicted_remaining_load,
            "pv_forecast_today_kwh": raw_forecast_today,
            "pv_forecast_tomorrow_kwh": raw_forecast_tomorrow,
            "pv_actual_today_kwh": actual_pv_today,
            "pv_east_kwh": self._float(CONF_PV_EAST_SENSOR),
            "pv_west_kwh": self._float(CONF_PV_WEST_SENSOR),
            "frank_price_today": self._float(CONF_FRANK_PRICES_TODAY_SENSOR),
            "frank_price_tomorrow": self._float(CONF_FRANK_PRICES_TOMORROW_SENSOR),
            "frank_cheapest_time_today": self._raw_state(
                CONF_FRANK_CHEAPEST_TIME_TODAY_SENSOR
            ),
            "frank_most_expensive_time_today": self._raw_state(
                CONF_FRANK_MOST_EXPENSIVE_TIME_TODAY_SENSOR
            ),
            "frank_cheapest_time_tomorrow": self._raw_state(
                CONF_FRANK_CHEAPEST_TIME_TOMORROW_SENSOR
            ),
            "frank_most_expensive_time_tomorrow": self._raw_state(
                CONF_FRANK_MOST_EXPENSIVE_TIME_TOMORROW_SENSOR
            ),
            "battery_current_kwh": battery_current,
            "battery_capacity_kwh": battery_capacity,
            "battery_soc": battery_soc,
            "required_reserve_kwh": required_reserve,
            "reserve_satisfied": reserve_satisfied,
            "recommendation": recommendation,
            "learned_profiles": len(self.model.profiles),
            # Learning metrics surfaced as dedicated sensors.
            "learning_confidence": confidence,
            "learning_days": self.model.learned_days,
            "learned_slots_count": learned_slots_count,
            "last_quarter_load_kwh": self.model.last_delta_per_slot,
            "profile_status": profile_status,
            # PV correction metrics surfaced as dedicated sensors.
            "pv_correction_factor": effective_pv_factor,
            "global_pv_factor": global_pv_factor,
            "season_pv_factor": season_pv_factor,
            "corrected_pv_forecast_today_kwh": corrected_forecast_today,
            "corrected_pv_forecast_tomorrow_kwh": corrected_forecast_tomorrow,
            "expected_remaining_pv_today_kwh": expected_remaining_pv_today,
            "pv_learning_confidence": pv_confidence,
            "pv_learning_days": pv_learning_days,
            "pv_profile_status": pv_profile_status,
            "last_pv_error": self.pv_model.last_pv_error,
            "last_pv_error_factor": self.pv_model.last_pv_error_factor,
            # Reserve correction metrics surfaced as dedicated sensors.
            "reserve_correction_factor": reserve_correction_factor,
            "reserve_floor_kwh": round(BATTERY_FLOOR_KWH, 3),
            "reserve_learning_days": self.reserve_model.reserve_learning_days,
            "reserve_miss_count": self.reserve_model.reserve_miss_count,
            "reserve_success_count": self.reserve_model.reserve_success_count,
            "reserve_learning_status": self.reserve_model.reserve_learning_status,
            "last_reserve_miss": self.reserve_model.last_reserve_miss,
            "last_reserve_success": self.reserve_model.last_reserve_success,
            "reserve_last_battery_energy": self.reserve_model.last_battery_energy,
            "reserve_day_min_battery_energy": (
                self.reserve_model.day_min_battery_energy
            ),
            "reserve_last_miss_date": self.reserve_model.last_miss_date,
            "reserve_last_success_date": self.reserve_model.last_success_date,
            # --- Trade prediction -----------------------------------------------
            # (populated below by compute_trade_prediction)
            # EV exclusion diagnostic fields.
            "ev_charger_power_sensor": self._config(CONF_EV_CHARGER_POWER_SENSOR),
            "ev_charger_power_kw": (
                max(self._float(CONF_EV_CHARGER_POWER_SENSOR) or 0.0, 0.0)
                if self._config(CONF_EV_CHARGER_POWER_SENSOR)
                else None
            ),
            "ev_excluded_last_quarter_kwh": self.model.ev_excluded_last_quarter_kwh,
            "ev_excluded_today_kwh": self.model.ev_excluded_today_kwh,
            "house_load_raw_last_quarter_kwh": self.model.house_load_raw_last_quarter_kwh,
            "house_load_corrected_last_quarter_kwh": (
                self.model.house_load_corrected_last_quarter_kwh
            ),
            "ev_exclusion_active": self.model.ev_exclusion_active,
            # Diagnostic / debug fields.
            "source_entity": self._config(CONF_CUMULATIVE_HOUSE_LOAD_SENSOR),
            "source_value": current_house_load,
            "current_house_load": current_house_load,
            "current_slot": current_slot,
            "previous_house_load": self.model.previous_house_load,
            "previous_slot": self.model.previous_slot,
            "previous_update_time": self.model.previous_update_time,
            "last_raw_delta": self.model.last_raw_delta,
            "last_delta_per_slot": self.model.last_delta_per_slot,
            "distributed_slots": self.model.distributed_slots,
            "update_count": self.model.update_count,
            "last_update": self.model.last_update,
            "storage_loaded": self.storage_loaded,
            "storage_saved": self.storage_saved,
            # Backward-compatible debug keys for the predicted-load sensor.
            "last_house_load": self.model.previous_house_load,
            "last_delta": self.model.last_raw_delta,
            "last_slot": self.model.previous_slot,
            # Trade prediction results (all keys prefixed in trade_engine).
            **trade_result,
            # Minimum spread diagnostics.
            "minimum_spread": minimum_spread_value,
            "minimum_spread_entity": minimum_spread_entity,
            "minimum_spread_value": minimum_spread_value,
            "minimum_spread_source": minimum_spread_source,
        }

    def _raw_state(self, key: str) -> str | None:
        """Return the raw string state of a configured entity."""
        state = self._state(key)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        return state.state
